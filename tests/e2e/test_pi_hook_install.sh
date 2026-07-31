#!/usr/bin/env bash
set -euo pipefail

# E2E test for the Pi Langfuse hook install/uninstall behavior (R-7).
# Tests:
#   1. install.sh runs non-interactively with provided credentials
#   2. registration is idempotent — two installs leave exactly one packages entry
#   3. the hook directory holds the bundle plus a package.json declaring pi.extensions
#   4. pi.env holds the expected keys, the pi tag, LANGSTASH_ENABLED/URL
#   5. no full secret key is echoed to stdout/stderr (prefix only)
#   6. a preset npm:pi-langfuse entry produces a mutual-exclusion warning and install continues
#   7. uninstall.sh removes the packages entry and the hook directory but preserves pi.env
#
# Per AGENTS.md: stderr is never discarded; on failure the tested command's
# stderr and exit code are echoed as diagnostics.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
HOOK_DIR="$REPO_ROOT/hooks/pi"

SECRET_KEY="sk-lf-e2e-secret-do-not-log-0123456789"
PUBLIC_KEY="pk-lf-e2e-public-0123456789"
BASE_URL="https://langfuse.e2e.test"

# --- Sandbox HOME and a pi CLI stub on PATH -------------------------------
TEST_HOME="$(mktemp -d)"
STUB_BIN="$TEST_HOME/stub-bin"
export HOME="$TEST_HOME"
mkdir -p "$STUB_BIN" "$HOME/.pi/agent"
echo '{"packages": []}' > "$HOME/.pi/agent/settings.json"

# pi stub: install/remove mutate the stub settings.json packages array the same
# way the real CLI does (verified on this machine).
cat > "$STUB_BIN/pi" <<'PISTUB'
#!/usr/bin/env bash
set -euo pipefail
SETTINGS="$HOME/.pi/agent/settings.json"
cmd="${1:-}"
target="${2:-}"
case "$cmd" in
    install|remove)
        python3 - "$SETTINGS" "$cmd" "$target" <<'PY'
import json, sys
from pathlib import Path

path, cmd, target = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
try:
    data = json.loads(path.read_text())
except (OSError, ValueError):
    data = {}
packages = data.get("packages")
if not isinstance(packages, list):
    packages = []
if cmd == "install":
    if target not in packages:
        packages.append(target)
else:
    packages = [entry for entry in packages if entry != target]
data["packages"] = packages
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(data, indent=2))
PY
        echo "pi-stub: $cmd $target"
        ;;
    list)
        python3 -c "
import json, sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text())
for entry in data.get('packages', []):
    print(entry)
" "$SETTINGS"
        ;;
    *)
        echo "pi-stub: unsupported command '$cmd'" >&2
        exit 2
        ;;
esac
PISTUB
chmod +x "$STUB_BIN/pi"
export PATH="$STUB_BIN:$PATH"

SETTINGS_FILE="$HOME/.pi/agent/settings.json"
PI_HOOK_DEST="$HOME/.pi/hooks/langfuse"
ENV_FILE="$HOME/.agent-exporter-to-langfuse/config/pi.env"

cleanup() { rm -rf "$TEST_HOME"; }
trap cleanup EXIT

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

count_entries() {
    python3 -c "
import json, sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text())
target = sys.argv[2]
print(sum(1 for entry in data.get('packages', []) if str(entry) == target))
" "$SETTINGS_FILE" "$PI_HOOK_DEST"
}

# Ensure the bundle exists so install.sh does not have to build inside the test.
if [ ! -f "$HOOK_DIR/dist/index.mjs" ]; then
    echo "Building pi hook dist..."
    BUILD_STDERR=$( (cd "$HOOK_DIR" && npm install --ignore-scripts && npm run build) 2>&1 ) || {
        rc=$?
        echo "FAIL: pi hook build failed (exit $rc)" >&2
        echo "    stderr: $BUILD_STDERR" >&2
        exit 1
    }
fi

# --- Test 1: first install ------------------------------------------------
echo "=== Test 1: install.sh runs non-interactively with provided credentials ==="
INSTALL_OUT_1="$TEST_HOME/install-1.log"
if bash "$HOOK_DIR/install.sh" \
    --secret-key "$SECRET_KEY" \
    --public-key "$PUBLIC_KEY" \
    --base-url "$BASE_URL" \
    --tags "team:olap" \
    -y > "$INSTALL_OUT_1" 2>&1; then
    pass "install.sh exited 0"
