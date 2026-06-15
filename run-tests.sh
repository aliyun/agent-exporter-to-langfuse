#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
EXIT_CODE=0

echo "=== exporter ==="
(cd "$SCRIPT_DIR/exporter" && uv run pytest -q) || EXIT_CODE=1

echo ""
echo "=== hooks/claude-code/hooks ==="
(cd "$SCRIPT_DIR/hooks/claude-code/hooks" && uv run pytest -q) || EXIT_CODE=1

echo ""
echo "=== hooks/langstash-deliver/python ==="
(cd "$SCRIPT_DIR/hooks/langstash-deliver/python" && uv run pytest -q) || EXIT_CODE=1

echo ""
echo "=== hooks/codex ==="
(cd "$SCRIPT_DIR/hooks/codex" && pnpm vitest run) || EXIT_CODE=1

echo ""
if [ "$EXIT_CODE" -eq 0 ]; then
    echo "All test suites passed."
else
    echo "Some test suites failed."
fi

exit $EXIT_CODE
