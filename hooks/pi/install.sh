#!/usr/bin/env bash
set -euo pipefail

PI_HOME="$HOME/.pi"
PI_SETTINGS_FILE="$PI_HOME/agent/settings.json"
PI_HOOK_DIR="$PI_HOME/hooks/langfuse"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BUNDLE_SRC="$SCRIPT_DIR/dist/index.mjs"
# Shared env directory
LANGFUSE_PROFILE_DIR="$HOME/.agent-exporter-to-langfuse/config"
LANGFUSE_ENV_FILE="$LANGFUSE_PROFILE_DIR/pi.env"

# --- Colors ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; }
prompt_input() { echo -ne "${CYAN}$1${NC}"; }

# --- Parse arguments ---
LANGFUSE_SECRET_KEY="${LANGFUSE_SECRET_KEY:-}"
LANGFUSE_PUBLIC_KEY="${LANGFUSE_PUBLIC_KEY:-}"
LANGFUSE_BASE_URL="${LANGFUSE_BASE_URL:-}"
LANGFUSE_USER_ID="${LANGFUSE_USER_ID:-}"
LANGFUSE_TAGS="${LANGFUSE_TAGS:-}"
UPGRADE_MODE=false
AUTO_YES=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --secret-key)  LANGFUSE_SECRET_KEY="$2"; shift 2 ;;
        --public-key)  LANGFUSE_PUBLIC_KEY="$2"; shift 2 ;;
        --base-url)    LANGFUSE_BASE_URL="$2"; shift 2 ;;
        --user-id)     LANGFUSE_USER_ID="$2"; shift 2 ;;
        --tags)        LANGFUSE_TAGS="$2"; shift 2 ;;
        -y|--yes)    AUTO_YES=true; shift ;;
        --upgrade)   UPGRADE_MODE=true; AUTO_YES=true; shift ;;
        *) shift ;;
    esac
done

# --- 1. Check prerequisites ---
if ! command -v npm &>/dev/null; then
    error "npm is not installed. Install Node.js first: https://nodejs.org/"
    exit 1
fi

if ! command -v python3 &>/dev/null; then
    error "python3 is not installed but is required to inspect $PI_SETTINGS_FILE"
    exit 1
fi

if ! command -v pi &>/dev/null; then
    error "Pi CLI not found. Please install Pi Coding Agent first."
    exit 1
fi

# --- 2. Collect Langfuse credentials ---
if [ -n "$LANGFUSE_SECRET_KEY" ] && [ -n "$LANGFUSE_PUBLIC_KEY" ] && [ -n "$LANGFUSE_BASE_URL" ]; then
    :
elif [ "$UPGRADE_MODE" = true ] && [ -f "$LANGFUSE_ENV_FILE" ]; then
    # shellcheck disable=SC1090
    . "$LANGFUSE_ENV_FILE"
    if [ -n "$LANGFUSE_SECRET_KEY" ] && [ -n "$LANGFUSE_PUBLIC_KEY" ] && [ -n "$LANGFUSE_BASE_URL" ]; then
        info "Upgrade mode: loaded credentials from $LANGFUSE_ENV_FILE"
    else
        error "Upgrade mode: $LANGFUSE_ENV_FILE is incomplete; rerun without --upgrade to reconfigure."
        exit 1
    fi
