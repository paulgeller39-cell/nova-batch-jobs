# QuantConnect Lean — Nova's second backtest engine (STAGED, not yet run)

Paul picked QuantConnect Lean off the master list (2026-11-24). Purpose:
an independent, event-driven backtest engine to cross-check Nova's own
`bot-backtest/` harness. **Backtests only** — no live trading, no broker
connections, no paid data, no QuantConnect account.

## Status (2026-11-24)

STAGED commit-ready in this repo. Nothing has run yet:

- The local agent VM **cannot** run Lean: no Docker (the engine only runs
  in the `quantconnect/lean` container), the current lean-cli (1.0.234,
  installed in the nova-tools venv) gates workspace creation behind a
  QuantConnect login (`lean init` prompts for user id + API token, and
  current docs build per-organization workspaces), and the VM has only
  ~1.4 GB free RAM. Local attempt was made and stopped there on purpose.
- The GitHub Actions route below is the intended home: runners have
  Docker and RAM, and the workflow needs no account and no secrets.
- The push to GitHub happens via the browser route, same as the first
  repo build. After the push, run the **Lean backtest** workflow manually
  (workflow_dispatch only — no schedule until it's proven).

## Layout

```
lean/
  engine-config.json          Launcher config for the Docker route:
                              backtesting environment, live-mode false,
                              algorithm at /Algorithm/main.py, data /Data,
                              results /Results.
  lean.json                   Placeholder for the OPTIONAL lean-cli route
                              (unused by the workflow; see below).
  NovaBtcEmaCross/
    main.py                   10/50 EMA cross, BTCUSD, Coinbase, daily,
                              long-only spot, cash account.
    config.json               lean-cli project config (CLI route only).
  results/                    Engine output (git-ignored).
.github/workflows/lean-backtest.yml
```

## How the workflow runs it

1. Checks out this repo.
2. Sparse-checks-out `Data/` from QuantConnect/Lean (the free sample data
   the repo ships, crypto included) into `lean-upstream/`.
3. Prints the `Data/crypto` file list — **the calibration record** (below).
4. Runs the official engine image with the mounts/flags from the Lean
   repo's own `run_docker.sh` pattern:
   `docker run --rm --mount .../engine-config.json:/Lean/Launcher/config.json
   -v NovaBtcEmaCross:/Algorithm:ro -v Data:/Data:ro -v results:/Results
   quantconnect/lean:latest --data-folder /Data
   --results-destination-folder /Results --config /Lean/Launcher/config.json`
5. Uploads `lean/results/` as the `lean-backtest-results` artifact.

## Calibration item (the one unvalidated piece)

The exact date/resolution coverage of BTCUSD in the repo's sample
`Data/crypto` is **not** verified — it can only be read off the first
run's coverage step. `main.py` currently asks for Coinbase BTCUSD daily,
2021-01-01 → 2021-12-31. On the first run:

- If the coverage step shows different dates, pin `SetStartDate` /
  `SetEndDate` in `main.py` to the files that actually exist.
- If only minute files exist for BTCUSD, switch the algorithm's
  `Resolution.Daily` to `Resolution.Minute` (and shorten the window).
- Lean fails loudly on missing data (it does not silently pass), so a
  bad first run is informative, not dangerous.

## The lean-cli route (optional, deliberately not used)

Current lean-cli builds a per-organization workspace via `lean init`,
which requires logging in to a QuantConnect account, and its commands
that run the engine require Docker. Paul has no QuantConnect account and
Nova doesn't create accounts on his behalf, so the workflow uses the
open-source engine image directly — same engine, no account. If a QC
account ever exists, `NovaBtcEmaCross/config.json` + a real `lean init`
workspace make the CLI route work with no code changes.

## Follow-ups before any head-to-head numbers

- Port Nova's cost assumptions into Lean as a custom fee + slippage
  model (bot-backtest charges 0.25% swap fee per side plus 0.1%/0.5%
  slippage by asset class). Until then, Lean's built-in Coinbase fee
  model applies and the two engines' outputs are NOT directly comparable.
- SOL backtests would need GeckoTerminal data converted into Lean's zip
  data format; the free sample data is majors-only.
