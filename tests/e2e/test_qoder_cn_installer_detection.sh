#!/usr/bin/env bash
set -euo pipefail

# E2E test for the unified installer's qoder-cn detection & dispatch (R-4) and
# release package integrity (R-4, plan task-7).
#
# The unified installer's langstash daemon flow (cmd_install: download,
# install-langstash.sh, launchd/systemd, CLI wrapper) is NOT exercised — only
# the detection + dispatch seam (install_detected_hooks + is_agent_installed)
# is driven, extracted verbatim from deploy/installer.sh into a harness at
# runtime. The detection E2E never touches the real HOME: every case runs
# against a sandbox HOME (mktemp) with placeholder credentials
# (sk-lf-e2e-* / pk-lf-e2e-*) and an unreachable base URL, and PATH is
# restricted to a test bin dir (uv symlink) + /usr/bin:/bin so no agent CLI
# on the host can influence detection.
#
# Cases:
#   Section D — detection & dispatch of the real deploy/installer.sh code:
#     1. sandbox with ~/.qoder-cn present (settings.json preset with
#        third-party Stop/SubagentStop registrations) and qoder/qoderwork
#        cross-product sentinels preset: install_detected_hooks dispatches
#        hooks/qoder-cn/install.sh --upgrade -y, the hook lands in
#        ~/.qoder-cn/hooks/langfuse/ with registration active in
#        ~/.qoder-cn/settings.json, hook-state.json records the install, and
#        the qoder/qoderwork sentinels stay byte-identical.
#     2. sandbox without ~/.qoder-cn, ver_dir = the repo root: the output
#        shows qoder-cn skipped, no agent hook is dispatched at all, no
#        ~/.qoder-cn side effect is created, and every other product's
#        detection branch still reports its normal skip message.
#     3. is_agent_installed branch matrix (direct harness calls): qoder-cn
#        detected by directory presence only (a fake qoder-cn CLI on PATH or
#        ~/.qoder-cn as a regular file must NOT count), qoder and qoderwork
#        detection branches keep their directory/CLI semantics.
#   Section P — release package integrity (tarball built by deploy/package.sh
#     from `git ls-files`, so hooks/qoder-cn/ must be staged):
#     4. git add hooks/qoder-cn/ (staging only, no commit — per plan task-7),
#        build the tarball, unpack it and assert the qoder-cn manifest
#        (install.sh, uninstall.sh, README.md, hooks/{langfuse_hook.py,
#        langfuse-entrypoint.sh, pyproject.toml, tests/}) plus the sibling
#        langstash-deliver copy source. package.sh stderr and exit code are
#        echoed on failure.
#
# Per AGENTS.md: stderr is never discarded; on failure the tested command's
# output and exit code are echoed as diagnostics.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
INSTALLER="$REPO_ROOT/deploy/installer.sh"
QODER_CN_SRC="$REPO_ROOT/hooks/qoder-cn"
SOURCE_HOOK="$QODER_CN_SRC/hooks/langfuse_hook.py"
SOURCE_RUNNER="$QODER_CN_SRC/hooks/langfuse-entrypoint.sh"
SOURCE_PYPROJECT="$QODER_CN_SRC/hooks/pyproject.toml"
SOURCE_LANGSTASH="$REPO_ROOT/hooks/langstash-deliver/python/langstash_deliver"

# Hermetic isolation: the harness dispatch inherits LANGFUSE_* from this
# process (mirroring installer.sh cmd_install's exports), so host credentials
# must never leak into the sandbox runs — placeholders only.
unset LANGFUSE_SECRET_KEY LANGFUSE_PUBLIC_KEY LANGFUSE_BASE_URL \
    LANGFUSE_USER_ID LANGFUSE_TAGS || true

SECRET_KEY="sk-lf-e2e-secret-do-not-log-0123456789"
PUBLIC_KEY="pk-lf-e2e-public-0123456789"
BASE_URL="https://langfuse.e2e.test"
USER_ID="e2e-user"
EXTRA_TAGS="team:olap"
ENTRYPOINT_CMD="~/.qoder-cn/hooks/langfuse/langfuse-entrypoint.sh"
# Mirrors cmd_install's write_pointer "current" <version> before dispatch.
E2E_VERSION="9.9.9-e2e"

