#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="$HOME/.agent-exporter-to-langfuse"
REPO_OWNER="aliyun"
REPO_NAME="agent-exporter-to-langfuse"
REPO_URL="https://github.com/${REPO_OWNER}/${REPO_NAME}.git"

# --- Colors ---
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[0;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; }

# --- Parse args ---
SPECIFIED_VERSION=""
ARG_PRE_RELEASE=false
PASSTHROUGH_ARGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --version) SPECIFIED_VERSION="$2"; shift 2 ;;
        --pre-release) ARG_PRE_RELEASE=true; shift ;;
        *) PASSTHROUGH_ARGS+=("$1"); shift ;;
    esac
done

# --- Check prerequisites ---
if [ -d "$INSTALL_DIR/.git" ]; then
    info "Already installed at $INSTALL_DIR"
    echo "  To upgrade: bash $INSTALL_DIR/upgrade.sh"
    echo "  To reinstall: bash $INSTALL_DIR/uninstall.sh && re-run this script"
    exit 0
fi

if ! command -v git &>/dev/null; then
    error "git is required. Install it first."
    exit 1
fi

# --- Determine version ---
LATEST_TAG="$SPECIFIED_VERSION"

if [ -z "$LATEST_TAG" ]; then
    if [ "$ARG_PRE_RELEASE" = true ]; then
        LATEST_TAG=$(git ls-remote --tags --sort=-v:refname "$REPO_URL" 'v*' 2>/dev/null \
            | grep -o 'refs/tags/v[0-9][^{}]*$' | head -1 | sed 's|refs/tags/||') || true
    else
        LATEST_TAG=$(git ls-remote --tags --sort=-v:refname "$REPO_URL" 'v*' 2>/dev/null \
            | grep -o 'refs/tags/v[0-9][^{}]*$' | grep -v '-' | head -1 | sed 's|refs/tags/||') || true
    fi
fi

# --- Clone ---
if [ -n "$LATEST_TAG" ]; then
    info "Installing version: $LATEST_TAG"
    git clone --depth 1 --branch "$LATEST_TAG" "$REPO_URL" "$INSTALL_DIR"
else
    warn "Could not detect latest release, cloning main branch"
    git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
fi

exec bash "$INSTALL_DIR/install.sh" ${PASSTHROUGH_ARGS[@]+"${PASSTHROUGH_ARGS[@]}"}
