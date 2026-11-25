#!/usr/bin/env python3
"""
Nova/Vorix bot strategy backtest — 3 months of Solana hourly bars.

Data: GeckoTerminal (keyless). Signals on CLOSED bars only; fills at the
NEXT bar's open (or worse via intraday stop logic). Long-only spot.
Costs on EVERY fill: class slippage per side (majors/midcaps 0.1%,
memecoins 0.5%) + 0.25% swap fee per side.
Position size: flat $100 per trade. One open position per symbol per strategy.
"""
import csv, json, math, os, sys, time
from datetime import datetime, timezone
import requests

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "data")
RES_DIR = os.path.join(BASE, "results")
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(RES_DIR, exist_ok=True)

START = int(datetime(2026, 8, 24, 0, 0, tzinfo=timezone.utc).timestamp())
END = int(datetime(2026, 11, 24, 23, 0, tzinfo=timezone.utc).timestamp())
GT = "https://api.geckoterminal.com/api/v2"
HDRS = {"accept": "application/json"}

# symbol, mint, class (major = 0.1% slip/side, meme = 0.5% slip/side)
UNIVERSE = [
    ("SOL",    "So11111111111111111111111111111111111111112",            "major"),
    ("JUP",    "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",            "major"),
    ("FARTCOIN", "9BB6NFEcjBCtnNLFko2FqVQBq8HHM13kCyYcdQbgpump",         "meme"),
    ("PYTH",   "HZ1JovNiVvGrGNiiYvEozEVgZ58xaU3RKwX8eACQBCt3",            "major"),
    ("ORCA",   "orcaEKTdK7LKz57vaAYr9QeNsVEPfiu6QeMU1kektZE",            "major"),
    ("BONK",   "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",           "meme"),
    ("WIF",    "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm",           "meme"),
    ("POPCAT", "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr",           "meme"),
    ("PENGU",  "2zMMhcVQEXDtdE6vsFS7S7D5oUodfJHE8vd1gnBouauv",           "meme"),
]
SLIP = {"major": 0.001, "meme": 0.005}
SWAP_FEE = 0.0025  # per side
NOTIONAL = 100.0
REF_BANKROLL = 1000.0  # only for expressing max drawdown as a %

# ---------------- data ----------------

def get_json(url, tries=5):
    for a in range(tries):
        r = requests.get(url, headers=HDRS, timeout=40)
        if r.status_code == 429:
            wait = 20 * (a + 1)
            print(f"  429 rate-limited, sleeping {wait}s", flush=True)
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"gave up on {url}")


def top_pool(mint):
    j = get_json(f"{GT}/networks/solana/tokens/{mint}/pools?page=1")
    best, best_liq = None, -1.0
    for p in j.get("data", []):
        rel = p.get("relationships", {})
        base_id = (rel.get("base_token", {}).get("data") or {}).get("id", "")
        if base_id != f"solana_{mint}":
            continue
        liq = float(p["attributes"].get("reserve_in_usd") or 0)
        if liq > best_liq:
            best, best_liq = p["attributes"]["address"], liq
    return best, best_liq


def fetch_ohlcv(pool):
    bars = {}
    before = END + 3600
    for _ in range(6):
        url = (f"{GT}/networks/solana/pools/{pool}/ohlcv/hour?aggregate=1"
               f"&before_timestamp={before}&limit=1000&currency=usd&token=base")
        j = get_json(url)
        lst = (j.get("data", {}).get("attributes", {}) or {}).get("ohlcv_list", [])
        if not lst:
            break
        for ts, o, h, l, c, v in lst:
            ts = int(ts)
            if START <= ts <= END:
                bars[ts] = (ts, float(o), float(h), float(l), float(c), float(v or 0))
        oldest = min(int(x[0]) for x in lst)
        if oldest <= START:
            break
        before = oldest
        time.sleep(1.5)
    return [bars[k] for k in sorted(bars)]


def load_or_fetch(symbol, mint):
    path = os.path.join(DATA_DIR, f"{symbol}_hour.csv")
    if os.path.exists(path):
        rows = []
        with open(path) as f:
            for r in csv.DictReader(f):
                rows.append((int(r["ts"]), float(r["open"]), float(r["high"]),
                             float(r["low"]), float(r["close"]), float(r["volume"])))
        if rows:
            return rows, "cache"
    try:
        pool, liq = top_pool(mint)
    except Exception as e:
        return [], f"error:{e}"
    if not pool:
        return [], "no_pool"
    time.sleep(1.5)
    bars = fetch_ohlcv(pool)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", "open", "high", "low", "close", "volume"])
        w.writerows(bars)
    manifest[symbol] = {"pool": pool, "pool_liquidity_usd": liq}
    return bars, "fetched"