# Test-scoped work dir: holds logs, the extracted harness, the sandbox HOMEs
# and the uv package cache (never the real HOME).
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
for tool in python3 uv git tar awk bash; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "FAIL: prerequisite '$tool' not found on PATH" >&2
        exit 1
    fi
done
for f in "$INSTALLER" "$QODER_CN_SRC/install.sh" "$QODER_CN_SRC/uninstall.sh" \
    "$QODER_CN_SRC/README.md" "$SOURCE_HOOK" "$SOURCE_RUNNER" \
    "$SOURCE_PYPROJECT"; do
    if [ ! -f "$f" ]; then
        echo "FAIL: required source file missing: $f" >&2
        exit 1
    fi
done
if [ ! -d "$SOURCE_LANGSTASH" ]; then
    echo "FAIL: langstash-deliver source missing: $SOURCE_LANGSTASH" >&2
    exit 1
fi

# --- harness: extract the detection+dispatch seam from deploy/installer.sh --
# The extraction pulls the contiguous block from the color/utility header
# (RED=...) through the closing brace of install_detected_hooks(), which
# contains everything install_detected_hooks needs: info/warn, INSTALL_DIR,
# read_pointer, write_hook_state_entry, list_known_agents, is_agent_installed.
HARNESS="$WORK_DIR/installer-detection-harness.sh"
awk '
    /^RED=/ { on = 1 }
    on { print }
    /^install_detected_hooks\(\) \{/ { in_hook = 1 }
    in_hook && /^}/ { exit }
' "$INSTALLER" > "$HARNESS"

# Extraction sanity — refuse to source a harness that did not capture exactly
# the detection seam (guards against deploy/installer.sh refactors silently
# turning this test into a false green).
harness_sane=true
if [ "$(tail -n 1 "$HARNESS")" != "}" ]; then
    harness_sane=false
    fail "harness extraction ends at the install_detected_hooks closing brace" \
        "installer: $INSTALLER" \
        "harness tail: $(tail -n 3 "$HARNESS" | tr '\n' '|')"
fi
for marker in \
    'INSTALL_DIR="$HOME/.agent-exporter-to-langfuse"' \
    'read_pointer() {' \
    'write_hook_state_entry() {' \
    'list_known_agents() {' \
    'is_agent_installed() {' \
    'install_detected_hooks() {'; do
    if ! grep -qF "$marker" "$HARNESS"; then
        harness_sane=false
        fail "harness extraction captured $marker from deploy/installer.sh" \
            "installer: $INSTALLER" "harness: $HARNESS"
    fi
done
for forbidden in 'cmd_install()' 'usage()' 'enhance_path_for_daemon()' 'install)' ; do
    if grep -qF "$forbidden" "$HARNESS"; then
        harness_sane=false
        fail "harness extraction must not leak the main dispatch flow ($forbidden)" \
            "installer: $INSTALLER" "harness: $HARNESS"
    fi
done
if ! grep -q 'qoder-cn)' "$HARNESS"; then
    harness_sane=false
    fail "harness extraction captured the qoder-cn detection branch" \
        "installer: $INSTALLER" "harness: $HARNESS"
fi
if [ "$harness_sane" = true ] && ! bash -n "$HARNESS"; then
    harness_sane=false
    fail "extracted harness passes bash -n syntax check" \
        "harness: $HARNESS" "bash -n stderr: $(bash -n "$HARNESS" 2>&1 || true)"
fi
if [ "$harness_sane" != true ]; then
    echo "FAIL: harness extraction from deploy/installer.sh is unusable — aborting" >&2
    exit 1
fi
pass "harness extracted the installer detection seam (is_agent_installed + install_detected_hooks + helpers)"

