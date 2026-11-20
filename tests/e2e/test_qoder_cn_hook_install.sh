#!/usr/bin/env bash
set -euo pipefail

# E2E test for the qoder-cn Langfuse hook install/uninstall lifecycle (R-2, R-3).
#
# Every case runs against a sandbox HOME (mktemp) with placeholder credentials
# (sk-lf-e2e-* / pk-lf-e2e-*) and an unreachable base URL, so the suite never
# contacts a real Langfuse, never touches the real ~/.qoder-cn and never
# touches launchd. install.sh does run `uv sync` inside the sandbox HOME.
#
# Cases:
#   Section A — full lifecycle with third-party registrations and cross-product
#   sentinels preset:
#     1. fresh install: the four artifact kinds land in
#        ~/.qoder-cn/hooks/langfuse/ (langfuse_hook.py, langfuse-entrypoint.sh,
#        langstash_deliver/, pyproject.toml) with byte-identical content, Stop
#        and SubagentStop each hold exactly one langfuse registration pointing
#        at the entrypoint, qoder-cn.env holds LANGFUSE_* + LANGSTASH_* with
#        the default tag qoder-cn, and the output only shows the 12-char
#        secret prefix.
#     2. repeat install with the Overwrite confirmation answered y (piped to
#        stdin): registrations are not duplicated, installed files are updated
#        with no old residue.
#     3. repeat install with the Overwrite confirmation answered n: the hook
#        script copy is skipped (old file content kept), registration still not
#        duplicated.
#     4. uninstall: hook dir, langfuse registrations and ~/.qoder-cn/state
#        langfuse files removed; third-party registrations preserved;
#        qoder-cn.env preserved; qoder/qoderwork sentinels unchanged.
#     5. clean-state (already uninstalled) uninstall: exit 0, no side effects.
#     6. reinstall via --upgrade -y: env credentials are reused and the full
#        installation is restored.
#   Section B — purge semantics when no other product env file remains:
#     7. uninstall on a never-installed sandbox: exit 0, nothing created.
#     8. uninstall --purge: env file removed, empty config dir removed, shell
#        rc loader line cleaned while unrelated rc content is preserved.
#   Section C — purge semantics while another product env file remains:
#     9. uninstall --purge: only qoder-cn.env is removed; the other product's
#        env file, the config dir and the shell rc loader line are kept.
#
# Per AGENTS.md: stderr is never discarded; on failure the tested command's
# output and exit code are echoed as diagnostics.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
HOOK_SRC_DIR="$REPO_ROOT/hooks/qoder-cn"
SOURCE_HOOK="$HOOK_SRC_DIR/hooks/langfuse_hook.py"
SOURCE_RUNNER="$HOOK_SRC_DIR/hooks/langfuse-entrypoint.sh"
SOURCE_PYPROJECT="$HOOK_SRC_DIR/hooks/pyproject.toml"
SOURCE_LANGSTASH="$REPO_ROOT/hooks/langstash-deliver/python/langstash_deliver"

# Hermetic isolation: install.sh falls back to LANGFUSE_* environment
# variables (the first credential branch), so credentials from the host shell
# must never leak into the sandbox runs.
unset LANGFUSE_SECRET_KEY LANGFUSE_PUBLIC_KEY LANGFUSE_BASE_URL \
    LANGFUSE_USER_ID LANGFUSE_TAGS || true

SECRET_KEY="sk-lf-e2e-secret-do-not-log-0123456789"
PUBLIC_KEY="pk-lf-e2e-public-0123456789"
BASE_URL="https://langfuse.e2e.test"
USER_ID="e2e-user"
EXTRA_TAGS="team:olap"
ENTRYPOINT_CMD="~/.qoder-cn/hooks/langfuse/langfuse-entrypoint.sh"

INSTALL_ARGS=(--secret-key "$SECRET_KEY" --public-key "$PUBLIC_KEY" --base-url "$BASE_URL" --user-id "$USER_ID" --tags "$EXTRA_TAGS")

# Test-scoped work dir: holds install logs and the uv package cache shared by
# all sandbox HOMEs (still fully sandboxed — never the real HOME).
WORK_DIR="$(mktemp -d)"
SANDBOX_DIRS=("$WORK_DIR")
LOG_DIR="$WORK_DIR/logs"
mkdir -p "$LOG_DIR"
export UV_CACHE_DIR="$WORK_DIR/uv-cache"

cleanup() {
    for d in "${SANDBOX_DIRS[@]}"; do
        rm -rf "$d"
    done
}
trap cleanup EXIT

PASS_COUNT=0
FAIL_COUNT=0

pass() { echo "  PASS: $1"; PASS_COUNT=$((PASS_COUNT + 1)); }
fail() {
    echo "  FAIL: $1" >&2
    shift
    for diag in "$@"; do
        echo "    $diag" >&2
    done
    FAIL_COUNT=$((FAIL_COUNT + 1))
}

# assert_eq <actual> <expected> <business-semantic description> [diagnostic...]
assert_eq() {
    if [ "$1" = "$2" ]; then
        pass "$3"
    else
        fail "$3" "actual: $1" "expected: $2" "${@:4}"
    fi
}

# assert_contains <haystack-file> <needle> <description> [diagnostic...]
assert_contains() {
    if grep -qF -e "$2" "$1"; then
        pass "$3"
    else
        fail "$3" "missing: $2" "in: $1" "content: $(cat "$1")" "${@:4}"
    fi
}

