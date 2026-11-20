#!/usr/bin/env bash
# E2E test suite for versioned upgrade architecture
# Uses real HOME — will uninstall before testing and restore after.
# Run from project root: bash tests/e2e/test_versioned_upgrade.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

source "$SCRIPT_DIR/e2e-helpers.sh"

INSTALL_DIR="$HOME/.agent-exporter-to-langfuse"

purge_install() {
    bash "$REPO_ROOT/deploy/installer.sh" uninstall --purge >/dev/null 2>&1 || true
    rm -f "$HOME/.local/bin/langstash"
    rm -rf "$INSTALL_DIR"
}

# Build test package
echo -e "${_E2E_BOLD}Building test package...${_E2E_NC}"
PKG_DIR="/tmp/e2e-pkg-$$"
mkdir -p "$PKG_DIR"
(cd "$REPO_ROOT" && bash deploy/package.sh --output-dir "$PKG_DIR") || { echo "Failed to build package"; exit 1; }
TARBALL="$(ls "$PKG_DIR"/agent-exporter-to-langfuse-*.tar.gz)"
VERSION="$(cat "$REPO_ROOT/VERSION" | tr -d '[:space:]')"
echo "Package: $TARBALL (v$VERSION)"
echo ""

e2e_suite "versioned-upgrade" 93

# ============================================================
e2e_case "E2E-1: Fresh install"
purge_install

LANGFUSE_SECRET_KEY="sk-lf-test-e2e" \
LANGFUSE_PUBLIC_KEY="pk-lf-test-e2e" \
LANGFUSE_BASE_URL="http://127.0.0.1:9999" \
bash "$REPO_ROOT/deploy/installer.sh" install \
    --package-url "file://$TARBALL" 2>&1 | tail -5

e2e_check "current pointer exists" "test -f '$INSTALL_DIR/current'"
e2e_check "current pointer value" "[ \"\$(cat '$INSTALL_DIR/current')\" = '$VERSION' ]"
e2e_check "version dir exists" "test -d '$INSTALL_DIR/versions/$VERSION'"
e2e_check "VERSION in version dir" "test -f '$INSTALL_DIR/versions/$VERSION/VERSION'"
e2e_check "config dir exists" "test -d '$INSTALL_DIR/config'"
e2e_check "data dir exists" "test -d '$INSTALL_DIR/data'"
e2e_check "logs dir exists" "test -d '$INSTALL_DIR/logs'"
e2e_check "CLI wrapper exists" "test -f '$HOME/.local/bin/langstash'"
e2e_check "CLI wrapper executable" "test -x '$HOME/.local/bin/langstash'"
e2e_check "no .git dir" "test ! -d '$INSTALL_DIR/.git'"
e2e_check "root dir clean (no exporter/)" "test ! -d '$INSTALL_DIR/exporter'"
e2e_check "root dir clean (no hooks/)" "test ! -d '$INSTALL_DIR/hooks'"

# ============================================================
e2e_case "E2E-2: SHA-256 rejection"
purge_install

TAMPERED_DIR="/tmp/e2e-tampered-$$"
mkdir -p "$TAMPERED_DIR"
cp "$TARBALL" "$TAMPERED_DIR/tampered.tar.gz"
echo "tampered-data" >> "$TAMPERED_DIR/tampered.tar.gz"
sed "s/agent-exporter-to-langfuse-.*/tampered.tar.gz/" "$PKG_DIR/SHA256SUMS" > "$TAMPERED_DIR/SHA256SUMS"

OUTPUT=$(bash "$REPO_ROOT/deploy/installer.sh" install \
    --package-url "file://$TAMPERED_DIR/tampered.tar.gz" \
    --version "$VERSION" 2>&1 || true)

e2e_check "SHA-256 mismatch detected" "echo '$OUTPUT' | grep -q 'SHA-256 mismatch'"
e2e_check "current pointer not created" "test ! -f '$INSTALL_DIR/current'"
rm -rf "$TAMPERED_DIR"

# ============================================================
e2e_case "E2E-2b: Missing SHA256SUMS for file://"
purge_install

