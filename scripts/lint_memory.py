"""Lint memory + ARCHITECTURE.md against current code state.

Catches drift between claims and reality:
  - Commit hashes referenced in docs that no longer exist in git
  - File paths referenced in docs that no longer exist on disk
  - Cross-file invariants violated (e.g. swing lookback drift)
  - "fix pending" language in old files where the fix may have shipped
  - Old verification dates that may need re-verifying
  - Broken markdown links in MEMORY.md

Run from repo root:
    python scripts/lint_memory.py

Exit code 0 if all checks pass, 1 otherwise. Designed to be used in pre-commit
hooks or CI but also useful as an ad-hoc audit tool. Reports findings as
[PASS] / [WARN] / [FAIL]; only [FAIL] affects exit code.
"""
from __future__ import annotations

import re
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MEMORY_DIR = Path.home() / ".claude" / "projects" / "C--Users-User" / "memory"


# ----- helpers ---------------------------------------------------------------

def color(text: str, c: str) -> str:
    """Use ANSI colors only on terminals that support it."""
    if not sys.stdout.isatty():
        return text
    codes = {"red": 31, "yellow": 33, "green": 32, "cyan": 36, "dim": 2}
    return f"\033[{codes.get(c, 0)}m{text}\033[0m"


def passed(msg: str) -> tuple[str, str]:
    return ("PASS", msg)


def warned(msg: str) -> tuple[str, str]:
    return ("WARN", msg)


def failed(msg: str) -> tuple[str, str]:
    return ("FAIL", msg)


def all_doc_files() -> list[Path]:
    """Every markdown file we want to lint claims from."""
    files: list[Path] = []
    for p in REPO_ROOT.glob("*.md"):
        files.append(p)
    if MEMORY_DIR.exists():
        for p in MEMORY_DIR.glob("*.md"):
            files.append(p)
    return files


def run_check(name: str, fn) -> list[tuple[str, str]]:
    """Run a check function and return its results, catching exceptions."""
    try:
        results = fn()
    except Exception as e:
        return [failed(f"{name}: check raised {type(e).__name__}: {e}")]
    if not results:
        return [passed(f"{name}: no issues")]
    return results


# ----- checks ----------------------------------------------------------------

def check_commit_hashes() -> list[tuple[str, str]]:
    """Commit hashes referenced explicitly (commit XXX or `XXX`) must resolve in this repo's git.

    Scoped intentionally narrow — only flags commits in files that primarily
    reference *this* repo (ARCHITECTURE.md, feedback files about the bridge).
    Project memory files often reference commits in *other* repos (proof_app,
    tandem, etc.) and we can't validate those from here. Also skips UUID-style
    `originSessionId` strings.
    """
    # Match: "commit XXX", "commit `XXX`", or backtick-wrapped 7-char-min hex,
    # explicitly NOT a UUID component (UUIDs have hyphens around the hex).
    hash_pattern = re.compile(
        r"(?:commit\s+`?|\bsha[:= ]\s*|\bhash[:= ]\s*)([0-9a-f]{7,40})\b",
        re.IGNORECASE,
    )
    # Files whose claims SHOULD be validated against this repo's git.
    # Other memory files (project_proof_app.md, project_tandem.md, etc.) reference
    # commits in different repos and can't be checked from here.
    in_repo_files = {
        "ARCHITECTURE.md",
        "feedback_trading_loss_patterns.md",
        "feedback_reasoning_self_contradiction.md",
        "feedback_bridge_restart_strips_tps.md",
        "project_session_2026_04_24_25.md",
        "project_tradingview_mcp_auto_trade.md",
    }

    findings = []
    seen_hashes: dict[str, list[str]] = {}

    for doc in all_doc_files():
        if doc.name not in in_repo_files:
            continue
        try:
            content = doc.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for match in hash_pattern.finditer(content):
            h = match.group(1).lower()
            seen_hashes.setdefault(h, []).append(doc.name)

    for h, docs in seen_hashes.items():
        result = subprocess.run(
            ["git", "cat-file", "-e", h],
            cwd=REPO_ROOT,
            capture_output=True,
        )
        if result.returncode != 0:
            unique_docs = sorted(set(docs))
            findings.append(warned(
                f"Commit hash `{h}` (in {', '.join(unique_docs[:3])}) "
                f"does not resolve in this repo — may be a rebased or wrong-repo reference"
            ))
    return findings


