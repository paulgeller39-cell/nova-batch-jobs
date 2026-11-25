#!/usr/bin/env python3
"""
High-win-rate hunt — strategies 14-19 (mean-reversion designs).

Same harness, data, costs and fill model as backtest.py (imported, not
edited). Textbook parameters fixed upfront by the task spec; zero tuning.
Every strategy carries the uniform -8% hard stop (stop_pct mechanism).

Rules (locked before the run):
 14 connors_rsi2        buy RSI(2) < 10;                     sell RSI(2) > 65
 15 double_oversold     buy RSI(14) < 30 AND Stoch %K < 20;  sell RSI(14) > 55
 16 bollinger_snapback  buy close below lower BB, next bar closes back inside;
                        sell close >= SMA-20 basis
 17 quick_target        buy RSI(14) <= 30; sell +4% TP (intraday high) or
                        RSI(14) >= 50, whichever first
 18 zscore_rev          buy z(50) < -2; sell z(50) > 0
 19 three_down          buy after 3 consecutive lower closes; sell +4% TP
                        (intraday high) or after 48 bars held, whichever first

Implementation choices (documented, per the no-tuning rule):
 - Stochastic %K(14,3) = 3-bar SMA of the raw 14-bar %K (standard slow %K).
 - Z-score sigma is the population stdev of the last 50 closes, matching the
   harness Bollinger convention (divide by n, not n-1).
 - Snap-back "inside the band" is literal: lower band < close < upper band.
 - Three-Down time stop: exit signal fires on the first closed bar where
   bars-since-entry >= 48; it fills at the next open like every signal exit
   (the trade record therefore shows 49 bars held).
"""
import csv, json, math, os, sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtest as bt

HUNT_RULES = {
    "connors_rsi2":       {"stop_pct": 0.08},
    "double_oversold":    {"stop_pct": 0.08},
    "bollinger_snapback": {"stop_pct": 0.08},
    "quick_target":       {"tp_pct": 0.04, "stop_pct": 0.08},
    "zscore_rev":         {"stop_pct": 0.08},
    "three_down":         {"tp_pct": 0.04, "stop_pct": 0.08},
}
bt.STRATEGY_RULES.update(HUNT_RULES)

_orig_entry = bt.signal_entry
_orig_exit = bt.signal_exit


def hunt_entry(name, ind, i, bars):
    if name not in HUNT_RULES:
        return _orig_entry(name, ind, i, bars)
    c = bars[i][4]
    if name == "connors_rsi2":
        r = ind["rsi2"][i]
        return r is not None and r < 10
    if name == "double_oversold":
        r, k = ind["rsi"][i], ind["stoch_k"][i]
        return r is not None and k is not None and r < 30 and k < 20
    if name == "bollinger_snapback":
        if i < 1:
            return False
        lb_p, lb, ub = ind["bb_low"][i - 1], ind["bb_low"][i], ind["bb_high"][i]
        if lb_p is None or lb is None or ub is None:
            return False
        return bars[i - 1][4] < lb_p and lb < c < ub
    if name == "quick_target":
        r = ind["rsi"][i]
        return r is not None and r <= 30
    if name == "zscore_rev":
        z = ind["z50"][i]
        return z is not None and z < -2
    if name == "three_down":
        if i < 3:
            return False
        cl = [bars[j][4] for j in range(i - 3, i + 1)]
        return cl[3] < cl[2] < cl[1] < cl[0]
    return False


def hunt_exit(name, ind, i, pos, bars):
    if name not in HUNT_RULES:
        return _orig_exit(name, ind, i, pos, bars)
    if name == "connors_rsi2":
        r = ind["rsi2"][i]
        if r is not None and r > 65:
            return "RSI2_TARGET"
    if name == "double_oversold":
        r = ind["rsi"][i]
        if r is not None and r > 55:
            return "RSI_TARGET"
    if name == "bollinger_snapback":
        mid = ind["bb_mid"][i]
        if mid is not None and bars[i][4] >= mid:
            return "BB_MEAN"
    if name == "quick_target":
        r = ind["rsi"][i]
        if r is not None and r >= 50:
            return "RSI_TARGET"
    if name == "zscore_rev":
        z = ind["z50"][i]
        if z is not None and z > 0:
            return "Z_MEAN"
    if name == "three_down":
        if i - pos["entry_idx"] >= 48:
            return "TIME_STOP"
    return None


bt.signal_entry = hunt_entry
bt.signal_exit = hunt_exit