# Driver: sources the harness and exposes dispatch/detect modes. Sourcing
# happens with the sandbox HOME already set, so INSTALL_DIR resolves inside
# the sandbox. Dispatch runs under `set -x` so the tarball of evidence shows
# the exact install.sh invocation (--upgrade -y) the unified installer makes.
DRIVER="$WORK_DIR/installer-detection-driver.sh"
cat > "$DRIVER" <<EOF
#!/usr/bin/env bash
set -euo pipefail
HARNESS_PATH="\$1"
MODE="\$2"
ARG="\$3"
# shellcheck disable=SC1090
source "\$HARNESS_PATH"
case "\$MODE" in
    dispatch)
        set -x
        install_detected_hooks "\$ARG"
        set +x
        ;;
    detect)
        if is_agent_installed "\$ARG"; then
            echo "INSTALLED"
        else
            echo "NOT_INSTALLED"
        fi
        ;;
    *)
        echo "driver: unknown mode: \$MODE" >&2
        exit 2
        ;;
esac
EOF
bash -n "$DRIVER" || { echo "FAIL: driver syntax error" >&2; exit 1; }

# Restricted PATH: uv (via symlink) + system tools only, so no host agent CLI
# can satisfy a `command -v` probe in is_agent_installed.
TESTBIN="$WORK_DIR/bin"
mkdir -p "$TESTBIN"
ln -s "$(command -v uv)" "$TESTBIN/uv"
BASE_PATH="$TESTBIN:/usr/bin:/bin"

# FAKEBIN holds fake agent CLIs for the branch-matrix cases; it is only ever
# prepended for the specific detect invocation under test.
FAKEBIN="$WORK_DIR/fakebin"
mkdir -p "$FAKEBIN"

DRIVER_RC=0
# run_driver <log> <mode> <arg> [extra-PATH-prefix]
# Uses the current $HOME plus the placeholder LANGFUSE_* credentials (exported
# for the child only), mirroring installer.sh cmd_install's export set.
run_driver() {
    local log="$1" mode="$2" arg="$3" extra_path="${4:-}"
    local run_path="$BASE_PATH"
    if [ -n "$extra_path" ]; then
        run_path="$extra_path:$BASE_PATH"
    fi
    set +e
    PATH="$run_path" \
        LANGFUSE_SECRET_KEY="$SECRET_KEY" \
        LANGFUSE_PUBLIC_KEY="$PUBLIC_KEY" \
        LANGFUSE_BASE_URL="$BASE_URL" \
        LANGFUSE_USER_ID="$USER_ID" \
        LANGFUSE_TAGS="$EXTRA_TAGS" \
        bash "$DRIVER" "$HARNESS" "$mode" "$arg" > "$log" 2>&1 < /dev/null
    DRIVER_RC=$?
    set -e
}

# --- settings.json query helper -------------------------------------------
# settings_query <mode> <settings.json> <event>
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
else:
    print("error: unknown mode %s" % mode)
    sys.exit(3)
PY
}

# --- sandbox / preset helpers ----------------------------------------------
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
    INSTALL_DIR_SB="$HOME/.agent-exporter-to-langfuse"
}

