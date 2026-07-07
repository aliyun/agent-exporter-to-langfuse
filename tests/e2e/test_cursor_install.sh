#!/usr/bin/env bash
set -euo pipefail

# E2E test for Cursor Langfuse hook install/uninstall idempotency.
# Tests:
#   1. install.sh run twice → 1 langfuse entry per event (idempotent)
#   2. user-preset hook preserved, langfuse entry appended as 2nd array item
#   3. uninstall.sh removes langfuse entries, preserves user hooks
#   4. cursor.env contains LANGSTASH_ENABLED=true and LANGSTASH_URL=http://127.0.0.1:5288
#   5. every langfuse entry's command contains an absolute node path
#
# Per AGENTS.md: does not discard stderr; on failure echoes tested command's stderr and exit code.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
HOOK_DIR="$REPO_ROOT/hooks/cursor"

# Use a temporary HOME to isolate the test
TEST_HOME="$(mktemp -d)"
export HOME="$TEST_HOME"
export LANGSTASH_ENABLED="false"

# Create fake cursor home
mkdir -p "$HOME/.cursor"

# Ensure dist is built
if [ ! -f "$HOOK_DIR/dist/index.mjs" ]; then
    echo "Building cursor hook dist..."
    (cd "$HOOK_DIR" && npm install --ignore-scripts && npm run build) || {
        rc=$?
        echo "FAIL: npm build failed (exit $rc)" >&2
        rm -rf "$TEST_HOME"
        exit 1
    }
    rm -rf "$HOOK_DIR/node_modules"
fi

PASS_COUNT=0
FAIL_COUNT=0

pass() { echo "  PASS: $1"; PASS_COUNT=$((PASS_COUNT + 1)); }
fail() {
    echo "  FAIL: $1" >&2
    if [ -n "${2:-}" ]; then
        echo "    stderr: $2" >&2
    fi
    if [ -n "${3:-}" ]; then
        echo "    exit code: $3" >&2
    fi
    FAIL_COUNT=$((FAIL_COUNT + 1))
}

# --- Test 1: install.sh runs successfully ---
echo "=== Test 1: install.sh runs with provided credentials ==="
INSTALL_STDERR=$(bash "$HOOK_DIR/install.sh" \
    --secret-key "sk-lf-test-secret" \
    --public-key "pk-lf-test-public" \
    --base-url "http://test-langfuse:3000" \
    --user-id "test-user" \
    --tags "team:e2e" 2>&1 1>/dev/null) || {
    INSTALL_RC=$?
    fail "install.sh failed" "$INSTALL_STDERR" "$INSTALL_RC"
}
if [ -f "$HOME/.cursor/hooks.json" ]; then
    pass "install.sh created ~/.cursor/hooks.json"
else
    fail "install.sh did not create ~/.cursor/hooks.json" "" ""
fi

# --- Test 2: install.sh is idempotent (run twice → no duplicates) ---
echo "=== Test 2: install.sh idempotency (double install) ==="
INSTALL2_STDERR=$(bash "$HOOK_DIR/install.sh" \
    --secret-key "sk-lf-test-secret" \
    --public-key "pk-lf-test-public" \
    --base-url "http://test-langfuse:3000" \
    --user-id "test-user" \
    --tags "team:e2e" 2>&1 1>/dev/null) || {
    INSTALL2_RC=$?
    fail "second install.sh failed" "$INSTALL2_STDERR" "$INSTALL2_RC"
}

