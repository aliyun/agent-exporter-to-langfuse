#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# --- Colors ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; }
prompt_input() { echo -ne "${CYAN}$1${NC}"; }

# --- Parse command-line arguments ---
LANGFUSE_SECRET_KEY="${LANGFUSE_SECRET_KEY:-}"
LANGFUSE_PUBLIC_KEY="${LANGFUSE_PUBLIC_KEY:-}"
LANGFUSE_BASE_URL="${LANGFUSE_BASE_URL:-}"
LANGFUSE_USER_ID="${LANGFUSE_USER_ID:-}"
LANGFUSE_TAGS="${LANGFUSE_TAGS:-}"
ARG_AGENTS=""
ARG_NO_INSTALL_UV=false
ARG_UPGRADE=false
ARG_YES=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --secret-key)  LANGFUSE_SECRET_KEY="$2"; shift 2 ;;
        --public-key)  LANGFUSE_PUBLIC_KEY="$2"; shift 2 ;;
        --base-url)    LANGFUSE_BASE_URL="$2"; shift 2 ;;
        --user-id)     LANGFUSE_USER_ID="$2"; shift 2 ;;
        --tags)        LANGFUSE_TAGS="$2"; shift 2 ;;
        --agents)      ARG_AGENTS="$2"; shift 2 ;;
        --no-install-uv) ARG_NO_INSTALL_UV=true; shift ;;
        --upgrade)     ARG_UPGRADE=true; shift ;;
        -y|--yes)      ARG_YES=true; shift ;;
        -h|--help)
            echo "Usage: bash install.sh [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --secret-key KEY    Langfuse Secret Key (sk-lf-...)"
            echo "  --public-key KEY    Langfuse Public Key (pk-lf-...)"
            echo "  --base-url URL      Langfuse Base URL (default: https://us.cloud.langfuse.com)"
            echo "  --user-id ID        Langfuse User ID (optional, defaults to OS username)"
            echo "  --tags TAGS         Extra tags (comma-separated, e.g. team:olap,env:prod). Agent name is always included."
            echo "  --agents LIST       Comma-separated agents to install (claude-code,qoder,qoderwork,opencode)"
            echo "  --no-install-uv     Skip automatic uv installation"
            echo "  --upgrade           Upgrade mode: reuse existing config, skip interactive prompts"
            echo "  -y, --yes           Skip interactive agent selection, install all detected"
            echo "  -h, --help          Show this help"
            exit 0
            ;;
        *) error "Unknown option: $1"; exit 1 ;;
    esac
done

# ============================================================
# Step 0: Version display + Relocate + Upgrade mode
# ============================================================
INSTALL_DIR="$HOME/.agent-exporter-to-langfuse"
VERSION=$(cat "$SCRIPT_DIR/VERSION" 2>/dev/null || echo "dev")

echo ""
echo -e "${BOLD}=== Agent Exporter to Langfuse v${VERSION} — Unified Installer ===${NC}"

# --- Sync to standard path if running from elsewhere ---
CURRENT="$(cd "$SCRIPT_DIR" && pwd)"
CANONICAL="$(cd "$INSTALL_DIR" 2>/dev/null && pwd 2>/dev/null)" || CANONICAL=""
if [ "$CURRENT" != "$CANONICAL" ] && [ "$CURRENT" != "$INSTALL_DIR" ]; then
    info "Syncing to $INSTALL_DIR ..."
    mkdir -p "$INSTALL_DIR"
    # Copy all non-ignored files (tracked + untracked, respects .gitignore)
    if command -v git &>/dev/null && [ -d "$SCRIPT_DIR/.git" ]; then
        (cd "$SCRIPT_DIR" && { git ls-files -z; git ls-files --others --exclude-standard -z; } | sort -uz | while IFS= read -r -d '' f; do
            mkdir -p "$INSTALL_DIR/$(dirname "$f")"
            cp -f "$f" "$INSTALL_DIR/$f"
        done)
    else
        cp -R "$SCRIPT_DIR/"* "$INSTALL_DIR/"
    fi
    info "Synced. Switching to $INSTALL_DIR"
    exec bash "$INSTALL_DIR/install.sh" "$@"
fi

