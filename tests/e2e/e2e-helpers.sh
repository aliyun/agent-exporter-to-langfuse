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
