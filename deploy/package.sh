#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

usage() {
    cat <<EOF
Usage: bash deploy/package.sh [OPTIONS]

Build a release tarball from the current repository.

Options:
  --output-dir DIR   Output directory (default: current directory)
  -h, --help         Show this help

Reads version from VERSION file (no 'v' prefix).
Outputs:
  agent-exporter-to-langfuse-<version>.tar.gz
  SHA256SUMS
EOF
    exit 0
}

OUTPUT_DIR="."

while [[ $# -gt 0 ]]; do
    case "$1" in
        --output-dir) OUTPUT_DIR="$2"; shift 2 ;;
        -h|--help) usage ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

VERSION_FILE="$REPO_ROOT/VERSION"
if [ ! -f "$VERSION_FILE" ]; then
    echo "ERROR: VERSION file not found at $VERSION_FILE" >&2
    exit 1
fi

VERSION="$(cat "$VERSION_FILE" | tr -d '[:space:]')"
if [ -z "$VERSION" ]; then
    echo "ERROR: VERSION file is empty" >&2
    exit 1
fi

TARBALL_NAME="agent-exporter-to-langfuse-${VERSION}.tar.gz"
STAGING_DIR="$(mktemp -d)"
STAGE_TARGET="$STAGING_DIR/agent-exporter-to-langfuse-${VERSION}"

trap 'rm -rf "$STAGING_DIR"' EXIT

echo "Packaging version ${VERSION} ..."

mkdir -p "$STAGE_TARGET"

if command -v git &>/dev/null && [ -d "$REPO_ROOT/.git" ]; then
    (cd "$REPO_ROOT" && git ls-files -z | while IFS= read -r -d '' f; do
        mkdir -p "$STAGE_TARGET/$(dirname "$f")"
        cp -f "$f" "$STAGE_TARGET/$f"
    done)
else
    cp -R "$REPO_ROOT/"* "$STAGE_TARGET/" 2>/dev/null || true
    cp "$REPO_ROOT/".[!.]* "$STAGE_TARGET/" 2>/dev/null || true
fi

rm -rf "$STAGE_TARGET/.git" \
       "$STAGE_TARGET/.venv" \
       "$STAGE_TARGET/.github" \
       "$STAGE_TARGET/node_modules"
find "$STAGE_TARGET" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find "$STAGE_TARGET" -type d -name ".venv" -exec rm -rf {} + 2>/dev/null || true
find "$STAGE_TARGET" -name "*.pyc" -delete 2>/dev/null || true

# Pre-build TS langstash-deliver so dist/ is available for codex and opencode hooks
TS_DELIVER="$STAGE_TARGET/hooks/langstash-deliver/typescript"
if [ -f "$TS_DELIVER/tsconfig.json" ]; then
    echo "Building TS langstash-deliver ..."
    if (cd "$TS_DELIVER" && npm install --ignore-scripts && npm run build); then
        :
    else
        rc=$?
        echo "ERROR: langstash-deliver build failed (exit code $rc)" >&2
        exit 1
    fi
    if [ ! -f "$TS_DELIVER/dist/index.js" ]; then
        echo "ERROR: langstash-deliver build succeeded but dist/index.js not found" >&2
        exit 1
    fi
    rm -rf "$TS_DELIVER/node_modules"
fi

mkdir -p "$OUTPUT_DIR"

(cd "$STAGING_DIR" && tar czf "$TARBALL_NAME" "agent-exporter-to-langfuse-${VERSION}")
mv "$STAGING_DIR/$TARBALL_NAME" "$OUTPUT_DIR/$TARBALL_NAME"

compute_sha256() {
    local file="$1"
    if command -v sha256sum &>/dev/null; then
        sha256sum "$file"
    elif command -v shasum &>/dev/null; then
        shasum -a 256 "$file"
    else
        echo "ERROR: Neither sha256sum nor shasum found" >&2
        exit 1
    fi
}

(cd "$OUTPUT_DIR" && compute_sha256 "$TARBALL_NAME" > SHA256SUMS)

echo "Created: $OUTPUT_DIR/$TARBALL_NAME"
echo "Created: $OUTPUT_DIR/SHA256SUMS"
echo "SHA256: $(cat "$OUTPUT_DIR/SHA256SUMS")"
