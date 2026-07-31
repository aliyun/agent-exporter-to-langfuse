#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
EXIT_CODE=0

echo "=== exporter ==="
(cd "$SCRIPT_DIR/exporter" && uv run pytest -q) || EXIT_CODE=1

echo ""
echo "=== hooks/claude-code/hooks ==="
(cd "$SCRIPT_DIR/hooks/claude-code/hooks" && uv run pytest -q) || EXIT_CODE=1

echo ""
echo "=== hooks/langstash-deliver/python ==="
(cd "$SCRIPT_DIR/hooks/langstash-deliver/python" && uv run pytest -q) || EXIT_CODE=1

# Build langstash-deliver TS dist (required by codex and cursor vitest for trace.ts import)
LANGSTASH_DELIVER_TS="$SCRIPT_DIR/hooks/langstash-deliver/typescript"
if [ ! -f "$LANGSTASH_DELIVER_TS/dist/index.js" ]; then
    echo ""
    echo "=== Building langstash-deliver TS dist ==="
    (cd "$LANGSTASH_DELIVER_TS" && npm install --ignore-scripts && npm run build) || EXIT_CODE=1
    rm -rf "$LANGSTASH_DELIVER_TS/node_modules"
fi

echo ""
echo "=== hooks/opencode/hooks ==="
(cd "$SCRIPT_DIR/hooks/opencode/hooks" && node --test langfuse-exporter.test.mjs) || EXIT_CODE=1

echo ""
echo "=== hooks/codex ==="
(cd "$SCRIPT_DIR/hooks/codex" && pnpm vitest run) || EXIT_CODE=1

echo ""
echo "=== hooks/cursor ==="
(cd "$SCRIPT_DIR/hooks/cursor" && npx vitest run) || EXIT_CODE=1

echo ""
echo "=== hooks/pi ==="
(cd "$SCRIPT_DIR/hooks/pi" && npx vitest run) || EXIT_CODE=1

echo ""
if [ "$EXIT_CODE" -eq 0 ]; then
    echo "All test suites passed."
else
    echo "Some test suites failed."
fi

exit $EXIT_CODE
