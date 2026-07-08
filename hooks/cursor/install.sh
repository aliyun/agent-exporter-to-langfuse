#!/usr/bin/env bash
set -euo pipefail

# Cursor Langfuse Hook Installer
# Builds and installs JS hook to ~/.cursor/hooks/langfuse/ and registers
# 11 hook events (9 Agent events + stop + sessionStart) in ~/.cursor/hooks.json
# using Cursor's flat-array schema.

CURSOR_HOME="${CURSOR_HOME:-$HOME/.cursor}"
LANGFUSE_HOOK_DIR="$CURSOR_HOME/hooks/langfuse"
CURSOR_HOOKS_JSON="$CURSOR_HOME/hooks.json"

# Shared config
LANGFUSE_PROFILE_DIR="$HOME/.agent-exporter-to-langfuse/config"
LANGFUSE_ENV_FILE="$LANGFUSE_PROFILE_DIR/cursor.env"

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

while [[ $# -gt 0 ]]; do
    case "$1" in
        --secret-key)  LANGFUSE_SECRET_KEY="$2"; shift 2 ;;
        --public-key)  LANGFUSE_PUBLIC_KEY="$2"; shift 2 ;;
        --base-url)    LANGFUSE_BASE_URL="$2"; shift 2 ;;
        --user-id)     LANGFUSE_USER_ID="$2"; shift 2 ;;
        --tags)        LANGFUSE_TAGS="$2"; shift 2 ;;
        --upgrade)     UPGRADE_MODE=true; shift ;;
        *) shift ;;
    esac
done

# --- 1. Check prerequisites ---
if [ ! -d "$CURSOR_HOME" ]; then
    error "Cursor IDE is not installed (no $CURSOR_HOME directory)."
    error "Install Cursor first: https://cursor.com"
    exit 1
fi

if ! command -v node &>/dev/null; then
    error "Node.js is not installed. Install Node.js >= 22: https://nodejs.org/"
    exit 1
fi

# --- 2. Collect Langfuse credentials ---
collect_credentials() {
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
}

if [ -n "$LANGFUSE_SECRET_KEY" ] && [ -n "$LANGFUSE_PUBLIC_KEY" ] && [ -n "$LANGFUSE_BASE_URL" ]; then
    info "Using provided Langfuse credentials"
elif [ "$UPGRADE_MODE" = true ] && [ -f "$LANGFUSE_ENV_FILE" ]; then
    # shellcheck disable=SC1090
    . "$LANGFUSE_ENV_FILE"
    if [ -n "$LANGFUSE_SECRET_KEY" ] && [ -n "$LANGFUSE_PUBLIC_KEY" ] && [ -n "$LANGFUSE_BASE_URL" ]; then
        info "Upgrade mode: loaded credentials from $LANGFUSE_ENV_FILE"
    else
        collect_credentials
    fi
else
    collect_credentials
fi

# --- 3. Build and install hook ---
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Use pre-built dist from tarball if available; otherwise fall back to npm build.
if [ -f "$SCRIPT_DIR/dist/index.mjs" ]; then
    info "Using pre-built dist/index.mjs, skipping build ..."
else
    # Fallback: build via npm. Requires langstash-deliver dist for trace.ts import.
    LANGSTASH_DELIVER_DIST="$SCRIPT_DIR/../langstash-deliver/typescript/dist/index.js"
    if [ ! -f "$LANGSTASH_DELIVER_DIST" ]; then
        error "langstash-deliver dist not found at $LANGSTASH_DELIVER_DIST"
        error "Build it first: cd hooks/langstash-deliver/typescript/ && npm install --ignore-scripts && npm run build"
        exit 1
    fi

    info "Pre-built dist not found, building via npm ..."
    if (cd "$SCRIPT_DIR" && npm install --ignore-scripts && npm run build); then
        :
    else
        build_rc=$?
        rm -rf "$SCRIPT_DIR/node_modules"
        error "npm install or npm run build failed (exit code $build_rc)"
        exit 1
    fi
    rm -rf "$SCRIPT_DIR/node_modules"

    if [ ! -f "$SCRIPT_DIR/dist/index.mjs" ]; then
        error "Build completed but dist/index.mjs not found"
        exit 1
    fi
