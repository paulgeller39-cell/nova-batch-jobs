#!/usr/bin/env python3
"""
Nova Coin Outcome Tracker
=========================
Registers every coin the Nova scanners surface, snapshots its behavior at
first sight, then follows its price so we can learn what actually happened
next. This dataset powers outcome predictions in the Vorix Scanners section
(behavior -> grade -> predicted outcome, with the cohort size shown).

READ-ONLY on the scanner feeds. Writes only under ~/workspace/coin-outcomes/data/.

Sources (all free / keyless):
  - RawScan feed:    https://nova-rawscan.web.app/data/signals.json
  - AlphaPulse feed: https://nova-alphapulse.web.app/data/latest.json
  - Price updates:   DexScreener token endpoint (batches of 30 mints)

Idempotent: safe to re-run every 10-15 minutes. Registrations are keyed by
mint; observations are append-only; checkpoints record the nearest ACTUAL
observation time (never a fabricated one).

Usage:
  python3 track.py            # register new coins + update open records
  python3 track.py --status   # print dataset summary
"""

import json
import sqlite3
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

BASE = Path(__file__).resolve().parent
DB_PATH = BASE / "data" / "outcomes.db"

RAWSCAN_URL = "https://nova-rawscan.web.app/data/signals.json"
ALPHAPULSE_URL = "https://nova-alphapulse.web.app/data/latest.json"
DEX_TOKENS_URL = "https://api.dexscreener.com/latest/dex/tokens/"

CHECKPOINTS = {"1h": 3600, "6h": 6 * 3600, "24h": 24 * 3600, "7d": 7 * 24 * 3600}
DEAD_AFTER_MISSES = 3          # consecutive runs with no DexScreener pair
DEAD_MIN_AGE_S = 3600          # ...and at least this old before calling it dead
COMPLETE_GRACE_S = 12 * 3600   # past 7d + grace with no 7d price -> complete as-is

UA = {"User-Agent": "NovaCoinOutcomeTracker/1.0"}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def now_ts() -> float:
    return time.time()


def fetch_json(url: str, timeout: int = 30):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def fnum(v):
    """Best-effort float conversion (feeds sometimes send prices as strings)."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS coins (
    mint              TEXT PRIMARY KEY,
    ticker            TEXT,
    name              TEXT,
    chain             TEXT,
    first_seen_ts     REAL NOT NULL,
    first_seen_feeds  TEXT,
    entry_price_usd   REAL,
    -- normalized behavior snapshot (banding fields for cohort.py)
    score             REAL,
    age_hours         REAL,
    mcap_usd          REAL,
    liquidity_usd     REAL,
    vol_h1_usd        REAL,
    vol_h6_usd        REAL,
    vol_24h_usd       REAL,
    buy_ratio_h1      REAL,
    buy_ratio_h24     REAL,
    txns_h1           REAL,
    chg_m5            REAL,
    chg_h1            REAL,
    chg_h6            REAL,
    chg_h24           REAL,
    holder_count      REAL,
    top10_pct         REAL,
    bundle_score      REAL,
    mint_renounced    INTEGER,
    freeze_renounced  INTEGER,
    honeypot          INTEGER,
    snapshot_json     TEXT,
    -- outcome tracking
    status            TEXT NOT NULL DEFAULT 'open',   -- open | complete | dead
    last_price_usd    REAL,
    last_obs_ts       REAL,
    min_price_usd     REAL,
    peak_price_usd    REAL,
    peak_ts           REAL,
    max_dd_from_peak_pct REAL DEFAULT 0,
    miss_count        INTEGER DEFAULT 0,
    cp_1h_price  REAL, cp_1h_ts  REAL,
    cp_6h_price  REAL, cp_6h_ts  REAL,
    cp_24h_price REAL, cp_24h_ts REAL,
    cp_7d_price  REAL, cp_7d_ts  REAL,
    closed_ts         REAL
);
CREATE TABLE IF NOT EXISTS observations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    mint       TEXT NOT NULL,
    ts         REAL NOT NULL,
    price_usd  REAL NOT NULL,
    mcap_usd   REAL,
    source     TEXT
);
CREATE INDEX IF NOT EXISTS idx_obs_mint ON observations(mint, ts);
"""


def open_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


# --------------------------------------------------------------------------
# feed normalization
# --------------------------------------------------------------------------