else
    if [ -f "$LANGFUSE_ENV_FILE" ]; then
        # shellcheck disable=SC1090
        . "$LANGFUSE_ENV_FILE"
    fi

    echo ""
    echo "=== Langfuse Configuration ==="
    echo "Enter your Langfuse credentials (get them from Langfuse project settings → API Keys)."
    echo ""

    DEFAULT_BASE_URL="${LANGFUSE_BASE_URL:-https://us.cloud.langfuse.com}"
    DEFAULT_PUBLIC_KEY="${LANGFUSE_PUBLIC_KEY:-}"
    DEFAULT_SECRET_KEY="${LANGFUSE_SECRET_KEY:-}"

    if [ -n "$DEFAULT_BASE_URL" ]; then
        prompt_input "Langfuse Base URL [$DEFAULT_BASE_URL]: "
    else
        prompt_input "Langfuse Base URL: "
    fi
    read -r INPUT_BASE_URL
    LANGFUSE_BASE_URL="${INPUT_BASE_URL:-$DEFAULT_BASE_URL}"

    if [ -n "$DEFAULT_PUBLIC_KEY" ]; then
        prompt_input "Langfuse Public Key [${DEFAULT_PUBLIC_KEY:0:12}...]: "
    else
        prompt_input "Langfuse Public Key (pk-lf-...): "
    fi
    read -r INPUT_PUBLIC_KEY
    LANGFUSE_PUBLIC_KEY="${INPUT_PUBLIC_KEY:-$DEFAULT_PUBLIC_KEY}"

    if [ -n "$DEFAULT_SECRET_KEY" ]; then
        prompt_input "Langfuse Secret Key [${DEFAULT_SECRET_KEY:0:12}...]: "
    else
        prompt_input "Langfuse Secret Key (sk-lf-...): "
    fi
    read -r INPUT_SECRET_KEY
    LANGFUSE_SECRET_KEY="${INPUT_SECRET_KEY:-$DEFAULT_SECRET_KEY}"

    if [ -z "$LANGFUSE_PUBLIC_KEY" ] || [ -z "$LANGFUSE_SECRET_KEY" ]; then
        error "Public Key and Secret Key are required."
        exit 1
    fi

    if [ -n "$LANGFUSE_USER_ID" ]; then
        prompt_input "Langfuse User ID [$LANGFUSE_USER_ID]: "
    else
        prompt_input "Langfuse User ID [default: OS username]: "
    fi
    read -r INPUT_USER_ID
    LANGFUSE_USER_ID="${INPUT_USER_ID:-$LANGFUSE_USER_ID}"

    if [ -n "$LANGFUSE_TAGS" ]; then
        prompt_input "Extra Tags [$LANGFUSE_TAGS]: "
    else
        prompt_input "Extra Tags (e.g. team:olap,env:prod) [none]: "
    fi
    read -r INPUT_TAGS
    LANGFUSE_TAGS="${INPUT_TAGS:-$LANGFUSE_TAGS}"

    echo ""
fi

