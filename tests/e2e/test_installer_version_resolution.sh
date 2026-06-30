#!/usr/bin/env bash
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

e2e_suite "installer-version-resolution" 5

# ============================================================
e2e_case "V-1: resolve_latest_version 302 redirect success"
MOCK_DIR="/tmp/e2e-vr1-$$"
mkdir -p "$MOCK_DIR"
cat > "$MOCK_DIR/curl" <<'MOCKEOF'
#!/usr/bin/env bash
if [[ "$*" == *"releases/latest"* ]]; then
    echo "HTTP/2 302"
    echo "location: https://github.com/aliyun/agent-exporter-to-langfuse/releases/tag/v1.2.3"
    exit 0
fi
exec /usr/bin/curl "$@"
MOCKEOF
chmod +x "$MOCK_DIR/curl"

_TRACE_FILE="/tmp/e2e-vr1-trace-$$"
mkdir -p "$INSTALL_DIR"
echo "0.1.0" > "$INSTALL_DIR/current"
PATH="$MOCK_DIR:$PATH" bash -x "$REPO_ROOT/deploy/installer.sh" upgrade \
    >"$_TRACE_FILE" 2>&1 || true

URL_LINE=$(grep 'tarball_url=.*https' "$_TRACE_FILE" | grep -v 'local ' | head -1)
if echo "$URL_LINE" | grep -q '/download/v1.2.3/agent-exporter-to-langfuse-1.2.3'; then
    e2e_pass "302 redirect returns correct version"
else
    e2e_fail "302 redirect returns correct version (got URL: $URL_LINE)"
fi
REDIRECT_MSG=$(grep 'Latest version (via 302 redirect)' "$_TRACE_FILE" || true)
if [ -n "$REDIRECT_MSG" ]; then
    e2e_pass "302 redirect info message present"
else
    e2e_fail "302 redirect info message present"
fi
rm -rf "$MOCK_DIR" "$_TRACE_FILE"
rm -rf "$INSTALL_DIR/versions" "$INSTALL_DIR/current"

# ============================================================
e2e_case "V-2: fallback to REST API when 302 redirect fails"
MOCK_DIR="/tmp/e2e-vr2-$$"
mkdir -p "$MOCK_DIR"
cat > "$MOCK_DIR/curl" <<'MOCKEOF'
#!/usr/bin/env bash
if [[ "$*" == *"releases/latest"* ]]; then
    echo "curl: (6) Could not resolve host" >&2
    exit 6
fi
if [[ "$*" == *"api.github.com/repos"*"/releases"* ]]; then
    echo '[{"tag_name":"v2.0.0"}]'
    exit 0
fi
exec /usr/bin/curl "$@"
MOCKEOF
chmod +x "$MOCK_DIR/curl"

_TRACE_FILE="/tmp/e2e-vr2-trace-$$"
mkdir -p "$INSTALL_DIR"
echo "0.1.0" > "$INSTALL_DIR/current"
PATH="$MOCK_DIR:$PATH" bash -x "$REPO_ROOT/deploy/installer.sh" upgrade \
    >"$_TRACE_FILE" 2>&1 || true

URL_LINE=$(grep 'tarball_url=.*https' "$_TRACE_FILE" | grep -v 'local ' | head -1)
if echo "$URL_LINE" | grep -q '/download/v2.0.0/agent-exporter-to-langfuse-2.0.0'; then
    e2e_pass "REST API fallback returns correct version"
else
    e2e_fail "REST API fallback returns correct version (got URL: $URL_LINE)"
fi
FALLBACK_WARN=$(grep 'falling back to REST API' "$_TRACE_FILE" || true)
if [ -n "$FALLBACK_WARN" ]; then
    e2e_pass "fallback warn message present"
else
    e2e_fail "fallback warn message present"
fi
rm -rf "$MOCK_DIR" "$_TRACE_FILE"
rm -rf "$INSTALL_DIR/versions" "$INSTALL_DIR/current"

# ============================================================
e2e_case "V-3: dual failure reports both reasons and exits non-zero"
MOCK_DIR="/tmp/e2e-vr3-$$"
mkdir -p "$MOCK_DIR"
cat > "$MOCK_DIR/curl" <<'MOCKEOF'
#!/usr/bin/env bash
if [[ "$*" == *"releases/latest"* ]]; then
    echo "curl: (6) Could not resolve host" >&2
    exit 6
fi
if [[ "$*" == *"api.github.com/repos"*"/releases"* ]]; then
    echo "curl: (28) Connection timed out" >&2
    exit 28
fi
exec /usr/bin/curl "$@"
MOCKEOF
chmod +x "$MOCK_DIR/curl"

_OUTPUT_FILE="/tmp/e2e-vr3-output-$$"
mkdir -p "$INSTALL_DIR"
echo "0.1.0" > "$INSTALL_DIR/current"
EXIT_CODE=0
PATH="$MOCK_DIR:$PATH" bash "$REPO_ROOT/deploy/installer.sh" upgrade \
    >"$_OUTPUT_FILE" 2>&1 || EXIT_CODE=$?

if [ "$EXIT_CODE" -ne 0 ]; then
    e2e_pass "dual failure exits non-zero (exit=$EXIT_CODE)"