else
    rc=$?
    fail "install.sh failed" "$(cat "$INSTALL_OUT_1")" "$rc"
fi

# --- Test 2: installed artifacts -----------------------------------------
echo "=== Test 2: hook directory holds the bundle and a pi.extensions package.json ==="
if [ -f "$PI_HOOK_DEST/index.mjs" ]; then
    pass "bundle copied to $PI_HOOK_DEST/index.mjs"
else
    fail "bundle missing at $PI_HOOK_DEST/index.mjs" "$(ls -la "$PI_HOOK_DEST" 2>&1 || true)"
fi

if [ -f "$PI_HOOK_DEST/package.json" ]; then
    EXT_ENTRY=$(python3 -c "
import json, sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text())
print((data.get('pi') or {}).get('extensions', []))
" "$PI_HOOK_DEST/package.json")
    case "$EXT_ENTRY" in
        *index.mjs*) pass "package.json declares pi.extensions -> $EXT_ENTRY" ;;
        *) fail "package.json pi.extensions unexpected: $EXT_ENTRY" ;;
    esac
else
    fail "package.json missing at $PI_HOOK_DEST/package.json" "$(ls -la "$PI_HOOK_DEST" 2>&1 || true)"
fi

# --- Test 3: pi.env contents ---------------------------------------------
echo "=== Test 3: pi.env holds the expected configuration ==="
if [ -f "$ENV_FILE" ]; then
    ENV_CONTENT=$(cat "$ENV_FILE")
    ENV_OK=true
    for expected in \
        "export LANGFUSE_BASE_URL=\"$BASE_URL\"" \
        "export LANGFUSE_PUBLIC_KEY=\"$PUBLIC_KEY\"" \
        "export LANGFUSE_SECRET_KEY=\"$SECRET_KEY\"" \
        "export LANGSTASH_ENABLED=\"true\"" \
        "export LANGSTASH_URL=\"http://127.0.0.1:5288\""
    do
        if ! printf '%s\n' "$ENV_CONTENT" | grep -qF "$expected"; then
            ENV_OK=false
            fail "pi.env missing line: $expected" "$ENV_CONTENT"
        fi
    done
    if printf '%s\n' "$ENV_CONTENT" | grep -qE '^export LANGFUSE_TAGS="pi(,|")'; then
        pass "pi.env LANGFUSE_TAGS starts with the fixed pi tag"
    else
        fail "pi.env LANGFUSE_TAGS missing the pi tag" "$ENV_CONTENT"
    fi
    if [ "$ENV_OK" = true ]; then
        pass "pi.env contains base url, credentials and langstash settings"
    fi
else
    fail "pi.env not created at $ENV_FILE" "$(ls -la "$(dirname "$ENV_FILE")" 2>&1 || true)"
fi

# --- Test 4: no full secret in the output --------------------------------
echo "=== Test 4: installer output never contains the full secret key ==="
if grep -qF "$SECRET_KEY" "$INSTALL_OUT_1"; then
    fail "installer output leaked the full secret key" "$(grep -nF "$SECRET_KEY" "$INSTALL_OUT_1")"
else
    if grep -qF "${SECRET_KEY:0:12}" "$INSTALL_OUT_1"; then
        pass "installer output shows only the 12-char secret prefix"
    else
        fail "installer output does not show the expected secret prefix" "$(cat "$INSTALL_OUT_1")"
    fi
fi

# --- Test 5: idempotent second install -----------------------------------
echo "=== Test 5: a second install leaves exactly one packages entry ==="
INSTALL_OUT_2="$TEST_HOME/install-2.log"
if bash "$HOOK_DIR/install.sh" \
    --secret-key "$SECRET_KEY" \
    --public-key "$PUBLIC_KEY" \
    --base-url "$BASE_URL" \
    --tags "team:olap" \
    -y > "$INSTALL_OUT_2" 2>&1; then
    ENTRY_COUNT=$(count_entries)
    if [ "$ENTRY_COUNT" = "1" ]; then
        pass "packages holds exactly one entry for $PI_HOOK_DEST"
    else
        fail "packages entry count is $ENTRY_COUNT, expected 1" "$(cat "$SETTINGS_FILE")"
    fi
    ENV_LINE_COUNT=$(grep -c '^export LANGFUSE_BASE_URL=' "$ENV_FILE")
    if [ "$ENV_LINE_COUNT" = "1" ]; then
        pass "pi.env is rewritten, not appended to"
    else
        fail "pi.env has $ENV_LINE_COUNT LANGFUSE_BASE_URL lines, expected 1" "$(cat "$ENV_FILE")"
    fi
