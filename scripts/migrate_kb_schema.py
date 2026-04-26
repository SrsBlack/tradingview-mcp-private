"""Migrate ICT concept cards to the canonical schema (SCHEMA.md).

Operations performed:
  1. Rename `bridge_usage` -> `bridge_integration` (4 cards)
  2. Rename `common_mistake` -> `common_mistakes` (4 cards), ensure value is array
  3. Insert stub `bridge_integration` on cards missing it
  4. Validate `id` matches filename (warn on mismatch, do not auto-fix)
  5. Preserve all other fields untouched (specialized concept-specific keys)

Idempotent: running twice produces no changes after the first run.

Usage:
    python scripts/migrate_kb_schema.py            # dry run, shows planned changes
    python scripts/migrate_kb_schema.py --apply    # actually write
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
KB_DIR = REPO_ROOT / "bridge" / "strategy_knowledge" / "ict_concepts"
SKIP_FILES = {"_index", "cross_correlations"}

STUB_BRIDGE_INTEGRATION = "[NOT YET DEFINED — see INTEGRATION_BACKLOG.md]"


def migrate_card(name: str, data: dict) -> tuple[dict, list[str]]:
    """Return (new_data, list_of_changes_applied)."""
    changes: list[str] = []
    new = dict(data)  # shallow copy

    # 1. bridge_usage → bridge_integration
    if "bridge_usage" in new and "bridge_integration" not in new:
        new["bridge_integration"] = new.pop("bridge_usage")
        changes.append("rename: bridge_usage -> bridge_integration")
    elif "bridge_usage" in new and "bridge_integration" in new:
        # Both present — preserve bridge_integration, drop bridge_usage
        new.pop("bridge_usage")
        changes.append("drop: bridge_usage (bridge_integration already present)")

    # 2. common_mistake (singular) → common_mistakes (plural)
    if "common_mistake" in new:
        old_val = new.pop("common_mistake")
        if "common_mistakes" not in new:
            # Promote: if it's a string, wrap in single-item list
            if isinstance(old_val, str):
                new["common_mistakes"] = [old_val]
            else:
                new["common_mistakes"] = old_val
            changes.append("rename: common_mistake -> common_mistakes")
        else:
            # Both present — drop singular (assumption: plural is more thought-out)
            changes.append("drop: common_mistake (common_mistakes already present)")

    # 3. Insert stub if bridge_integration missing
    if "bridge_integration" not in new:
        new["bridge_integration"] = STUB_BRIDGE_INTEGRATION
        changes.append("insert: bridge_integration stub")

    # 4. Terminal cards: insert feeds_into: [] if missing (per SCHEMA.md)
    if "feeds_into" not in new:
        new["feeds_into"] = []
        changes.append("insert: feeds_into: [] (terminal card)")

    # 4. Validate id matches filename
    if new.get("id") != name:
        changes.append(f"WARN: id={new.get('id')!r} != filename={name!r} (not auto-fixing)")

    return new, changes


def reorder_keys(data: dict) -> dict:
    """Place required fields first, in canonical order, then everything else."""
    canonical_order = [
        "id", "layer", "definition", "depends_on", "feeds_into",
        "related_to", "bridge_integration",
        "trading_rules", "common_mistakes", "scoring",
    ]
    out = {}
    for k in canonical_order:
        if k in data:
            out[k] = data[k]
    # Then any other keys, preserving original order
    for k, v in data.items():
        if k not in out:
            out[k] = v
    return out


def main() -> int:
    apply = "--apply" in sys.argv

    if not KB_DIR.exists():
        print(f"FAIL: {KB_DIR} does not exist")
        return 1

    cards = sorted(p for p in KB_DIR.glob("*.json") if p.stem not in SKIP_FILES)
    print(f"{'Card':<42} Changes")
    print("-" * 100)

    total_changes = 0
    cards_changed = 0
    stubs_inserted = 0

    for card_path in cards:
        name = card_path.stem
        try:
            data = json.loads(card_path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  {name:<40} FAIL to parse: {e}")
            continue

        if not isinstance(data, dict):
            print(f"  {name:<40} skip (root is not a dict)")
            continue

        new_data, changes = migrate_card(name, data)
        new_data = reorder_keys(new_data)

        if not changes and new_data == data:
            # No-op
            continue

        cards_changed += 1
        total_changes += len(changes)
        if any("stub" in c for c in changes):
            stubs_inserted += 1

        print(f"  {name:<40} {'; '.join(changes) if changes else 'reorder only'}")

        if apply:
            card_path.write_text(json.dumps(new_data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print()
    print(f"Cards needing changes: {cards_changed}/{len(cards)}")
    print(f"Total changes: {total_changes}")
    print(f"Stubs inserted: {stubs_inserted}")
    print()

    if not apply:
        print("DRY RUN — no files written. Re-run with --apply to commit changes.")
    else:
        print("APPLIED — files written.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
