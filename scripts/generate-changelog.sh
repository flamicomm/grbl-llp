#!/usr/bin/env bash
# generate-changelog.sh
# Generate changelog entries between two git tags, formatted for CHANGELOG.md.
#
# Usage:
#   ./scripts/generate-changelog.sh              # between last tag and HEAD
#   ./scripts/generate-changelog.sh v1.0.0 v1.1.0  # between two tags
#   ./scripts/generate-changelog.sh --stdout      # print to stdout instead of editing CHANGELOG.md

set -euo pipefail

CHANGELOG_FILE="CHANGELOG.md"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$REPO_ROOT"

# Determine tags
if [ $# -ge 2 ]; then
    FROM_TAG="$1"
    TO_TAG="$2"
elif [ $# -ge 1 ] && [ "$1" != "--stdout" ]; then
    FROM_TAG="$1"
    TO_TAG="HEAD"
else
    FROM_TAG="$(git describe --tags --abbrev=0 HEAD 2>/dev/null || echo "")"
    if [ -z "$FROM_TAG" ]; then
        FROM_TAG="$(git rev-list --max-parents=0 HEAD)"
    fi
    TO_TAG="HEAD"
fi

# Get the previous tag for the from-to range
if [ "$TO_TAG" = "HEAD" ]; then
    PREV_TAG="$(git describe --tags --abbrev=0 HEAD~1 2>/dev/null || echo "$FROM_TAG")"
else
    PREV_TAG="$FROM_TAG"
fi

# Generate markdown entries
generate_entries() {
    echo "### Added"
    git log "$PREV_TAG..$TO_TAG" --oneline --grep="^feat\|^add\|^feature\|^implement\|^create" --format="  - %s" 2>/dev/null || true

    echo ""
    echo "### Changed"
    git log "$PREV_TAG..$TO_TAG" --oneline --grep="^change\|^update\|^refactor\|^modify\|^optimize\|^improve" --format="  - %s" 2>/dev/null || true

    echo ""
    echo "### Fixed"
    git log "$PREV_TAG..$TO_TAG" --oneline --grep="^fix\|^bug\|^correct\|^resolve" --format="  - %s" 2>/dev/null || true

    echo ""
    echo "### Removed"
    git log "$PREV_TAG..$TO_TAG" --oneline --grep="^remove\|^delete\|^drop" --format="  - %s" 2>/dev/null || true

    # Unclassified commits
    UNCLASSIFIED=$(git log "$PREV_TAG..$TO_TAG" --oneline --invert-grep \
        --grep="^feat\|^add\|^feature\|^change\|^update\|^refactor\|^fix\|^bug\|^remove\|^delete\|^merge\|^Merge" \
        --format="  - %s" 2>/dev/null || true)
    if [ -n "$UNCLASSIFIED" ]; then
        echo ""
        echo "### Other"
        echo "$UNCLASSIFIED"
    fi
}

if [ "${1:-}" = "--stdout" ]; then
    # Detect version from tag name
    VERSION="${TO_TAG#v}"
    echo "## [$VERSION] - $(date +%Y-%m-%d)"
    echo ""
    generate_entries
else
    echo "Generating changelog entries between $PREV_TAG and $TO_TAG..."
    echo ""
    VERSION="${TO_TAG#v}"
    echo "## [$VERSION] - $(date +%Y-%m-%d)" >> "$CHANGELOG_FILE"
    echo "" >> "$CHANGELOG_FILE"
    generate_entries >> "$CHANGELOG_FILE"
    echo "" >> "$CHANGELOG_FILE"

    echo "Done. Entries appended to $CHANGELOG_FILE"
    echo "Please review and edit before committing."
fi
