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

e2e_suite "versioned-upgrade" 40

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
# Cleanup
rm -rf "$PKG_DIR"

e2e_summary || exit 1
