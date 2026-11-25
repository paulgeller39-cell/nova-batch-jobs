#!/usr/bin/env python3
"""
Strategy 7 — CONFLUENCE — backtest add-on.

Runs the confluence strategy on the EXACT same harness, cached data and cost
model as the original six-strategy backtest (backtest.py). It imports the
original module, registers one additional strategy, and writes its results to
SEPARATE files (results/confluence_*) so the original six strategies' outputs
are never touched.

Rules (locked by Paul + Everest BEFORE this run; zero tuning):

BUY — enter when at least 4 of these 5 hold on a closed bar; fill next open:
  1. RSI(14) <= 35
  2. Close > EMA-50
  3. MACD histogram turning positive: hist > 0 AND hist > previous bar's hist
  4. Close <= lower Bollinger band (SMA20 - 2 sigma)
  5. No Donchian breakdown: close > prior 10-bar Donchian low (excl. current bar)

SELL — exit when at least 3 of these 5 hold on a closed bar; fill next open:
  1. RSI(14) >= 70
  2. Close < EMA-50
  3. MACD histogram turning negative: hist < 0 AND hist < previous bar's hist
  4. Close >= upper Bollinger band (SMA20 + 2 sigma)
  5. Donchian breakdown: close < prior 10-bar Donchian low (excl. current bar)

HARD STOP: -8% from entry, via the harness's existing intraday stop logic
(STRATEGY_RULES stop_pct): on every bar, if the bar's low <= entry_raw * 0.92
the position fills at the stop price — or at the bar's open if the bar opened
below the stop (gap-through). Exit reason "STOP". Signal exits reason
"CONFLUENCE_EXIT"; end-of-window closes reason "EOD" (harness convention).

Implementation notes:
- Upper Bollinger band: the harness computes bb_mid (SMA20) and bb_low
  (SMA20 - 2 sigma, population sigma over the same 20 closes). bb_up is
  derived as 2*bb_mid - bb_low, which is exactly SMA20 + 2 sigma.
- MACD histogram = macd - macd_sig from the harness series.
- "Prior 10-bar Donchian low (excluding the current bar)" at bar i is the
  harness's don_low10[i-1] (min of lows over bars i-10..i-1) — the same
  indexing the harness's own donchian exit uses.
- Entries require every component to be defined (RSI/BB/Donchian warmups
  mean the earliest possible signal bar is ~bar 20).
"""
import csv, json, os, sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtest as bt

NAME = "confluence"
bt.STRATEGY_RULES[NAME] = {"stop_pct": 0.08}

_orig_build_indicators = bt.build_indicators
_orig_signal_entry = bt.signal_entry
_orig_signal_exit = bt.signal_exit


def build_indicators(bars):
    ind = _orig_build_indicators(bars)
    ind["bb_up"] = [
        None if ind["bb_mid"][i] is None or ind["bb_low"][i] is None
        else 2 * ind["bb_mid"][i] - ind["bb_low"][i]
        for i in range(len(bars))
    ]
    ind["macd_hist"] = [m - s for m, s in zip(ind["macd"], ind["macd_sig"])]
    return ind


def buy_votes(ind, i, bars):
    """Return list of 5 bools, or None if any component is undefined."""
    if i < 1:
        return None
    r = ind["rsi"][i]
    lb = ind["bb_low"][i]
    dl = ind["don_low10"][i - 1]
    e50 = ind["ema50"][i]
    h, hp = ind["macd_hist"][i], ind["macd_hist"][i - 1]
    if None in (r, lb, dl, e50):
        return None
    c = bars[i][4]
    return [
        r <= 35,
        c > e50,
        h > 0 and h > hp,
        c <= lb,
        c > dl,
    ]


def sell_votes(ind, i, bars):
    if i < 1:
        return None
    r = ind["rsi"][i]
    ub = ind["bb_up"][i]
    dl = ind["don_low10"][i - 1]
    e50 = ind["ema50"][i]
    h, hp = ind["macd_hist"][i], ind["macd_hist"][i - 1]
    if None in (r, ub, dl, e50):
        return None
    c = bars[i][4]
    return [
        r >= 70,
        c < e50,
        h < 0 and h < hp,
        c >= ub,
        c < dl,
    ]


def signal_entry(name, ind, i, bars):
    if name == NAME:
        v = buy_votes(ind, i, bars)
        return v is not None and sum(v) >= 4
    return _orig_signal_entry(name, ind, i, bars)


def signal_exit(name, ind, i, pos, bars):
    if name == NAME:
        v = sell_votes(ind, i, bars)
        if v is not None and sum(v) >= 3:
            return "CONFLUENCE_EXIT"
        return None
    return _orig_signal_exit(name, ind, i, pos, bars)


bt.build_indicators = build_indicators
bt.signal_entry = signal_entry
bt.signal_exit = signal_exit


def main():
    all_trades = []
    data_report = {}
    for symbol, mint, cls in bt.UNIVERSE:
        bars, how = bt.load_or_fetch(symbol, mint)
        if not bars:
            data_report[symbol] = {"status": how, "bars": 0}
            print(f"{symbol}: NO DATA ({how})", flush=True)
            continue
        ts = [b[0] for b in bars]
        data_report[symbol] = {
            "status": how, "bars": len(bars),
            "first": datetime.fromtimestamp(ts[0], timezone.utc).isoformat(),
            "last": datetime.fromtimestamp(ts[-1], timezone.utc).isoformat(),
        }
        print(f"{symbol}: {len(bars)} bars ({how})", flush=True)
        ind = bt.build_indicators(bars)
        all_trades.extend(bt.run_strategy(NAME, symbol, cls, bars, ind))

    res = bt.RES_DIR
    with open(os.path.join(res, "confluence_trades.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["strategy", "symbol", "entry_time", "exit_time",
                                          "entry_price", "exit_price", "pnl_usd", "return_pct",
                                          "exit_reason", "bars_held"])
        w.writeheader(); w.writerows(all_trades)

    summary = bt.metrics(all_trades)
    by_symbol = {s: bt.metrics([t for t in all_trades if t["symbol"] == s])
                 for s, _, _ in bt.UNIVERSE}
    reasons = {}
    for t in all_trades:
        reasons[t["exit_reason"]] = reasons.get(t["exit_reason"], 0) + 1

    with open(os.path.join(res, "confluence_summary.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["strategy"] + list(summary.keys()))
        w.writerow([NAME] + list(summary.values()))
    with open(os.path.join(res, "confluence_summary_by_symbol.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["strategy|symbol"] + list(next(iter(by_symbol.values())).keys()))
        for k, v in by_symbol.items():
            w.writerow([f"{NAME}|{k}"] + list(v.values()))
    with open(os.path.join(res, "confluence_summary.json"), "w") as f:
        json.dump({"data": data_report, "summary": summary, "by_symbol": by_symbol,
                   "exit_reasons": reasons}, f, indent=2)

    print(json.dumps({"summary": summary, "exit_reasons": reasons}, indent=2))
    print(json.dumps(by_symbol, indent=2))


if __name__ == "__main__":
    main()
