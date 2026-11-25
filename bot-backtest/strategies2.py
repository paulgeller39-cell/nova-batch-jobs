#!/usr/bin/env python3
"""
Strategies 8-13 — "different tools" slate — backtest add-on.

Runs six NEW strategies, each built on a genuinely different tool than the
original six (RSI / EMA / Donchian / Bollinger / scanner-proxy / MACD) and
than the confluence combo, on the EXACT same harness, cached data and cost
model as backtest.py. It imports the original module, registers six
additional strategies, and writes results to SEPARATE files
(results/extra_*) so no earlier output is touched. backtest.py and
confluence.py are not modified.

Rules (textbook defaults, fixed upfront, zero tuning):

8.  stochastic    Stochastic %K(14,3): buy when smoothed %K crosses up out
                  of oversold (%K[i-1] < 20 and %K[i] > 20); sell when it
                  crosses down out of overbought (%K[i-1] > 80, %K[i] < 80).
9.  supertrend    Supertrend (ATR-10 Wilder, multiplier 3): buy when the
                  trend direction flips down->up on a closed bar; sell on
                  the up->down flip.
10. keltner       Keltner channel breakout (EMA-20 basis, ATR-10 x2 bands):
                  buy on close crossing above the upper band; sell on close
                  back below the EMA-20 basis.
11. adx_trend     ADX-14 (Wilder) trend-strength with EMA-50 filter: buy
                  when ADX > 25 AND close > EMA-50 (state rule); sell when
                  ADX < 20 OR close < EMA-50.
12. vwap_rev      Rolling 24h VWAP reversion (typical price x volume over
                  the trailing 24 hourly bars — the cached bars carry real
                  volume on every bar, so this is a true VWAP, not a mean
                  substitute): a dip to <= 0.98 x VWAP "arms" the setup;
                  buy when a later bar closes back above VWAP (reclaim);
                  sell when close >= 1.02 x VWAP (target) or close <= 0.95 x
                  entry price (strategy stop, signal-based, fills next open).
13. vol_breakout  Volume breakout: buy when close breaks the prior 20-bar
                  high (excl. current bar) AND bar volume > 1.5x the 20-bar
                  average volume (harness vol_sma20, which includes the
                  current bar — the same series the harness's donchian
                  volume filter uses); sell on close below the prior
                  10-bar low (excl. current bar).

HARD STOP for all six: -8% from entry via the harness's existing intraday
stop mechanism (STRATEGY_RULES stop_pct: 0.08 — the same convention the
confluence addendum documents): on every bar, if the bar's low <= entry
price x 0.92 the position fills at the stop price, or at the bar's open if
the bar opened below the stop (gap-through). Exit reason "STOP".

Indicator conventions (textbook):
- ATR-10: Wilder. TR[0] = H-L; TR[i] = max(H-L, |H-C[i-1]|, |L-C[i-1]|).
  First ATR at bar 9 = mean(TR[0..9]); then ATR = (ATR_prev*9 + TR)/10.
- Stochastic: raw %K = 100*(C - LL14)/(HH14 - LL14) over the trailing 14
  bars incl. current; if HH == LL the previous raw value is carried (50 if
  none yet). Smoothed %K = SMA-3 of raw. Raw defined from bar 13,
  smoothed from bar 15.
- Supertrend: basic bands = (H+L)/2 +/- 3*ATR10; final bands follow the
  standard ratchet (upper only moves down unless price closed above it,
  lower only moves up unless price closed below it); direction flips when
  close crosses the PREVIOUS bar's opposite final band. Initialised at the
  first ATR bar (bar 9): final bands = basic bands, direction = up if
  close >= (H+L)/2 else down.
- ADX-14: Wilder. +DM/-DM/TR smoothed with the Wilder sum method (first
  smoothed value = sum of bars 1..14, then s = s - s/14 + new). DX from
  bar 14; first ADX at bar 27 = mean(DX[14..27]); then ADX =
  (ADX_prev*13 + DX)/14.
- VWAP-24: defined from bar 23; if a window's total volume were 0 the
  value would be None (does not occur in this data — every bar has
  volume).
"""
import csv, json, os, sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtest as bt

NAMES = ["stochastic", "supertrend", "keltner", "adx_trend", "vwap_rev", "vol_breakout"]
for _n in NAMES:
    bt.STRATEGY_RULES[_n] = {"stop_pct": 0.08}

_orig_build_indicators = bt.build_indicators
_orig_signal_entry = bt.signal_entry
_orig_signal_exit = bt.signal_exit


# ---------------- extra indicators ----------------

