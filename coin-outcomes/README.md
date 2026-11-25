# Nova Coin Outcome Tracker

The dataset behind outcome predictions in the Vorix Scanners section —
Paul's design ("the match"): **find coins by what they're doing → grade
them → use the data we collect to predict the outcome.** Predictions need
history: every scanned coin's behavior snapshot, plus what its price
actually did afterwards. This tracker builds that history from day one.

## What runs

| File | Job |
|---|---|
| `track.py` | Registers new coins from the live scanner feeds and updates the price outcomes of everything still open. |
| `cohort.py` | Stats over the tracked population: "coins that behaved like this did X" — the numbers a prediction displays. |
| `data/outcomes.db` | SQLite store (created on first run). |

Both scripts are **read-only on the scanner feeds** and write only inside
this directory. Safe to run `track.py` every 10–15 minutes next to the
existing scanner cron; it is idempotent — re-runs never duplicate a
registration or lose a record.

```bash
python3 track.py            # register + update
python3 track.py --status   # dataset summary
python3 cohort.py --age fresh --mcap small --score high
python3 cohort.py --buyratio buy-heavy --json
```

## Sources (free / keyless)

- **RawScan feed** — `https://nova-rawscan.web.app/data/signals.json` (thin schema, ~21 fields)
- **AlphaPulse feed** — `https://nova-alphapulse.web.app/data/latest.json` (rich schema, ~150 fields on its best-covered tokens)
- **Price updates** — DexScreener token endpoint, best-liquidity pair per mint. The endpoint silently caps its response at ~30 pairs (one many-pair token can crowd out a batch, and EVM addresses come back checksummed), so the tracker batches by 5 with case-insensitive matching and individually retries every mint a batch didn't cover.

When a coin appears in both feeds, AlphaPulse's richer record fills the
snapshot and the RawScan `score` is preserved alongside AlphaPulse's
`potential` grade.

## Record schema (`coins` table, one row per mint)

**Identity / first sight**
`mint` (PK), `ticker`, `name`, `chain`, `first_seen_ts`, `first_seen_feeds`,
`entry_price_usd` (price at first sight — every outcome is measured from here),
`snapshot_json` (the full raw feed record(s), untouched).

**Behavior snapshot (normalized, used for cohort banding)**
`score`, `age_hours`, `mcap_usd`, `liquidity_usd`, `vol_h1_usd`,
`vol_h6_usd`, `vol_24h_usd`, `buy_ratio_h1`, `buy_ratio_h24`, `txns_h1`,
`chg_m5`, `chg_h1`, `chg_h6`, `chg_h24`, plus safety when the feed carries
it: `holder_count`, `top10_pct`, `bundle_score`, `mint_renounced`,
`freeze_renounced`, `honeypot`. Any of these can be NULL — the feeds don't
cover every coin equally (see the gap list in the build report).

**Outcome tracking**
`observations` (append-only price log: mint, ts, price, mcap, source) and
rolled-up state on the coin row: `last_price_usd`, `min_price_usd`,
`peak_price_usd` + `peak_ts`, `max_dd_from_peak_pct`, and checkpoints
`cp_1h / cp_6h / cp_24h / cp_7d` (price + **actual observation timestamp** —
the first observation at or after the target time; a checkpoint is never
back-filled or invented).

**Status lifecycle**
`open` → `complete` (the +7d checkpoint landed, or 7d + 12h grace passed)
or `dead` (no DexScreener pair on 3 consecutive runs and at least 1h old —
recorded with its last known state, because a coin vanishing is an outcome
too).

## Cohort bands

| Dimension | Bands |
|---|---|
| age | newborn <1h · fresh 1–6h · day 6–24h · established 1–7d · old >7d |
| mcap | micro <$50k · small $50–250k · mid $250k–1M · large $1–10M · mega >$10M |
| liquidity | thin <$10k · low $10–50k · ok $50–150k · deep >$150k |
| score | low <60 · mid 60–79 · high 80–89 · top 90+ |
| buy ratio (1h) | sell-heavy <0.45 · balanced 0.45–0.55 · buy-lean 0.55–0.65 · buy-heavy >0.65 |

A coin counts toward outcome stats once it has a +24h checkpoint or is
closed. `cohort.py` always prints **n** (and `n_seen` for the profile), and
labels anything under n=30 as a small sample.

## How Vorix Scanners will consume this

For a live pick, the Scanners section takes the coin's behavior profile,
bands it with the table above, and calls the cohort stats for that profile.
The prediction line shows the basis, not just a verdict, e.g.:

> Coins that looked like this (fresh, small-cap, buy-heavy, score 80+):
> **n=47** · 38% ran +50% or more · median peak +64% in ~5h · median worst
> drop from peak −41%.

Rules carried from Nova's honesty standard:

- **n is always shown.** No n, no prediction.
- **n < 30 → the prediction is not displayed** as a stat (the coin still
  shows its grade and live data). Seeded published base rates may be shown
  separately, labeled as published research, never mixed into Nova's own n.
- Stats are probabilities over a cohort, never a promise about one coin.
