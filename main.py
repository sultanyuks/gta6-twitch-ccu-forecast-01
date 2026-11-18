#!/usr/bin/env python3
"""Forecast a launch day's hourly Twitch viewers from its first hour.

Idea: a launch day's shape, measured as the log ratio of each hour to the
day's first hour, is a property of the kind of launch it is. Past big
launches (the case's comparables) give an empirical distribution of those
log ratios for every hour. This program pools the comparables' release-day
curves, weights them toward the launches most similar to the target --
console-only releases like the target's own kind, and releases whose first
hour was of a similar size -- and reads the five forecast quantiles off the
weighted distribution, hour by hour. The quantile curves are smoothed
across neighbouring hours, and the spread is pulled in slightly (a factor
0.9 on the log distance from the median) -- the calibration that
leave-one-out cross-validation over the comparable pool preferred.

Only the Python standard library is used; there is no network access and
no randomness, so two runs on the same case agree byte for byte.
"""

import json
import math
import os
import re
import sys
from datetime import datetime, timedelta, timezone

LEVELS = (0.05, 0.25, 0.5, 0.75, 0.95)
CONSOLE_WEIGHT = 20.0   # a console-only comparable counts this many times
SCALE_SIGMA = 1.5       # width of the first-hour-size similarity kernel
SPREAD = 0.9            # calibration on the log spread around the median
Z = (-1.644854, -0.674490, 0.0, 0.674490, 1.644854)  # normal quantiles


def parse_time(text):
    """Parse the kit's ISO-8601 UTC timestamps."""
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
    except ValueError:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))


def stamp(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def release_day(comp):
    """The comparable's 24 release-day prints, or None when unusable.

    Hour 1 is the point at day_start; every hour of the day must be present
    with a positive viewer count, else the curve cannot anchor ratios.
    """
    try:
        start = parse_time(comp["day_start"])
    except (KeyError, ValueError, AttributeError):
        return None
    by_hour = {}
    for point in comp.get("hourly", []):
        by_hour[point.get("from")] = point.get("viewers")
    day = []
    for h in range(24):
        viewers = by_hour.get(stamp(start + timedelta(hours=h)))
        if viewers is None or viewers <= 0:
            return None
        day.append(float(viewers))
    return day


def is_console_only(comp):
    """True when the comparable's platform list has no PC token."""
    tokens = re.split(r"[^a-z0-9]+", comp.get("platforms", "").lower())
    return "pc" not in tokens


def weighted_quantile(values, weights, tau):
    """A quantile of the weighted sample, linear on the weighted CDF."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    xs = [values[i] for i in order]
    ws = [weights[i] for i in order]
    total = sum(ws)
    if total <= 0:
        pos = tau * (len(xs) - 1)
        lo = int(math.floor(pos))
        hi = min(lo + 1, len(xs) - 1)
        return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)
    cum = []
    acc = 0.0
    for w in ws:
        acc += w
        cum.append((acc - 0.5 * w) / total)
    if tau <= cum[0]:
        return xs[0]
    if tau >= cum[-1]:
        return xs[-1]
    for i in range(1, len(xs)):
        if tau <= cum[i]:
            t = (tau - cum[i - 1]) / (cum[i] - cum[i - 1])
            return xs[i - 1] + (xs[i] - xs[i - 1]) * t
    return xs[-1]


def forecast(case):
    """The 23 forecast rows (hours 2..24) for one case."""
    first = 1.0
    known = case.get("known") or []
    if known and known[0].get("viewers"):
        first = max(float(known[0]["viewers"]), 1.0)
    log_first = math.log(first)

    curves = []   # (log ratios for hours 2..24, weight)
    for comp in case.get("comparables", []):
        day = release_day(comp)
        if day is None:
            continue
        weight = CONSOLE_WEIGHT if is_console_only(comp) else 1.0
        size_gap = (math.log(day[0]) - log_first) / SCALE_SIGMA
        weight *= math.exp(-0.5 * size_gap * size_gap)
        ratios = [math.log(day[h] / day[0]) for h in range(1, 24)]
        curves.append((ratios, weight))

    # Raw weighted quantiles of the log ratios, one row per hour.
    raw = []
    for i in range(23):
        if curves:
            values = [c[0][i] for c in curves]
            weights = [c[1] for c in curves]
            raw.append([weighted_quantile(values, weights, t) for t in LEVELS])
        else:
            # No usable comparable: stay at the first hour, with a spread
            # that widens like the square root of the horizon.
            spread = 0.15 * math.sqrt(i + 1)
            raw.append([z * spread for z in Z])

    # A launch day's shape bends smoothly, while quantiles read off a few
    # dozen curves jump around; blend each hour with its neighbours
    # (weights 1-2-1) before calibrating the spread around the median.
    smooth = []
    for i in range(23):
        row = []
        for j in range(5):
            near = raw[max(i - 1, 0)][j] + 2.0 * raw[i][j] + raw[min(i + 1, 22)][j]
            row.append(near / 4.0)
        smooth.append(row)

    rows = []
    for i in range(23):
        hour = i + 2
        median = smooth[i][2]
        qs = [median + SPREAD * (q - median) for q in smooth[i]]
        row = []
        prev = 0.0
        for q in qs:
            value = first * math.exp(q)
            value = min(max(value, 0.01), 1_000_000_000.0)
            value = max(value, prev)  # quantiles never decrease
            prev = value
            row.append(round(value, 2))
        rows.append({"hour": hour, "q": row})
    return rows


def main():
    in_dir = os.environ.get("MAKEFUN_INPUT_DIR") or "/input"
    out_dir = os.environ.get("MAKEFUN_OUTPUT_DIR") or "/output"
    os.makedirs(out_dir, exist_ok=True)
    names = sorted(n for n in os.listdir(in_dir) if n.endswith(".in"))
    for name in names:
        with open(os.path.join(in_dir, name), encoding="utf-8") as fh:
            case = json.load(fh)
        payload = {"hours": forecast(case)}
        out_name = name[:-3] + ".out"
        with open(os.path.join(out_dir, out_name), "w", encoding="utf-8") as fh:
            json.dump(payload, fh, separators=(",", ":"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
