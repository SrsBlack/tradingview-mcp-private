# Memory & Code Lint

> Periodic audit to catch drift between memory claims, ARCHITECTURE.md, and the actual codebase.
>
> Inspired by Karpathy's "LLM Wiki" pattern (https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) — the bookkeeping is what humans abandon, so make it cheap and explicit.
>
> Run cadence: every session that touches `bridge/`, before any major work, and on demand when something feels off.

---

## Quick start

```bash
cd ~/tradingview-mcp-jackson
python scripts/lint_memory.py             # automated drift checks (10 sec)
```

Read the output. Anything tagged `[FAIL]` is real drift; fix in code or memory before continuing.

For manual audits (logic claims, backtest numbers, runbook accuracy), follow [§ Manual checklist](#manual-checklist) below.

---

## What `lint_memory.py` checks (automated)

| Check | What it does | What "fail" means |
|---|---|---|
| **Commit hashes** | Every `commit-hash`-looking string in memory + ARCHITECTURE.md → `git cat-file -e` | Referenced commit was rebased away or never existed |
| **File paths** | Every `bridge/*.py` etc. referenced in docs → `os.path.exists()` | File moved, renamed, or deleted |
| **Cross-file invariants** | Defined invariants (e.g. "H4 lookback in ict_pipeline.py == in live_executor_adapter.py") | Values drifted between files |
| **Position cap consistency** | `BRIDGE_MAX_POSITIONS` in code, default in PaperExecutor, count in rules.json human description | Drift between code and human-readable docs |
| **KB schema conformance** | Every ICT concept card matches `bridge/strategy_knowledge/ict_concepts/SCHEMA.md` (required fields, no deprecated fields, no orphan files); reports stub count for INTEGRATION_BACKLOG progress | Card violates schema or new orphan exists; stub count = visible track-2 progress |
| **Suspicious "pending" language** | Files with `proper fix pending`, `needs to be`, `TODO:` and last-modified > 14 days ago | Likely stale claim that something is undone when it's actually shipped |
| **Old verification dates** | Memory entries with `verified <date>` or `as of <date>` > 30 days old | Claim may no longer be true; prompt re-verify |
| **MEMORY.md broken links** | Every `./*.md` link in MEMORY.md → file exists | Renamed or deleted memory file |

---

## Manual checklist

These can't be automated reliably — they need human judgment or backtest re-runs.

### Code/architecture drift
- [ ] **Module list in ARCHITECTURE.md § 3** matches `ls bridge/*.py` (count and names)
- [ ] **Pre-gate count** in ARCHITECTURE.md § 4.1 matches actual `_pre_gate()` returns in `bridge/claude_decision.py`
- [ ] **Open positions section in TL;DR** still reflects current MT5 state (or has been removed if too volatile)
- [ ] **Decision history § 8** has been extended with the most recent commits (last 5 if any new feat/fix)

### Logic/claim drift
- [ ] Re-run `python verify_new_gates.py`. The "8/8 broker-verified losers, +$1,103.90" claim in `feedback_reasoning_self_contradiction.md` and `MEMORY.md` should still hold; if not, update the numbers.
- [ ] Re-run `python verify_mtf_invalidation.py` and `python verify_d1_only_invalidation.py`. The 2026-04-26 backtest snapshot in `feedback_trading_loss_patterns.md` is dated; if new trades have landed, decide whether to refresh the figures or annotate as "as of 2026-04-26."
- [ ] Verify any "current state" tables (live MTF readings, account balance) in ARCHITECTURE.md still reflect reality. If they've drifted significantly, either update or remove the snapshot and reference live data.

### Memory hygiene
- [ ] Read `MEMORY.md` end-to-end. Any line that no longer matches the linked file's content? (Common: file gets updated, index line forgotten.)
- [ ] Are there feedback files that have been superseded but not marked? E.g., a fix shipped in commit X should mark the original problem entry as "fixed in X".
- [ ] Any duplicate entries — two memory files claiming similar things?

### Repo hygiene
- [ ] `git status --short | grep -v "__pycache__"` is clean (no orphaned uncommitted changes from past sessions)
- [ ] `git log origin/main..HEAD` is empty (everything pushed)
- [ ] `archive/README.md` accurately describes what's archived (no orphaned archive files)

---

## When to add a new automated check

When you find drift via the manual checklist, ask: "would 5 lines of Python catch this automatically next time?" If yes, add the check to `scripts/lint_memory.py`. Keep the script simple; if a check needs >30 lines, write it as its own script and reference from LINT.md.

The point of automation here isn't perfection — it's making the recurring drift cheap to catch so the bookkeeping doesn't get abandoned.

---

## Anti-patterns (things NOT to do during lint)

- **Don't update memory just to make it "feel current."** Stale dates are information; if a claim is correct as-of 2026-04-26, that's its provenance. Update only when the claim itself is wrong now.
- **Don't auto-fix in the lint script.** Lint reports drift, humans decide whether the code or the doc is the source of truth for that case. Auto-fix risks rewriting accurate memory because code changed in a way the script can't understand.
- **Don't over-fit the lint to one drift case.** If you add a check, make sure it's general enough to catch a class of issues, not just the one you saw.