def normalize(rec: dict, feed: str) -> dict:
    """Map a RawScan signal or AlphaPulse token record to normalized fields."""
    n = {
        "mint": rec.get("contract"),
        "ticker": rec.get("ticker"),
        "name": rec.get("name"),
        "chain": (rec.get("chain") or "solana").lower(),
        "price": fnum(rec.get("priceUsd")),
        "age_hours": fnum(rec.get("ageHours")),
        "liquidity": fnum(rec.get("liquidity")),
        "vol_h1": fnum(rec.get("volH1")),
        "vol_h6": fnum(rec.get("volH6")),
        "buy_ratio_h1": fnum(rec.get("buyRatioH1")),
        "buy_ratio_h24": fnum(rec.get("buyRatioH24")),
        "txns_h1": fnum(rec.get("txnsH1Total")),
    }
    if feed == "rawscan":
        n.update({
            "score": fnum(rec.get("score")),
            "mcap": None,  # thin RawScan schema carries no market cap / fdv
            "vol_24h": fnum(rec.get("volume24h")),
            "chg_m5": fnum(rec.get("chgM5")),
            "chg_h1": fnum(rec.get("chgH1")),
            "chg_h6": fnum(rec.get("chgH6")),
            "chg_h24": fnum(rec.get("chgH24")),
            "holder_count": None,
            "top10_pct": None,
            "bundle_score": None,
            "mint_renounced": None,
            "freeze_renounced": None,
            "honeypot": None,
        })
    else:  # alphapulse
        n.update({
            # AlphaPulse has no single "score"; `potential` is its grade.
            "score": fnum(rec.get("potential")),
            "mcap": fnum(rec.get("marketCap")) or fnum(rec.get("mcap")) or fnum(rec.get("fdv")),
            "vol_24h": fnum(rec.get("volH24")) or fnum(rec.get("volume24h")),
            "chg_m5": fnum(rec.get("priceChangeM5")),
            "chg_h1": fnum(rec.get("priceChangeH1")),
            "chg_h6": fnum(rec.get("priceChangeH6")),
            "chg_h24": fnum(rec.get("priceChangeH24")),
            "holder_count": fnum(rec.get("gpHolderCount")) or fnum(rec.get("rcTotalHolders"))
                            or fnum(rec.get("bsHoldersCount")),
            "top10_pct": fnum(rec.get("top10Pct")) or fnum(rec.get("holdersTop10Pct"))
                         or fnum(rec.get("gpTop1Pct")),
            "bundle_score": fnum(rec.get("bundleScore")),
            "mint_renounced": _renounced(rec.get("mintRenounced"), rec.get("gpMintActive")),
            "freeze_renounced": _renounced(rec.get("freezeRenounced"), rec.get("gpFreezeActive")),
            "honeypot": _bool_int(rec.get("gpHoneypot")),
        })
    return n


def _renounced(renounced_val, active_val):
    if renounced_val is not None:
        return _bool_int(renounced_val)
    if active_val is not None:  # GoPlus style: authority still active = NOT renounced
        return 0 if active_val else 1
    return None


def _bool_int(v):
    if v is None:
        return None
    return 1 if v else 0


def load_feeds():
    """Return {mint: {'norm': merged_norm, 'raw': {feed: record}}} — read-only."""
    merged = {}

    try:
        rs = fetch_json(RAWSCAN_URL)
        for rec in rs.get("signals", []):
            mint = rec.get("contract")
            if not mint:
                continue
            merged.setdefault(mint, {"raw": {}, "norm": None})
            merged[mint]["raw"]["rawscan"] = rec
            merged[mint]["norm"] = normalize(rec, "rawscan")
    except Exception as e:
        print(f"[warn] RawScan feed failed: {e}", file=sys.stderr)

    try:
        ap = fetch_json(ALPHAPULSE_URL)
        for rec in ap.get("tokens", []):
            mint = rec.get("contract")
            if not mint:
                continue
            slot = merged.setdefault(mint, {"raw": {}, "norm": None})
            slot["raw"]["alphapulse"] = rec
            ap_norm = normalize(rec, "alphapulse")
            if slot["norm"] is None:
                slot["norm"] = ap_norm
            else:
                # AlphaPulse is the richer record: fill gaps, keep RawScan score.
                rs_score = slot["norm"].get("score")
                for k, v in ap_norm.items():
                    if v is not None:
                        slot["norm"][k] = v
                if rs_score is not None:
                    slot["norm"]["score_rawscan"] = rs_score
    except Exception as e:
        print(f"[warn] AlphaPulse feed failed: {e}", file=sys.stderr)

    return merged