# --- 3. Mutual exclusion check against the npm pi-langfuse extension ---
# Both extensions would trace the same Pi run and produce duplicate traces.
PI_LANGFUSE_REGISTERED=$(python3 -c "
import json, sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    data = json.loads(path.read_text())
except (OSError, ValueError):
    print('no')
    sys.exit(0)

packages = data.get('packages')
if not isinstance(packages, list):
    print('no')
    sys.exit(0)

hit = any('pi-langfuse' in str(entry) for entry in packages)
print('yes' if hit else 'no')
" "$PI_SETTINGS_FILE")

if [ "$PI_LANGFUSE_REGISTERED" = "yes" ]; then
    warn "The npm pi-langfuse extension is registered in $PI_SETTINGS_FILE."
    warn "Running both extensions sends duplicate traces for every Pi run — pick one."
    warn "Remove it with: pi remove npm:pi-langfuse"
    if [ "$AUTO_YES" != true ]; then
        read -rp "Continue installing this hook anyway? [y/N] " answer
        if [[ ! "$answer" =~ ^[Yy]$ ]]; then
            info "Installation aborted."
            exit 0
        fi
    fi
fi

# --- 4. Build the bundle if needed and install it ---
if [ ! -f "$BUNDLE_SRC" ]; then
    info "Bundle not found at $BUNDLE_SRC, building on-demand..."
    if ! ( cd "$SCRIPT_DIR" && npm install --ignore-scripts && npm run build ); then
        build_rc=$?
        error "pi hook build failed (exit code: $build_rc)"
        exit 1
    fi
fi

if [ ! -f "$BUNDLE_SRC" ]; then
    error "Build produced no bundle at $BUNDLE_SRC"
    exit 1
fi

mkdir -p "$PI_HOOK_DIR"
cp "$BUNDLE_SRC" "$PI_HOOK_DIR/index.mjs"
cat > "$PI_HOOK_DIR/package.json" <<'PKGJSON'
{
  "name": "agent-exporter-to-langfuse-pi",
  "version": "0.1.0",
  "private": true,
  "description": "Send Pi Coding Agent telemetry to Langfuse via agent-exporter-to-langfuse",
  "type": "module",
  "pi": {
    "extensions": ["./index.mjs"]
  },
  "engines": {
    "node": ">=22"
  }
}
PKGJSON
info "Hook installed to $PI_HOOK_DIR"

# --- 5. Register the hook directory with Pi (idempotent) ---
ALREADY_REGISTERED=$(python3 -c "
import json, sys
from pathlib import Path

path = Path(sys.argv[1])
target = sys.argv[2]
try:
    data = json.loads(path.read_text())
except (OSError, ValueError):
    print('no')
    sys.exit(0)

packages = data.get('packages')
if not isinstance(packages, list):
    print('no')
    sys.exit(0)

print('yes' if any(str(entry) == target for entry in packages) else 'no')
" "$PI_SETTINGS_FILE" "$PI_HOOK_DIR")

if [ "$ALREADY_REGISTERED" = "yes" ]; then
    info "Already registered in $PI_SETTINGS_FILE (skipping pi install)"
else
    if pi install "$PI_HOOK_DIR"; then
        info "Registered with Pi: $PI_HOOK_DIR"
    else
        install_rc=$?
        error "pi install failed (exit code: $install_rc) for $PI_HOOK_DIR"
        exit 1
    fi
fi

# --- 6. Write env file (standalone) ---
# Build final tags: ensure agent name is included exactly once
case ",$LANGFUSE_TAGS," in
    *,pi,*) FINAL_TAGS="$LANGFUSE_TAGS" ;;
    *) FINAL_TAGS="pi${LANGFUSE_TAGS:+,$LANGFUSE_TAGS}" ;;
esac

mkdir -p "$LANGFUSE_PROFILE_DIR"
{
    echo "export LANGFUSE_BASE_URL=\"$LANGFUSE_BASE_URL\""
    echo "export LANGFUSE_PUBLIC_KEY=\"$LANGFUSE_PUBLIC_KEY\""
    echo "export LANGFUSE_SECRET_KEY=\"$LANGFUSE_SECRET_KEY\""
    [ -n "$LANGFUSE_USER_ID" ] && echo "export LANGFUSE_USER_ID=\"$LANGFUSE_USER_ID\""
    echo "export LANGFUSE_TAGS=\"$FINAL_TAGS\""
    echo "export LANGSTASH_ENABLED=\"true\""
    echo "export LANGSTASH_URL=\"http://127.0.0.1:5288\""
} > "$LANGFUSE_ENV_FILE"
info "Env vars written to $LANGFUSE_ENV_FILE."

echo ""
info "Installation complete!"
echo ""
echo "  Langfuse Base URL:   $LANGFUSE_BASE_URL"
echo "  Langfuse Public Key: ${LANGFUSE_PUBLIC_KEY:0:12}..."
echo "  Langfuse Secret Key: ${LANGFUSE_SECRET_KEY:0:12}..."
echo "  Langfuse User ID:   ${LANGFUSE_USER_ID:-<OS username>}"
echo "  Langfuse Tags:      $FINAL_TAGS"
echo ""
echo "  Hook: $PI_HOOK_DIR"
echo "  Env file: $LANGFUSE_ENV_FILE"
echo "  Restart Pi to start tracing."