def atr_series(highs, lows, closes, n=10):
    N = len(closes)
    tr = [None] * N
    for i in range(N):
        if i == 0:
            tr[i] = highs[i] - lows[i]
        else:
            tr[i] = max(highs[i] - lows[i],
                        abs(highs[i] - closes[i - 1]),
                        abs(lows[i] - closes[i - 1]))
    out = [None] * N
    if N > n:
        a = sum(tr[:n]) / n
        out[n - 1] = a
        for i in range(n, N):
            a = (a * (n - 1) + tr[i]) / n
            out[i] = a
    return out


def stoch_k_series(highs, lows, closes, n=14, smooth=3):
    N = len(closes)
    raw = [None] * N
    last = 50.0
    for i in range(n - 1, N):
        hh = max(highs[i - n + 1: i + 1])
        ll = min(lows[i - n + 1: i + 1])
        if hh > ll:
            last = 100.0 * (closes[i] - ll) / (hh - ll)
        raw[i] = last
    out = [None] * N
    for i in range(n - 1 + smooth - 1, N):
        w = raw[i - smooth + 1: i + 1]
        if all(x is not None for x in w):
            out[i] = sum(w) / smooth
    return out


def supertrend_dir_series(highs, lows, closes, atr, mult=3.0):
    N = len(closes)
    d = [None] * N
    fu = fl = None
    cur = None
    for i in range(N):
        if atr[i] is None:
            continue
        hl2 = (highs[i] + lows[i]) / 2
        bu = hl2 + mult * atr[i]
        bl = hl2 - mult * atr[i]
        if cur is None:
            fu, fl = bu, bl
            cur = 1 if closes[i] >= hl2 else -1
        else:
            pfu, pfl, pcur = fu, fl, cur
            fu = bu if (bu < pfu or closes[i - 1] > pfu) else pfu
            fl = bl if (bl > pfl or closes[i - 1] < pfl) else pfl
            if closes[i] > pfu:
                cur = 1
            elif closes[i] < pfl:
                cur = -1
            else:
                cur = pcur
        d[i] = cur
    return d


def adx_series(highs, lows, closes, n=14):
    N = len(closes)
    tr = [0.0] * N
    pdm = [0.0] * N
    mdm = [0.0] * N
    for i in range(1, N):
        up = highs[i] - highs[i - 1]
        dn = lows[i - 1] - lows[i]
        pdm[i] = up if (up > dn and up > 0) else 0.0
        mdm[i] = dn if (dn > up and dn > 0) else 0.0
        tr[i] = max(highs[i] - lows[i],
                    abs(highs[i] - closes[i - 1]),
                    abs(lows[i] - closes[i - 1]))
    dx = [None] * N
    if N > 2 * n:
        s_tr = sum(tr[1:n + 1])
        s_p = sum(pdm[1:n + 1])
        s_m = sum(mdm[1:n + 1])
        for i in range(n, N):
            if i > n:
                s_tr = s_tr - s_tr / n + tr[i]
                s_p = s_p - s_p / n + pdm[i]
                s_m = s_m - s_m / n + mdm[i]
            if s_tr > 0:
                pdi = 100.0 * s_p / s_tr
                mdi = 100.0 * s_m / s_tr
                dx[i] = 100.0 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) > 0 else 0.0
    out = [None] * N
    if N > 2 * n:
        first = 2 * n - 1  # bar 27 for n=14: mean of DX[14..27]
        vals = dx[n:first + 1]
        if all(v is not None for v in vals):
            a = sum(vals) / len(vals)
            out[first] = a
            for i in range(first + 1, N):
                if dx[i] is not None:
                    a = (a * (n - 1) + dx[i]) / n
                    out[i] = a
    return out


def vwap_series(bars, n=24):
    N = len(bars)
    out = [None] * N
    for i in range(n - 1, N):
        num = 0.0
        den = 0.0
        for b in bars[i - n + 1: i + 1]:
            tp = (b[2] + b[3] + b[4]) / 3
            num += tp * b[5]
            den += b[5]
        out[i] = num / den if den > 0 else None
    return out


def vwap_reclaim_series(closes, vwap):
    """Armed by a close <= 0.98*VWAP; fires on the first later bar that
    closes back above VWAP after having closed at/below it. Re-arming
    requires a fresh dip."""
    N = len(closes)
    out = [False] * N
    armed = False
    for i in range(N):
        v = vwap[i]
        if v is None:
            continue
        if (armed and closes[i] > v and i > 0 and vwap[i - 1] is not None
                and closes[i - 1] <= vwap[i - 1]):
            out[i] = True
            armed = False
        if closes[i] <= 0.98 * v:
            armed = True
    return out