NOSUMS_DIR="/tmp/e2e-nosums-$$"
mkdir -p "$NOSUMS_DIR"
cp "$TARBALL" "$NOSUMS_DIR/"
NOSUMS_TARBALL="$(ls "$NOSUMS_DIR"/agent-exporter-to-langfuse-*.tar.gz)"

OUTPUT=$(LANGFUSE_SECRET_KEY="sk-lf-test" LANGFUSE_PUBLIC_KEY="pk-lf-test" LANGFUSE_BASE_URL="http://127.0.0.1:9999" \
    bash "$REPO_ROOT/deploy/installer.sh" install \
    --package-url "file://$NOSUMS_TARBALL" 2>&1 || true)

e2e_check "file:// without SHA256SUMS warns" "echo '$OUTPUT' | grep -q 'SHA256SUMS not found next to local package'"
e2e_check "file:// without SHA256SUMS installs" "test -f '$INSTALL_DIR/current'"
rm -rf "$NOSUMS_DIR"
purge_install

# ============================================================
e2e_case "E2E-2c: --skip-verify bypasses SHA-256"
purge_install

TAMPERED_DIR2="/tmp/e2e-tampered2-$$"
mkdir -p "$TAMPERED_DIR2"
cp "$TARBALL" "$TAMPERED_DIR2/"
cp "$PKG_DIR/SHA256SUMS" "$TAMPERED_DIR2/"
TAMPERED_TARBALL2="$(ls "$TAMPERED_DIR2"/agent-exporter-to-langfuse-*.tar.gz)"
echo "tampered-data" >> "$TAMPERED_TARBALL2"

OUTPUT=$(LANGFUSE_SECRET_KEY="sk-lf-test" LANGFUSE_PUBLIC_KEY="pk-lf-test" LANGFUSE_BASE_URL="http://127.0.0.1:9999" \
    bash "$REPO_ROOT/deploy/installer.sh" install \
    --package-url "file://$TAMPERED_TARBALL2" \
    --version "$VERSION" --skip-verify 2>&1 || true)

e2e_check "--skip-verify shows warning" "echo '$OUTPUT' | grep -q 'verification skipped'"
rm -rf "$TAMPERED_DIR2"
purge_install

# ============================================================
e2e_case "E2E-3: Uninstall without purge"

LANGFUSE_SECRET_KEY="sk-lf-test" LANGFUSE_PUBLIC_KEY="pk-lf-test" LANGFUSE_BASE_URL="http://127.0.0.1:9999" \
bash "$REPO_ROOT/deploy/installer.sh" install \
    --package-url "file://$TARBALL" >/dev/null 2>&1 || true
echo "preserve-me" > "$INSTALL_DIR/data/test.txt"

bash "$REPO_ROOT/deploy/installer.sh" uninstall >/dev/null 2>&1 || true

e2e_check "versions removed" "test ! -d '$INSTALL_DIR/versions'"
e2e_check "current removed" "test ! -f '$INSTALL_DIR/current'"
e2e_check "previous removed" "test ! -f '$INSTALL_DIR/previous'"
e2e_check "hook-state removed" "test ! -f '$INSTALL_DIR/hook-state.json'"
e2e_check "wrapper removed" "test ! -f '$HOME/.local/bin/langstash'"
e2e_check "data preserved" "test -f '$INSTALL_DIR/data/test.txt'"

# ============================================================
e2e_case "E2E-4: Uninstall --purge"

LANGFUSE_SECRET_KEY="sk-lf-test" LANGFUSE_PUBLIC_KEY="pk-lf-test" LANGFUSE_BASE_URL="http://127.0.0.1:9999" \
bash "$REPO_ROOT/deploy/installer.sh" install \
    --package-url "file://$TARBALL" >/dev/null 2>&1 || true

bash "$REPO_ROOT/deploy/installer.sh" uninstall --purge >/dev/null 2>&1 || true

e2e_check "install dir removed" "test ! -d '$INSTALL_DIR'"
e2e_check "wrapper removed" "test ! -f '$HOME/.local/bin/langstash'"

# ============================================================
e2e_case "E2E-5: Reinstall after uninstall reuses data"

LANGFUSE_SECRET_KEY="sk-lf-test" LANGFUSE_PUBLIC_KEY="pk-lf-test" LANGFUSE_BASE_URL="http://127.0.0.1:9999" \
bash "$REPO_ROOT/deploy/installer.sh" install \
    --package-url "file://$TARBALL" >/dev/null 2>&1 || true