else
    rc=$?
    fail "second install.sh failed" "$(cat "$INSTALL_OUT_2")" "$rc"
fi

# --- Test 6: mutual exclusion warning ------------------------------------
echo "=== Test 6: a preset npm:pi-langfuse entry warns and continues in -y mode ==="
python3 -c "
import json, sys
from pathlib import Path
path = Path(sys.argv[1])
data = json.loads(path.read_text())
packages = data.get('packages', [])
if 'npm:pi-langfuse' not in packages:
    packages.insert(0, 'npm:pi-langfuse')
data['packages'] = packages
path.write_text(json.dumps(data, indent=2))
" "$SETTINGS_FILE"

INSTALL_OUT_3="$TEST_HOME/install-3.log"
if bash "$HOOK_DIR/install.sh" \
    --secret-key "$SECRET_KEY" \
    --public-key "$PUBLIC_KEY" \
    --base-url "$BASE_URL" \
    -y > "$INSTALL_OUT_3" 2>&1; then
    if grep -q "pi-langfuse extension is registered" "$INSTALL_OUT_3"; then
        pass "installer warns about the npm pi-langfuse extension"
    else
        fail "installer printed no mutual-exclusion warning" "$(cat "$INSTALL_OUT_3")"
    fi
    if grep -q "Installation complete" "$INSTALL_OUT_3"; then
        pass "installer continues after the warning in -y mode"
    else
        fail "installer did not complete in -y mode" "$(cat "$INSTALL_OUT_3")"
    fi
else
    rc=$?
    fail "install.sh with preset npm:pi-langfuse failed" "$(cat "$INSTALL_OUT_3")" "$rc"
fi

# --- Test 7: uninstall ----------------------------------------------------
echo "=== Test 7: uninstall.sh unregisters and removes the hook, keeps pi.env ==="
UNINSTALL_OUT="$TEST_HOME/uninstall.log"
if bash "$HOOK_DIR/uninstall.sh" > "$UNINSTALL_OUT" 2>&1; then
    ENTRY_COUNT=$(count_entries)
    if [ "$ENTRY_COUNT" = "0" ]; then
        pass "packages entry removed"
    else
        fail "packages still has $ENTRY_COUNT entries for the hook" "$(cat "$SETTINGS_FILE")"
    fi

    if [ ! -d "$PI_HOOK_DEST" ]; then
        pass "hook directory removed"
    else
        fail "hook directory still present" "$(ls -la "$PI_HOOK_DEST" 2>&1 || true)"
    fi

    if [ -f "$ENV_FILE" ]; then
        pass "pi.env preserved for reinstall"
    else
        fail "pi.env was removed without --purge" "$(cat "$UNINSTALL_OUT")"
    fi

    if grep -qF "npm:pi-langfuse" "$SETTINGS_FILE"; then
        pass "unrelated packages entries are preserved"
    else
        fail "uninstall removed an unrelated packages entry" "$(cat "$SETTINGS_FILE")"
    fi

    # Second uninstall must stay idempotent.
    if bash "$HOOK_DIR/uninstall.sh" >> "$UNINSTALL_OUT" 2>&1; then
        pass "second uninstall.sh is idempotent"
    else
        rc=$?
        fail "second uninstall.sh failed" "$(cat "$UNINSTALL_OUT")" "$rc"
    fi
else
    rc=$?
    fail "uninstall.sh failed" "$(cat "$UNINSTALL_OUT")" "$rc"
fi

echo ""
echo "=== Summary ==="
echo "  Passed: $PASS_COUNT"
echo "  Failed: $FAIL_COUNT"

if [ "$FAIL_COUNT" -gt 0 ]; then
    exit 1
fi
exit 0
