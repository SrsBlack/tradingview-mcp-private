#!/usr/bin/env bash
# One-shot installer for the git hooks in this repo.
# Symlinks (or copies on Windows) scripts/git-hooks/* into .git/hooks/.
#
# Re-run anytime — idempotent.

set -e

REPO_ROOT="$(git rev-parse --show-toplevel)"
SRC="$REPO_ROOT/scripts/git-hooks"
DST="$REPO_ROOT/.git/hooks"

if [ ! -d "$DST" ]; then
    echo "✗ $DST does not exist (not in a git repo?)"
    exit 1
fi

installed=0
for hook in pre-push; do
    src_file="$SRC/$hook"
    dst_file="$DST/$hook"

    if [ ! -f "$src_file" ]; then
        echo "  skip: $hook (not in $SRC)"
        continue
    fi

    # Try symlink first; fall back to copy on Windows or if symlink fails
    rm -f "$dst_file"
    if ln -s "$src_file" "$dst_file" 2>/dev/null; then
        echo "  installed: $hook (symlink)"
    else
        cp "$src_file" "$dst_file"
        echo "  installed: $hook (copy — re-run install.sh after edits)"
    fi
    chmod +x "$dst_file" 2>/dev/null || true
    installed=$((installed + 1))
done

echo
if [ "$installed" -gt 0 ]; then
    echo "✓ Installed $installed hook(s)."
    echo "  pre-push will run scripts/lint_memory.py and block on FAIL."
    echo "  Bypass once with: git push --no-verify"
else
    echo "No hooks installed."
fi