# Check that each event has exactly 1 langfuse entry
EVENTS="beforeSubmitPrompt afterAgentResponse afterAgentThought beforeShellExecution afterShellExecution beforeMCPExecution afterMCPExecution beforeReadFile afterFileEdit stop sessionStart"
IDEMPOTENT_OK=true
for event in $EVENTS; do
    COUNT=$(python3 -c "
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    arr = d.get(sys.argv[2], [])
    print(sum(1 for e in arr if isinstance(e, dict) and 'langfuse' in str(e.get('command', ''))))
except Exception as ex:
    print(f'error: {ex}', file=sys.stderr)
    print(0)
" "$HOME/.cursor/hooks.json" "$event" || echo "0")
    if [ "$COUNT" -ne 1 ]; then
        fail "event $event has $COUNT langfuse entries (expected 1)" "" ""
        IDEMPOTENT_OK=false
    fi
done
if [ "$IDEMPOTENT_OK" = "true" ]; then
    pass "double install produces exactly 1 langfuse entry per event"
fi

# --- Test 3: user-preset hook preserved ---
echo "=== Test 3: user-preset hook preservation ==="
# Uninstall first, then add a user hook, then reinstall
UNINSTALL_STDERR=$(bash "$HOOK_DIR/uninstall.sh" --purge 2>&1 1>/dev/null) || true

# Add a user-preset hook
python3 -c "
import json
d = {'afterFileEdit': [{'command': 'format.sh'}]}
with open('$HOME/.cursor/hooks.json', 'w') as f:
    json.dump(d, f, indent=2)
"

INSTALL3_STDERR=$(bash "$HOOK_DIR/install.sh" \
    --secret-key "sk-lf-test-secret" \
    --public-key "pk-lf-test-public" \
    --base-url "http://test-langfuse:3000" \
    --user-id "test-user" 2>&1 1>/dev/null) || true

# Check that format.sh is preserved and langfuse is appended
USER_HOOK_PRESERVED=$(python3 -c "
import json
try:
    d = json.load(open('$HOME/.cursor/hooks.json'))
    arr = d.get('afterFileEdit', [])
    has_format = any(e.get('command') == 'format.sh' for e in arr if isinstance(e, dict))
    has_langfuse = any('langfuse' in str(e.get('command', '')) for e in arr if isinstance(e, dict))
    print('yes' if has_format and has_langfuse else 'no')
except Exception as ex:
    print(f'error: {ex}', file=sys.stderr)
    print('no')
" || echo "no")

if [ "$USER_HOOK_PRESERVED" = "yes" ]; then
    pass "user-preset hook (format.sh) preserved, langfuse appended"
else
    fail "user-preset hook not preserved or langfuse not appended" "$INSTALL3_STDERR" ""
fi

# --- Test 4: cursor.env contents ---
echo "=== Test 4: cursor.env contains required vars ==="
ENV_FILE="$HOME/.agent-exporter-to-langfuse/config/cursor.env"
if [ -f "$ENV_FILE" ]; then
    if grep -q 'LANGSTASH_ENABLED="true"' "$ENV_FILE"; then
        pass "cursor.env contains LANGSTASH_ENABLED=true"
    else
        fail "cursor.env missing LANGSTASH_ENABLED=true" "" ""
    fi
    if grep -q 'LANGSTASH_URL="http://127.0.0.1:5288"' "$ENV_FILE"; then
        pass "cursor.env contains LANGSTASH_URL=http://127.0.0.1:5288"
    else
        fail "cursor.env missing LANGSTASH_URL" "" ""
    fi
else
    fail "cursor.env not found at $ENV_FILE" "" ""
fi

# --- Test 5: hook command uses absolute node path ---
echo "=== Test 5: hook command uses absolute node path ==="
NODE_CMD_OK=true
for event in $EVENTS; do
    CMD=$(python3 -c "
import json
try:
    d = json.load(open('$HOME/.cursor/hooks.json'))
    arr = d.get('$event', [])
    for e in arr:
        if isinstance(e, dict) and 'langfuse' in str(e.get('command', '')):
            print(e.get('command', ''))
            break
except Exception as ex:
    print(f'error: {ex}', file=sys.stderr)
" || echo "")
    if [ -z "$CMD" ]; then
        continue
    fi
    # Command should start with / (absolute path to node), not bare "node "
    if echo "$CMD" | grep -q '^"/'; then
        : # good, starts with quoted absolute path
    else
        fail "event $event command does not use absolute node path: $CMD" "" ""
        NODE_CMD_OK=false
    fi
done
if [ "$NODE_CMD_OK" = "true" ]; then
    pass "all langfuse hook commands use absolute node path"
fi

# --- Test 6: uninstall removes langfuse entries, preserves user hooks ---
echo "=== Test 6: uninstall preserves user hooks ==="
UNINSTALL2_STDERR=$(bash "$HOOK_DIR/uninstall.sh" --purge 2>&1 1>/dev/null) || true

# Check that format.sh is still there and langfuse is gone
USER_HOOK_AFTER=$(python3 -c "
import json
try:
    d = json.load(open('$HOME/.cursor/hooks.json'))
    arr = d.get('afterFileEdit', [])
    has_format = any(e.get('command') == 'format.sh' for e in arr if isinstance(e, dict))
    has_langfuse = any('langfuse' in str(e.get('command', '')) for e in arr if isinstance(e, dict))
    print('format_preserved' if has_format else 'format_gone')
    print('langfuse_gone' if not has_langfuse else 'langfuse_remains')
except Exception as ex:
    print(f'error: {ex}', file=sys.stderr)
    print('error')
" || echo "error")

if echo "$USER_HOOK_AFTER" | grep -q "format_preserved" && echo "$USER_HOOK_AFTER" | grep -q "langfuse_gone"; then
    pass "uninstall removes langfuse entries, preserves user hooks"
else
    fail "uninstall did not correctly clean up: $USER_HOOK_AFTER" "$UNINSTALL2_STDERR" ""
fi

# --- Summary ---
echo ""
echo "=== Summary ==="
echo "  Passed: $PASS_COUNT"
echo "  Failed: $FAIL_COUNT"

# Cleanup
rm -rf "$TEST_HOME"

if [ "$FAIL_COUNT" -gt 0 ]; then
    exit 1
fi
exit 0