echo "user-data" > "$INSTALL_DIR/data/custom.txt"

bash "$REPO_ROOT/deploy/installer.sh" uninstall >/dev/null 2>&1 || true

LANGFUSE_SECRET_KEY="sk-lf-test" LANGFUSE_PUBLIC_KEY="pk-lf-test" LANGFUSE_BASE_URL="http://127.0.0.1:9999" \
bash "$REPO_ROOT/deploy/installer.sh" install \
    --package-url "file://$TARBALL" >/dev/null 2>&1 || true

e2e_check "reinstall succeeds" "test -f '$INSTALL_DIR/current'"
e2e_check "user data preserved" "test -f '$INSTALL_DIR/data/custom.txt'"
purge_install

# ============================================================
# E2E-5b / E2E-5c: install-time update of [langfuse] keys in an existing
# config.toml. Scenario-local helpers below mutate config.toml between
# reinstalls the way a user would (custom lines, removed section / keys).
# ============================================================

# Seed user-customized content into config.toml: a custom key inside
# [storage], a trailing comment, and a custom section. The preserved-line
# assertions below depend on these exact lines.
seed_custom_config_lines() {
    python3 - "$CONFIG_FILE" <<'PYEOF'
import sys
path = sys.argv[1]
text = open(path).read()
if "[storage]" in text and "seeded_storage_note" not in text:
    text = text.replace("[storage]", '[storage]\nseeded_storage_note = "keep-me"', 1)
text += "\n# seeded custom comment\n[custom]\nseeded_key = \"seeded-value\"\n"
open(path, "w").write(text)
PYEOF
}

# Remove the whole [langfuse] section from config.toml (setup for the
# section-backfill reinstall).
strip_langfuse_section() {
    python3 - "$CONFIG_FILE" <<'PYEOF'
import re
import sys
path = sys.argv[1]
text = open(path).read()
m = re.search(r"^\[langfuse\]\s*$", text, re.MULTILINE)
if not m:
    sys.exit("no [langfuse] section found in " + path)
nxt = re.search(r"^\[", text[m.end():], re.MULTILINE)
end = m.end() + nxt.start() if nxt else len(text)
open(path, "w").write(text[:m.start()] + text[end:])
PYEOF
}

# Remove one key line from config.toml (setup for the key-backfill reinstall).
remove_langfuse_key() {
    python3 - "$CONFIG_FILE" "$1" <<'PYEOF'
import re
import sys
path, key = sys.argv[1], sys.argv[2]
text = open(path).read()
line_re = re.compile(rf"^{re.escape(key)}\s*=\s*.*$", re.MULTILINE)
new_text, count = line_re.subn("", text)
if count != 1:
    sys.exit(f"expected exactly one '{key}' line in {path}, found {count}")
open(path, "w").write(new_text)
PYEOF
}

# ============================================================
e2e_case "E2E-5b: Reinstall updates provided [langfuse] keys in existing config"
purge_install
CONFIG_FILE="$INSTALL_DIR/config/config.toml"
SEEDED_REF="/tmp/e2e-seeded-config-$$"

# Fresh install with three values: the first-install template path writes
# all three keys.
e2e_cmd "LANGFUSE_PUBLIC_KEY='pk-lf-old' LANGFUSE_SECRET_KEY='sk-lf-old' LANGFUSE_BASE_URL='http://old-lf:3000' bash '$REPO_ROOT/deploy/installer.sh' install --package-url 'file://$TARBALL'"
e2e_cmd_check "E2E-5b fresh install with three values exits 0"
e2e_kv_check "E2E-5b fresh install writes public_key" public_key "pk-lf-old" "$CONFIG_FILE"
e2e_kv_check "E2E-5b fresh install writes secret_key" secret_key "sk-lf-old" "$CONFIG_FILE"
e2e_kv_check "E2E-5b fresh install writes base_url" base_url "http://old-lf:3000" "$CONFIG_FILE"

# Seed user-customized content, then reinstall with three new values.
e2e_cmd "seed_custom_config_lines"
e2e_cmd_check "E2E-5b seeding custom config lines exits 0"
cp "$CONFIG_FILE" "$SEEDED_REF"

