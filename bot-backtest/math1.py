#!/usr/bin/env python3
"""
Math strategy slate — strategies 20-25 (quantitative/statistical family).

Same harness, cached data, cost model and fill conventions as backtest.py
(imported, not edited). Rules locked from the published literature BEFORE
the run (see ~/workspace/nova-trade-build/MATH-STRATEGY-RESEARCH.md);
zero tuning after seeing results.

 20 ou_halflife   Ornstein-Uhlenbeck / AR(1) fit mean reversion
                  (QuantArenas MR-04 published ruleset, transferred to
                  hourly bars): trailing 60-close AR(1) fit excluding the
                  decision close; valid iff 0<b<1 and half-life in [1,30]
                  bars; enter z < -1.5; exit z > -0.5, fit invalid, or
                  half-life out of range. z uses the fitted equilibrium
                  volatility sigma_eq = sigma_eps / sqrt(1-b^2).
 21 kalman_mr     Kalman local-level dynamic mean reversion (Chan):
                  state = dynamic mean of log price, variance ratio
                  delta = 1e-4; gap = log price - filtered mean, z-scored
                  on a rolling 60-bar window; enter z < -2; exit z > -0.5.
 22 hurst_rsi     The harness's own rsi_reversion rules, unchanged
                  (RSI<30 in, RSI>=55 out, TP +8%, stop -6%), gated by a
                  rolling R/S Hurst exponent (100-bar window, lags
                  10/25/50/100): entries only when H < 0.5.
 23 pairs_longonly  Engle-Granger style rolling pairs, LONG-ONLY variant:
                  5 pre-registered pairs; rolling 168-bar OLS hedge ratio
                  on log prices; spread z over the same window; enter the
                  lagging (cheap) leg when |z| > 2; exit that leg when
                  the spread reverts past -/+0.5; -8% intraday stop on the
                  held leg. Custom runner (two legs), harness costs/fills.
 24 grid_geo      Geometric grid (Edgoro published settings): +-10% range,
                  20 geometric levels (ratio (1.1/0.9)^(1/20) ~ 1.01),
                  $100 lots, max 10 open lots per token, fresh grid at
                  each calendar-month boundary (inventory liquidated at
                  market). Conservative intrabar convention: OHLC path
                  o->extreme->extreme->c; lots bought in a bar cannot be
                  sold in the same bar. Full accounting: headline numbers
                  include end-of-window / reset liquidation of inventory,
                  not just completed round trips. Custom runner.
 25 volgate_rsi   rsi_reversion rules unchanged, gated by realized vol:
                  entries only when trailing 168-bar hourly log-return
                  stdev is at or below its own trailing 720-bar median
                  (discrete long-only analog of volatility targeting —
                  cut exposure into vol spikes, Barroso & Santa-Clara /
                  Moreira & Muir mechanism).

Uniform safety: strategies 20/21/23 carry the -8% hard stop; 22/25 keep
rsi_reversion's own +8% TP / -6% stop so the ONLY difference vs the #2
baseline is the gate. Grid has no stop by design (it IS the position).

Diagnostics collected for the report (do not affect any trade):
 - per-token OU half-life distribution and valid-fit share of bars
 - per-token Hurst mean and share of bars with H < 0.5 (tests the published
   claim that R/S Hurst on crypto hourly almost never reads < 0.5)
 - per-pair full-window Engle-Granger ADF(4) t-stat on OLS residuals and
   spread half-life (full-window = lookahead, diagnostics ONLY)
"""
import csv, json, math, os, sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtest as bt

MATH_RULES = {
    "ou_halflife": {"stop_pct": 0.08},
    "kalman_mr":   {"stop_pct": 0.08},
    "hurst_rsi":   {"tp_pct": 0.08, "stop_pct": 0.06},
    "volgate_rsi": {"tp_pct": 0.08, "stop_pct": 0.06},
}
bt.STRATEGY_RULES.update(MATH_RULES)

