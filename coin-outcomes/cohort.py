#!/usr/bin/env python3
"""
Nova Cohort Stats
=================
Given a behavior profile, find the historical coins in the outcome tracker
that behaved the same way at first sight, and report what actually happened
to them. This is the stats engine behind the Vorix Scanners prediction line:

    "Coins that looked like this: n=__ · __% ran +50% or more · median peak
     +__% in __h · median worst drop from peak __%"

Bands (snapshot values at first-seen):
  age:       newborn <1h | fresh 1-6h | day 6-24h | established 1-7d | old >7d
  mcap:      micro <$50k | small $50k-250k | mid $250k-1M | large $1M-10M | mega >$10M
  liquidity: thin <$10k | low $10-50k | ok $50-150k | deep >$150k
  score:     low <60 | mid 60-79 | high 80-89 | top 90+
  buyratio:  sell-heavy <0.45 | balanced 0.45-0.55 | buy-lean 0.55-0.65 | buy-heavy >0.65

Usage:
  python3 cohort.py                          # whole tracked population
  python3 cohort.py --age fresh --mcap small
  python3 cohort.py --score high --buyratio buy-heavy --json

A coin counts toward outcome stats once it has at least a +24h checkpoint
or is closed (complete/dead) — younger records are still counted in n_seen
so the output is honest about how much history actually backs the numbers.
n is ALWAYS printed. Below n=30 the stats are labeled as a small sample.
"""

import argparse
import json
import sqlite3
import statistics
import sys
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "data" / "outcomes.db"

BANDS = {
    "age": ("age_hours", [(None, 1, "newborn"), (1, 6, "fresh"), (6, 24, "day"),
                          (24, 168, "established"), (168, None, "old")]),
    "mcap": ("mcap_usd", [(None, 50_000, "micro"), (50_000, 250_000, "small"),
                          (250_000, 1_000_000, "mid"), (1_000_000, 10_000_000, "large"),
                          (10_000_000, None, "mega")]),
    "liquidity": ("liquidity_usd", [(None, 10_000, "thin"), (10_000, 50_000, "low"),
                                    (50_000, 150_000, "ok"), (150_000, None, "deep")]),
    "score": ("score", [(None, 60, "low"), (60, 80, "mid"), (80, 90, "high"),
                        (90, None, "top")]),
    "buyratio": ("buy_ratio_h1", [(None, 0.45, "sell-heavy"), (0.45, 0.55, "balanced"),
                                  (0.55, 0.65, "buy-lean"), (0.65, None, "buy-heavy")]),
}


def band_clause(field, ranges, label):
    for lo, hi, name in ranges:
        if name == label:
            parts, vals = [], []
            if lo is not None:
                parts.append(f"{field} >= ?")
                vals.append(lo)
            if hi is not None:
                parts.append(f"{field} < ?")
                vals.append(hi)
            return " AND ".join(parts) if parts else "1=1", vals
    valid = [n for _, _, n in ranges]
    raise SystemExit(f"unknown band '{label}' — valid: {', '.join(valid)}")


def pct(num, den):
    return round(100.0 * num / den, 1) if den else None


def cohort_stats(filters: dict) -> dict:
    if not DB_PATH.exists():
        raise SystemExit("no dataset yet — run track.py first")
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row

    where, vals = [], []
    for key, label in filters.items():
        field, ranges = BANDS[key]
        clause, v = band_clause(field, ranges, label)
        where.append(f"({clause})")
        vals.extend(v)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    n_seen = db.execute(f"SELECT COUNT(*) c FROM coins {where_sql}", vals).fetchone()["c"]
    # outcome-known: has +24h checkpoint or is closed
    rows = db.execute(
        f"""SELECT * FROM coins {where_sql}
            {'AND' if where else 'WHERE'}
            (cp_24h_price IS NOT NULL OR status IN ('complete','dead'))""",
        vals).fetchall()

    n = len(rows)
    stats = {"filters": filters, "n_seen": n_seen, "n": n}
    if n == 0:
        stats["note"] = "no coins with known outcomes match this profile yet"
        return stats

    peak_gain = []       # % from entry to running peak
    time_to_peak_h = []  # hours from first-seen to peak
    max_dd = []          # % max drawdown from peak
    fell50 = ran50 = ran2x = 0
    for r in rows:
        entry = r["entry_price_usd"]
        if not entry:
            continue
        peak = r["peak_price_usd"] or entry
        gain = (peak / entry - 1) * 100
        peak_gain.append(gain)
        if gain >= 50:
            ran50 += 1
        if gain >= 100:
            ran2x += 1
        if r["peak_ts"] and r["first_seen_ts"]:
            time_to_peak_h.append((r["peak_ts"] - r["first_seen_ts"]) / 3600)
        max_dd.append(r["max_dd_from_peak_pct"] or 0)
        minp = r["min_price_usd"]
        if minp is not None and minp <= entry * 0.5:
            fell50 += 1

    m = len(peak_gain)
    stats.update({
        "pct_peak_gain_50plus": pct(ran50, m),
        "pct_peak_gain_2x": pct(ran2x, m),
        "pct_fell_50_from_entry": pct(fell50, m),
        "median_time_to_peak_h": round(statistics.median(time_to_peak_h), 2) if time_to_peak_h else None,
        "median_peak_gain_pct": round(statistics.median(peak_gain), 1) if peak_gain else None,
        "median_max_drawdown_from_peak_pct": round(statistics.median(max_dd), 1) if max_dd else None,
    })
    if n < 30:
        stats["note"] = (f"SMALL SAMPLE: n={n} — treat as a hint, not a prediction. "
                         "Cohort stats are only shown in-product once n >= 30.")
    return stats


def main():
    ap = argparse.ArgumentParser(description="Nova cohort outcome stats")
    for key in BANDS:
        ap.add_argument(f"--{key}", help=f"{key} band: " +
                        ", ".join(n for _, _, n in BANDS[key][1]))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    filters = {k: getattr(args, k) for k in BANDS if getattr(args, k)}
    stats = cohort_stats(filters)

    if args.json:
        print(json.dumps(stats, indent=2))
        return
    label = ", ".join(f"{k}={v}" for k, v in filters.items()) or "all tracked coins"
    print(f"Cohort: {label}")
    print(f"n = {stats['n']} coins with known outcomes "
          f"({stats['n_seen']} seen in this profile so far)")
    if stats["n"] == 0:
        print(stats["note"])
        return
    print(f"  ran +50% or more at peak : {stats['pct_peak_gain_50plus']}%")
    print(f"  ran 2x or more at peak   : {stats['pct_peak_gain_2x']}%")
    print(f"  fell -50%+ from entry    : {stats['pct_fell_50_from_entry']}%")
    print(f"  median time to peak      : {stats['median_time_to_peak_h']} h")
    print(f"  median peak gain         : +{stats['median_peak_gain_pct']}%")
    print(f"  median max drop from peak: -{stats['median_max_drawdown_from_peak_pct']}%")
    if "note" in stats:
        print(f"  NOTE: {stats['note']}")


if __name__ == "__main__":
    main()