e2e_cmd "LANGFUSE_PUBLIC_KEY='pk-lf-new' LANGFUSE_SECRET_KEY='sk-lf-new' LANGFUSE_BASE_URL='http://new-lf:3000' bash '$REPO_ROOT/deploy/installer.sh' install --package-url 'file://$TARBALL'"
e2e_cmd_check "E2E-5b reinstall with three new values exits 0"
e2e_kv_check "E2E-5b reinstall updates public_key" public_key "pk-lf-new" "$CONFIG_FILE"
e2e_kv_check "E2E-5b reinstall updates secret_key" secret_key "sk-lf-new" "$CONFIG_FILE"
e2e_kv_check "E2E-5b reinstall updates base_url" base_url "http://new-lf:3000" "$CONFIG_FILE"
e2e_grep_check "E2E-5b seeded [storage] custom line preserved" '^seeded_storage_note = "keep-me"$' "$CONFIG_FILE"
e2e_grep_check "E2E-5b seeded comment preserved" '^# seeded custom comment$' "$CONFIG_FILE"
e2e_grep_check "E2E-5b seeded custom section preserved" '^\[custom\]$' "$CONFIG_FILE"
e2e_cmd "diff <(grep -Ev '^(public_key|secret_key|base_url) = ' '$SEEDED_REF') <(grep -Ev '^(public_key|secret_key|base_url) = ' '$CONFIG_FILE')"
e2e_cmd_check "E2E-5b non-[langfuse] lines unchanged after reinstall"

# Reinstall with only base_url provided: only that key changes.
e2e_cmd "LANGFUSE_PUBLIC_KEY='' LANGFUSE_SECRET_KEY='' LANGFUSE_BASE_URL='http://final-lf:3000' bash '$REPO_ROOT/deploy/installer.sh' install --package-url 'file://$TARBALL'"
e2e_cmd_check "E2E-5b reinstall with only base_url exits 0"
e2e_kv_check "E2E-5b only provided base_url updated" base_url "http://final-lf:3000" "$CONFIG_FILE"
e2e_kv_check "E2E-5b unprovided public_key keeps existing value" public_key "pk-lf-new" "$CONFIG_FILE"
e2e_kv_check "E2E-5b unprovided secret_key keeps existing value" secret_key "sk-lf-new" "$CONFIG_FILE"

# Reinstall with no Langfuse values: all three keys keep their values.
e2e_cmd "LANGFUSE_PUBLIC_KEY='' LANGFUSE_SECRET_KEY='' LANGFUSE_BASE_URL='' bash '$REPO_ROOT/deploy/installer.sh' install --package-url 'file://$TARBALL'"
e2e_cmd_check "E2E-5b reinstall with no langfuse values exits 0"
e2e_kv_check "E2E-5b public_key kept when nothing provided" public_key "pk-lf-new" "$CONFIG_FILE"
e2e_kv_check "E2E-5b secret_key kept when nothing provided" secret_key "sk-lf-new" "$CONFIG_FILE"
e2e_kv_check "E2E-5b base_url kept when nothing provided" base_url "http://final-lf:3000" "$CONFIG_FILE"

# Missing [langfuse] section: a reinstall with provided values recreates it.
e2e_cmd "strip_langfuse_section"
e2e_cmd_check "E2E-5b stripping [langfuse] section exits 0"
e2e_nogrep_check "E2E-5b [langfuse] section absent after strip" '^\[langfuse\]$' "$CONFIG_FILE"
e2e_cmd "LANGFUSE_PUBLIC_KEY='pk-lf-back' LANGFUSE_SECRET_KEY='sk-lf-back' LANGFUSE_BASE_URL='http://back-lf:3000' bash '$REPO_ROOT/deploy/installer.sh' install --package-url 'file://$TARBALL'"
e2e_cmd_check "E2E-5b reinstall after section removal exits 0"
e2e_grep_check "E2E-5b [langfuse] section recreated" '^\[langfuse\]$' "$CONFIG_FILE"
e2e_kv_check "E2E-5b public_key backfilled with provided value" public_key "pk-lf-back" "$CONFIG_FILE"
e2e_kv_check "E2E-5b secret_key backfilled with provided value" secret_key "sk-lf-back" "$CONFIG_FILE"
e2e_kv_check "E2E-5b base_url backfilled with provided value" base_url "http://back-lf:3000" "$CONFIG_FILE"