def check_file_paths() -> list[tuple[str, str]]:
    """File paths referenced in ARCHITECTURE.md must exist (file or directory)."""
    # Match backtick-wrapped path-like strings. Handles trailing-slash dirs.
    path_pattern = re.compile(
        r"`(bridge/[\w/]+(?:\.py)?/?|scripts/[\w/]+\.py|verify_[\w]+\.py|"
        r"bridge/strategy_knowledge/[\w/]+/?|rules\.json|bridge_safety_state\.json)`"
    )

    findings = []
    arch = REPO_ROOT / "ARCHITECTURE.md"
    if not arch.exists():
        return [warned("ARCHITECTURE.md not found at repo root")]

    content = arch.read_text(encoding="utf-8")
    for match in path_pattern.finditer(content):
        rel_path = match.group(1).rstrip("/")
        full = REPO_ROOT / rel_path
        if not full.exists():
            findings.append(failed(
                f"ARCHITECTURE.md references `{rel_path}` but does not exist at {full}"
            ))
    return findings


def check_lookback_invariant() -> list[tuple[str, str]]:
    """H4/D1/W1 swing lookback values in ict_pipeline.py must match live_executor_adapter.py."""
    pipeline = REPO_ROOT / "bridge" / "ict_pipeline.py"
    adapter = REPO_ROOT / "bridge" / "live_executor_adapter.py"

    if not pipeline.exists() or not adapter.exists():
        return [failed("ict_pipeline.py or live_executor_adapter.py missing")]

    pipeline_text = pipeline.read_text(encoding="utf-8")
    adapter_text = adapter.read_text(encoding="utf-8")

    # Pipeline values: lookback=N in detect_swings calls. We expect three:
    # H4 lookback=5, D1 lookback=3, W1 lookback=2.
    # Adapter has tf_config dict with (mt5.TIMEFRAME_X, n_bars, lookback) tuples.
    findings = []

    # Pipeline H4 (search context for "df_structure" → H4)
    pipe_h4 = re.search(r"detect_swings\(df_structure,\s*lookback=(\d+)", pipeline_text)
    pipe_d1 = re.search(r"d1_swings\s*=\s*detect_swings\([^,]+,\s*lookback=(\d+)", pipeline_text)
    pipe_w1 = re.search(r"w1_swings\s*=\s*detect_swings\([^,]+,\s*lookback=(\d+)", pipeline_text)

    # Adapter tf_config — extract the third element of each tuple
    h4_match = re.search(r'"H4":\s*\([^,]+,\s*\d+,\s*(\d+)\)', adapter_text)
    d1_match = re.search(r'"D1":\s*\([^,]+,\s*\d+,\s*(\d+)\)', adapter_text)
    w1_match = re.search(r'"W1":\s*\([^,]+,\s*\d+,\s*(\d+)\)', adapter_text)

    pairs = [
        ("H4", pipe_h4, h4_match),
        ("D1", pipe_d1, d1_match),
        ("W1", pipe_w1, w1_match),
    ]
    for tf, p_match, a_match in pairs:
        if not p_match:
            findings.append(warned(f"Could not find {tf} lookback in ict_pipeline.py — pattern may have changed"))
            continue
        if not a_match:
            findings.append(warned(f"Could not find {tf} lookback in live_executor_adapter.py — pattern may have changed"))
            continue
        p_val = p_match.group(1)
        a_val = a_match.group(1)
        if p_val != a_val:
            findings.append(failed(
                f"{tf} swing lookback DRIFT: ict_pipeline.py = {p_val}, "
                f"live_executor_adapter.py = {a_val}. Entry and exit will compute "
                f"bias on different swings — see KEEP IN SYNC comments."
            ))

    return findings