_orig_entry = bt.signal_entry
_orig_exit = bt.signal_exit

DIAG = {"ou": {}, "hurst": {}, "pairs": {}}

# ---------------- shared helpers ----------------

def _mean(xs):
    return sum(xs) / len(xs)


def _pstd(xs, m=None):
    if m is None:
        m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / len(xs))


def _ols(xs, ys):
    """Return (alpha, beta) of ys ~ alpha + beta*xs."""
    mx, my = _mean(xs), _mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None, None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    beta = sxy / sxx
    return my - beta * mx, beta

# ---------------- 20: OU / half-life ----------------

OU_LOOKBACK = 60
OU_ENTRY_Z = -1.5
OU_EXIT_Z = -0.5
OU_HL_MIN, OU_HL_MAX = 1.0, 30.0


def ou_series(closes):
    """Per-bar (z, valid, half_life) from the trailing AR(1)/OU fit."""
    n = len(closes)
    z = [None] * n
    valid = [False] * n
    hl = [None] * n
    for i in range(OU_LOOKBACK, n):
        w = closes[i - OU_LOOKBACK: i]           # 60 closes, excl. decision close
        xs, ys = w[:-1], w[1:]                   # (x_{t-1}, x_t) pairs
        a, b = _ols(xs, ys)
        if a is None or not (0.0 < b < 1.0):
            continue
        theta = -math.log(b)
        half = math.log(2) / theta
        mu = a / (1.0 - b)
        resid = [y - (a + b * x) for x, y in zip(xs, ys)]
        sig_eq = _pstd(resid) / math.sqrt(1.0 - b * b)
        if sig_eq <= 0:
            continue
        hl[i] = half
        z[i] = (closes[i] - mu) / sig_eq
        valid[i] = OU_HL_MIN <= half <= OU_HL_MAX
    return z, valid, hl

# ---------------- 21: Kalman dynamic mean ----------------

KALMAN_DELTA = 1e-4      # Chan's variance-ratio default
KALMAN_Z_WIN = 60


def kalman_series(closes):
    """Local-level Kalman filter on log price; returns z of the gap."""
    n = len(closes)
    x = [math.log(c) for c in closes]
    rets = [0.0] + [x[i] - x[i - 1] for i in range(1, n)]
    gap = [None] * n
    mu = x[0]
    P = 1.0
    for i in range(n):
        # observation variance from trailing 168-bar return variance (causal)
        lo = max(0, i - 167)
        w = rets[lo: i + 1] or [0.0]
        ve = max(sum(v * v for v in w) / len(w), 1e-12)
        vw = KALMAN_DELTA * ve
        if i > 0:
            p_pred = P + vw
            K = p_pred / (p_pred + ve)
            mu = mu + K * (x[i] - mu)
            P = (1.0 - K) * p_pred
        gap[i] = x[i] - mu
    z = [None] * n
    for i in range(KALMAN_Z_WIN - 1, n):
        w = gap[i - KALMAN_Z_WIN + 1: i + 1]
        m, s = _mean(w), _pstd(w)
        if s > 0:
            z[i] = (gap[i] - m) / s
    return z

# ---------------- 22: Hurst (R/S) ----------------

HURST_WIN = 100
HURST_LAGS = (10, 25, 50, 100)


def _rs_hurst(rets):
    """Classic R/S Hurst over one window of returns, multi-lag regression."""
    pts = []
    n = len(rets)
    for lag in HURST_LAGS:
        if lag > n:
            continue
        rss = []
        for start in range(0, n - lag + 1, lag):
            seg = rets[start: start + lag]
            m = _mean(seg)
            cum, run = [], 0.0
            for v in seg:
                run += v - m
                cum.append(run)
            R = max(cum) - min(cum)
            S = _pstd(seg, m)
            if S > 0:
                rss.append(R / S)
        if rss:
            pts.append((math.log(lag), math.log(_mean(rss))))
    if len(pts) < 3:
        return None
    _, slope = _ols([p[0] for p in pts], [p[1] for p in pts])
    return slope


