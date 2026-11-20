#!/usr/bin/env bash
# E2E test helper — source this file to get structured marker output
# alongside human-readable colored text.
#
# Usage:
#   source tests/e2e/e2e-helpers.sh
#   e2e_suite "my-suite" 5
#   e2e_case "test one"
#   e2e_pass "test one"
#   e2e_case "test two"
#   e2e_fail "test two"
#   e2e_summary

_E2E_RED='\033[0;31m'
_E2E_GREEN='\033[0;32m'
_E2E_BOLD='\033[1m'
_E2E_NC='\033[0m'

_E2E_PASS_COUNT=0
_E2E_FAIL_COUNT=0
_E2E_TOTAL=0
_E2E_SUITE_NAME=""

e2e_suite() {
    _E2E_SUITE_NAME="${1:-unnamed}"
    _E2E_TOTAL="${2:-0}"
    _E2E_PASS_COUNT=0
    _E2E_FAIL_COUNT=0
    echo "##e2e## suite ${_E2E_SUITE_NAME} ${_E2E_TOTAL}"
    echo -e "${_E2E_BOLD}=== ${_E2E_SUITE_NAME} (${_E2E_TOTAL} tests) ===${_E2E_NC}"
}

e2e_case() {
    echo "##e2e## case $1"
    echo -e "${_E2E_BOLD}--- $1 ---${_E2E_NC}"
}

e2e_pass() {
    _E2E_PASS_COUNT=$((_E2E_PASS_COUNT + 1))
    echo "##e2e## pass $1"
    echo -e "  ${_E2E_GREEN}PASS${_E2E_NC} $1"
}

e2e_fail() {
    _E2E_FAIL_COUNT=$((_E2E_FAIL_COUNT + 1))
    echo "##e2e## fail $1"
    echo -e "  ${_E2E_RED}FAIL${_E2E_NC} $1"
}

e2e_summary() {
    local total=$((_E2E_PASS_COUNT + _E2E_FAIL_COUNT))
    echo "##e2e## summary ${_E2E_PASS_COUNT} ${_E2E_FAIL_COUNT} ${total}"
    echo -e "${_E2E_BOLD}=== Summary ===${_E2E_NC}"
    echo -e "  Total: ${total}  ${_E2E_GREEN}Passed: ${_E2E_PASS_COUNT}${_E2E_NC}  ${_E2E_RED}Failed: ${_E2E_FAIL_COUNT}${_E2E_NC}"
    if [ "${_E2E_FAIL_COUNT}" -gt 0 ]; then
        return 1
    fi
    return 0
}

# check() helper — compatible with existing E2E pattern
e2e_check() {
    local name="$1"
    local cmd="$2"
    e2e_case "$name"
    if eval "$cmd" 2>/dev/null; then
        e2e_pass "$name"
    else
        e2e_fail "$name"
    fi
}

# ============================================================
# Diagnostics-compliant check forms (AGENTS.md test diagnostics): unlike
# e2e_check, these capture the checked command's combined stdout/stderr and
# exit code, and echo both on failure instead of discarding them.
# ============================================================

# e2e_cmd <command> — run command via eval, capturing its combined
# stdout/stderr and exit code into E2E_CMD_OUT / E2E_CMD_RC. Does not abort
# the suite when the command fails.
e2e_cmd() {
    E2E_CMD_OUT=""
    E2E_CMD_RC=0
    E2E_CMD_OUT="$(eval "$1" 2>&1)" || E2E_CMD_RC=$?
}

# e2e_cmd_check <name> — PASS when the last e2e_cmd exited 0; on failure echo
# the command's exit code and its captured combined output.
e2e_cmd_check() {
    local name="$1"
    e2e_case "$name"
    if [ "$E2E_CMD_RC" -eq 0 ]; then
        e2e_pass "$name"
    else
        e2e_fail "$name"
        echo "  [diag] command exited with code: $E2E_CMD_RC" >&2
        echo "  [diag] combined stdout/stderr:" >&2
        if [ -n "$E2E_CMD_OUT" ]; then
            printf '%s\n' "$E2E_CMD_OUT" | sed 's/^/    /' >&2
        else
            echo "    (no output)" >&2
        fi
    fi
}

