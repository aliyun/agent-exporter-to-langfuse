#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="$HOME/.agent-exporter-to-langfuse"
REPO_OWNER="aliyun"
REPO_NAME="agent-exporter-to-langfuse"

# --- Colors ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BOLD='\033[1m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; }

ARG_PRE_RELEASE=false
PASSTHROUGH_ARGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --pre-release) ARG_PRE_RELEASE=true; shift ;;
        *) PASSTHROUGH_ARGS+=("$1"); shift ;;
    esac
done

# --- SemVer compare: returns 1 if a > b, -1 if a < b, 0 if equal ---
# Handles pre-release suffixes: 0.2.0 > 0.2.0-alpha, 0.2.0-beta > 0.2.0-alpha
semver_compare() {
    local a="${1#v}" b="${2#v}"
    local a_core="${a%%-*}" b_core="${b%%-*}"
    local a_pre="" b_pre=""
    case "$a" in *-*) a_pre="${a#*-}" ;; esac
    case "$b" in *-*) b_pre="${b#*-}" ;; esac

    IFS='.' read -r a1 a2 a3 <<< "$a_core"
    IFS='.' read -r b1 b2 b3 <<< "$b_core"
    a1=${a1:-0}; a2=${a2:-0}; a3=${a3:-0}
    b1=${b1:-0}; b2=${b2:-0}; b3=${b3:-0}

    if [ "$a1" -gt "$b1" ] 2>/dev/null; then echo 1; return; fi
    if [ "$a1" -lt "$b1" ] 2>/dev/null; then echo -1; return; fi
    if [ "$a2" -gt "$b2" ] 2>/dev/null; then echo 1; return; fi
    if [ "$a2" -lt "$b2" ] 2>/dev/null; then echo -1; return; fi
    if [ "$a3" -gt "$b3" ] 2>/dev/null; then echo 1; return; fi
    if [ "$a3" -lt "$b3" ] 2>/dev/null; then echo -1; return; fi

    # Same core version — compare pre-release
    if [ -z "$a_pre" ] && [ -z "$b_pre" ]; then echo 0; return; fi
    if [ -z "$a_pre" ]; then echo 1; return; fi   # no pre > has pre
    if [ -z "$b_pre" ]; then echo -1; return; fi  # has pre < no pre
    if [ "$a_pre" \> "$b_pre" ]; then echo 1; return; fi
    if [ "$a_pre" \< "$b_pre" ]; then echo -1; return; fi
    echo 0
}

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ "$(cd "$SCRIPT_DIR" && pwd)" != "$(cd "$INSTALL_DIR" 2>/dev/null && pwd 2>/dev/null)" ] 2>/dev/null; then
    error "Please run upgrade from the install directory:"
    echo "  bash $INSTALL_DIR/upgrade.sh"
    exit 1
fi

if [ ! -d "$INSTALL_DIR/.git" ]; then
    error "Not installed at $INSTALL_DIR. Run install.sh first."
    exit 1
fi

cd "$INSTALL_DIR"

OLD_VERSION=$(cat VERSION 2>/dev/null || echo "unknown")
info "Current version: v${OLD_VERSION}"

# --- Fetch tags ---
git fetch origin --tags --quiet 2>/dev/null || true

# --- Get latest release tag ---
LATEST_TAG=""

if [ "$ARG_PRE_RELEASE" = true ]; then
    API_URL="https://api.github.com/repos/${REPO_OWNER}/${REPO_NAME}/releases"
else
    API_URL="https://api.github.com/repos/${REPO_OWNER}/${REPO_NAME}/releases/latest"
fi

if command -v curl &>/dev/null; then
    LATEST_TAG=$(curl -fsSL --connect-timeout 5 "$API_URL" 2>/dev/null \
        | grep '"tag_name"' | head -1 \
        | sed 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/') || true
fi

# Fallback: latest v* tag from git
if [ -z "$LATEST_TAG" ]; then
    if [ "$ARG_PRE_RELEASE" = true ]; then
        LATEST_TAG=$(git tag -l 'v*' --sort=-v:refname 2>/dev/null | head -1) || true
    else
        LATEST_TAG=$(git tag -l 'v*' --sort=-v:refname 2>/dev/null | grep -v '-' | head -1) || true
    fi
fi

if [ -z "$LATEST_TAG" ]; then
    error "No release tags found."
    exit 1
fi

# --- Compare versions ---
CURRENT_TAG=$(git describe --tags --exact-match HEAD 2>/dev/null || echo "")
LOCAL_VER="${CURRENT_TAG:-v${OLD_VERSION}}"
CMP=$(semver_compare "$LATEST_TAG" "$LOCAL_VER")

if [ "$CMP" -le 0 ]; then
    info "Already up to date (${LOCAL_VER})."
    exit 0
fi

# --- Upgrade ---
info "Upgrading: ${LOCAL_VER} → ${LATEST_TAG} ..."
git checkout "$LATEST_TAG" --quiet

NEW_VERSION=$(cat VERSION 2>/dev/null || echo "unknown")

bash "$INSTALL_DIR/install.sh" --upgrade ${PASSTHROUGH_ARGS[@]+"${PASSTHROUGH_ARGS[@]}"}

echo ""
info "Upgrade complete: v${OLD_VERSION} → v${NEW_VERSION}"