# Missing single key: a reinstall with only that value backfills it.
e2e_cmd "remove_langfuse_key secret_key"
e2e_cmd_check "E2E-5b removing secret_key line exits 0"
e2e_nogrep_check "E2E-5b secret_key line absent after removal" '^secret_key[[:space:]]*=' "$CONFIG_FILE"
e2e_cmd "LANGFUSE_PUBLIC_KEY='' LANGFUSE_SECRET_KEY='sk-lf-key2' LANGFUSE_BASE_URL='' bash '$REPO_ROOT/deploy/installer.sh' install --package-url 'file://$TARBALL'"
e2e_cmd_check "E2E-5b reinstall after key removal exits 0"
e2e_kv_check "E2E-5b secret_key backfilled with provided value" secret_key "sk-lf-key2" "$CONFIG_FILE"
e2e_kv_check "E2E-5b public_key unchanged when other key backfilled" public_key "pk-lf-back" "$CONFIG_FILE"
e2e_kv_check "E2E-5b base_url unchanged when other key backfilled" base_url "http://back-lf:3000" "$CONFIG_FILE"

rm -f "$SEEDED_REF"
purge_install

# ============================================================
e2e_case "E2E-5c: Service reloads updated [langfuse] config (/health flip)"
purge_install
CONFIG_FILE="$INSTALL_DIR/config/config.toml"

# Fresh install without any Langfuse values: the first-install template
# writes empty keys, so /health reports no_credentials (503). wait_health
# cannot succeed on 503, so keep HEALTH_TIMEOUT short for this install.
e2e_cmd "HEALTH_TIMEOUT=6 LANGFUSE_PUBLIC_KEY='' LANGFUSE_SECRET_KEY='' LANGFUSE_BASE_URL='' bash '$REPO_ROOT/deploy/installer.sh' install --package-url 'file://$TARBALL'"
e2e_cmd_check "E2E-5c fresh install without langfuse values exits 0"
e2e_kv_check "E2E-5c valueless fresh install leaves public_key empty" public_key "" "$CONFIG_FILE"
e2e_kv_check "E2E-5c valueless fresh install leaves secret_key empty" secret_key "" "$CONFIG_FILE"
e2e_kv_check "E2E-5c valueless fresh install uses default base_url" base_url "https://us.cloud.langfuse.com" "$CONFIG_FILE"
e2e_health_check "E2E-5c no credentials: /health is 503 no_credentials" 503 '"no_credentials"'

# Reinstall with three values: the existing file's keys are updated, the
# install flow restarts the service, and /health flips to configured. If the
# service did not reload the new file (still holding empty credentials in
# memory), the langfuse_configured check below fails.
e2e_cmd "LANGFUSE_PUBLIC_KEY='pk-lf-flip' LANGFUSE_SECRET_KEY='sk-lf-flip' LANGFUSE_BASE_URL='http://flip-lf:3000' bash '$REPO_ROOT/deploy/installer.sh' install --package-url 'file://$TARBALL'"
e2e_cmd_check "E2E-5c reinstall with three values exits 0"
e2e_health_check "E2E-5c after reinstall /health is 200 with langfuse_configured true" 200 '"langfuse_configured":true'
purge_install

# ============================================================
e2e_case "E2E-6: Legacy git layout migration"
purge_install

mkdir -p "$INSTALL_DIR/.git/objects" "$INSTALL_DIR/.git/refs"
echo "ref: refs/heads/main" > "$INSTALL_DIR/.git/HEAD"
mkdir -p "$INSTALL_DIR/exporter/src" "$INSTALL_DIR/hooks/claude-code"
mkdir -p "$INSTALL_DIR/config" "$INSTALL_DIR/data" "$INSTALL_DIR/logs"
echo "0.0.1-legacy" > "$INSTALL_DIR/VERSION"
echo "keep-me" > "$INSTALL_DIR/config/config.toml"

bash "$REPO_ROOT/deploy/installer.sh" upgrade \
    --package-url "file://$TARBALL" >/dev/null 2>&1 || true