# e2e_kv_check <name> <key> <expected-value> <file> — PASS when the file
# contains the exact TOML line `key = "value"`; on failure echo the expected
# line and the actual line found in the file.
e2e_kv_check() {
    local name="$1" key="$2" expected_value="$3" file="$4"
    e2e_case "$name"
    local actual="" rc=0
    actual="$(grep -E "^${key}[[:space:]]*=" "$file" 2>&1 | head -n 1)" || rc=$?
    local expected="${key} = \"${expected_value}\""
    if [ "$actual" = "$expected" ]; then
        e2e_pass "$name"
    else
        e2e_fail "$name"
        echo "  [diag] expected exact line: $expected" >&2
        if [ -z "$actual" ]; then
            echo "  [diag] actual: no '${key}' line in $file (grep exit code: $rc)" >&2
        else
            echo "  [diag] actual line: $actual" >&2
        fi
    fi
}

# e2e_grep_check <name> <pattern> <file> — PASS when grep -E matches; on
# failure echo the pattern and the actual file content.
e2e_grep_check() {
    local name="$1" pattern="$2" file="$3"
    e2e_case "$name"
    local out="" rc=0
    out="$(grep -E "$pattern" "$file" 2>&1)" || rc=$?
    if [ "$rc" -eq 0 ]; then
        e2e_pass "$name"
    else
        e2e_fail "$name"
        echo "  [diag] pattern not found (grep exit code: $rc): $pattern" >&2
        echo "  [diag] file: $file — actual content:" >&2
        sed 's/^/    /' "$file" >&2
    fi
}

# e2e_nogrep_check <name> <pattern> <file> — PASS when grep -E matches
# nothing (exit 1); a match or a grep error fails with the matching lines /
# error output.
e2e_nogrep_check() {
    local name="$1" pattern="$2" file="$3"
    e2e_case "$name"
    local out="" rc=0
    out="$(grep -E "$pattern" "$file" 2>&1)" || rc=$?
    if [ "$rc" -eq 1 ]; then
        e2e_pass "$name"
    else
        e2e_fail "$name"
        echo "  [diag] expected pattern to be absent, grep exited with code: $rc — $pattern" >&2
        if [ -n "$out" ]; then
            printf '%s\n' "$out" | sed 's/^/    /' >&2
        else
            echo "    (no output)" >&2
        fi
    fi
}

# e2e_health_check <name> <expected-http-code> <expected-body-substring>
# Poll the langstash /health endpoint (http://127.0.0.1:5288/health) until it
# answers (up to 20 seconds). PASS when the HTTP status code and a body
# substring both match; on failure echo expected and actual values.
e2e_health_check() {
    local name="$1" expected_code="$2" expected_body="$3"
    local url="http://127.0.0.1:5288/health"
    e2e_case "$name"
    local raw="" rc=1 elapsed=0 body="" code=""
    while [ "$rc" -ne 0 ] && [ "$elapsed" -lt 20 ]; do
        raw="$(curl -s -w '\n%{http_code}' "$url" 2>&1)" && rc=0 || rc=$?
        if [ "$rc" -ne 0 ]; then
            sleep 2
            elapsed=$((elapsed + 2))
        fi
    done
    if [ "$rc" -eq 0 ]; then
        code="${raw##*$'\n'}"
        body="${raw%$'\n'*}"
    fi
    if [ "$rc" -eq 0 ] && [ "$code" = "$expected_code" ] \
        && printf '%s' "$body" | grep -qF "$expected_body"; then
        e2e_pass "$name"
    else
        e2e_fail "$name"
        echo "  [diag] expected: HTTP $expected_code with body containing: $expected_body" >&2
        if [ "$rc" -eq 0 ]; then
            echo "  [diag] actual: HTTP $code, body: $body" >&2
        else
            echo "  [diag] actual: /health did not answer within ${elapsed}s (curl exit code: $rc)" >&2
            echo "  [diag] curl output: $raw" >&2
        fi
    fi
}