def hunt_indicators(bars):
    ind = bt.build_indicators(bars)
    closes = [b[4] for b in bars]
    highs = [b[2] for b in bars]
    lows = [b[3] for b in bars]
    ind["rsi2"] = bt.rsi_series(closes, 2)
    # Stochastic %K(14,3): 3-bar SMA of raw 14-bar %K
    hh14 = bt.rolling(highs, 14, max)
    ll14 = bt.rolling(lows, 14, min)
    raw_k = []
    for i in range(len(closes)):
        if hh14[i] is None or ll14[i] is None:
            raw_k.append(None)
        elif hh14[i] == ll14[i]:
            raw_k.append(50.0)
        else:
            raw_k.append(100.0 * (closes[i] - ll14[i]) / (hh14[i] - ll14[i]))
    stoch = [None] * len(closes)
    for i in range(2, len(closes)):
        w = raw_k[i - 2: i + 1]
        if all(x is not None for x in w):
            stoch[i] = sum(w) / 3
    ind["stoch_k"] = stoch
    # SMA-50 / population sigma-50 / z-score
    sma50 = bt.sma_series(closes, 50)
    z = [None] * len(closes)
    for i in range(49, len(closes)):
        w = closes[i - 49: i + 1]
        m = sum(w) / 50
        sd = math.sqrt(sum((x - m) ** 2 for x in w) / 50)
        if sd > 0:
            z[i] = (closes[i] - m) / sd
    ind["sma50"] = sma50
    ind["z50"] = z
    # Upper Bollinger band (build_indicators only exports mid/low)
    ind["bb_high"] = [None if ind["bb_mid"][i] is None or ind["bb_low"][i] is None
                      else 2 * ind["bb_mid"][i] - ind["bb_low"][i]
                      for i in range(len(closes))]
    return ind


def extra_metrics(trades):
    m = bt.metrics(trades)
    if trades:
        wins = [t["pnl_usd"] for t in trades if t["pnl_usd"] > 0]
        losses = [t["pnl_usd"] for t in trades if t["pnl_usd"] <= 0]
        m["avg_win_usd"] = round(sum(wins) / len(wins), 2) if wins else 0
        m["avg_loss_usd"] = round(sum(losses) / len(losses), 2) if losses else 0
        m["win_loss_ratio"] = (round(abs(m["avg_win_usd"] / m["avg_loss_usd"]), 2)
                               if losses and m["avg_loss_usd"] else None)
    else:
        m["avg_win_usd"] = 0
        m["avg_loss_usd"] = 0
        m["win_loss_ratio"] = None
    return m


def main():
    all_trades = []
    data_report = {}
    for symbol, mint, cls in bt.UNIVERSE:
        bars, how = bt.load_or_fetch(symbol, mint)
        if not bars:
            data_report[symbol] = {"status": how, "bars": 0}
            print(f"{symbol}: NO DATA ({how})", flush=True)
            continue
        data_report[symbol] = {
            "status": how, "bars": len(bars),
            "first": datetime.fromtimestamp(bars[0][0], timezone.utc).isoformat(),
            "last": datetime.fromtimestamp(bars[-1][0], timezone.utc).isoformat(),
        }
        print(f"{symbol}: {len(bars)} bars ({how})", flush=True)
        ind = hunt_indicators(bars)
        for name in HUNT_RULES:
            all_trades.extend(bt.run_strategy(name, symbol, cls, bars, ind))

    res = bt.RES_DIR
    with open(os.path.join(res, "hunt_trades.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["strategy", "symbol", "entry_time", "exit_time",
                                          "entry_price", "exit_price", "pnl_usd", "return_pct",
                                          "exit_reason", "bars_held"])
        w.writeheader(); w.writerows(all_trades)

    summary, by_symbol = {}, {}
    for name in HUNT_RULES:
        tr = [t for t in all_trades if t["strategy"] == name]
        summary[name] = extra_metrics(tr)
        for symbol, _, _ in bt.UNIVERSE:
            by_symbol[f"{name}|{symbol}"] = extra_metrics([t for t in tr if t["symbol"] == symbol])

    with open(os.path.join(res, "hunt_summary.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["strategy"] + list(next(iter(summary.values())).keys()))
        for k, v in summary.items():
            w.writerow([k] + list(v.values()))
    with open(os.path.join(res, "hunt_summary.json"), "w") as f:
        json.dump({"data": data_report, "summary": summary, "by_symbol": by_symbol}, f, indent=2)

    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
