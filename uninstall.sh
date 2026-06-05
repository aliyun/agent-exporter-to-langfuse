#!/usr/bin/env bash
set -euo pipefail

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
ARG_AGENTS=""
ARG_YES=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --agents) ARG_AGENTS="$2"; shift 2 ;;
        -y|--yes) ARG_YES=true; shift ;;
        -h|--help)
            echo "Usage: bash uninstall.sh [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --agents LIST    Comma-separated agents to uninstall (claude-code,qoder,qoderwork,opencode)"
            echo "  -y, --yes        Skip interactive selection, uninstall all installed"
            echo "  -h, --help       Show this help"
            exit 0
            ;;
        *) error "Unknown option: $1"; exit 1 ;;
    esac
done

# ============================================================
# Step 1: Detect installed Langfuse hooks
# ============================================================
echo ""
echo -e "${BOLD}=== Agent Exporter to Langfuse — Unified Uninstaller ===${NC}"
echo ""

declare -a INSTALLED_AGENTS=()

# Detect by checking if langfuse hook/plugin is actually installed
if command -v claude &>/dev/null && claude plugin list 2>/dev/null | grep -q "langfuse"; then
    INSTALLED_AGENTS+=("claude-code")
elif [ -d "$HOME/.claude" ] && [ -f "$HOME/.claude/plugins/langfuse/plugin.json" ] 2>/dev/null; then
    INSTALLED_AGENTS+=("claude-code")
fi

if [ -d "$HOME/.qoder/hooks/langfuse" ] || [ -f "$HOME/.qoder/langfuse.env" ]; then
    INSTALLED_AGENTS+=("qoder")
fi

if [ -d "$HOME/.qoderwork/hooks/langfuse" ] || [ -f "$HOME/.qoderwork/langfuse.env" ]; then
    INSTALLED_AGENTS+=("qoderwork")
fi

if [ -f "$HOME/.config/opencode/plugins/langfuse-exporter.mjs" ] || [ -f "$HOME/.config/opencode/langfuse.env" ]; then
    INSTALLED_AGENTS+=("opencode")
fi

if [ ${#INSTALLED_AGENTS[@]} -eq 0 ]; then
    info "No Langfuse hooks found. Nothing to uninstall."
    exit 0
fi

# --- Agent selection ---
declare -a SELECTED_AGENTS=()

if [ -n "$ARG_AGENTS" ]; then
    IFS=',' read -ra AGENT_LIST <<< "$ARG_AGENTS"
    for agent in "${AGENT_LIST[@]}"; do
        case "$agent" in
            claude-code|qoder|qoderwork|opencode) SELECTED_AGENTS+=("$agent") ;;
            *) error "Unknown agent: $agent"; exit 1 ;;
        esac
    done
elif [ "$ARG_YES" = true ]; then
    SELECTED_AGENTS=("${INSTALLED_AGENTS[@]}")
else
    echo -e "${BOLD}Langfuse hooks found for:${NC}"
    echo ""
    for i in "${!INSTALLED_AGENTS[@]}"; do
        local_agent="${INSTALLED_AGENTS[$i]}"
        num=$((i + 1))
        echo -e "  ${GREEN}[$num]${NC} $local_agent"
    done
    echo ""
    echo -e "  ${DIM}Enter numbers to uninstall (comma-separated), or press Enter for all.${NC}"
    prompt_input "Select agents [1-${#INSTALLED_AGENTS[@]}, default: all]: "
    read -r SELECTION

    if [ -z "$SELECTION" ] || [ "$SELECTION" = "all" ]; then
        SELECTED_AGENTS=("${INSTALLED_AGENTS[@]}")
    else
        IFS=',' read -ra SEL_NUMS <<< "$SELECTION"
        for num in "${SEL_NUMS[@]}"; do
            num=$(echo "$num" | tr -d ' ')
            if ! [[ "$num" =~ ^[0-9]+$ ]]; then
                warn "Invalid selection: $num (skipped)"
                continue
            fi
            idx=$((num - 1))
            if [ "$idx" -ge 0 ] && [ "$idx" -lt "${#INSTALLED_AGENTS[@]}" ]; then
                SELECTED_AGENTS+=("${INSTALLED_AGENTS[$idx]}")
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
info "Will uninstall for: ${SELECTED_AGENTS[*]}"
echo ""

# Confirmation
if [ "$ARG_YES" != true ]; then
    prompt_input "Proceed with uninstall? [y/N]: "
    read -r CONFIRM
    if [[ ! "$CONFIRM" =~ ^[Yy]$ ]]; then
        info "Cancelled."
        exit 0
    fi
    echo ""
fi

# ============================================================
# Uninstall: delegate to each component's own uninstall.sh
# ============================================================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

uninstall_claude_code() {
    echo ""
    echo -e "${BOLD}--- Uninstalling: Claude Code ---${NC}"
    bash "$SCRIPT_DIR/hooks/claude-code/uninstall.sh"
}

uninstall_qoder() {
    echo ""
    echo -e "${BOLD}--- Uninstalling: Qoder ---${NC}"
    bash "$SCRIPT_DIR/hooks/qoder/uninstall.sh"
}

uninstall_qoderwork() {
    echo ""
    echo -e "${BOLD}--- Uninstalling: QoderWork ---${NC}"
    bash "$SCRIPT_DIR/hooks/qoderwork/uninstall.sh"
}

uninstall_opencode() {
    echo ""
    echo -e "${BOLD}--- Uninstalling: OpenCode ---${NC}"
    bash "$SCRIPT_DIR/hooks/opencode/uninstall.sh"
}

# --- Run uninstall ---
for agent in "${SELECTED_AGENTS[@]}"; do
    case "$agent" in
        claude-code) uninstall_claude_code ;;
        qoder)       uninstall_qoder ;;
        qoderwork)   uninstall_qoderwork ;;
        opencode)    uninstall_opencode ;;
    esac
done

# ============================================================
# Summary
# ============================================================
echo ""
echo -e "${BOLD}=== Uninstall Complete ===${NC}"
echo ""
echo "  Removed for: ${SELECTED_AGENTS[*]}"
echo ""
echo "  Restart your coding agents to apply."