e2e_check ".git removed" "test ! -d '$INSTALL_DIR/.git'"
e2e_check "legacy version migrated" "test -d '$INSTALL_DIR/versions/0.0.1-legacy'"
e2e_check "current pointer to new version" "[ \"\$(cat '$INSTALL_DIR/current')\" = '$VERSION' ]"
e2e_check "previous pointer to legacy" "[ \"\$(cat '$INSTALL_DIR/previous')\" = '0.0.1-legacy' ]"
e2e_check "config preserved" "grep -q 'keep-me' '$INSTALL_DIR/config/config.toml'"

bash "$REPO_ROOT/deploy/installer.sh" upgrade \
    --package-url "file://$TARBALL" >/dev/null 2>&1 || true
e2e_check "idempotent (no error on re-run)" "test -f '$INSTALL_DIR/current'"
purge_install

# ============================================================
e2e_case "E2E-7: Rollback"
purge_install

mkdir -p "$INSTALL_DIR/versions/0.1.0/exporter/.venv/bin" "$INSTALL_DIR/versions/0.2.0/exporter/.venv/bin"
printf '#!/bin/sh\nexit 0\n' > "$INSTALL_DIR/versions/0.1.0/exporter/.venv/bin/langstash"
chmod +x "$INSTALL_DIR/versions/0.1.0/exporter/.venv/bin/langstash"
printf '#!/bin/sh\nexit 0\n' > "$INSTALL_DIR/versions/0.2.0/exporter/.venv/bin/langstash"
chmod +x "$INSTALL_DIR/versions/0.2.0/exporter/.venv/bin/langstash"
echo "0.1.0" > "$INSTALL_DIR/versions/0.1.0/VERSION"
echo "0.2.0" > "$INSTALL_DIR/versions/0.2.0/VERSION"
echo "0.2.0" > "$INSTALL_DIR/current"
echo "0.1.0" > "$INSTALL_DIR/previous"

HEALTH_TIMEOUT=2 bash "$REPO_ROOT/deploy/installer.sh" rollback 2>&1 || true

e2e_check "current swapped to 0.1.0" "[ \"\$(cat '$INSTALL_DIR/current')\" = '0.1.0' ]"
e2e_check "previous swapped to 0.2.0" "[ \"\$(cat '$INSTALL_DIR/previous')\" = '0.2.0' ]"
purge_install

# ============================================================
e2e_case "E2E-8: Hook self-containment"

HITS="$(set +o pipefail; find "$REPO_ROOT/hooks" \( -name '*.sh' -o -name '*.py' \) -print0 2>/dev/null | xargs -0 grep -l '\.agent-exporter-to-langfuse/versions/' 2>/dev/null | wc -l | tr -d '[:space:]'; true)"
if [ "${HITS:-0}" = "0" ]; then e2e_pass "hooks don't reference versions/"; else e2e_fail "hooks reference versions/ ($HITS hits)"; fi

# ============================================================
e2e_case "E2E-9: No git commands in exporter/src"

GIT_HITS="$(set +o pipefail; find "$REPO_ROOT/exporter/src" -name '*.py' -print0 2>/dev/null | xargs -0 grep -lE 'git ls-remote|git fetch|git checkout|git clone' 2>/dev/null | wc -l | tr -d '[:space:]'; true)"
if [ "${GIT_HITS:-0}" = "0" ]; then e2e_pass "no git commands in exporter/src"; else e2e_fail "git commands found in exporter/src ($GIT_HITS hits)"; fi

# ============================================================
e2e_case "E2E-10: package.sh"

XPLAT_DIR="/tmp/e2e-xplat-$$"
OUTPUT=$(cd "$REPO_ROOT" && bash deploy/package.sh --output-dir "$XPLAT_DIR" 2>&1)
e2e_check "package.sh succeeds" "test -f '$XPLAT_DIR/SHA256SUMS'"
e2e_check "tarball created" "ls $XPLAT_DIR/agent-exporter-to-langfuse-*.tar.gz >/dev/null 2>&1"
rm -rf "$XPLAT_DIR"

# ============================================================
e2e_case "E2E-11: CLI subcommands"