def check_position_cap_consistency() -> list[tuple[str, str]]:
    """BRIDGE_MAX_POSITIONS in code should match human-readable doc claims."""
    live = REPO_ROOT / "bridge" / "live_executor.py"
    paper = REPO_ROOT / "bridge" / "paper_executor.py"
    rules = REPO_ROOT / "rules.json"

    findings = []
    if not live.exists():
        return [failed("live_executor.py missing")]

    # Extract BRIDGE_MAX_POSITIONS = N
    live_text = live.read_text(encoding="utf-8")
    m = re.search(r"BRIDGE_MAX_POSITIONS\s*=\s*(\d+)", live_text)
    if not m:
        return [warned("Could not find BRIDGE_MAX_POSITIONS in live_executor.py")]
    code_cap = int(m.group(1))

    # Check paper executor uses BRIDGE_MAX_POSITIONS (not hardcoded number)
    if paper.exists():
        paper_text = paper.read_text(encoding="utf-8")
        if re.search(r"max_positions:\s*int\s*=\s*\d+\b", paper_text):
            findings.append(failed(
                "paper_executor.py has hardcoded max_positions integer — "
                "should reference BRIDGE_MAX_POSITIONS to avoid drift"
            ))

    # Check rules.json human description has correct number
    if rules.exists():
        rules_text = rules.read_text(encoding="utf-8")
        m_rules = re.search(r"Maximum (\d+) concurrent open positions", rules_text)
        if m_rules:
            rules_cap = int(m_rules.group(1))
            if rules_cap != code_cap:
                findings.append(failed(
                    f"rules.json says 'Maximum {rules_cap} concurrent open positions' "
                    f"but BRIDGE_MAX_POSITIONS = {code_cap}"
                ))

    return findings


def check_pending_language() -> list[tuple[str, str]]:
    """Memory files modified > 14 days ago that still claim something is pending."""
    suspicious = re.compile(
        r"(proper fix pending|fix pending\b|TODO:|fix is needed|needs to be implemented|not yet|to be added)",
        re.IGNORECASE,
    )
    cutoff = datetime.now(timezone.utc) - timedelta(days=14)
    findings = []

    if not MEMORY_DIR.exists():
        return [warned(f"Memory dir {MEMORY_DIR} not found — skipping")]

    for p in MEMORY_DIR.glob("*.md"):
        try:
            mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
            content = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if mtime > cutoff:
            continue  # recently updated, skip
        for match in suspicious.finditer(content):
            phrase = match.group(0)
            findings.append(warned(
                f"{p.name} (last modified {mtime.date()}, >14d ago) contains "
                f"'{phrase}' — verify the work hasn't actually shipped"
            ))
            break  # one warning per file is enough
    return findings


def check_old_verifications() -> list[tuple[str, str]]:
    """Memory entries with 'verified <date>' or 'as of <date>' > 30 days old."""
    pattern = re.compile(
        r"(verified|as of|confirmed|checked)[\s:]+(\d{4}-\d{2}-\d{2})",
        re.IGNORECASE,
    )
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=30)
    findings = []

    for doc in all_doc_files():
        try:
            content = doc.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for match in pattern.finditer(content):
            try:
                claim_date = datetime.strptime(match.group(2), "%Y-%m-%d").date()
            except ValueError:
                continue
            if claim_date < cutoff:
                findings.append(warned(
                    f"{doc.name} contains '{match.group(0)}' (>30 days old) — "
                    f"consider re-verifying the claim and updating the date"
                ))
    # Dedup: max one warning per doc
    seen = set()
    deduped = []
    for sev, msg in findings:
        # Extract doc name from message
        doc_match = re.match(r"(\w+\.md)", msg)
        key = doc_match.group(1) if doc_match else msg
        if key not in seen:
            deduped.append((sev, msg))
            seen.add(key)
    return deduped