fi

mkdir -p "$LANGFUSE_HOOK_DIR/dist"
cp -f "$SCRIPT_DIR/dist/index.mjs" "$LANGFUSE_HOOK_DIR/dist/"

info "Hook built and copied to $LANGFUSE_HOOK_DIR"

# --- 4. Register hooks in ~/.cursor/hooks.json (flat-array schema) ---
# Resolve node absolute path so the hook command does not depend on runtime PATH
NODE_PATH="$(command -v node)"
HOOK_COMMAND="$NODE_PATH $HOME/.cursor/hooks/langfuse/dist/index.mjs"

# 11 events: 9 Agent events + stop + sessionStart
HOOK_EVENTS="beforeSubmitPrompt,afterAgentResponse,afterAgentThought,beforeShellExecution,afterShellExecution,beforeMCPExecution,afterMCPExecution,beforeReadFile,afterFileEdit,stop,sessionStart"

python3 -c "
import json, sys, os

hooks_json_path = sys.argv[1]
hook_command = sys.argv[2]
events = sys.argv[3].split(',')

# Load or create hooks.json
if os.path.exists(hooks_json_path):
    with open(hooks_json_path, 'r') as f:
        hooks_data = json.load(f)
    if not isinstance(hooks_data, dict):
        hooks_data = {}
else:
    hooks_data = {}

# Write to BOTH the top-level and the hooks key (if it exists).
# Cursor versions differ on which format they read — some read the top-level
# flat format, others read the 'hooks' key. Writing to both ensures compatibility.
targets = [hooks_data]
if 'hooks' in hooks_data and isinstance(hooks_data['hooks'], dict):
    targets.append(hooks_data['hooks'])

added_count = 0
exists_count = 0
for target in targets:
    for event in events:
        if event not in target or not isinstance(target[event], list):
            target[event] = []

        # Check if langfuse hook already exists in this event's array
        hook_exists = False
        for entry in target[event]:
            if isinstance(entry, dict) and 'langfuse' in str(entry.get('command', '')):
                hook_exists = True
                break

        if not hook_exists:
            target[event].append({
                'command': hook_command,
                'timeout': 30
            })
            added_count += 1
        else:
            exists_count += 1

# Write back
with open(hooks_json_path, 'w') as f:
    json.dump(hooks_data, f, indent=2)
    f.write('\n')

print('added:%d' % added_count)
print('exists:%d' % exists_count)
" "$CURSOR_HOOKS_JSON" "$HOOK_COMMAND" "$HOOK_EVENTS" | while IFS= read -r status; do
    case "$status" in
        added:*) info "Registered ${status#added:} new hook entries in $CURSOR_HOOKS_JSON" ;;
        exists:*) info "All hook entries already registered in $CURSOR_HOOKS_JSON (${status#exists:} existing)" ;;
    esac
done

# --- 5. Write env file ---
case ",$LANGFUSE_TAGS," in
    *,cursor,*) FINAL_TAGS="$LANGFUSE_TAGS" ;;
    *) FINAL_TAGS="cursor${LANGFUSE_TAGS:+,$LANGFUSE_TAGS}" ;;
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
info "Env vars written to $LANGFUSE_ENV_FILE"

echo ""
info "Installation complete!"
echo ""
echo "  Langfuse Base URL:   $LANGFUSE_BASE_URL"
echo "  Langfuse Public Key: ${LANGFUSE_PUBLIC_KEY:0:12}..."
echo "  Langfuse Secret Key: ${LANGFUSE_SECRET_KEY:0:12}..."
echo "  Langfuse User ID:   ${LANGFUSE_USER_ID:-<OS username>}"
echo "  Langfuse Tags:      $FINAL_TAGS"
echo ""
echo "  Env file: $LANGFUSE_ENV_FILE"
echo "  Hook dir: $LANGFUSE_HOOK_DIR"
echo "  Hooks.json: $CURSOR_HOOKS_JSON"
echo "  Restart Cursor to start tracing."