# assert_not_contains <haystack-file> <needle> <description> [diagnostic...]
assert_not_contains() {
    if grep -qF -e "$2" "$1"; then
        fail "$3" "unexpectedly found: $2" "in: $1" "content: $(cat "$1")" "${@:4}"
    else
        pass "$3"
    fi
}

# --- prerequisites ---------------------------------------------------------
for tool in python3 uv; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "FAIL: prerequisite '$tool' not found on PATH (install.sh requires it)" >&2
        exit 1
    fi
done
for f in "$HOOK_SRC_DIR/install.sh" "$HOOK_SRC_DIR/uninstall.sh" \
    "$SOURCE_HOOK" "$SOURCE_RUNNER" "$SOURCE_PYPROJECT"; do
    if [ ! -f "$f" ]; then
        echo "FAIL: required source file missing: $f" >&2
        exit 1
    fi
done
if [ ! -d "$SOURCE_LANGSTASH" ]; then
    echo "FAIL: langstash-deliver source missing: $SOURCE_LANGSTASH" >&2
    exit 1
fi

# --- settings.json query helper -------------------------------------------
# settings_query <mode> <settings.json> <event>
#   count-langfuse      -> number of matcher entries whose command contains 'langfuse'
#   langfuse-commands   -> one langfuse command per line
#   count-third-party   -> number of matcher entries without a langfuse command
#   third-party-commands-> one third-party command per line
settings_query() {
    python3 - "$1" "$2" "$3" <<'PY'
import json, sys

mode, path, event = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    with open(path) as f:
        settings = json.load(f)
except (OSError, ValueError) as exc:
    print("error: cannot read settings %s: %s" % (path, exc))
    sys.exit(3)
entries = settings.get('hooks', {}).get(event, [])
def is_langfuse(matcher):
    return any('langfuse' in h.get('command', '') for h in matcher.get('hooks', []))
if mode == 'count-langfuse':
    print(sum(1 for m in entries if is_langfuse(m)))
elif mode == 'langfuse-commands':
    for m in entries:
        if is_langfuse(m):
            for h in m.get('hooks', []):
                print(h.get('command', ''))
elif mode == 'count-third-party':
    print(sum(1 for m in entries if not is_langfuse(m)))
elif mode == 'third-party-commands':
    for m in entries:
        if not is_langfuse(m):
            for h in m.get('hooks', []):
                print(h.get('command', ''))
else:
    print("error: unknown mode %s" % mode)
    sys.exit(3)
PY
}

# --- sandbox / preset helpers ----------------------------------------------
# new_sandbox: switch HOME to a fresh mktemp dir and force uninstall.sh to
# resolve the shell rc to ~/.profile regardless of the host login shell.
new_sandbox() {
    local sb
    sb="$(mktemp -d)"
    SANDBOX_DIRS+=("$sb")
    HOME="$sb"
    export HOME
    export SHELL=/bin/bash
    unset ZSH_VERSION || true
    SETTINGS_FILE="$HOME/.qoder-cn/settings.json"
    HOOK_DIR="$HOME/.qoder-cn/hooks/langfuse"
    ENV_FILE="$HOME/.agent-exporter-to-langfuse/config/qoder-cn.env"
}

preset_third_party_settings() {
    # qoder-cn settings.json with third-party Stop/SubagentStop hook
    # registrations (mirrors the real qoder-cn settings.json which has
    # third-party Stop hooks like loongsuite-pilot).
    mkdir -p "$HOME/.qoder-cn"
    python3 - "$SETTINGS_FILE" <<'PY'
import json, sys

settings = {
    "hooks": {
        "Stop": [
            {"hooks": [{"type": "command", "command": "loongsuite-pilot --on-stop", "timeout": 10}]},
            {"matcher": "subagent", "hooks": [{"type": "command", "command": "third-party-subagent-stop", "timeout": 15}]},
        ],
        "SubagentStop": [
            {"hooks": [{"type": "command", "command": "third-party-subagentstop-hook", "timeout": 30}]},
        ],
    }
}
with open(sys.argv[1], 'w') as f:
    json.dump(settings, f, indent=2)
PY
}