preset_qoder_cn_settings() {
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
    # qoder (international) and qoderwork user-side state that the unified
    # installer's qoder-cn dispatch must never read or modify.
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

# check_sentinels_unchanged <when>
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

# =============================================================================
# Section D1: ~/.qoder-cn present → dispatch installs the qoder-cn hook
# =============================================================================
echo "=== Section D1: dispatch with ~/.qoder-cn present ==="
new_sandbox
preset_qoder_cn_settings
preset_cross_product_sentinels

# ver_dir precondition mirroring cmd_install: INSTALL_DIR exists and holds a
# current version pointer before install_detected_hooks runs.
mkdir -p "$INSTALL_DIR_SB"
printf '%s' "$E2E_VERSION" > "$INSTALL_DIR_SB/current"

# Mini ver_dir: only qoder-cn is dispatchable (symlinked to the real repo
# dir). The qoder/qoderwork sentinels coexist in the sandbox HOME, so a
# wrongly broad dispatch would be caught by the sentinel assertions below;
# restricting the ver_dir keeps the case focused on the qoder-cn dispatch
# path itself. install.sh resolves its langstash-deliver copy source through
# the symlinked sibling layout exactly as in an extracted release package.
VER_DIR_PRESENT="$WORK_DIR/verdir-present"
mkdir -p "$VER_DIR_PRESENT/hooks"
ln -s "$QODER_CN_SRC" "$VER_DIR_PRESENT/hooks/qoder-cn"

LOG_D1="$LOG_DIR/dispatch-present.log"
run_driver "$LOG_D1" dispatch "$VER_DIR_PRESENT"
if [ "$DRIVER_RC" -eq 0 ]; then
    pass "install_detected_hooks exited 0 with ~/.qoder-cn present"
else
    fail "install_detected_hooks failed with ~/.qoder-cn present" \
        "command: HOME=$HOME PATH=<restricted> bash driver dispatch $VER_DIR_PRESENT" \
        "exit code: $DRIVER_RC" \
        "output: $(cat "$LOG_D1")"
fi

# Dispatch decision + the unified installer's exact invocation (bash -x trace).
assert_contains "$LOG_D1" "Installing hook: qoder-cn" \
    "installer dispatches the qoder-cn hook when ~/.qoder-cn exists"
assert_not_contains "$LOG_D1" "Skipping qoder-cn" \
    "installer does not skip qoder-cn when ~/.qoder-cn exists"
assert_contains "$LOG_D1" "hooks/qoder-cn/install.sh --upgrade -y" \
    "dispatch invokes hooks/qoder-cn/install.sh with the unified upgrade args"
# Direct branch check: the qoder/qoderwork detection branches keep working on
# this sandbox (their sentinel dirs exist) even though only qoder-cn was
# dispatched.
LOG_D1_DETECT_QODER="$LOG_DIR/detect-present-qoder.log"
run_driver "$LOG_D1_DETECT_QODER" detect qoder
assert_eq "$(cat "$LOG_D1_DETECT_QODER")" "INSTALLED" \
    "qoder detection branch still reports installed with ~/.qoder present"
LOG_D1_DETECT_QW="$LOG_DIR/detect-present-qoderwork.log"
run_driver "$LOG_D1_DETECT_QW" detect qoderwork
assert_eq "$(cat "$LOG_D1_DETECT_QW")" "INSTALLED" \
    "qoderwork detection branch still reports installed with ~/.qoderwork present"

# Hook artifacts land in the sandbox and match the repo sources.
if [ -f "$HOOK_DIR/langfuse_hook.py" ] && [ -f "$HOOK_DIR/langfuse-entrypoint.sh" ] \
    && [ -f "$HOOK_DIR/pyproject.toml" ] && [ -d "$HOOK_DIR/langstash_deliver" ]; then
    pass "dispatch installs all four artifact kinds into $HOOK_DIR"
else
    fail "dispatch did not install the hook artifacts" \
        "listing: $(ls -la "$HOOK_DIR" 2>&1 || true)" \
        "dispatch log: $(cat "$LOG_D1")"
fi
if cmp -s "$HOOK_DIR/langfuse_hook.py" "$SOURCE_HOOK"; then
    pass "installed langfuse_hook.py is byte-identical to the repo source"
else
    fail "installed langfuse_hook.py differs from the repo source" \
        "diff: $(diff "$HOOK_DIR/langfuse_hook.py" "$SOURCE_HOOK" 2>&1 | head -20)"
fi
if cmp -s "$HOOK_DIR/langfuse-entrypoint.sh" "$SOURCE_RUNNER"; then
    pass "installed langfuse-entrypoint.sh is byte-identical to the repo source"
else
    fail "installed langfuse-entrypoint.sh differs from the repo source" \
        "diff: $(diff "$HOOK_DIR/langfuse-entrypoint.sh" "$SOURCE_RUNNER" 2>&1 | head -20)"
fi
if cmp -s "$HOOK_DIR/pyproject.toml" "$SOURCE_PYPROJECT"; then
    pass "installed pyproject.toml is byte-identical to the repo source"
else
    fail "installed pyproject.toml differs from the repo source" \
        "diff: $(diff "$HOOK_DIR/pyproject.toml" "$SOURCE_PYPROJECT" 2>&1 | head -20)"
fi
if diff -r "$HOOK_DIR/langstash_deliver" "$SOURCE_LANGSTASH" > /dev/null 2>&1; then
    pass "langstash_deliver copy is byte-identical to the repo source"
else
    fail "langstash_deliver copy differs from the repo source" \
        "diff: $(diff -r "$HOOK_DIR/langstash_deliver" "$SOURCE_LANGSTASH" 2>&1 | head -20)"
fi
if [ -x "$HOOK_DIR/langfuse-entrypoint.sh" ]; then
    pass "installed langfuse-entrypoint.sh is executable"
else
    fail "installed langfuse-entrypoint.sh is not executable" \
        "listing: $(ls -la "$HOOK_DIR" 2>&1 || true)"
fi

# Registration active in the sandbox settings.json, third-party preserved.
assert_eq "$(settings_query count-langfuse "$SETTINGS_FILE" Stop)" "1" \
    "Stop holds exactly one langfuse registration after dispatch" \
    "settings.json: $(cat "$SETTINGS_FILE")"
assert_eq "$(settings_query count-langfuse "$SETTINGS_FILE" SubagentStop)" "1" \
    "SubagentStop holds exactly one langfuse registration after dispatch" \
    "settings.json: $(cat "$SETTINGS_FILE")"
assert_eq "$(settings_query langfuse-commands "$SETTINGS_FILE" Stop)" "$ENTRYPOINT_CMD" \
    "Stop langfuse registration points at the qoder-cn entrypoint" \
    "settings.json: $(cat "$SETTINGS_FILE")"
assert_eq "$(settings_query langfuse-commands "$SETTINGS_FILE" SubagentStop)" "$ENTRYPOINT_CMD" \
    "SubagentStop langfuse registration points at the qoder-cn entrypoint" \
    "settings.json: $(cat "$SETTINGS_FILE")"
assert_eq "$(settings_query count-third-party "$SETTINGS_FILE" Stop)" "2" \
    "third-party Stop registrations preserved by the dispatched install" \
    "settings.json: $(cat "$SETTINGS_FILE")"
assert_eq "$(settings_query count-third-party "$SETTINGS_FILE" SubagentStop)" "1" \
    "third-party SubagentStop registrations preserved by the dispatched install" \
    "settings.json: $(cat "$SETTINGS_FILE")"
assert_contains "$SETTINGS_FILE" "loongsuite-pilot --on-stop" \
    "loongsuite third-party Stop registration preserved by the dispatched install"

# Env file with the default tag, written by the dispatched install.
ENV_CONTENT=""
if [ -f "$ENV_FILE" ]; then
    pass "dispatch created the env file at $ENV_FILE"
    ENV_CONTENT="$(cat "$ENV_FILE")"
else
    fail "dispatch did not create the env file at $ENV_FILE" \
        "dispatch log: $(cat "$LOG_D1")"
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
if printf '%s\n' "$ENV_CONTENT" | grep -qF 'export LANGFUSE_TAGS="qoder-cn,team:olap"'; then
    pass "env file LANGFUSE_TAGS merges the default qoder-cn tag with user tags"
else
    fail "env file LANGFUSE_TAGS does not merge default and user tags" "content: $ENV_CONTENT"
fi

# Dispatch bookkeeping: hook-state.json records qoder-cn as installed at the
# current version pointer.
HOOK_STATE_FILE="$INSTALL_DIR_SB/hook-state.json"
if [ -f "$HOOK_STATE_FILE" ]; then
    pass "dispatch wrote the hook-state bookkeeping file"
else
    fail "dispatch did not write hook-state.json" \
        "listing: $(ls -la "$INSTALL_DIR_SB" 2>&1 || true)"
fi
HOOK_STATE_QODER_CN="$(python3 -c "
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    e = d.get('qoder-cn', {})
    print('%s|%s' % (e.get('status', ''), e.get('version', '')))
except Exception as exc:
    print('error: %s' % exc)
" "$HOOK_STATE_FILE" 2>&1)"
assert_eq "$HOOK_STATE_QODER_CN" "installed|$E2E_VERSION" \
    "hook-state.json records qoder-cn installed at the current version" \
    "hook-state.json: $(cat "$HOOK_STATE_FILE" 2>&1)"

# Install output must not leak the full secret (prefix only).
if grep -qF "$SECRET_KEY" "$LOG_D1"; then
    fail "dispatch output leaked the full secret key" \
        "leak: $(grep -nF "$SECRET_KEY" "$LOG_D1")"
else
    pass "dispatch output does not contain the full secret key"
fi

# Cross-product sentinels unchanged by the dispatched qoder-cn install.
check_sentinels_unchanged "after the qoder-cn dispatch"

# =============================================================================
# Section D2: ~/.qoder-cn absent → qoder-cn skipped, other branches unchanged
# =============================================================================
echo "=== Section D2: dispatch with ~/.qoder-cn absent ==="
new_sandbox
# ver_dir precondition mirroring cmd_install (INSTALL_DIR exists); no product
# directory exists in this sandbox and PATH holds no agent CLI, so every
# detection branch must report its normal skip.
mkdir -p "$INSTALL_DIR_SB"

LOG_D2="$LOG_DIR/dispatch-absent.log"
run_driver "$LOG_D2" dispatch "$REPO_ROOT"
if [ "$DRIVER_RC" -eq 0 ]; then
    pass "install_detected_hooks exited 0 with ~/.qoder-cn absent"
else
    fail "install_detected_hooks failed with ~/.qoder-cn absent" \
        "command: HOME=$HOME PATH=<restricted> bash driver dispatch $REPO_ROOT" \
        "exit code: $DRIVER_RC" \
        "output: $(cat "$LOG_D2")"
fi

assert_contains "$LOG_D2" "Skipping qoder-cn (not installed)" \
    "installer skips qoder-cn when ~/.qoder-cn does not exist"
assert_not_contains "$LOG_D2" "hooks/qoder-cn/install.sh --upgrade -y" \
    "installer does not invoke qoder-cn install.sh when ~/.qoder-cn does not exist"
assert_eq "$(grep -c 'Installing hook:' "$LOG_D2" || true)" "0" \
    "no agent hook is dispatched at all when no product is installed" \
    "dispatch log: $(cat "$LOG_D2")"
if [ ! -e "$HOME/.qoder-cn" ]; then
    pass "skipping qoder-cn creates no ~/.qoder-cn side effect"
else
    fail "skipping qoder-cn still created ~/.qoder-cn" \
        "listing: $(ls -la "$HOME/.qoder-cn" 2>&1 || true)"
fi
if [ ! -f "$INSTALL_DIR_SB/hook-state.json" ]; then
    pass "skipped dispatch writes no hook-state entry"
else
    fail "skipped dispatch still wrote hook-state.json" \
        "hook-state.json: $(cat "$INSTALL_DIR_SB/hook-state.json")"
fi
# Other products' detection branches keep their normal behavior.
for agent in claude-code codex cursor opencode pi qoder qoderwork; do
    assert_contains "$LOG_D2" "Skipping $agent (not installed)" \
        "$agent detection branch still reports its normal skip message"
done

# =============================================================================
# Section D3: is_agent_installed branch matrix
# =============================================================================
echo "=== Section D3: is_agent_installed branch matrix ==="
new_sandbox

# qoder-cn: directory presence is the only detection signal.
LOG_D3_A="$LOG_DIR/detect-qoder-cn-absent.log"
run_driver "$LOG_D3_A" detect qoder-cn
assert_eq "$(cat "$LOG_D3_A")" "NOT_INSTALLED" \
    "qoder-cn not installed when ~/.qoder-cn is absent"

printf '#!/bin/sh\nexit 0\n' > "$FAKEBIN/qoder-cn"
chmod +x "$FAKEBIN/qoder-cn"
LOG_D3_B="$LOG_DIR/detect-qoder-cn-fake-cli.log"
run_driver "$LOG_D3_B" detect qoder-cn "$FAKEBIN"
assert_eq "$(cat "$LOG_D3_B")" "NOT_INSTALLED" \
    "a qoder-cn CLI on PATH does not count as installed (detection is directory-only)"
rm -f "$FAKEBIN/qoder-cn"

mkdir -p "$HOME/.qoder-cn"
LOG_D3_C="$LOG_DIR/detect-qoder-cn-dir.log"
run_driver "$LOG_D3_C" detect qoder-cn
assert_eq "$(cat "$LOG_D3_C")" "INSTALLED" \
    "qoder-cn installed when ~/.qoder-cn directory exists (no settings.json needed)"
rm -rf "$HOME/.qoder-cn"

: > "$HOME/.qoder-cn"
LOG_D3_D="$LOG_DIR/detect-qoder-cn-file.log"
run_driver "$LOG_D3_D" detect qoder-cn
assert_eq "$(cat "$LOG_D3_D")" "NOT_INSTALLED" \
    "~/.qoder-cn as a regular file does not count as installed"
rm -f "$HOME/.qoder-cn"

# qoder: directory and CLI probes both keep working.
LOG_D3_E="$LOG_DIR/detect-qoder-absent.log"
run_driver "$LOG_D3_E" detect qoder
assert_eq "$(cat "$LOG_D3_E")" "NOT_INSTALLED" \
    "qoder not installed without ~/.qoder and without a qoder CLI"
mkdir -p "$HOME/.qoder"
LOG_D3_F="$LOG_DIR/detect-qoder-dir.log"
run_driver "$LOG_D3_F" detect qoder
assert_eq "$(cat "$LOG_D3_F")" "INSTALLED" \
    "qoder detection branch reports installed with ~/.qoder present"
rm -rf "$HOME/.qoder"
printf '#!/bin/sh\nexit 0\n' > "$FAKEBIN/qoder"
chmod +x "$FAKEBIN/qoder"
LOG_D3_G="$LOG_DIR/detect-qoder-cli.log"
run_driver "$LOG_D3_G" detect qoder "$FAKEBIN"
assert_eq "$(cat "$LOG_D3_G")" "INSTALLED" \
    "qoder detection branch keeps its CLI probe (command -v qoder)"
rm -f "$FAKEBIN/qoder"

# qoderwork: directory probe unchanged.
LOG_D3_H="$LOG_DIR/detect-qoderwork-absent.log"
run_driver "$LOG_D3_H" detect qoderwork
assert_eq "$(cat "$LOG_D3_H")" "NOT_INSTALLED" \
    "qoderwork not installed without ~/.qoderwork"
mkdir -p "$HOME/.qoderwork"
LOG_D3_I="$LOG_DIR/detect-qoderwork-dir.log"
run_driver "$LOG_D3_I" detect qoderwork
assert_eq "$(cat "$LOG_D3_I")" "INSTALLED" \
    "qoderwork detection branch reports installed with ~/.qoderwork present"
rm -rf "$HOME/.qoderwork"

# =============================================================================
# Section P: release package integrity (plan task-7)
# =============================================================================
echo "=== Section P: release package integrity ==="
LOG_P="$LOG_DIR/package-build.log"

# package.sh packs the repo via `git ls-files`, so the new product directory
# must be staged first. Staging only — committing is out of scope here (it
# requires explicit user confirmation per AGENTS.md).
GIT_ADD_RC=0
git -C "$REPO_ROOT" add hooks/qoder-cn/ > "$LOG_P" 2>&1 || GIT_ADD_RC=$?
if [ "$GIT_ADD_RC" -eq 0 ]; then
    pass "git add hooks/qoder-cn/ staged the product directory for packaging"
else
    fail "git add hooks/qoder-cn/ failed" \
        "command: git -C $REPO_ROOT add hooks/qoder-cn/" \
        "exit code: $GIT_ADD_RC" \
        "stderr: $(cat "$LOG_P")"
fi

PKG_OUT="$WORK_DIR/pkg-out"
mkdir -p "$PKG_OUT"
PKG_RC=0
bash "$REPO_ROOT/deploy/package.sh" --output-dir "$PKG_OUT" >> "$LOG_P" 2>&1 || PKG_RC=$?
if [ "$PKG_RC" -eq 0 ]; then
    pass "deploy/package.sh built the release tarball (exit 0)"
else
    fail "deploy/package.sh failed to build the release tarball" \
        "command: bash deploy/package.sh --output-dir $PKG_OUT" \
        "exit code: $PKG_RC" \
        "output: $(cat "$LOG_P")"
fi

TARBALL=""
for f in "$PKG_OUT"/agent-exporter-to-langfuse-*.tar.gz; do
    [ -f "$f" ] && TARBALL="$f" && break
done
if [ -n "$TARBALL" ]; then
    pass "release tarball produced: $(basename "$TARBALL")"
else
    fail "release tarball not found in $PKG_OUT" \
        "listing: $(ls -la "$PKG_OUT" 2>&1 || true)" \
        "package log: $(cat "$LOG_P")"
fi

UNPACK_DIR="$WORK_DIR/unpack"
mkdir -p "$UNPACK_DIR"
TAR_RC=0
tar xzf "$TARBALL" -C "$UNPACK_DIR" >> "$LOG_P" 2>&1 || TAR_RC=$?
if [ "$TAR_RC" -eq 0 ]; then
    pass "release tarball unpacked"
else
    fail "release tarball could not be unpacked" \
        "command: tar xzf $TARBALL -C $UNPACK_DIR" \
        "exit code: $TAR_RC" \
        "stderr: $(cat "$LOG_P")"
fi

PKG_ROOT=""
for d in "$UNPACK_DIR"/agent-exporter-to-langfuse-*/; do
    [ -d "$d" ] && PKG_ROOT="$d" && break
done
if [ -n "$PKG_ROOT" ]; then
    pass "unpacked package root found: ${PKG_ROOT%/}"
else
    fail "unpacked package root not found under $UNPACK_DIR" \
        "listing: $(ls -la "$UNPACK_DIR" 2>&1 || true)"
fi

if [ -n "$PKG_ROOT" ]; then
    # Tarball manifest evidence (kept in the suite output / logs).
    echo "--- tarball manifest: hooks/qoder-cn ---"
    tar tzf "$TARBALL" | grep "hooks/qoder-cn/" | LC_ALL=C sort || true
    echo "--- tarball manifest: langstash-deliver copy source ---"
    tar tzf "$TARBALL" | grep "hooks/langstash-deliver/python/langstash_deliver" | head -3 || true

    for f in \
        "hooks/qoder-cn/install.sh" \
        "hooks/qoder-cn/uninstall.sh" \
        "hooks/qoder-cn/README.md" \
        "hooks/qoder-cn/hooks/langfuse_hook.py" \
        "hooks/qoder-cn/hooks/langfuse-entrypoint.sh" \
        "hooks/qoder-cn/hooks/pyproject.toml"; do
        if [ -f "$PKG_ROOT/$f" ]; then
            pass "tarball contains $f"
        else
            fail "tarball is missing $f" \
                "listing: $(ls -la "$(dirname "$PKG_ROOT/$f")" 2>&1 || true)"
        fi
    done

    if [ -d "$PKG_ROOT/hooks/qoder-cn/hooks/tests" ] \
        && ls "$PKG_ROOT/hooks/qoder-cn/hooks/tests/"test_*.py >/dev/null 2>&1; then
        pass "tarball contains hooks/qoder-cn/hooks/tests/ with the pytest suite"
    else
        fail "tarball is missing the hooks/qoder-cn/hooks/tests/ suite" \
            "listing: $(ls -la "$PKG_ROOT/hooks/qoder-cn/hooks/tests" 2>&1 || true)"
    fi

    if [ -d "$PKG_ROOT/hooks/langstash-deliver/python/langstash_deliver" ]; then
        pass "tarball contains the langstash-deliver copy source (hooks/langstash-deliver/python/langstash_deliver)"
    else
        fail "tarball is missing the langstash-deliver copy source" \
            "listing: $(ls -la "$PKG_ROOT/hooks/langstash-deliver/python" 2>&1 || true)"
    fi
fi

# =============================================================================
echo ""
echo "=== Summary ==="
echo "  Passed: $PASS_COUNT"
echo "  Failed: $FAIL_COUNT"

if [ "$FAIL_COUNT" -gt 0 ]; then
    exit 1
fi
exit 0