def hurst_series(closes):
    n = len(closes)
    rets = [0.0] + [math.log(closes[i] / closes[i - 1]) for i in range(1, n)]
    out = [None] * n
    for i in range(HURST_WIN, n):
        out[i] = _rs_hurst(rets[i - HURST_WIN + 1: i + 1])
    return out

# ---------------- 25: volatility gate ----------------

VOL_WIN = 168
VOL_MED_WIN = 720


def volgate_series(closes):
    """True at bar i when realized vol <= its trailing median (entry gate)."""
    n = len(closes)
    rets = [0.0] + [math.log(closes[i] / closes[i - 1]) for i in range(1, n)]
    vol = [None] * n
    for i in range(VOL_WIN - 1, n):
        vol[i] = _pstd(rets[i - VOL_WIN + 1: i + 1])
    gate = [False] * n
    for i in range(n):
        if vol[i] is None:
            continue
        lo = max(0, i - VOL_MED_WIN + 1)
        hist = sorted(v for v in vol[lo: i + 1] if v is not None)
        if len(hist) < VOL_WIN:
            continue
        med = hist[len(hist) // 2] if len(hist) % 2 else 0.5 * (hist[len(hist) // 2 - 1] + hist[len(hist) // 2])
        gate[i] = vol[i] <= med
    return gate

# ---------------- signal wiring (20/21/22/25) ----------------

def math_entry(name, ind, i, bars):
    if name not in MATH_RULES:
        return _orig_entry(name, ind, i, bars)
    if name == "ou_halflife":
        z = ind["ou_z"][i]
        return bool(ind["ou_valid"][i]) and z is not None and z < OU_ENTRY_Z
    if name == "kalman_mr":
        z = ind["kalman_z"][i]
        return z is not None and z < -2.0
    if name == "hurst_rsi":
        h = ind["hurst"][i]
        if h is None or h >= 0.5:
            return False
        return _orig_entry("rsi_reversion", ind, i, bars)
    if name == "volgate_rsi":
        if not ind["vol_gate"][i]:
            return False
        return _orig_entry("rsi_reversion", ind, i, bars)
    return False


def math_exit(name, ind, i, pos, bars):
    if name not in MATH_RULES:
        return _orig_exit(name, ind, i, pos, bars)
    if name == "ou_halflife":
        z = ind["ou_z"][i]
        if not ind["ou_valid"][i] or z is None:
            return "OU_FIT_INVALID"
        if z > OU_EXIT_Z:
            return "OU_REVERTED"
        return None
    if name == "kalman_mr":
        z = ind["kalman_z"][i]
        if z is not None and z > -0.5:
            return "KALMAN_REVERTED"
        return None
    if name in ("hurst_rsi", "volgate_rsi"):
        return _orig_exit("rsi_reversion", ind, i, pos, bars)
    return None


bt.signal_entry = math_entry
bt.signal_exit = math_exit


def math_indicators(bars, symbol):
    ind = bt.build_indicators(bars)
    closes = [b[4] for b in bars]
    z, valid, hl = ou_series(closes)
    ind["ou_z"], ind["ou_valid"] = z, valid
    ind["kalman_z"] = kalman_series(closes)
    ind["hurst"] = hurst_series(closes)
    ind["vol_gate"] = volgate_series(closes)
    hs = [h for h in ind["hurst"] if h is not None]
    DIAG["hurst"][symbol] = {
        "bars_with_h": len(hs),
        "mean_h": round(_mean(hs), 3) if hs else None,
        "share_h_below_0.5": round(sum(1 for h in hs if h < 0.5) / len(hs), 3) if hs else None,
    }
    hls = [v for v in hl if v is not None]
    DIAG["ou"][symbol] = {
        "bars_with_valid_slope_fit": len(hls),
        "median_half_life_bars": round(sorted(hls)[len(hls) // 2], 2) if hls else None,
        "share_bars_entry_eligible_fit": round(sum(1 for v in valid if v) / len(valid), 3),
    }
    return ind

# ---------------- 23: pairs (custom runner) ----------------

PAIRS = [("SOL", "JUP"), ("JUP", "ORCA"), ("BONK", "WIF"),
         ("WIF", "POPCAT"), ("POPCAT", "PENGU")]
PAIR_WIN = 168
PAIR_ENTRY_Z = 2.0
PAIR_EXIT_Z = 0.5
PAIR_STOP = 0.08


def _adf_tstat(resid, lags=4):
    """ADF t-stat (constant, no trend) on a residual series. Diagnostics."""
    y = resid
    n = len(y)
    if n < 100:
        return None
    rows, dep = [], []
    for t in range(lags + 1, n):
        rows.append([1.0, y[t - 1]] + [y[t - k] - y[t - k - 1] for k in range(1, lags + 1)])
        dep.append(y[t] - y[t - 1])
    # OLS via normal equations (small system)
    k = len(rows[0])
    xtx = [[sum(r[a] * r[b] for r in rows) for b in range(k)] for a in range(k)]
    xty = [sum(r[a] * d for r, d in zip(rows, dep)) for a in range(k)]
    # Gaussian elimination
    M = [row[:] + [xty[i]] for i, row in enumerate(xtx)]
    for col in range(k):
        piv = max(range(col, k), key=lambda rr: abs(M[rr][col]))
        if abs(M[piv][col]) < 1e-14:
            return None
        M[col], M[piv] = M[piv], M[col]
        for rr in range(k):
            if rr != col:
                f = M[rr][col] / M[col][col]
                M[rr] = [v - f * u for v, u in zip(M[rr], M[col])]
    beta = [M[i][k] / M[i][i] for i in range(k)]
    fitted = [sum(r[j] * beta[j] for j in range(k)) for r in rows]
    dof = len(dep) - k
    s2 = sum((d - f) ** 2 for d, f in zip(dep, fitted)) / dof
    # standard error of beta[1] from s2 * (X'X)^-1[1][1]; invert X'X via Gauss-Jordan
    inv = [[1.0 if i == j else 0.0 for j in range(k)] for i in range(k)]
    A = [row[:] for row in xtx]
    for col in range(k):
        piv = max(range(col, k), key=lambda rr: abs(A[rr][col]))
        A[col], A[piv] = A[piv], A[col]
        inv[col], inv[piv] = inv[piv], inv[col]
        pv = A[col][col]
        A[col] = [v / pv for v in A[col]]
        inv[col] = [v / pv for v in inv[col]]
        for rr in range(k):
            if rr != col and A[rr][col] != 0.0:
                f = A[rr][col]
                A[rr] = [v - f * u for v, u in zip(A[rr], A[col])]
                inv[rr] = [v - f * u for v, u in zip(inv[rr], inv[col])]
    se = math.sqrt(s2 * inv[1][1])
    return beta[1] / se if se > 0 else None


def run_pairs(data):
    """data: {symbol: (cls, bars)}. Returns trade records."""
    trades = []
    cls_of = {s: data[s][0] for s in data}
    for A, B in PAIRS:
        if A not in data or B not in data:
            continue
        barsA = {b[0]: b for b in data[A][1]}
        barsB = {b[0]: b for b in data[B][1]}
        ts = sorted(set(barsA) & set(barsB))
        if len(ts) < PAIR_WIN + 50:
            continue
        la = [math.log(barsA[t][4]) for t in ts]
        lb = [math.log(barsB[t][4]) for t in ts]
        # diagnostics: full-window EG (lookahead, report only)
        alpha_f, beta_f = _ols(lb, la)
        resid = [la[i] - (alpha_f + beta_f * lb[i]) for i in range(len(ts))]
        adf = _adf_tstat(resid)
        diffs = [resid[i] - resid[i - 1] for i in range(1, len(resid))]
        _, slope = _ols(resid[:-1], diffs)
        hl_full = round(-math.log(2) / slope, 1) if slope and slope < 0 else None
        DIAG["pairs"][f"{A}/{B}"] = {
            "aligned_bars": len(ts), "full_window_beta": round(beta_f, 3),
            "adf_tstat_resid": round(adf, 2) if adf is not None else None,
            "spread_half_life_bars_full_window": hl_full,
        }
        pos = None        # dict(leg, entry_idx, entry_raw, entry_eff)
        pending = None    # ("enter", leg) or ("exit", reason)
        for i in range(len(ts)):
            t = ts[i]
            # fills at this bar's open
            if pending and pending[0] == "exit" and pos is not None:
                leg_bars = barsA if pos["leg"] == A else barsB
                raw_exit = leg_bars[t][1]
                eff_exit = raw_exit * (1 - pos["cost"])
                pnl = bt.NOTIONAL * (eff_exit / pos["entry_eff"] - 1)
                trades.append({
                    "strategy": "pairs_longonly", "symbol": pos["leg"],
                    "entry_time": datetime.fromtimestamp(ts[pos["entry_idx"]], timezone.utc).isoformat(),
                    "exit_time": datetime.fromtimestamp(t, timezone.utc).isoformat(),
                    "entry_price": pos["entry_raw"], "exit_price": raw_exit,
                    "pnl_usd": round(pnl, 4), "return_pct": round(100 * pnl / bt.NOTIONAL, 3),
                    "exit_reason": pending[1], "bars_held": i - pos["entry_idx"],
                })
                pos, pending = None, None
            if pending and pending[0] == "enter" and pos is None and i > 0:
                leg = pending[1]
                leg_bars = barsA if leg == A else barsB
                cost = bt.SLIP[cls_of[leg]] + bt.SWAP_FEE
                o = leg_bars[t][1]
                pos = {"leg": leg, "entry_idx": i, "entry_raw": o,
                       "entry_eff": o * (1 + cost), "cost": cost}
                pending = None
            # intraday -8% stop on held leg
            if pos is not None:
                leg_bars = barsA if pos["leg"] == A else barsB
                _, o, h, l, c, v = leg_bars[t]
                sp = pos["entry_raw"] * (1 - PAIR_STOP)
                if l <= sp:
                    raw_exit = min(o, sp) if o < sp else sp
                    eff_exit = raw_exit * (1 - pos["cost"])
                    pnl = bt.NOTIONAL * (eff_exit / pos["entry_eff"] - 1)
                    trades.append({
                        "strategy": "pairs_longonly", "symbol": pos["leg"],
                        "entry_time": datetime.fromtimestamp(ts[pos["entry_idx"]], timezone.utc).isoformat(),
                        "exit_time": datetime.fromtimestamp(t, timezone.utc).isoformat(),
                        "entry_price": pos["entry_raw"], "exit_price": raw_exit,
                        "pnl_usd": round(pnl, 4), "return_pct": round(100 * pnl / bt.NOTIONAL, 3),
                        "exit_reason": "STOP", "bars_held": i - pos["entry_idx"],
                    })
                    pos = None
            # signals on closed bar i (rolling 168-bar OLS, causal)
            if i >= PAIR_WIN - 1 and i < len(ts) - 1:
                w_lb, w_la = lb[i - PAIR_WIN + 1: i + 1], la[i - PAIR_WIN + 1: i + 1]
                alpha, beta = _ols(w_lb, w_la)
                if alpha is not None:
                    spread = [w_la[k] - (alpha + beta * w_lb[k]) for k in range(PAIR_WIN)]
                    m, s = _mean(spread), _pstd(spread)
                    if s > 0:
                        z = (spread[-1] - m) / s
                        if pos is not None and pending is None:
                            if pos["leg"] == A and z >= -PAIR_EXIT_Z:
                                pending = ("exit", "PAIR_REVERTED")
                            elif pos["leg"] == B and z <= PAIR_EXIT_Z:
                                pending = ("exit", "PAIR_REVERTED")
                        elif pos is None and pending is None:
                            if z < -PAIR_ENTRY_Z:
                                pending = ("enter", A)
                            elif z > PAIR_ENTRY_Z:
                                pending = ("enter", B)
        if pos is not None:
            leg_bars = barsA if pos["leg"] == A else barsB
            raw_exit = leg_bars[ts[-1]][4]
            eff_exit = raw_exit * (1 - pos["cost"])
            pnl = bt.NOTIONAL * (eff_exit / pos["entry_eff"] - 1)
            trades.append({
                "strategy": "pairs_longonly", "symbol": pos["leg"],
                "entry_time": datetime.fromtimestamp(ts[pos["entry_idx"]], timezone.utc).isoformat(),
                "exit_time": datetime.fromtimestamp(ts[-1], timezone.utc).isoformat(),
                "entry_price": pos["entry_raw"], "exit_price": raw_exit,
                "pnl_usd": round(pnl, 4), "return_pct": round(100 * pnl / bt.NOTIONAL, 3),
                "exit_reason": "EOD", "bars_held": len(ts) - 1 - pos["entry_idx"],
            })
    return trades

# ---------------- 24: grid (custom runner) ----------------

GRID_LEVELS = 20
GRID_RATIO = (1.1 / 0.9) ** (1.0 / GRID_LEVELS)
GRID_MAX_LOTS = 10


def run_grid(symbol, cls, bars):
    cost = bt.SLIP[cls] + bt.SWAP_FEE
    trades = []
    lots = []            # dict(level_k, entry_idx, entry_price)
    anchor = None
    anchor_month = None
    levels = []

    def close_lot(lot, idx, raw_exit, reason):
        eff_exit = raw_exit * (1 - cost)
        eff_entry = lot["entry_price"] * (1 + cost)
        pnl = bt.NOTIONAL * (eff_exit / eff_entry - 1)
        trades.append({
            "strategy": "grid_geo", "symbol": symbol,
            "entry_time": datetime.fromtimestamp(bars[lot["entry_idx"]][0], timezone.utc).isoformat(),
            "exit_time": datetime.fromtimestamp(bars[idx][0], timezone.utc).isoformat(),
            "entry_price": lot["entry_price"], "exit_price": raw_exit,
            "pnl_usd": round(pnl, 4), "return_pct": round(100 * pnl / bt.NOTIONAL, 3),
            "exit_reason": reason, "bars_held": idx - lot["entry_idx"],
        })

    for idx, (ts_, o, h, l, c, v) in enumerate(bars):
        dt = datetime.fromtimestamp(ts_, timezone.utc)
        month = (dt.year, dt.month)
        if anchor is None or month != anchor_month:
            # fresh grid: liquidate any inventory at this bar's open first
            for lot in lots:
                close_lot(lot, idx, o, "GRID_RESET")
            lots = []
            anchor, anchor_month = o, month
            levels = [anchor * GRID_RATIO ** k for k in range(-GRID_LEVELS // 2, GRID_LEVELS // 2 + 1)]
        # OHLC path segments (conservative convention)
        if c >= o:
            path = [(o, h), (h, l), (l, c)]
        else:
            path = [(o, l), (l, h), (h, c)]
        for p0, p1 in path:
            if p1 < p0:  # downward segment: buy fills at crossed levels
                crossed = sorted([lv for lv in levels if p1 <= lv < p0], reverse=True)
                for lv in crossed:
                    k = levels.index(lv)
                    if k >= GRID_LEVELS // 2:      # top level has no sell target
                        continue
                    if any(lot["level_k"] == k for lot in lots):
                        continue
                    if len(lots) >= GRID_MAX_LOTS:
                        continue
                    lots.append({"level_k": k, "entry_idx": idx, "entry_price": lv})
            elif p1 > p0:  # upward segment: sell lots whose target is crossed
                crossed = sorted([lv for lv in levels if p0 < lv <= p1])
                for lv in crossed:
                    for lot in list(lots):
                        if lot["entry_idx"] < idx and levels[lot["level_k"] + 1] == lv:
                            close_lot(lot, idx, lv, "GRID_TP")
                            lots.remove(lot)
                            break
    for lot in lots:
        close_lot(lot, len(bars) - 1, bars[-1][4], "GRID_EOD")
    return trades

# ---------------- metrics ----------------

def extra_metrics(trades):
    m = bt.metrics(trades)
    m["net_pct_of_1000_base"] = round(100 * m["net_pnl_usd"] / bt.REF_BANKROLL, 2)
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


def verdict(m):
    checks = {
        "win_rate>=60": m["win_rate_pct"] >= 60,
        "trades>=30": m["trades"] >= 30,
        "net>=+10%_of_1000": m["net_pct_of_1000_base"] >= 10,
        "concentration<=50": m["top_trade_share_of_gross_profit_pct"] <= 50,
        "drawdown<=20%": m["max_drawdown_pct_of_1000"] <= 20,
    }
    checks["MEETS_STANDARD"] = all(checks.values())
    return checks

# ---------------- main ----------------

def main():
    all_trades = []
    data = {}
    data_report = {}
    for symbol, mint, cls in bt.UNIVERSE:
        bars, how = bt.load_or_fetch(symbol, mint)
        if not bars:
            data_report[symbol] = {"status": how, "bars": 0}
            print(f"{symbol}: NO DATA ({how})", flush=True)
            continue
        data[symbol] = (cls, bars)
        data_report[symbol] = {
            "status": how, "bars": len(bars),
            "first": datetime.fromtimestamp(bars[0][0], timezone.utc).isoformat(),
            "last": datetime.fromtimestamp(bars[-1][0], timezone.utc).isoformat(),
        }
        print(f"{symbol}: {len(bars)} bars ({how})", flush=True)
        ind = math_indicators(bars, symbol)
        for name in MATH_RULES:
            all_trades.extend(bt.run_strategy(name, symbol, cls, bars, ind))
        all_trades.extend(run_grid(symbol, cls, bars))
    all_trades.extend(run_pairs(data))

    names = list(MATH_RULES) + ["pairs_longonly", "grid_geo"]
    res = bt.RES_DIR
    with open(os.path.join(res, "math_trades.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["strategy", "symbol", "entry_time", "exit_time",
                                          "entry_price", "exit_price", "pnl_usd", "return_pct",
                                          "exit_reason", "bars_held"])
        w.writeheader(); w.writerows(all_trades)

    summary, by_symbol, verdicts = {}, {}, {}
    for name in names:
        tr = [t for t in all_trades if t["strategy"] == name]
        summary[name] = extra_metrics(tr)
        verdicts[name] = verdict(summary[name])
        for symbol, _, _ in bt.UNIVERSE:
            by_symbol[f"{name}|{symbol}"] = extra_metrics([t for t in tr if t["symbol"] == symbol])

    with open(os.path.join(res, "math_summary.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["strategy"] + list(next(iter(summary.values())).keys()))
        for k, v in summary.items():
            w.writerow([k] + list(v.values()))
    with open(os.path.join(res, "math_summary.json"), "w") as f:
        json.dump({"data": data_report, "summary": summary, "by_symbol": by_symbol,
                   "verdicts_vs_nova_standard": verdicts, "diagnostics": DIAG}, f, indent=2)

    print(json.dumps({"summary": summary, "verdicts": verdicts}, indent=2))
    print(json.dumps(DIAG, indent=2))


if __name__ == "__main__":
    main()