def build_indicators(bars):
    ind = _orig_build_indicators(bars)
    closes = [b[4] for b in bars]
    highs = [b[2] for b in bars]
    lows = [b[3] for b in bars]
    atr10 = atr_series(highs, lows, closes, 10)
    ind["atr10"] = atr10
    ind["stoch_k"] = stoch_k_series(highs, lows, closes)
    ind["st_dir"] = supertrend_dir_series(highs, lows, closes, atr10)
    ind["kelt_up"] = [None if ind["ema20"][i] is None or atr10[i] is None
                      else ind["ema20"][i] + 2 * atr10[i] for i in range(len(bars))]
    ind["adx"] = adx_series(highs, lows, closes)
    vw = vwap_series(bars)
    ind["vwap24"] = vw
    ind["vwap_reclaim"] = vwap_reclaim_series(closes, vw)
    return ind


# ---------------- signals ----------------

def signal_entry(name, ind, i, bars):
    c = bars[i][4]
    if name == "stochastic":
        k = ind["stoch_k"]
        if i < 1 or k[i] is None or k[i - 1] is None:
            return False
        return k[i - 1] < 20 and k[i] > 20
    if name == "supertrend":
        d = ind["st_dir"]
        if i < 1 or d[i] is None or d[i - 1] is None:
            return False
        return d[i - 1] == -1 and d[i] == 1
    if name == "keltner":
        up = ind["kelt_up"]
        if i < 1 or up[i] is None or up[i - 1] is None:
            return False
        return bars[i - 1][4] <= up[i - 1] and c > up[i]
    if name == "adx_trend":
        a, e = ind["adx"][i], ind["ema50"][i]
        return a is not None and a > 25 and c > e
    if name == "vwap_rev":
        return bool(ind["vwap_reclaim"][i])
    if name == "vol_breakout":
        if i < 21:
            return False
        hh = ind["don_high20"][i - 1]
        va = ind["vol_sma20"][i]
        return hh is not None and va is not None and c > hh and bars[i][5] > 1.5 * va
    return _orig_signal_entry(name, ind, i, bars)


def signal_exit(name, ind, i, pos, bars):
    c = bars[i][4]
    if name == "stochastic":
        k = ind["stoch_k"]
        if i < 1 or k[i] is None or k[i - 1] is None:
            return None
        return "STOCH_EXIT" if (k[i - 1] > 80 and k[i] < 80) else None
    if name == "supertrend":
        d = ind["st_dir"]
        if i < 1 or d[i] is None or d[i - 1] is None:
            return None
        return "SUPERTREND_FLIP_DOWN" if (d[i - 1] == 1 and d[i] == -1) else None
    if name == "keltner":
        e = ind["ema20"][i]
        return "KELTNER_BASIS_EXIT" if (e is not None and c < e) else None
    if name == "adx_trend":
        a, e = ind["adx"][i], ind["ema50"][i]
        if a is not None and (a < 20 or c < e):
            return "ADX_EXIT"
        return None
    if name == "vwap_rev":
        v = ind["vwap24"][i]
        if v is not None and c >= 1.02 * v:
            return "VWAP_TARGET"
        if c <= 0.95 * pos["entry_raw"]:
            return "VWAP_STOP5"
        return None
    if name == "vol_breakout":
        if i >= 11:
            ll = ind["don_low10"][i - 1]
            if ll is not None and c < ll:
                return "VOL_BREAK_EXIT"
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
        for name in NAMES:
            all_trades.extend(bt.run_strategy(name, symbol, cls, bars, ind))

    res = bt.RES_DIR
    with open(os.path.join(res, "extra_trades.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["strategy", "symbol", "entry_time", "exit_time",
                                          "entry_price", "exit_price", "pnl_usd", "return_pct",
                                          "exit_reason", "bars_held"])
        w.writeheader(); w.writerows(all_trades)

    summary = {}
    by_symbol = {}
    reasons = {}
    for name in NAMES:
        tr = [t for t in all_trades if t["strategy"] == name]
        summary[name] = bt.metrics(tr)
        r = {}
        for t in tr:
            r[t["exit_reason"]] = r.get(t["exit_reason"], 0) + 1
        reasons[name] = r
        for symbol, _, _ in bt.UNIVERSE:
            by_symbol[f"{name}|{symbol}"] = bt.metrics([t for t in tr if t["symbol"] == symbol])

    with open(os.path.join(res, "extra_summary.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["strategy"] + list(next(iter(summary.values())).keys()))
        for k, v in summary.items():
            w.writerow([k] + list(v.values()))
    with open(os.path.join(res, "extra_summary_by_symbol.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["strategy|symbol"] + list(next(iter(by_symbol.values())).keys()))
        for k, v in by_symbol.items():
            w.writerow([k] + list(v.values()))
    with open(os.path.join(res, "extra_summary.json"), "w") as f:
        json.dump({"data": data_report, "summary": summary, "by_symbol": by_symbol,
                   "exit_reasons": reasons}, f, indent=2)

    print(json.dumps({"summary": summary, "exit_reasons": reasons}, indent=2))
    print(json.dumps(by_symbol, indent=2))


if __name__ == "__main__":
    main()
