#!/usr/bin/env bash
set -euo pipefail

HOOK_DIR="$HOME/.qoderwork/hooks/langfuse"
SETTINGS_FILE="$HOME/.qoderwork/settings.json"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SOURCE_HOOK="$SCRIPT_DIR/hooks/langfuse_hook.py"
SOURCE_RUNNER="$SCRIPT_DIR/hooks/langfuse-entrypoint.sh"
# Shared env directory
LANGFUSE_PROFILE_DIR="$HOME/.agent-exporter-to-langfuse/config"
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
if ! command -v uv &>/dev/null; then
    error "uv is not installed. Install it first: https://docs.astral.sh/uv/"
    exit 1
fi

if [ ! -f "$SOURCE_HOOK" ]; then
    error "Source hook not found: $SOURCE_HOOK"
    exit 1
fi

# --- 2. Collect Langfuse credentials ---
LANGFUSE_ENV_FILE="$LANGFUSE_PROFILE_DIR/qoderwork.env"

if [ -n "$LANGFUSE_SECRET_KEY" ] && [ -n "$LANGFUSE_PUBLIC_KEY" ] && [ -n "$LANGFUSE_BASE_URL" ]; then
    :
elif [ "$UPGRADE_MODE" = true ] && [ -f "$LANGFUSE_ENV_FILE" ]; then
    # shellcheck disable=SC1090
    . "$LANGFUSE_ENV_FILE"
    if [ -n "$LANGFUSE_SECRET_KEY" ] && [ -n "$LANGFUSE_PUBLIC_KEY" ] && [ -n "$LANGFUSE_BASE_URL" ]; then
        info "Upgrade mode: loaded credentials from $LANGFUSE_ENV_FILE"
    else
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

# --- 3. Install hook script ---
LANGSTASH_SRC="$SCRIPT_DIR/../langstash-deliver/python/langstash_deliver"

if [ -f "$HOOK_DIR/langfuse_hook.py" ]; then
    warn "Hook script already exists at $HOOK_DIR/langfuse_hook.py"
    read -rp "Overwrite? [y/N] " answer
    if [[ ! "$answer" =~ ^[Yy]$ ]]; then
        info "Skipped hook script copy."
    else
        cp "$SOURCE_HOOK" "$HOOK_DIR/langfuse_hook.py"
        cp "$SOURCE_RUNNER" "$HOOK_DIR/langfuse-entrypoint.sh"
        chmod +x "$HOOK_DIR/langfuse-entrypoint.sh"
        info "Hook script updated."
    fi
else
    mkdir -p "$HOOK_DIR"
    cp "$SOURCE_HOOK" "$HOOK_DIR/langfuse_hook.py"
    cp "$SOURCE_RUNNER" "$HOOK_DIR/langfuse-entrypoint.sh"
    chmod +x "$HOOK_DIR/langfuse-entrypoint.sh"
    info "Hook script installed to $HOOK_DIR/"
fi

# Copy langstash-deliver package locally (avoids runtime pip install)
if [ -d "$LANGSTASH_SRC" ]; then
    rm -rf "$HOOK_DIR/langstash_deliver"
    cp -R "$LANGSTASH_SRC" "$HOOK_DIR/langstash_deliver"
    info "langstash-deliver copied to $HOOK_DIR/"
else
    warn "langstash-deliver source not found at $LANGSTASH_SRC, skipping"
fi

# --- 4. Set up pyproject.toml for uv ---
SOURCE_PYPROJECT="$SCRIPT_DIR/hooks/pyproject.toml"
if [ -f "$SOURCE_PYPROJECT" ]; then
    cp "$SOURCE_PYPROJECT" "$HOOK_DIR/pyproject.toml"
    info "pyproject.toml installed to $HOOK_DIR/"
fi

# --- 4.1. Pre-install Python dependencies ---
info "Installing Python dependencies ..."
(cd "$HOOK_DIR" && uv sync 2>&1) || {
    error "uv sync failed in $HOOK_DIR"
    exit 1
}
info "Python dependencies installed."

# --- 5. Configure settings.json ---
HOOK_COMMAND="~/.qoderwork/hooks/langfuse/langfuse-entrypoint.sh"

mkdir -p "$(dirname "$SETTINGS_FILE")"
if [ ! -f "$SETTINGS_FILE" ]; then
    echo '{}' > "$SETTINGS_FILE"
fi

python3 -c "
import json, sys

settings_path = sys.argv[2]
hook_command = sys.argv[1]
hook_events = ['Stop', 'SubagentStop']

with open(settings_path) as f:
    settings = json.load(f)

if 'hooks' not in settings:
    settings['hooks'] = {}

added = []
skipped = []
for event in hook_events:
    if event not in settings['hooks']:
        settings['hooks'][event] = []
    already = any(
        'langfuse' in h.get('command', '')
        for matcher in settings['hooks'][event]
        for h in matcher.get('hooks', [])
    )
    if already:
        skipped.append(event)
    else:
        settings['hooks'][event].append({
            'hooks': [
                {
                    'type': 'command',
                    'command': hook_command,
                    'timeout': 20
                }
            ]
        })
        added.append(event)

with open(settings_path, 'w') as f:
    json.dump(settings, f, indent=2)

if added:
    print('added:' + ','.join(added))
if skipped:
    print('skipped:' + ','.join(skipped))
" "$HOOK_COMMAND" "$SETTINGS_FILE" | while IFS= read -r line; do
    case "$line" in
        added:*)   info "Added Langfuse hooks to $SETTINGS_FILE: ${line#added:}" ;;
        skipped:*) warn "Already configured in $SETTINGS_FILE: ${line#skipped:}" ;;
    esac
done

# --- 6. Write env file (standalone) ---
case ",$LANGFUSE_TAGS," in
    *,qoderwork,*) FINAL_TAGS="$LANGFUSE_TAGS" ;;
    *) FINAL_TAGS="qoderwork${LANGFUSE_TAGS:+,$LANGFUSE_TAGS}" ;;
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
# Hard-link to hook directory so the VM entrypoint can find it (shared data, no duplication)
ln -f "$LANGFUSE_ENV_FILE" "$HOOK_DIR/langfuse.env"
info "Env vars written to $LANGFUSE_ENV_FILE (hard-linked to $HOOK_DIR/langfuse.env)"

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
echo "  Restart QoderWork to start tracing."
