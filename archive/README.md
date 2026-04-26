# Archived Docs

Historical planning and architecture docs that no longer reflect the current system. Preserved for context — when reading old commits or wondering "why does this look this way," these are the docs that explain the original intent.

## Contents

| File | Created | Why archived |
|------|---------|--------------|
| `AUTO_TRADE_2026-04-08.md` | 2026-04-08 | Pre-bridge architecture: TS-side `src/services/*` files referenced no longer exist; the system was rewritten as a Python bridge under `bridge/`. The pipeline diagram and component descriptions describe a system that was superseded. |
| `ICT_IMPLEMENTATION_PLAN_2026-04-08.md` | 2026-04-08 | Phase-by-phase plan for wiring 22 ICT concepts. Marked "ALL PHASES COMPLETE" at the time, but the codebase has gone through multiple major restructures since. |
| `STRATEGY_MASTER_PLAN_2026-04-08.md` | 2026-04-08 | 591-line master plan for integrating 33 ChartFanatics strategies + MT5 backtest insights. Aspirational and partially executed — the actual system that emerged differs significantly (different watchlist, different scoring, different risk model). |

## Where to find current architecture

See `ARCHITECTURE.md` at the repo root.
