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
        API_URL="https://api.github.com/repos/${REPO_OWNER}/${REPO_NAME}/releases"
    else
        API_URL="https://api.github.com/repos/${REPO_OWNER}/${REPO_NAME}/releases/latest"
    fi
    if command -v curl &>/dev/null; then
        LATEST_TAG=$(curl -fsSL --connect-timeout 5 "$API_URL" 2>/dev/null \
            | grep '"tag_name"' | head -1 \
            | sed 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/') || true
    elif command -v wget &>/dev/null; then
        LATEST_TAG=$(wget -qO- --timeout=5 "$API_URL" 2>/dev/null \
            | grep '"tag_name"' | head -1 \
            | sed 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/') || true
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

export LANGFUSE_SECRET_KEY="${LANGFUSE_SECRET_KEY:-}"
export LANGFUSE_PUBLIC_KEY="${LANGFUSE_PUBLIC_KEY:-}"
export LANGFUSE_BASE_URL="${LANGFUSE_BASE_URL:-}"
export LANGFUSE_USER_ID="${LANGFUSE_USER_ID:-}"
export LANGFUSE_TAGS="${LANGFUSE_TAGS:-}"

exec bash "$INSTALL_DIR/install.sh" ${PASSTHROUGH_ARGS[@]+"${PASSTHROUGH_ARGS[@]}"}
