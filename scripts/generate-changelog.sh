#!/usr/bin/env bash
# generate-changelog.sh
# Generate changelog entries between two git refs, formatted for CHANGELOG.md.
#
# Usage:
#   ./scripts/generate-changelog.sh                         # last tag → HEAD
#   ./scripts/generate-changelog.sh v1.0.1 v1.1.0           # between two tags
#   ./scripts/generate-changelog.sh v1.0.1 HEAD             # tag → HEAD
#   ./scripts/generate-changelog.sh --stdout v1.0.1 HEAD    # preview only

set -euo pipefail

CHANGELOG_FILE="CHANGELOG.md"
STDOUT_MODE=false

# Parse --stdout flag before positional args
POSITIONAL=()
while [ $# -gt 0 ]; do
    case "$1" in
        --stdout) STDOUT_MODE=true; shift ;;
        *) POSITIONAL+=("$1"); shift ;;
    esac
done
set -- "${POSITIONAL[@]}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

# Determine tags
if [ $# -ge 2 ]; then
    FROM_TAG="$1"
    TO_TAG="$2"
elif [ $# -eq 1 ]; then
    FROM_TAG="$1"
    TO_TAG="HEAD"
else
    FROM_TAG="$(git describe --tags --abbrev=0 --always HEAD 2>/dev/null || echo "")"
    if [ -z "$FROM_TAG" ]; then
        FROM_TAG="$(git rev-list --max-parents=0 HEAD)"
    fi
    TO_TAG="HEAD"
fi

# Generate markdown entries between FROM_TAG and TO_TAG
generate_entries() {
    echo "### Added"
    git log "$FROM_TAG..$TO_TAG" --oneline --grep="^feat\|^add\|^feature\|^implement\|^create" --format="  - %s" 2>/dev/null || true

    echo ""
    echo "### Changed"
    git log "$FROM_TAG..$TO_TAG" --oneline --grep="^change\|^update\|^refactor\|^modify\|^optimize\|^improve" --format="  - %s" 2>/dev/null || true

    echo ""
    echo "### Fixed"
    git log "$FROM_TAG..$TO_TAG" --oneline --grep="^fix\|^bug\|^correct\|^resolve" --format="  - %s" 2>/dev/null || true

    echo ""
    echo "### Removed"
    git log "$FROM_TAG..$TO_TAG" --oneline --grep="^remove\|^delete\|^drop" --format="  - %s" 2>/dev/null || true

    # Unclassified commits
    UNCLASSIFIED="$(git log "$FROM_TAG..$TO_TAG" --oneline --invert-grep \
        --grep="^feat\|^add\|^feature\|^change\|^update\|^refactor\|^fix\|^bug\|^remove\|^delete\|^merge\|^Merge" \
        --format="  - %s" 2>/dev/null || true)"
    if [ -n "$UNCLASSIFIED" ]; then
        echo ""
        echo "### Other"
        echo "$UNCLASSIFIED"
    fi
}

# Detect version string
VERSION="${TO_TAG#v}"

if [ "$STDOUT_MODE" = true ]; then
    echo "## [$VERSION] - $(date +%Y-%m-%d)"
    echo ""
    generate_entries
else
    echo "Generating changelog entries between $FROM_TAG and $TO_TAG..."
    {
        echo ""
        echo "## [$VERSION] - $(date +%Y-%m-%d)"
        echo ""
        generate_entries
    } >> "$CHANGELOG_FILE"
    echo "Done. Entries appended to $CHANGELOG_FILE"
    echo "Please review and edit before committing."
fi