# --------------------------------------------------------------------------
# registration + outcome updates
# --------------------------------------------------------------------------

def register_new(db, feed_coins, ts):
    new = 0
    for mint, slot in feed_coins.items():
        n = slot["norm"]
        if n is None or n.get("price") is None:
            continue
        cur = db.execute("SELECT 1 FROM coins WHERE mint = ?", (mint,))
        if cur.fetchone():
            continue
        db.execute(
            """INSERT INTO coins
               (mint, ticker, name, chain, first_seen_ts, first_seen_feeds,
                entry_price_usd, score, age_hours, mcap_usd, liquidity_usd,
                vol_h1_usd, vol_h6_usd, vol_24h_usd, buy_ratio_h1, buy_ratio_h24,
                txns_h1, chg_m5, chg_h1, chg_h6, chg_h24, holder_count, top10_pct,
                bundle_score, mint_renounced, freeze_renounced, honeypot,
                snapshot_json, last_price_usd, last_obs_ts, min_price_usd,
                peak_price_usd, peak_ts)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (mint, n.get("ticker"), n.get("name"), n.get("chain"), ts,
             ",".join(sorted(slot["raw"].keys())), n["price"], n.get("score"),
             n.get("age_hours"), n.get("mcap"), n.get("liquidity"),
             n.get("vol_h1"), n.get("vol_h6"), n.get("vol_24h"),
             n.get("buy_ratio_h1"), n.get("buy_ratio_h24"), n.get("txns_h1"),
             n.get("chg_m5"), n.get("chg_h1"), n.get("chg_h6"), n.get("chg_h24"),
             n.get("holder_count"), n.get("top10_pct"), n.get("bundle_score"),
             n.get("mint_renounced"), n.get("freeze_renounced"), n.get("honeypot"),
             json.dumps(slot["raw"]), n["price"], ts, n["price"], n["price"], ts),
        )
        db.execute(
            "INSERT INTO observations (mint, ts, price_usd, mcap_usd, source) VALUES (?,?,?,?,?)",
            (mint, ts, n["price"], n.get("mcap"), "feed:first-seen"),
        )
        new += 1
    db.commit()
    return new


def _best_pair(pairs, mint):
    """Highest-liquidity (price, mcap) among pairs whose base token is `mint`.
    Address match is case-insensitive (DexScreener checksums EVM addresses)."""
    best = None
    for p in pairs:
        base = (p.get("baseToken") or {}).get("address") or ""
        if base.lower() != mint.lower():
            continue
        price = fnum(p.get("priceUsd"))
        if price is None:
            continue
        liq = fnum((p.get("liquidity") or {}).get("usd")) or 0
        if best is None or liq > best[2]:
            best = (price, fnum(p.get("marketCap")) or fnum(p.get("fdv")), liq)
    return (best[0], best[1]) if best else None


def fetch_prices(mints):
    """DexScreener price lookup.

    Returns {mint: (price, mcap) | None | 'unknown'} — None means DexScreener
    answered and has no pair for the mint; 'unknown' means the request itself
    failed and the caller must not count it as a miss.

    The tokens endpoint silently caps its response at ~30 pairs, so one
    many-pair token can crowd a whole batch out. Strategy: batch by 5, then
    individually retry every mint the batch didn't cover.
    """
    out = {}
    for i in range(0, len(mints), 5):
        batch = mints[i:i + 5]
        try:
            data = fetch_json(DEX_TOKENS_URL + ",".join(batch))
            pairs = data.get("pairs") or []
        except Exception as e:
            print(f"[warn] DexScreener batch failed: {e}", file=sys.stderr)
            for m in batch:
                out[m] = "unknown"
            continue
        for m in batch:
            hit = _best_pair(pairs, m)
            if hit:
                out[m] = hit
        time.sleep(0.3)

    for m in mints:
        if m in out:
            continue
        try:
            data = fetch_json(DEX_TOKENS_URL + m)
            out[m] = _best_pair(data.get("pairs") or [], m)
        except Exception as e:
            print(f"[warn] DexScreener lookup failed for {m}: {e}", file=sys.stderr)
            out[m] = "unknown"
        time.sleep(0.25)
    return out


def update_open(db, ts):
    rows = db.execute("SELECT * FROM coins WHERE status = 'open'").fetchall()
    if not rows:
        return {"updated": 0, "completed": 0, "dead": 0}
    prices = fetch_prices([r["mint"] for r in rows])
    updated = completed = dead = 0

    for r in rows:
        mint = r["mint"]
        got = prices.get(mint)
        age = ts - r["first_seen_ts"]

        if got == "unknown":
            continue  # request failed — no observation, no miss counted
        if got is None:
            misses = r["miss_count"] + 1
            if misses >= DEAD_AFTER_MISSES and age >= DEAD_MIN_AGE_S:
                db.execute(
                    "UPDATE coins SET status='dead', miss_count=?, closed_ts=? WHERE mint=?",
                    (misses, ts, mint))
                dead += 1
            elif age >= CHECKPOINTS["7d"] + COMPLETE_GRACE_S:
                db.execute(
                    "UPDATE coins SET status='complete', miss_count=?, closed_ts=? WHERE mint=?",
                    (misses, ts, mint))
                completed += 1
            else:
                db.execute("UPDATE coins SET miss_count=? WHERE mint=?", (misses, mint))
            continue

        price, mcap = got
        db.execute(
            "INSERT INTO observations (mint, ts, price_usd, mcap_usd, source) VALUES (?,?,?,?,?)",
            (mint, ts, price, mcap, "dexscreener"))
        updated += 1

        peak = r["peak_price_usd"] or r["entry_price_usd"]
        peak_ts = r["peak_ts"] or r["first_seen_ts"]
        if price > peak:
            peak, peak_ts = price, ts
        dd = r["max_dd_from_peak_pct"] or 0
        if peak > 0:
            dd = max(dd, (peak - price) / peak * 100.0)
        min_price = price if r["min_price_usd"] is None else min(r["min_price_usd"], price)
        if r["mcap_usd"] is None and mcap is not None:
            db.execute("UPDATE coins SET mcap_usd=? WHERE mint=?", (mcap, mint))

        sets = ["last_price_usd=?", "last_obs_ts=?", "min_price_usd=?",
                "peak_price_usd=?", "peak_ts=?", "max_dd_from_peak_pct=?", "miss_count=0"]
        vals = [price, ts, min_price, peak, peak_ts, dd]

        # checkpoints: first observation at/after the target time is the
        # nearest actual observation we have — record ITS timestamp.
        for label, offset in CHECKPOINTS.items():
            col_p, col_t = f"cp_{label}_price", f"cp_{label}_ts"
            if r[col_p] is None and age >= offset:
                sets += [f"{col_p}=?", f"{col_t}=?"]
                vals += [price, ts]

        status = "open"
        cp7 = r["cp_7d_price"]
        if cp7 is not None or (r["cp_7d_ts"] is None and age >= CHECKPOINTS["7d"] and "cp_7d_price=?" in " ".join(sets)):
            status = "complete"
        if status == "complete":
            sets.append("status='complete'")
            sets.append("closed_ts=?")
            vals.append(ts)
            completed += 1

        db.execute(f"UPDATE coins SET {', '.join(sets)} WHERE mint=?", (*vals, mint))

    db.commit()
    return {"updated": updated, "completed": completed, "dead": dead}


# --------------------------------------------------------------------------

def status(db):
    rows = db.execute(
        "SELECT status, COUNT(*) c FROM coins GROUP BY status").fetchall()
    total = db.execute("SELECT COUNT(*) c FROM coins").fetchone()["c"]
    obs = db.execute("SELECT COUNT(*) c FROM observations").fetchone()["c"]
    return total, obs, {r["status"]: r["c"] for r in rows}


def main():
    db = open_db()
    ts = now_ts()
    if "--status" in sys.argv:
        total, obs, by = status(db)
        print(f"coins tracked: {total} | observations: {obs} | by status: {by}")
        return
    feed_coins = load_feeds()
    print(f"feeds: {len(feed_coins)} unique coins in current feeds")
    new = register_new(db, feed_coins, ts)
    res = update_open(db, ts)
    total, obs, by = status(db)
    print(f"registered new: {new} | price updates: {res['updated']} | "
          f"completed: {res['completed']} | marked dead: {res['dead']}")
    print(f"dataset: {total} coins, {obs} observations, by status: {by}")


if __name__ == "__main__":
    main()