# --- Upgrade mode: reuse existing config ---
if [ "$ARG_UPGRADE" = true ]; then
    info "Upgrade mode: reusing existing configuration"
    CONFIG_DIR="$INSTALL_DIR/config"
    for env_file in "$CONFIG_DIR"/*.env; do
        [ -f "$env_file" ] || continue
        agent_name=$(basename "$env_file" .env)
        # Source env to pick up credentials
        . "$env_file"
    done
fi

# ============================================================
# Step 1: Detect installed coding agents
# ============================================================
echo ""

declare -a DETECTED_AGENTS=()

detect_claude_code() {
    if command -v claude &>/dev/null; then return 0; fi
    if [ -d "$HOME/.claude" ]; then return 0; fi
    return 1
}

detect_qoder() {
    if [ -d "$HOME/.qoder" ]; then return 0; fi
    if command -v qoder &>/dev/null || command -v qodercli &>/dev/null; then return 0; fi
    return 1
}

detect_qoderwork() {
    if [ -d "$HOME/.qoderwork" ]; then return 0; fi
    return 1
}

detect_opencode() {
    if [ -d "$HOME/.config/opencode" ]; then return 0; fi
    if command -v opencode &>/dev/null; then return 0; fi
    return 1
}

# Build detection list
detect_claude_code && DETECTED_AGENTS+=("claude-code")
detect_qoder && DETECTED_AGENTS+=("qoder")
detect_qoderwork && DETECTED_AGENTS+=("qoderwork")
detect_opencode && DETECTED_AGENTS+=("opencode")

if [ ${#DETECTED_AGENTS[@]} -eq 0 ]; then
    error "No coding agents detected. Supported: Claude Code, Qoder, QoderWork, OpenCode."
    echo "  Install at least one agent, or use --agents to specify manually."
    exit 1
fi

# --- Agent selection ---
declare -a SELECTED_AGENTS=()

if [ "$ARG_UPGRADE" = true ]; then
    # Upgrade mode: infer agents from existing env files
    for env_file in "$INSTALL_DIR/config"/*.env; do
        [ -f "$env_file" ] || continue
        agent_name=$(basename "$env_file" .env)
        case "$agent_name" in
            claude-code|qoder|qoderwork|opencode) SELECTED_AGENTS+=("$agent_name") ;;
        esac
    done
    if [ ${#SELECTED_AGENTS[@]} -eq 0 ]; then
        SELECTED_AGENTS=("${DETECTED_AGENTS[@]}")
    fi
elif [ -n "$ARG_AGENTS" ]; then
    # Explicit --agents flag: use directly, no interaction
    IFS=',' read -ra AGENT_LIST <<< "$ARG_AGENTS"
    for agent in "${AGENT_LIST[@]}"; do
        case "$agent" in
            claude-code|qoder|qoderwork|opencode) SELECTED_AGENTS+=("$agent") ;;
            *) error "Unknown agent: $agent"; exit 1 ;;
        esac
    done
elif [ "$ARG_YES" = true ]; then
    # --yes: install all detected without asking
    SELECTED_AGENTS=("${DETECTED_AGENTS[@]}")
else
    # Interactive selection
    echo -e "${BOLD}Detected coding agents:${NC}"
    echo ""
    for i in "${!DETECTED_AGENTS[@]}"; do
        local_agent="${DETECTED_AGENTS[$i]}"
        num=$((i + 1))
        echo -e "  ${GREEN}[$num]${NC} $local_agent"
    done
    echo ""
    echo -e "  ${DIM}Enter numbers to install (comma-separated), or press Enter for all.${NC}"
    prompt_input "Select agents [1-${#DETECTED_AGENTS[@]}, default: all]: "
    read -r SELECTION </dev/tty

    if [ -z "$SELECTION" ] || [ "$SELECTION" = "all" ]; then
        SELECTED_AGENTS=("${DETECTED_AGENTS[@]}")
    else
        IFS=',' read -ra SEL_NUMS <<< "$SELECTION"
        for num in "${SEL_NUMS[@]}"; do
            num=$(echo "$num" | tr -d ' ')
            if ! [[ "$num" =~ ^[0-9]+$ ]]; then
                warn "Invalid selection: $num (skipped)"
                continue
            fi
            idx=$((num - 1))
            if [ "$idx" -ge 0 ] && [ "$idx" -lt "${#DETECTED_AGENTS[@]}" ]; then
                SELECTED_AGENTS+=("${DETECTED_AGENTS[$idx]}")
            else
                warn "Invalid selection: $num (skipped)"
            fi
        done
    fi
fi

if [ ${#SELECTED_AGENTS[@]} -eq 0 ]; then
    error "No agents selected."
    exit 1
fi

echo ""
info "Will install for: ${SELECTED_AGENTS[*]}"
echo ""

# ============================================================
# Step 2: Install uv / check npm
# ============================================================
NEED_UV=false
NEED_NPM=false
for agent in "${SELECTED_AGENTS[@]}"; do
    case "$agent" in
        opencode) NEED_NPM=true ;;
        *) NEED_UV=true ;;
    esac
done

if [ "$NEED_UV" = true ]; then
    if command -v uv &>/dev/null; then
        info "uv is already installed: $(uv --version)"
    elif [ "$ARG_NO_INSTALL_UV" = true ]; then
        error "uv is required but --no-install-uv was specified."
        exit 1
    else
        info "Installing uv ..."
        curl -LsSf https://astral.sh/uv/install.sh | sh
        export PATH="$HOME/.local/bin:$PATH"
        if command -v uv &>/dev/null; then
            info "uv installed: $(uv --version)"
        else
            error "uv installation failed. Install manually: https://docs.astral.sh/uv/"
            exit 1
        fi
    fi
fi

if [ "$NEED_NPM" = true ]; then
    if command -v npm &>/dev/null; then
        info "npm is available: $(npm --version)"
    else
        error "npm is required for OpenCode but not found. Install Node.js: https://nodejs.org/"
        exit 1
    fi
fi

# ============================================================
# Step 3: Collect 3 required Langfuse parameters
# ============================================================
if [ -z "$LANGFUSE_SECRET_KEY" ] || [ -z "$LANGFUSE_PUBLIC_KEY" ] || [ -z "$LANGFUSE_BASE_URL" ]; then
    echo -e "${BOLD}=== Langfuse Configuration ===${NC}"
    echo "Enter your Langfuse credentials (from Langfuse project settings -> API Keys)."
    echo ""

    DEFAULT_BASE_URL="${LANGFUSE_BASE_URL:-https://us.cloud.langfuse.com}"

    if [ -z "$LANGFUSE_BASE_URL" ]; then
        prompt_input "Langfuse Base URL [$DEFAULT_BASE_URL]: "
        read -r INPUT_BASE_URL </dev/tty
        LANGFUSE_BASE_URL="${INPUT_BASE_URL:-$DEFAULT_BASE_URL}"
    fi

    if [ -z "$LANGFUSE_PUBLIC_KEY" ]; then
        prompt_input "Langfuse Public Key (pk-lf-...): "
        read -r LANGFUSE_PUBLIC_KEY </dev/tty
    fi

    if [ -z "$LANGFUSE_SECRET_KEY" ]; then
        prompt_input "Langfuse Secret Key (sk-lf-...): "
        read -r LANGFUSE_SECRET_KEY </dev/tty
    fi

    if [ -n "$LANGFUSE_USER_ID" ]; then
        prompt_input "Langfuse User ID [$LANGFUSE_USER_ID]: "
    else
        prompt_input "Langfuse User ID [default: OS username]: "
    fi
    read -r INPUT_USER_ID </dev/tty
    LANGFUSE_USER_ID="${INPUT_USER_ID:-$LANGFUSE_USER_ID}"

    if [ -n "$LANGFUSE_TAGS" ]; then
        prompt_input "Extra Tags [$LANGFUSE_TAGS]: "
    else
        prompt_input "Extra Tags (e.g. team:olap,env:prod) [none]: "
    fi
    read -r INPUT_TAGS </dev/tty
    LANGFUSE_TAGS="${INPUT_TAGS:-$LANGFUSE_TAGS}"

    echo ""
fi

if [ -z "$LANGFUSE_PUBLIC_KEY" ] || [ -z "$LANGFUSE_SECRET_KEY" ] || [ -z "$LANGFUSE_BASE_URL" ]; then
    error "All three parameters are required: LANGFUSE_SECRET_KEY, LANGFUSE_PUBLIC_KEY, LANGFUSE_BASE_URL"
    exit 1
fi

# ============================================================
# Step 4: Install for each selected agent
# ============================================================

# -------------------------------------------------------
# Install: delegate to each component's own install.sh
# -------------------------------------------------------
install_claude_code() {
    echo ""
    echo -e "${BOLD}--- Installing: Claude Code ---${NC}"
    bash "$SCRIPT_DIR/hooks/claude-code/install.sh" \
        --secret-key "$LANGFUSE_SECRET_KEY" \
        --public-key "$LANGFUSE_PUBLIC_KEY" \
        --base-url "$LANGFUSE_BASE_URL" \
        ${LANGFUSE_USER_ID:+--user-id "$LANGFUSE_USER_ID"} \
        ${LANGFUSE_TAGS:+--tags "$LANGFUSE_TAGS"}
}

install_qoder() {
    echo ""
    echo -e "${BOLD}--- Installing: Qoder ---${NC}"
    bash "$SCRIPT_DIR/hooks/qoder/install.sh" \
        --secret-key "$LANGFUSE_SECRET_KEY" \
        --public-key "$LANGFUSE_PUBLIC_KEY" \
        --base-url "$LANGFUSE_BASE_URL" \
        ${LANGFUSE_USER_ID:+--user-id "$LANGFUSE_USER_ID"} \
        ${LANGFUSE_TAGS:+--tags "$LANGFUSE_TAGS"}
}

install_qoderwork() {
    echo ""
    echo -e "${BOLD}--- Installing: QoderWork ---${NC}"
    bash "$SCRIPT_DIR/hooks/qoderwork/install.sh" \
        --secret-key "$LANGFUSE_SECRET_KEY" \
        --public-key "$LANGFUSE_PUBLIC_KEY" \
        --base-url "$LANGFUSE_BASE_URL" \
        ${LANGFUSE_USER_ID:+--user-id "$LANGFUSE_USER_ID"} \
        ${LANGFUSE_TAGS:+--tags "$LANGFUSE_TAGS"}
}

install_opencode() {
    echo ""
    echo -e "${BOLD}--- Installing: OpenCode ---${NC}"
    bash "$SCRIPT_DIR/hooks/opencode/install.sh" \
        --secret-key "$LANGFUSE_SECRET_KEY" \
        --public-key "$LANGFUSE_PUBLIC_KEY" \
        --base-url "$LANGFUSE_BASE_URL" \
        ${LANGFUSE_USER_ID:+--user-id "$LANGFUSE_USER_ID"} \
        ${LANGFUSE_TAGS:+--tags "$LANGFUSE_TAGS"}
}

# --- Run installation ---
for agent in "${SELECTED_AGENTS[@]}"; do
    case "$agent" in
        claude-code) install_claude_code ;;
        qoder)       install_qoder ;;
        qoderwork)   install_qoderwork ;;
        opencode)    install_opencode ;;
    esac
done

# ============================================================
# Step 5: Install langstash service
# ============================================================
echo ""
echo -e "${BOLD}--- Installing: langstash ---${NC}"
INSTALL_DIR="$INSTALL_DIR" \
LANGFUSE_PUBLIC_KEY="$LANGFUSE_PUBLIC_KEY" \
LANGFUSE_SECRET_KEY="$LANGFUSE_SECRET_KEY" \
LANGFUSE_BASE_URL="$LANGFUSE_BASE_URL" \
bash "$SCRIPT_DIR/exporter/install-langstash.sh" || warn "langstash installation skipped"

# ============================================================
# Step 6: Summary
# ============================================================
echo ""
echo -e "${BOLD}=== Installation Complete ===${NC}"
echo ""
echo "  Langfuse Base URL:   $LANGFUSE_BASE_URL"
echo "  Langfuse Public Key: ${LANGFUSE_PUBLIC_KEY:0:12}..."
echo "  Langfuse Secret Key: ${LANGFUSE_SECRET_KEY:0:12}..."
echo "  Langfuse User ID:   ${LANGFUSE_USER_ID:-<OS username>}"
echo "  Langfuse Tags:      <agent-name>${LANGFUSE_TAGS:+,$LANGFUSE_TAGS}"
echo ""
echo "  Installed for: ${SELECTED_AGENTS[*]}"
echo ""
echo "  Restart your coding agents to start tracing."
echo "  To uninstall: bash $INSTALL_DIR/uninstall.sh"