manifest = {}

# ---------------- indicators ----------------

def ema_series(vals, n):
    k = 2 / (n + 1)
    out = []
    e = None
    for v in vals:
        e = v if e is None else (v - e) * k + e
        out.append(e)
    return out


def rsi_series(closes, n=14):
    out = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains, losses = [], []
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0)); losses.append(max(-d, 0.0))
    ag, al = sum(gains) / n, sum(losses) / n
    def rsi(ag, al):
        if al == 0:
            return 100.0
        return 100 - 100 / (1 + ag / al)
    out[n] = rsi(ag, al)
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(d, 0.0)) / n
        al = (al * (n - 1) + max(-d, 0.0)) / n
        out[i] = rsi(ag, al)
    return out


def sma_series(vals, n):
    out = [None] * len(vals)
    s = 0.0
    for i, v in enumerate(vals):
        s += v
        if i >= n:
            s -= vals[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def rolling(vals, n, fn):
    out = [None] * len(vals)
    for i in range(n - 1, len(vals)):
        out[i] = fn(vals[i - n + 1: i + 1])
    return out

# ---------------- backtest engine ----------------

def run_strategy(name, symbol, cls, bars, ind):
    """ind: dict of precomputed indicator series keyed by strategy need."""
    slip = SLIP[cls]
    cost = slip + SWAP_FEE
    trades = []
    pos = None          # dict(entry_idx, entry_raw, entry_eff, peak)
    pending_entry = False
    pending_exit = None  # reason string

    def close_trade(idx, raw_exit, reason):
        nonlocal pos
        eff_exit = raw_exit * (1 - cost)
        pnl = NOTIONAL * (eff_exit / pos["entry_eff"] - 1)
        trades.append({
            "strategy": name, "symbol": symbol,
            "entry_time": datetime.fromtimestamp(bars[pos["entry_idx"]][0], timezone.utc).isoformat(),
            "exit_time": datetime.fromtimestamp(bars[idx][0], timezone.utc).isoformat(),
            "entry_price": pos["entry_raw"], "exit_price": raw_exit,
            "pnl_usd": round(pnl, 4), "return_pct": round(100 * pnl / NOTIONAL, 3),
            "exit_reason": reason, "bars_held": idx - pos["entry_idx"],
        })
        pos = None

    def intraday_exit(idx):
        """Check TP/SL/trailing for open position on bar idx. Returns True if closed."""
        ts, o, h, l, c, v = bars[idx]
        rules = STRATEGY_RULES[name]
        cands = []  # (priority, raw_price, reason) — stop-type first (conservative)
        if rules.get("stop_pct"):
            sp = pos["entry_raw"] * (1 - rules["stop_pct"])
            if l <= sp:
                cands.append((0, min(o, sp) if o < sp else sp, "STOP"))
        if rules.get("trail_pct"):
            sp = pos["peak"] * (1 - rules["trail_pct"])
            if l <= sp and sp < pos["entry_raw"] * 10:  # sanity
                cands.append((0, min(o, sp) if o < sp else sp, "TRAIL"))
        if rules.get("tp_pct"):
            tp = pos["entry_raw"] * (1 + rules["tp_pct"])
            if h >= tp:
                cands.append((1, max(o, tp) if o > tp else tp, "TAKE_PROFIT"))
        if cands:
            cands.sort(key=lambda x: x[0])
            close_trade(idx, cands[0][1], cands[0][2])
            return True
        return False

    n = len(bars)
    for idx in range(n):
        ts, o, h, l, c, v = bars[idx]
        # fills at this bar's open from signals on the previous closed bar
        if pending_exit and pos is not None:
            close_trade(idx, o, pending_exit)
            pending_exit = None
        if pending_entry and pos is None and idx > 0:
            pos = {"entry_idx": idx, "entry_raw": o, "entry_eff": o * (1 + cost), "peak": o}
            pending_entry = False
        # intraday exits (peak uses bars strictly before idx -> no lookahead)
        if pos is not None:
            if not intraday_exit(idx):
                # update peak AFTER intraday check, using this bar's high
                pos["peak"] = max(pos["peak"], h)
        # signals on this closed bar
        if idx < n - 1:
            if pos is not None and pending_exit is None:
                if signal_exit(name, ind, idx, pos, bars):
                    pending_exit = signal_exit(name, ind, idx, pos, bars)
                elif name == "scanner_proxy" and idx - pos["entry_idx"] >= 48:
                    pending_exit = "TIME_STOP"
            if pos is None and not pending_entry:
                if signal_entry(name, ind, idx, bars):
                    pending_entry = True
    if pos is not None:
        close_trade(n - 1, bars[n - 1][4], "EOD")
    return trades


STRATEGY_RULES = {
    "ema_trend":      {"trail_pct": 0.08},
    "rsi_reversion":  {"tp_pct": 0.08, "stop_pct": 0.06},
    "donchian":       {"trail_pct": 0.10},
    "bollinger_rev":  {"tp_pct": 0.07, "stop_pct": 0.06},
    "scanner_proxy":  {"tp_pct": 0.20, "stop_pct": 0.10},
    "macd_rider":     {"trail_pct": 0.08},
}


def signal_entry(name, ind, i, bars):
    c = bars[i][4]
    if name == "ema_trend":
        if i < 55: return False
        e20, e50 = ind["ema20"], ind["ema50"]
        return e20[i - 1] <= e50[i - 1] and e20[i] > e50[i]
    if name == "rsi_reversion":
        r = ind["rsi"][i]
        return r is not None and r < 30
    if name == "donchian":
        if i < 21: return False
        hh = ind["don_high20"][i - 1]  # highest high of the 20 bars ending at i-1
        va = ind["vol_sma20"][i]
        return hh is not None and c > hh and va is not None and bars[i][5] >= 1.2 * va
    if name == "bollinger_rev":
        lb = ind["bb_low"][i]
        return lb is not None and c < lb
    if name == "scanner_proxy":
        if i < 25: return False
        ret24 = c / bars[i - 24][4] - 1
        va = ind["vol_sma24"][i]
        e20 = ind["ema20"][i]
        return ret24 >= 0.15 and va is not None and bars[i][5] >= 2.0 * va and c > e20
    if name == "macd_rider":
        if i < 35: return False
        m, s = ind["macd"], ind["macd_sig"]
        return m[i - 1] <= s[i - 1] and m[i] > s[i]
    return False


def signal_exit(name, ind, i, pos, bars):
    c = bars[i][4]
    if name == "ema_trend":
        e20, e50 = ind["ema20"], ind["ema50"]
        if e20[i - 1] >= e50[i - 1] and e20[i] < e50[i]:
            return "EMA_CROSS_DOWN"
    if name == "rsi_reversion":
        r = ind["rsi"][i]
        if r is not None and r >= 55:
            return "RSI_TARGET"
    if name == "donchian":
        if i >= 11:
            ll = ind["don_low10"][i - 1]
            if ll is not None and c < ll:
                return "DONCHIAN_EXIT"
    if name == "bollinger_rev":
        mid = ind["bb_mid"][i]
        if mid is not None and c >= mid:
            return "BB_MEAN"
    if name == "macd_rider":
        m, s = ind["macd"], ind["macd_sig"]
        if m[i - 1] >= s[i - 1] and m[i] < s[i]:
            return "MACD_CROSS_DOWN"
    return None


def build_indicators(bars):
    closes = [b[4] for b in bars]
    highs = [b[2] for b in bars]
    lows = [b[3] for b in bars]
    vols = [b[5] for b in bars]
    sma20 = sma_series(closes, 20)
    sd20 = []
    for i in range(len(closes)):
        if i >= 19:
            w = closes[i - 19: i + 1]
            m = sum(w) / 20
            sd20.append(math.sqrt(sum((x - m) ** 2 for x in w) / 20))
        else:
            sd20.append(None)
    ema12, ema26 = ema_series(closes, 12), ema_series(closes, 26)
    macd = [a - b for a, b in zip(ema12, ema26)]
    return {
        "ema20": ema_series(closes, 20), "ema50": ema_series(closes, 50),
        "rsi": rsi_series(closes, 14),
        "don_high20": rolling(highs, 20, max), "don_low10": rolling(lows, 10, min),
        "vol_sma20": sma_series(vols, 20), "vol_sma24": sma_series(vols, 24),
        "bb_mid": sma20,
        "bb_low": [None if sma20[i] is None or sd20[i] is None else sma20[i] - 2 * sd20[i] for i in range(len(closes))],
        "macd": macd, "macd_sig": ema_series(macd, 9),
    }

# ---------------- metrics ----------------

def metrics(trades):
    if not trades:
        return {"trades": 0, "win_rate_pct": 0, "net_pnl_usd": 0, "net_return_on_deployed_pct": 0,
                "profit_factor": 0, "max_drawdown_usd": 0, "max_drawdown_pct_of_1000": 0,
                "best_trade_usd": 0, "worst_trade_usd": 0, "top_trade_share_of_gross_profit_pct": 0,
                "avg_return_pct": 0}
    pnls = [t["pnl_usd"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_p = sum(wins); gross_l = -sum(losses)
    net = sum(pnls)
    cum, peak, dd = 0.0, 0.0, 0.0
    for t in sorted(trades, key=lambda x: x["exit_time"]):
        cum += t["pnl_usd"]
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return {
        "trades": len(trades),
        "win_rate_pct": round(100 * len(wins) / len(trades), 1),
        "net_pnl_usd": round(net, 2),
        "net_return_on_deployed_pct": round(100 * net / (NOTIONAL * len(trades)), 2),
        "profit_factor": round(gross_p / gross_l, 2) if gross_l > 0 else float("inf"),
        "max_drawdown_usd": round(dd, 2),
        "max_drawdown_pct_of_1000": round(100 * dd / REF_BANKROLL, 1),
        "best_trade_usd": round(max(pnls), 2),
        "worst_trade_usd": round(min(pnls), 2),
        "top_trade_share_of_gross_profit_pct": round(100 * max(pnls) / gross_p, 1) if gross_p > 0 else 0,
        "avg_return_pct": round(sum(t["return_pct"] for t in trades) / len(trades), 2),
    }

# ---------------- main ----------------

def main():
    all_trades = []
    data_report = {}
    for symbol, mint, cls in UNIVERSE:
        bars, how = load_or_fetch(symbol, mint)
        if not bars:
            data_report[symbol] = {"status": how, "bars": 0}
            print(f"{symbol}: NO DATA ({how})", flush=True)
            continue
        ts = [b[0] for b in bars]
        gaps = sum(1 for a, b in zip(ts, ts[1:]) if b - a > 3600)
        gap_hours = sum((b - a) // 3600 - 1 for a, b in zip(ts, ts[1:]) if b - a > 3600)
        data_report[symbol] = {
            "status": how, "bars": len(bars),
            "first": datetime.fromtimestamp(ts[0], timezone.utc).isoformat(),
            "last": datetime.fromtimestamp(ts[-1], timezone.utc).isoformat(),
            "gap_segments": gaps, "missing_hours_inside_range": int(gap_hours),
            **({"pool": manifest[symbol]["pool"], "pool_liquidity_usd": manifest[symbol]["pool_liquidity_usd"]} if symbol in manifest else {}),
        }
        print(f"{symbol}: {len(bars)} bars {data_report[symbol]['first']} -> {data_report[symbol]['last']}", flush=True)
        ind = build_indicators(bars)
        for name in STRATEGY_RULES:
            all_trades.extend(run_strategy(name, symbol, cls, bars, ind))
        time.sleep(1.0)

    with open(os.path.join(RES_DIR, "trades_all.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["strategy", "symbol", "entry_time", "exit_time",
                                          "entry_price", "exit_price", "pnl_usd", "return_pct",
                                          "exit_reason", "bars_held"])
        w.writeheader(); w.writerows(all_trades)

    summary = {}
    by_symbol = {}
    for name in STRATEGY_RULES:
        tr = [t for t in all_trades if t["strategy"] == name]
        summary[name] = metrics(tr)
        for symbol, _, _ in UNIVERSE:
            by_symbol[f"{name}|{symbol}"] = metrics([t for t in tr if t["symbol"] == symbol])

    with open(os.path.join(RES_DIR, "summary.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["strategy"] + list(next(iter(summary.values())).keys()))
        for k, v in summary.items():
            w.writerow([k] + list(v.values()))
    with open(os.path.join(RES_DIR, "summary_by_symbol.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["strategy|symbol"] + list(next(iter(by_symbol.values())).keys()))
        for k, v in by_symbol.items():
            w.writerow([k] + list(v.values()))
    with open(os.path.join(RES_DIR, "summary.json"), "w") as f:
        json.dump({"data": data_report, "summary": summary, "by_symbol": by_symbol}, f, indent=2)
    with open(os.path.join(DATA_DIR, "manifest.json"), "w") as f:
        json.dump({"manifest": manifest, "data_report": data_report}, f, indent=2)

    print(json.dumps(summary, indent=2))
    print(json.dumps(data_report, indent=2))


if __name__ == "__main__":
    main()