else
    e2e_fail "dual failure exits non-zero (exit=$EXIT_CODE)"
fi
REDIRECT_REASON=$(grep '302 redirect:' "$_OUTPUT_FILE" || true)
API_REASON=$(grep 'REST API:' "$_OUTPUT_FILE" || true)
if [ -n "$REDIRECT_REASON" ] && [ -n "$API_REASON" ]; then
    e2e_pass "dual failure reports both error reasons"
else
    e2e_fail "dual failure reports both error reasons (redirect='$REDIRECT_REASON', api='$API_REASON')"
fi
rm -rf "$MOCK_DIR" "$_OUTPUT_FILE"
rm -rf "$INSTALL_DIR/versions" "$INSTALL_DIR/current"

# ============================================================
e2e_case "V-4: --version <explicit> bypasses version resolution"
MOCK_DIR="/tmp/e2e-vr4-$$"
mkdir -p "$MOCK_DIR"
cat > "$MOCK_DIR/curl" <<'MOCKEOF'
#!/usr/bin/env bash
if [[ "$*" == *"releases/latest"* ]] || [[ "$*" == *"api.github.com"* ]]; then
    echo "SHOULD_NOT_BE_CALLED" >&2
    exit 1
fi
exec /usr/bin/curl "$@"
MOCKEOF
chmod +x "$MOCK_DIR/curl"

_TRACE_FILE="/tmp/e2e-vr4-trace-$$"
mkdir -p "$INSTALL_DIR"
echo "0.1.0" > "$INSTALL_DIR/current"
PATH="$MOCK_DIR:$PATH" bash -x "$REPO_ROOT/deploy/installer.sh" upgrade \
    --version 3.0.0 >"$_TRACE_FILE" 2>&1 || true

URL_LINE=$(grep 'tarball_url=.*https' "$_TRACE_FILE" | grep -v 'local ' | head -1)
if echo "$URL_LINE" | grep -q '/download/v3.0.0/agent-exporter-to-langfuse-3.0.0'; then
    e2e_pass "--version explicit bypasses resolution"
else
    e2e_fail "--version explicit bypasses resolution (got URL: $URL_LINE)"
fi
NO_RAW_CALL=$(grep 'SHOULD_NOT_BE_CALLED' "$_TRACE_FILE" || true)
if [ -z "$NO_RAW_CALL" ]; then
    e2e_pass "no raw/API call with --version"
else
    e2e_fail "no raw/API call with --version"
fi
rm -rf "$MOCK_DIR" "$_TRACE_FILE"
rm -rf "$INSTALL_DIR/versions" "$INSTALL_DIR/current"

# ============================================================
e2e_case "V-5: tarball 404 mentions version and suggests --version"
MOCK_DIR="/tmp/e2e-vr5-$$"
mkdir -p "$MOCK_DIR"
cat > "$MOCK_DIR/curl" <<'MOCKEOF'
#!/usr/bin/env bash
if [[ "$*" == *"releases/latest"* ]]; then
    echo "HTTP/2 302"
    echo "location: https://github.com/aliyun/agent-exporter-to-langfuse/releases/tag/v9.9.9"
    exit 0
fi
if [[ "$*" == *"/releases/download/v9.9.9/"*".tar.gz"* ]]; then
    echo "curl: (22) The requested URL returned error: 404 Not Found" >&2
    exit 22
fi
exec /usr/bin/curl "$@"
MOCKEOF
chmod +x "$MOCK_DIR/curl"

_OUTPUT_FILE="/tmp/e2e-vr5-output-$$"
purge_install
mkdir -p "$INSTALL_DIR"
echo "0.1.0" > "$INSTALL_DIR/current"
EXIT_CODE=0
PATH="$MOCK_DIR:$PATH" bash "$REPO_ROOT/deploy/installer.sh" upgrade \
    >"$_OUTPUT_FILE" 2>&1 || EXIT_CODE=$?

if [ "$EXIT_CODE" -ne 0 ]; then
    e2e_pass "tarball 404 exits non-zero (exit=$EXIT_CODE)"
else
    e2e_fail "tarball 404 exits non-zero (exit=$EXIT_CODE)"
fi
VERSION_MENTIONED=$(grep 'v9.9.9' "$_OUTPUT_FILE" || true)
if [ -n "$VERSION_MENTIONED" ]; then
    e2e_pass "error mentions version v9.9.9"
else
    e2e_fail "error mentions version v9.9.9"
fi
MAY_LACK=$(grep 'may not have a release' "$_OUTPUT_FILE" || true)
if [ -n "$MAY_LACK" ]; then
    e2e_pass "error indicates version may lack a release"
else
    e2e_fail "error indicates version may lack a release"
fi
SUGGEST_VERSION=$(grep '\-\-version' "$_OUTPUT_FILE" || true)
if [ -n "$SUGGEST_VERSION" ]; then
    e2e_pass "error suggests --version"
else
    e2e_fail "error suggests --version"
fi
rm -rf "$MOCK_DIR" "$_OUTPUT_FILE"
purge_install

# ============================================================
# Cleanup
rm -rf "$INSTALL_DIR/versions" "$INSTALL_DIR/current" 2>/dev/null || true

e2e_summary || exit 1