def check_memory_index_links() -> list[tuple[str, str]]:
    """Every ./*.md link in MEMORY.md must point to an existing file."""
    if not MEMORY_DIR.exists():
        return [warned(f"Memory dir {MEMORY_DIR} not found — skipping")]

    index = MEMORY_DIR / "MEMORY.md"
    if not index.exists():
        return [warned("MEMORY.md not found in memory dir")]

    content = index.read_text(encoding="utf-8")
    link_pattern = re.compile(r"\[[^\]]+\]\((\./[\w_]+\.md)\)")
    findings = []
    for match in link_pattern.finditer(content):
        rel = match.group(1)
        target = (MEMORY_DIR / rel).resolve()
        if not target.exists():
            findings.append(failed(
                f"MEMORY.md link `{rel}` does not exist at {target.name}"
            ))
    return findings


def check_repo_clean() -> list[tuple[str, str]]:
    """Working tree shouldn't have uncommitted changes (excluding pycache, hud-state)."""
    result = subprocess.run(
        ["git", "status", "--short"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return [warned(f"git status failed: {result.stderr.strip()}")]
    findings = []
    for line in result.stdout.splitlines():
        if "__pycache__" in line or ".omc/" in line or "_pycache" in line:
            continue
        if line.strip():
            findings.append(warned(f"Uncommitted: {line.strip()}"))
    return findings


def check_unpushed() -> list[tuple[str, str]]:
    """Local commits not pushed to origin."""
    result = subprocess.run(
        ["git", "log", "origin/main..HEAD", "--oneline"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return []  # likely no upstream tracking; not our problem
    findings = []
    for line in result.stdout.splitlines():
        if line.strip():
            findings.append(warned(f"Unpushed commit: {line.strip()}"))
    return findings


def check_kb_schema() -> list[tuple[str, str]]:
    """ICT concept cards conform to SCHEMA.md.

    Required fields per SCHEMA.md: id, layer, definition, depends_on,
    feeds_into, bridge_integration. As of 2026-04-26 (commit 58adb41) Track 2
    closed and the backlog is empty — '[NOT YET DEFINED' stubs are now FAIL,
    not WARN. Spelling drift (bridge_usage, common_mistake singular) is also
    FAIL since the schema migration should have fixed all of those.
    """
    import json
    kb_dir = REPO_ROOT / "bridge" / "strategy_knowledge" / "ict_concepts"
    if not kb_dir.exists():
        return [warned(f"KB dir {kb_dir} not found — skipping")]

    REQUIRED = ["id", "layer", "definition", "depends_on", "feeds_into", "bridge_integration"]
    DEPRECATED = ["bridge_usage", "common_mistake"]
    STUB_MARKER = "[NOT YET DEFINED"

    findings = []
    on_disk_cards: set[str] = set()
    stub_count = 0
    cards_checked = 0

    for card_path in sorted(kb_dir.glob("*.json")):
        if card_path.stem in ("_index", "cross_correlations"):
            continue
        on_disk_cards.add(card_path.stem)
        cards_checked += 1
        try:
            data = json.loads(card_path.read_text(encoding="utf-8"))
        except Exception as e:
            findings.append(failed(f"{card_path.name}: failed to parse JSON ({e})"))
            continue
        if not isinstance(data, dict):
            findings.append(failed(f"{card_path.name}: root is not a dict"))
            continue

        # Required fields
        for field in REQUIRED:
            if field not in data:
                findings.append(failed(
                    f"{card_path.name}: missing required field '{field}' (per SCHEMA.md)"
                ))

        # Deprecated fields
        for dep in DEPRECATED:
            if dep in data:
                target = "bridge_integration" if dep == "bridge_usage" else "common_mistakes"
                findings.append(failed(
                    f"{card_path.name}: deprecated field '{dep}' (rename to '{target}' — see SCHEMA.md)"
                ))

        # id matches filename
        if data.get("id") and data["id"] != card_path.stem:
            findings.append(warned(
                f"{card_path.name}: id={data['id']!r} doesn't match filename"
            ))

        # Stubs are FAIL (Track 2 closed 2026-04-26 commit 58adb41 — backlog empty).
        # Reintroducing a [NOT YET DEFINED marker means a card was added or
        # regressed without real bridge_integration text. Either fill it in
        # or remove the card.
        bi = data.get("bridge_integration", "")
        if isinstance(bi, str) and STUB_MARKER in bi:
            stub_count += 1
            findings.append(failed(
                f"{card_path.name}: bridge_integration is a stub "
                f"('[NOT YET DEFINED' marker present). Fill in real text or "
                f"remove the card. See SCHEMA.md and INTEGRATION_BACKLOG.md."
            ))

    # _index.json catalogues all on-disk cards (walks dependency graph + sections + concepts)
    idx_path = kb_dir / "_index.json"
    if idx_path.exists():
        try:
            idx = json.loads(idx_path.read_text(encoding="utf-8"))
            listed: set[str] = set()
            def walk(o):
                if isinstance(o, dict):
                    for k, v in o.items():
                        if k in ("concepts", "sections") and isinstance(v, list):
                            for c in v:
                                if isinstance(c, str):
                                    listed.add(c)
                        else:
                            walk(v)
                elif isinstance(o, list):
                    for x in o: walk(x)
            walk(idx)
            orphans = on_disk_cards - listed
            for o in sorted(orphans):
                findings.append(failed(
                    f"_index.json: card '{o}' on disk but not catalogued in dependency graph or sections"
                ))
        except Exception as e:
            findings.append(failed(f"_index.json: parse error {e}"))

    # Summary line — useful even when no fail
    findings.append(warned(
        f"KB stats: {cards_checked} cards, {stub_count} bridge_integration stubs "
        f"(see INTEGRATION_BACKLOG.md to drain)"
    ))

    return findings


# ----- main ------------------------------------------------------------------

CHECKS = [
    ("commit hashes resolve",      check_commit_hashes),
    ("doc file paths exist",       check_file_paths),
    ("swing lookback invariant",   check_lookback_invariant),
    ("position cap consistency",   check_position_cap_consistency),
    ("KB schema conformance",      check_kb_schema),
    ("'pending' language stale?",  check_pending_language),
    ("verification dates fresh",   check_old_verifications),
    ("MEMORY.md links resolve",    check_memory_index_links),
    ("repo working tree clean",    check_repo_clean),
    ("local commits pushed",       check_unpushed),
]


def main() -> int:
    print(color(f"\nMemory + Code Lint  (run from {REPO_ROOT.name})\n", "cyan"))
    total_pass = 0
    total_warn = 0
    total_fail = 0

    for name, fn in CHECKS:
        results = run_check(name, fn)
        # Determine overall section status
        has_fail = any(sev == "FAIL" for sev, _ in results)
        has_warn = any(sev == "WARN" for sev, _ in results)
        section_color = "red" if has_fail else ("yellow" if has_warn else "green")
        marker = "FAIL" if has_fail else ("WARN" if has_warn else "PASS")
        print(f"  [{color(marker, section_color)}] {name}")
        for sev, msg in results:
            if sev == "PASS":
                total_pass += 1
            elif sev == "WARN":
                total_warn += 1
                print(f"        {color('warn:', 'yellow')} {msg}")
            else:
                total_fail += 1
                print(f"        {color('fail:', 'red')} {msg}")

    print()
    print(color(
        f"Summary: {total_pass} pass, {total_warn} warn, {total_fail} fail",
        "red" if total_fail else ("yellow" if total_warn else "green"),
    ))
    print()
    if total_fail:
        print(color("FAIL — fix code or memory before continuing.", "red"))
        return 1
    if total_warn:
        print(color("WARN — review and decide; not blocking.", "yellow"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