CLI_HELP=$(cd "$REPO_ROOT/exporter" && uv run langstash --help 2>&1 || true)
for cmd in run start stop restart status upgrade rollback uninstall; do
    if echo "$CLI_HELP" | grep -q "$cmd"; then e2e_pass "CLI shows $cmd subcommand"; else e2e_fail "CLI shows $cmd subcommand"; fi
done

# ============================================================
e2e_case "E2E-12: cmd_install --version with v-prefix"
_TRACE_FILE="/tmp/e2e-trace-12-$$"
bash -x "$REPO_ROOT/deploy/installer.sh" install \
    --version v0.9.9 >"$_TRACE_FILE" 2>&1 || true
URL_LINE=$(grep 'tarball_url=.*https' "$_TRACE_FILE" | grep -v 'local ' | head -1)
if echo "$URL_LINE" | grep -q '/download/v0.9.9/agent-exporter-to-langfuse-0.9.9'; then
    e2e_pass "v-prefix stripped in cmd_install URL"
else
    e2e_fail "v-prefix stripped in cmd_install URL (got: $URL_LINE)"
fi
if echo "$URL_LINE" | grep -q 'vv0'; then
    e2e_fail "no double-v in cmd_install URL"
else
    e2e_pass "no double-v in cmd_install URL"
fi
rm -f "$_TRACE_FILE"

# ============================================================
e2e_case "E2E-13: cmd_upgrade --version with v-prefix"
mkdir -p "$INSTALL_DIR/versions" "$INSTALL_DIR"
echo "0.1.0" > "$INSTALL_DIR/current"
_TRACE_FILE="/tmp/e2e-trace-13-$$"
bash -x "$REPO_ROOT/deploy/installer.sh" upgrade \
    --version v0.9.9 >"$_TRACE_FILE" 2>&1 || true
URL_LINE=$(grep 'tarball_url=.*https' "$_TRACE_FILE" | grep -v 'local ' | head -1)
if echo "$URL_LINE" | grep -q '/download/v0.9.9/agent-exporter-to-langfuse-0.9.9'; then
    e2e_pass "v-prefix stripped in cmd_upgrade URL"
else
    e2e_fail "v-prefix stripped in cmd_upgrade URL (got: $URL_LINE)"
fi
if echo "$URL_LINE" | grep -q 'vv0'; then
    e2e_fail "no double-v in cmd_upgrade URL"
else
    e2e_pass "no double-v in cmd_upgrade URL"
fi
rm -rf "$INSTALL_DIR/versions" "$INSTALL_DIR/current" "$_TRACE_FILE"

# ============================================================
e2e_case "E2E-14: cmd_upgrade auto-query via GitHub API"
mkdir -p "$INSTALL_DIR/versions" "$INSTALL_DIR"
echo "0.1.0" > "$INSTALL_DIR/current"
MOCK_DIR="/tmp/e2e-mock-curl-$$"
mkdir -p "$MOCK_DIR"
cat > "$MOCK_DIR/curl" <<'MOCKEOF'
#!/usr/bin/env bash
if [[ "$*" == *"releases/latest"* ]]; then
    echo "curl: (6) Could not resolve host" >&2
    exit 6
fi
if [[ "$*" == *"api.github.com/repos"*"/releases"* ]]; then
    echo '[{"tag_name":"v0.9.9"}]'
    exit 0
fi
exec /usr/bin/curl "$@"
MOCKEOF
chmod +x "$MOCK_DIR/curl"
_TRACE_FILE="/tmp/e2e-trace-14-$$"
PATH="$MOCK_DIR:$PATH" bash -x "$REPO_ROOT/deploy/installer.sh" upgrade \
    >"$_TRACE_FILE" 2>&1 || true
URL_LINE=$(grep 'tarball_url=.*https' "$_TRACE_FILE" | grep -v 'local ' | head -1)
if echo "$URL_LINE" | grep -q '/download/v0.9.9/'; then
    e2e_pass "auto-query URL correct in cmd_upgrade"
else
    e2e_fail "auto-query URL correct in cmd_upgrade (got: $URL_LINE)"
fi
rm -rf "$MOCK_DIR" "$INSTALL_DIR/versions" "$INSTALL_DIR/current" "$_TRACE_FILE"

# ============================================================
# Cleanup
rm -rf "$PKG_DIR"

e2e_summary || exit 1