preset_cross_product_sentinels() {
    # qoder (international) and qoderwork user-side state that the qoder-cn
    # installer/uninstaller must never read or modify.
    mkdir -p "$HOME/.qoder/hooks/langfuse" "$HOME/.qoderwork/hooks/langfuse" \
        "$HOME/.agent-exporter-to-langfuse/config"
    cat > "$HOME/.qoder/settings.json" <<'EOF'
{"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "~/.qoder/hooks/langfuse/langfuse-entrypoint.sh", "timeout": 20}]}]}}
EOF
    echo "qoder sentinel entrypoint v1" > "$HOME/.qoder/hooks/langfuse/langfuse-entrypoint.sh"
    printf 'export LANGFUSE_BASE_URL="https://qoder.e2e.test"\n' > "$HOME/.agent-exporter-to-langfuse/config/qoder.env"
    cat > "$HOME/.qoderwork/settings.json" <<'EOF'
{"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "~/.qoderwork/hooks/langfuse/langfuse-entrypoint.sh", "timeout": 20}]}]}}
EOF
    echo "qoderwork sentinel hook v1" > "$HOME/.qoderwork/hooks/langfuse/langfuse_hook.py"
    printf 'export LANGFUSE_BASE_URL="https://qoderwork.e2e.test"\n' > "$HOME/.agent-exporter-to-langfuse/config/qoderwork.env"
    QODER_SNAPSHOT="$(snapshot_tree "$HOME/.qoder")"
    QODERWORK_SNAPSHOT="$(snapshot_tree "$HOME/.qoderwork")"
    QODER_ENV_CKSUM="$(cksum_of "$HOME/.agent-exporter-to-langfuse/config/qoder.env")"
    QODERWORK_ENV_CKSUM="$(cksum_of "$HOME/.agent-exporter-to-langfuse/config/qoderwork.env")"
}

preset_state_files() {
    mkdir -p "$HOME/.qoder-cn/state"
    echo "old hook log line" > "$HOME/.qoder-cn/state/langfuse_hook.log"
    echo "rotated log line" > "$HOME/.qoder-cn/state/langfuse_hook.log.1"
    printf '{}\n' > "$HOME/.qoder-cn/state/langfuse_state.json"
    printf 'lock\n' > "$HOME/.qoder-cn/state/langfuse_state.lock"
    echo "other product state" > "$HOME/.qoder-cn/state/other-product-state.json"
}

# snapshot_tree <dir>: "<cksum> <size> <path>" for every file under dir, sorted.
snapshot_tree() {
    local root="$1"
    if [ ! -d "$root" ]; then
        echo "<absent>"
        return
    fi
    (cd "$root" && find . -type f | LC_ALL=C sort | while IFS= read -r f; do
        cksum "$f"
    done)
}

cksum_of() {
    if [ -f "$1" ]; then
        cksum "$1"
    else
        echo "<missing>"
    fi
}

# check_sentinels_unchanged <when>: compare qoder/qoderwork trees and env
# files against the snapshots taken at preset time.
check_sentinels_unchanged() {
    local when="$1"
    local cur
    cur="$(snapshot_tree "$HOME/.qoder")"
    if [ "$cur" = "$QODER_SNAPSHOT" ]; then
        pass "qoder user-side state unchanged $when"
    else
        fail "qoder user-side state changed $when" \
            "before: $QODER_SNAPSHOT" "after: $cur"
    fi
    cur="$(snapshot_tree "$HOME/.qoderwork")"
    if [ "$cur" = "$QODERWORK_SNAPSHOT" ]; then
        pass "qoderwork user-side state unchanged $when"
    else
        fail "qoderwork user-side state changed $when" \
            "before: $QODERWORK_SNAPSHOT" "after: $cur"
    fi
    cur="$(cksum_of "$HOME/.agent-exporter-to-langfuse/config/qoder.env")"
    if [ "$cur" = "$QODER_ENV_CKSUM" ]; then
        pass "qoder env file unchanged $when"
    else
        fail "qoder env file changed $when" "before: $QODER_ENV_CKSUM" "after: $cur"
    fi
    cur="$(cksum_of "$HOME/.agent-exporter-to-langfuse/config/qoderwork.env")"
    if [ "$cur" = "$QODERWORK_ENV_CKSUM" ]; then
        pass "qoderwork env file unchanged $when"
    else
        fail "qoderwork env file changed $when" "before: $QODERWORK_ENV_CKSUM" "after: $cur"
    fi
}

preset_shell_loader() {
    # Historical profile loader written by older installers; the qoder-family
    # uninstallers clean it when the config dir is emptied.
    cat > "$HOME/.profile" <<'EOF'
# e2e rc preamble
export E2E_RC_MARKER=1

# Agent Langfuse Exporters
for f in "$HOME"/.agent-exporter-to-langfuse/config/*.env; do [ -f "$f" ] && . "$f"; done
# e2e rc postscript
EOF
}

# --- install/uninstall runners ---------------------------------------------
INSTALL_RC=0
# run_install <stdin-answer-or-empty> <log-file> [extra install.sh args...]
run_install() {
    local answer="${1:-}"
    shift
    local log="$1"
    shift
    set +e
    if [ -n "$answer" ]; then
        printf '%s\n' "$answer" | bash "$HOOK_SRC_DIR/install.sh" "$@" > "$log" 2>&1
    else
        bash "$HOOK_SRC_DIR/install.sh" "$@" < /dev/null > "$log" 2>&1
    fi
    INSTALL_RC=$?
    set -e
}

UNINSTALL_RC=0
# run_uninstall <log-file> [uninstall.sh args...]
run_uninstall() {
    local log="$1"
    shift
    set +e
    bash "$HOOK_SRC_DIR/uninstall.sh" "$@" < /dev/null > "$log" 2>&1
    UNINSTALL_RC=$?
    set -e
}

install_cmd_desc() {
    echo "bash $HOOK_SRC_DIR/install.sh --secret-key <sk> --public-key <pk> --base-url $BASE_URL --user-id $USER_ID --tags $EXTRA_TAGS"
}

# =============================================================================
# Section A: full lifecycle with third-party registrations and cross-product
# sentinels
# =============================================================================
echo "=== Section A: install / repeat install / uninstall / reinstall ==="
new_sandbox
preset_third_party_settings
preset_cross_product_sentinels

# --- A1: fresh install -----------------------------------------------------
echo "--- A1: fresh install with argument credentials ---"
LOG_A1="$LOG_DIR/install-a1.log"
run_install "" "$LOG_A1" "${INSTALL_ARGS[@]}"
if [ "$INSTALL_RC" -eq 0 ]; then
    pass "install.sh exited 0 on a fresh sandbox"
else
    fail "install.sh failed on a fresh sandbox" \
        "command: $(install_cmd_desc)" \
        "exit code: $INSTALL_RC" \
        "output: $(cat "$LOG_A1")"
fi

# Four artifact kinds.
if [ -f "$HOOK_DIR/langfuse_hook.py" ] && [ -f "$HOOK_DIR/langfuse-entrypoint.sh" ] \
    && [ -f "$HOOK_DIR/pyproject.toml" ] && [ -d "$HOOK_DIR/langstash_deliver" ]; then
    pass "all four artifact kinds present in $HOOK_DIR"
else
    fail "hook directory is missing artifacts" \
        "listing: $(ls -la "$HOOK_DIR" 2>&1 || true)"
fi
if cmp -s "$HOOK_DIR/langfuse_hook.py" "$SOURCE_HOOK"; then
    pass "langfuse_hook.py copied byte-identical"
else
    fail "langfuse_hook.py differs from the repo source" \
        "diff: $(diff "$HOOK_DIR/langfuse_hook.py" "$SOURCE_HOOK" 2>&1 | head -20)"
fi
if cmp -s "$HOOK_DIR/langfuse-entrypoint.sh" "$SOURCE_RUNNER"; then
    pass "langfuse-entrypoint.sh copied byte-identical"
else
    fail "langfuse-entrypoint.sh differs from the repo source" \
        "diff: $(diff "$HOOK_DIR/langfuse-entrypoint.sh" "$SOURCE_RUNNER" 2>&1 | head -20)"
fi
if cmp -s "$HOOK_DIR/pyproject.toml" "$SOURCE_PYPROJECT"; then
    pass "pyproject.toml copied byte-identical"
else
    fail "pyproject.toml differs from the repo source" \
        "diff: $(diff "$HOOK_DIR/pyproject.toml" "$SOURCE_PYPROJECT" 2>&1 | head -20)"
fi
if [ -x "$HOOK_DIR/langfuse-entrypoint.sh" ]; then
    pass "langfuse-entrypoint.sh is executable"
else
    fail "langfuse-entrypoint.sh is not executable" \
        "listing: $(ls -la "$HOOK_DIR" 2>&1 || true)"
fi
if diff -r "$HOOK_DIR/langstash_deliver" "$SOURCE_LANGSTASH" > /dev/null 2>&1; then
    pass "langstash_deliver copied byte-identical"
else
    fail "langstash_deliver copy differs from the repo source" \
        "diff: $(diff -r "$HOOK_DIR/langstash_deliver" "$SOURCE_LANGSTASH" 2>&1 | head -20)"
fi

# Registration: exactly one langfuse entry per event, pointing at the entrypoint.
STOP_COUNT=$(settings_query count-langfuse "$SETTINGS_FILE" Stop)
assert_eq "$STOP_COUNT" "1" \
    "Stop holds exactly one langfuse registration after fresh install" \
    "settings.json: $(cat "$SETTINGS_FILE")"
SUBAGENTSTOP_COUNT=$(settings_query count-langfuse "$SETTINGS_FILE" SubagentStop)
assert_eq "$SUBAGENTSTOP_COUNT" "1" \
    "SubagentStop holds exactly one langfuse registration after fresh install" \
    "settings.json: $(cat "$SETTINGS_FILE")"
STOP_CMD=$(settings_query langfuse-commands "$SETTINGS_FILE" Stop)
assert_eq "$STOP_CMD" "$ENTRYPOINT_CMD" \
    "Stop langfuse registration points at the qoder-cn entrypoint" \
    "settings.json: $(cat "$SETTINGS_FILE")"
SUBAGENTSTOP_CMD=$(settings_query langfuse-commands "$SETTINGS_FILE" SubagentStop)
assert_eq "$SUBAGENTSTOP_CMD" "$ENTRYPOINT_CMD" \
    "SubagentStop langfuse registration points at the qoder-cn entrypoint" \
    "settings.json: $(cat "$SETTINGS_FILE")"

# Third-party registrations preserved by the installer.
assert_eq "$(settings_query count-third-party "$SETTINGS_FILE" Stop)" "2" \
    "third-party Stop registrations preserved after install" \
    "settings.json: $(cat "$SETTINGS_FILE")"
assert_eq "$(settings_query count-third-party "$SETTINGS_FILE" SubagentStop)" "1" \
    "third-party SubagentStop registrations preserved after install" \
    "settings.json: $(cat "$SETTINGS_FILE")"
assert_contains "$SETTINGS_FILE" "loongsuite-pilot --on-stop" \
    "loongsuite third-party Stop registration preserved"

# Env file contents.
ENV_CONTENT=""
if [ -f "$ENV_FILE" ]; then
    pass "env file created at $ENV_FILE"
    ENV_CONTENT="$(cat "$ENV_FILE")"
else
    fail "env file missing at $ENV_FILE" \
        "listing: $(ls -la "$(dirname "$ENV_FILE")" 2>&1 || true)"
fi
for expected_line in \
    "export LANGFUSE_BASE_URL=\"$BASE_URL\"" \
    "export LANGFUSE_PUBLIC_KEY=\"$PUBLIC_KEY\"" \
    "export LANGFUSE_SECRET_KEY=\"$SECRET_KEY\"" \
    "export LANGFUSE_USER_ID=\"$USER_ID\"" \
    "export LANGSTASH_ENABLED=\"true\"" \
    "export LANGSTASH_URL=\"http://127.0.0.1:5288\""; do
    if printf '%s\n' "$ENV_CONTENT" | grep -qF "$expected_line"; then
        pass "env file holds $expected_line"
    else
        fail "env file is missing line: $expected_line" "content: $ENV_CONTENT"
    fi
done
if printf '%s\n' "$ENV_CONTENT" | grep -qE '^export LANGFUSE_TAGS="qoder-cn(,|")'; then
    pass "env file LANGFUSE_TAGS starts with the default qoder-cn tag"
else
    fail "env file LANGFUSE_TAGS missing the qoder-cn tag" "content: $ENV_CONTENT"
fi
if printf '%s\n' "$ENV_CONTENT" | grep -qF 'export LANGFUSE_TAGS="qoder-cn,team:olap"'; then
    pass "env file LANGFUSE_TAGS merges default tag with user tags"
else
    fail "env file LANGFUSE_TAGS does not merge default and user tags" "content: $ENV_CONTENT"
fi

# Secret prefix only.
if grep -qF "$SECRET_KEY" "$LOG_A1"; then
    fail "install output leaked the full secret key" \
        "leak: $(grep -nF "$SECRET_KEY" "$LOG_A1")"
else
    pass "install output does not contain the full secret key"
fi
if grep -qF "${SECRET_KEY:0:12}" "$LOG_A1"; then
    pass "install output shows only the 12-char secret prefix"
else
    fail "install output does not show the 12-char secret prefix" \
        "output: $(cat "$LOG_A1")"
fi

# Completion prompt is product-adapted.
assert_contains "$LOG_A1" "Restart Qoder CN to start tracing." \
    "install completion prompt mentions Qoder CN"

check_sentinels_unchanged "after fresh install"

# --- A2: repeat install, confirm overwrite --------------------------------
echo "--- A2: repeat install with Overwrite confirmed (y) ---"
echo "# E2E OLD VERSION MARKER" >> "$HOOK_DIR/langfuse_hook.py"
echo "# E2E OLD ENTRYPOINT MARKER" >> "$HOOK_DIR/langfuse-entrypoint.sh"
echo "stale" > "$HOOK_DIR/langstash_deliver/STALE-E2E-FILE"

LOG_A2="$LOG_DIR/install-a2.log"
run_install "y" "$LOG_A2" "${INSTALL_ARGS[@]}"
if [ "$INSTALL_RC" -eq 0 ]; then
    pass "repeat install.sh exited 0 after confirming overwrite"
else
    fail "repeat install.sh failed after confirming overwrite" \
        "command: $(install_cmd_desc) (stdin: y)" \
        "exit code: $INSTALL_RC" \
        "output: $(cat "$LOG_A2")"
fi
# NOTE: bash renders the `read -p` prompt ("Overwrite? [y/N]") only when stdin
# is a terminal, so a piped-stdin E2E cannot observe the prompt text itself;
# the confirmation branch is verified through its warning line plus the two
# observable outcomes (copy skipped / hook updated) driven by the piped
# answer.
assert_contains "$LOG_A2" "Hook script already exists at" \
    "repeat install enters the pre-existing hook branch and asks for Overwrite"
assert_contains "$LOG_A2" "Hook script updated." \
    "answering y to the Overwrite prompt copies the hook files"
assert_not_contains "$HOOK_DIR/langfuse_hook.py" "E2E OLD VERSION MARKER" \
    "confirmed overwrite leaves no old-version residue in langfuse_hook.py"
assert_not_contains "$HOOK_DIR/langfuse-entrypoint.sh" "E2E OLD ENTRYPOINT MARKER" \
    "confirmed overwrite leaves no old-version residue in the entrypoint"
if cmp -s "$HOOK_DIR/langfuse_hook.py" "$SOURCE_HOOK"; then
    pass "langfuse_hook.py updated to the current source after overwrite"
else
    fail "langfuse_hook.py not updated to the current source after overwrite" \
        "diff: $(diff "$HOOK_DIR/langfuse_hook.py" "$SOURCE_HOOK" 2>&1 | head -20)"
fi
if [ -f "$HOOK_DIR/langstash_deliver/STALE-E2E-FILE" ]; then
    fail "langstash_deliver still holds stale files after reinstall" \
        "listing: $(ls -la "$HOOK_DIR/langstash_deliver" 2>&1 || true)"
else
    pass "langstash_deliver holds no stale files after reinstall"
fi
assert_eq "$(settings_query count-langfuse "$SETTINGS_FILE" Stop)" "1" \
    "Stop registration is not duplicated by repeat install" \
    "settings.json: $(cat "$SETTINGS_FILE")"
assert_eq "$(settings_query count-langfuse "$SETTINGS_FILE" SubagentStop)" "1" \
    "SubagentStop registration is not duplicated by repeat install" \
    "settings.json: $(cat "$SETTINGS_FILE")"
assert_eq "$(grep -c '^export LANGFUSE_BASE_URL=' "$ENV_FILE")" "1" \
    "env file is rewritten, not appended to, by repeat install" \
    "content: $(cat "$ENV_FILE")"
check_sentinels_unchanged "after repeat install (confirm)"

# --- A3: repeat install, decline overwrite --------------------------------
echo "--- A3: repeat install with Overwrite declined (n) ---"
echo "# E2E OLD VERSION MARKER 2" >> "$HOOK_DIR/langfuse_hook.py"
echo "# E2E OLD ENTRYPOINT MARKER 2" >> "$HOOK_DIR/langfuse-entrypoint.sh"

LOG_A3="$LOG_DIR/install-a3.log"
run_install "n" "$LOG_A3" "${INSTALL_ARGS[@]}"
if [ "$INSTALL_RC" -eq 0 ]; then
    pass "repeat install.sh exited 0 after declining overwrite"
else
    fail "repeat install.sh failed after declining overwrite" \
        "command: $(install_cmd_desc) (stdin: n)" \
        "exit code: $INSTALL_RC" \
        "output: $(cat "$LOG_A3")"
fi
assert_contains "$LOG_A3" "Hook script already exists at" \
    "pre-existing hook install enters the Overwrite confirmation branch"
assert_contains "$LOG_A3" "Skipped hook script copy." \
    "answering n to the Overwrite prompt skips the hook script copy"
assert_contains "$HOOK_DIR/langfuse_hook.py" "E2E OLD VERSION MARKER 2" \
    "declining the Overwrite prompt keeps the existing hook file content"
assert_contains "$HOOK_DIR/langfuse-entrypoint.sh" "E2E OLD ENTRYPOINT MARKER 2" \
    "declining the Overwrite prompt keeps the existing entrypoint content"
assert_eq "$(settings_query count-langfuse "$SETTINGS_FILE" Stop)" "1" \
    "Stop registration is not duplicated when the copy is declined" \
    "settings.json: $(cat "$SETTINGS_FILE")"
assert_eq "$(settings_query count-langfuse "$SETTINGS_FILE" SubagentStop)" "1" \
    "SubagentStop registration is not duplicated when the copy is declined" \
    "settings.json: $(cat "$SETTINGS_FILE")"
check_sentinels_unchanged "after repeat install (decline)"

# Restore the pristine hook files so later uninstall/reinstall steps work
# from a normal installed state.
cp "$SOURCE_HOOK" "$HOOK_DIR/langfuse_hook.py"
cp "$SOURCE_RUNNER" "$HOOK_DIR/langfuse-entrypoint.sh"
chmod +x "$HOOK_DIR/langfuse-entrypoint.sh"

# --- A4: uninstall (default, no purge) -------------------------------------
echo "--- A4: uninstall removes the hook and registrations, keeps env ---"
preset_state_files
ENV_CKSUM_BEFORE="$(cksum_of "$ENV_FILE")"

LOG_A4="$LOG_DIR/uninstall-a4.log"
run_uninstall "$LOG_A4"
if [ "$UNINSTALL_RC" -eq 0 ]; then
    pass "uninstall.sh exited 0"
else
    fail "uninstall.sh failed" \
        "command: bash $HOOK_SRC_DIR/uninstall.sh" \
        "exit code: $UNINSTALL_RC" \
        "output: $(cat "$LOG_A4")"
fi
if [ ! -d "$HOOK_DIR" ]; then
    pass "uninstall removes the hook directory"
else
    fail "hook directory still present after uninstall" \
        "listing: $(ls -la "$HOOK_DIR" 2>&1 || true)"
fi
assert_eq "$(settings_query count-langfuse "$SETTINGS_FILE" Stop)" "0" \
    "uninstall removes the langfuse Stop registration" \
    "settings.json: $(cat "$SETTINGS_FILE")"
assert_eq "$(settings_query count-langfuse "$SETTINGS_FILE" SubagentStop)" "0" \
    "uninstall removes the langfuse SubagentStop registration" \
    "settings.json: $(cat "$SETTINGS_FILE")"
assert_eq "$(settings_query count-third-party "$SETTINGS_FILE" Stop)" "2" \
    "uninstall preserves third-party Stop registrations" \
    "settings.json: $(cat "$SETTINGS_FILE")"
assert_eq "$(settings_query count-third-party "$SETTINGS_FILE" SubagentStop)" "1" \
    "uninstall preserves third-party SubagentStop registrations" \
    "settings.json: $(cat "$SETTINGS_FILE")"
assert_contains "$SETTINGS_FILE" "loongsuite-pilot --on-stop" \
    "loongsuite third-party Stop registration survives uninstall"
assert_contains "$SETTINGS_FILE" "third-party-subagentstop-hook" \
    "third-party SubagentStop registration survives uninstall"
for state_file in langfuse_hook.log langfuse_hook.log.1 langfuse_state.json langfuse_state.lock; do
    if [ ! -f "$HOME/.qoder-cn/state/$state_file" ]; then
        pass "uninstall removes ~/.qoder-cn/state/$state_file"
    else
        fail "uninstall left state file behind: $state_file" \
            "listing: $(ls -la "$HOME/.qoder-cn/state" 2>&1 || true)"
    fi
done
if [ -f "$HOME/.qoder-cn/state/other-product-state.json" ]; then
    pass "uninstall preserves non-langfuse state files"
else
    fail "uninstall removed a non-langfuse state file" \
        "listing: $(ls -la "$HOME/.qoder-cn/state" 2>&1 || true)"
fi
if [ -f "$ENV_FILE" ]; then
    pass "env file is preserved by default (no --purge)"
else
    fail "env file was removed without --purge" "output: $(cat "$LOG_A4")"
fi
assert_eq "$(cksum_of "$ENV_FILE")" "$ENV_CKSUM_BEFORE" \
    "env file content unchanged by uninstall"
assert_contains "$LOG_A4" "Env file preserved" \
    "uninstall reports the env file as preserved"
check_sentinels_unchanged "after uninstall"

# --- A5: clean-state uninstall is idempotent -------------------------------
echo "--- A5: uninstall on already-uninstalled state ---"
LOG_A5="$LOG_DIR/uninstall-a5.log"
run_uninstall "$LOG_A5"
if [ "$UNINSTALL_RC" -eq 0 ]; then
    pass "clean-state uninstall.sh exits 0"
else
    fail "clean-state uninstall.sh failed" \
        "command: bash $HOOK_SRC_DIR/uninstall.sh" \
        "exit code: $UNINSTALL_RC" \
        "output: $(cat "$LOG_A5")"
fi
assert_eq "$(settings_query count-third-party "$SETTINGS_FILE" Stop)" "2" \
    "clean-state uninstall leaves third-party registrations untouched" \
    "settings.json: $(cat "$SETTINGS_FILE")"
if [ -f "$ENV_FILE" ]; then
    pass "clean-state uninstall leaves the env file in place"
else
    fail "clean-state uninstall removed the env file" "output: $(cat "$LOG_A5")"
fi
if [ ! -d "$HOOK_DIR" ]; then
    pass "clean-state uninstall creates no hook directory"
else
    fail "clean-state uninstall created a hook directory" \
        "listing: $(ls -la "$HOOK_DIR" 2>&1 || true)"
fi

# --- A6: reinstall via --upgrade reusing env credentials -------------------
echo "--- A6: reinstall via --upgrade -y reuses env credentials ---"
LOG_A6="$LOG_DIR/install-a6.log"
run_install "" "$LOG_A6" --upgrade -y
if [ "$INSTALL_RC" -eq 0 ]; then
    pass "reinstall via --upgrade -y exited 0"
else
    fail "reinstall via --upgrade -y failed" \
        "command: bash $HOOK_SRC_DIR/install.sh --upgrade -y" \
        "exit code: $INSTALL_RC" \
        "output: $(cat "$LOG_A6")"
fi
assert_contains "$LOG_A6" "Upgrade mode: loaded credentials from $ENV_FILE" \
    "--upgrade reuses the credentials from the existing env file"
if [ -f "$HOOK_DIR/langfuse_hook.py" ] && [ -f "$HOOK_DIR/langfuse-entrypoint.sh" ] \
    && [ -f "$HOOK_DIR/pyproject.toml" ] && [ -d "$HOOK_DIR/langstash_deliver" ]; then
    pass "reinstall restores all four artifact kinds"
else
    fail "reinstall did not restore the artifacts" \
        "listing: $(ls -la "$HOOK_DIR" 2>&1 || true)"
fi
if cmp -s "$HOOK_DIR/langfuse_hook.py" "$SOURCE_HOOK"; then
    pass "reinstall restores the current langfuse_hook.py"
else
    fail "reinstalled langfuse_hook.py differs from the source" \
        "diff: $(diff "$HOOK_DIR/langfuse_hook.py" "$SOURCE_HOOK" 2>&1 | head -20)"
fi
assert_eq "$(settings_query count-langfuse "$SETTINGS_FILE" Stop)" "1" \
    "reinstall restores exactly one langfuse Stop registration" \
    "settings.json: $(cat "$SETTINGS_FILE")"
assert_eq "$(settings_query count-langfuse "$SETTINGS_FILE" SubagentStop)" "1" \
    "reinstall restores exactly one langfuse SubagentStop registration" \
    "settings.json: $(cat "$SETTINGS_FILE")"
assert_eq "$(grep -c '^export LANGFUSE_BASE_URL=' "$ENV_FILE")" "1" \
    "reinstall rewrites the env file exactly once" \
    "content: $(cat "$ENV_FILE")"
assert_contains "$ENV_FILE" 'export LANGFUSE_TAGS="qoder-cn,team:olap"' \
    "reinstall keeps the default tag merged with user tags in the env file"
check_sentinels_unchanged "after reinstall"

# =============================================================================
# Section B: purge semantics with no other product env file
# =============================================================================
echo "=== Section B: --purge with no other product env file ==="
new_sandbox
preset_shell_loader
PROFILE_CKSUM_BEFORE="$(cksum_of "$HOME/.profile")"

# --- B1: uninstall on a never-installed sandbox ----------------------------
echo "--- B1: uninstall on a never-installed sandbox ---"
LOG_B1="$LOG_DIR/uninstall-b1.log"
run_uninstall "$LOG_B1"
if [ "$UNINSTALL_RC" -eq 0 ]; then
    pass "uninstall on a never-installed sandbox exits 0"
else
    fail "uninstall on a never-installed sandbox failed" \
        "command: bash $HOOK_SRC_DIR/uninstall.sh" \
        "exit code: $UNINSTALL_RC" \
        "output: $(cat "$LOG_B1")"
fi
if [ ! -d "$HOME/.qoder-cn" ]; then
    pass "uninstall on a never-installed sandbox creates no ~/.qoder-cn"
else
    fail "uninstall created ~/.qoder-cn on a clean sandbox" \
        "listing: $(ls -la "$HOME/.qoder-cn" 2>&1 || true)"
fi
if [ ! -d "$HOME/.agent-exporter-to-langfuse" ]; then
    pass "uninstall on a never-installed sandbox creates no config dir"
else
    fail "uninstall created the config dir on a clean sandbox" \
        "listing: $(ls -la "$HOME/.agent-exporter-to-langfuse" 2>&1 || true)"
fi
assert_eq "$(cksum_of "$HOME/.profile")" "$PROFILE_CKSUM_BEFORE" \
    "uninstall on a never-installed sandbox leaves the shell rc untouched"

# --- B2: install then --purge ----------------------------------------------
echo "--- B2: install then uninstall --purge ---"
LOG_B2_INSTALL="$LOG_DIR/install-b2.log"
run_install "" "$LOG_B2_INSTALL" "${INSTALL_ARGS[@]}"
if [ "$INSTALL_RC" -eq 0 ]; then
    pass "install.sh exited 0 before the purge test"
else
    fail "install.sh failed before the purge test" \
        "command: $(install_cmd_desc)" \
        "exit code: $INSTALL_RC" \
        "output: $(cat "$LOG_B2_INSTALL")"
fi

LOG_B2="$LOG_DIR/uninstall-b2.log"
run_uninstall "$LOG_B2" --purge
if [ "$UNINSTALL_RC" -eq 0 ]; then
    pass "uninstall --purge exited 0"
else
    fail "uninstall --purge failed" \
        "command: bash $HOOK_SRC_DIR/uninstall.sh --purge" \
        "exit code: $UNINSTALL_RC" \
        "output: $(cat "$LOG_B2")"
fi
if [ ! -f "$ENV_FILE" ]; then
    pass "--purge removes the qoder-cn env file"
else
    fail "--purge left the env file in place" "content: $(cat "$ENV_FILE")"
fi
if [ ! -d "$HOME/.agent-exporter-to-langfuse/config" ]; then
    pass "--purge removes the config dir when no other product env file remains"
else
    fail "--purge left the config dir behind while no other env file remains" \
        "listing: $(ls -la "$HOME/.agent-exporter-to-langfuse/config" 2>&1 || true)"
fi
if [ ! -d "$HOOK_DIR" ]; then
    pass "--purge uninstall removes the hook directory"
else
    fail "--purge uninstall left the hook directory" \
        "listing: $(ls -la "$HOOK_DIR" 2>&1 || true)"
fi
assert_not_contains "$HOME/.profile" "# Agent Langfuse Exporters" \
    "--purge (config dir emptied) removes the loader comment line from the shell rc"
assert_not_contains "$HOME/.profile" "agent-exporter-to-langfuse" \
    "--purge (config dir emptied) removes the loader line from the shell rc"
assert_contains "$HOME/.profile" "E2E_RC_MARKER" \
    "unrelated shell rc content is preserved by the loader cleanup"
assert_contains "$HOME/.profile" "e2e rc postscript" \
    "shell rc content after the loader is preserved by the cleanup"

# =============================================================================
# Section C: purge semantics while another product env file remains
# =============================================================================
echo "=== Section C: --purge while another product env file remains ==="
new_sandbox
preset_shell_loader
mkdir -p "$HOME/.agent-exporter-to-langfuse/config"
printf 'export LANGFUSE_BASE_URL="https://qoder.e2e.test"\n' \
    > "$HOME/.agent-exporter-to-langfuse/config/qoder.env"
QODER_ENV_CKSUM_C="$(cksum_of "$HOME/.agent-exporter-to-langfuse/config/qoder.env")"

LOG_C_INSTALL="$LOG_DIR/install-c.log"
run_install "" "$LOG_C_INSTALL" "${INSTALL_ARGS[@]}"
if [ "$INSTALL_RC" -eq 0 ]; then
    pass "install.sh exited 0 with another product env file present"
else
    fail "install.sh failed with another product env file present" \
        "command: $(install_cmd_desc)" \
        "exit code: $INSTALL_RC" \
        "output: $(cat "$LOG_C_INSTALL")"
fi

LOG_C="$LOG_DIR/uninstall-c.log"
run_uninstall "$LOG_C" --purge
if [ "$UNINSTALL_RC" -eq 0 ]; then
    pass "uninstall --purge exited 0 while another env file remains"
else
    fail "uninstall --purge failed while another env file remains" \
        "command: bash $HOOK_SRC_DIR/uninstall.sh --purge" \
        "exit code: $UNINSTALL_RC" \
        "output: $(cat "$LOG_C")"
fi
if [ ! -f "$ENV_FILE" ]; then
    pass "--purge removes the qoder-cn env file while qoder.env remains"
else
    fail "--purge left the qoder-cn env file in place" "content: $(cat "$ENV_FILE")"
fi
assert_eq "$(cksum_of "$HOME/.agent-exporter-to-langfuse/config/qoder.env")" \
    "$QODER_ENV_CKSUM_C" \
    "--purge leaves the other product's env file byte-identical"
if [ -d "$HOME/.agent-exporter-to-langfuse/config" ]; then
    pass "config dir is kept while another product env file remains"
else
    fail "config dir was removed although another product env file remains" \
        "listing: $(ls -la "$HOME/.agent-exporter-to-langfuse" 2>&1 || true)"
fi
assert_contains "$HOME/.profile" "# Agent Langfuse Exporters" \
    "loader comment line is kept while another product env file remains"
assert_contains "$HOME/.profile" "agent-exporter-to-langfuse" \
    "loader line is kept while another product env file remains"
assert_contains "$HOME/.profile" "E2E_RC_MARKER" \
    "shell rc content is preserved while the loader line is kept"

# =============================================================================
echo ""
echo "=== Summary ==="
echo "  Passed: $PASS_COUNT"
echo "  Failed: $FAIL_COUNT"

if [ "$FAIL_COUNT" -gt 0 ]; then
    exit 1
fi
exit 0
