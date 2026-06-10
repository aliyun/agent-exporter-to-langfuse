#!/usr/bin/env bash
set -euo pipefail

# Recover from deleted CWD (e.g., previous uninstall removed this directory)
if ! pwd &>/dev/null; then
    cd "$HOME" 2>/dev/null || cd /
fi

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
            echo "  --agents LIST    Comma-separated agents to uninstall (claude-code,qoder,qoderwork,opencode,codex)"
            echo "  -y, --yes        Uninstall everything without prompting"
            echo "  -h, --help       Show this help"
            exit 0
            ;;
        *) error "Unknown option: $1"; exit 1 ;;
    esac
done

# ============================================================
# Step 0: Ensure running from INSTALL_DIR
# ============================================================
INSTALL_DIR="$HOME/.agent-exporter-to-langfuse"
SCRIPT_DIR="$(cd "$(dirname "$0")" 2>/dev/null && pwd -P 2>/dev/null)" || SCRIPT_DIR=""
if [ -z "$SCRIPT_DIR" ]; then
    # CWD is gone and $0 is relative — fall back to INSTALL_DIR
    if [ -d "$INSTALL_DIR" ]; then
        SCRIPT_DIR="$INSTALL_DIR"
    else
        error "Install directory $INSTALL_DIR not found. Nothing to uninstall."
        exit 0
    fi
fi
CANON_SCRIPT="$(cd "$SCRIPT_DIR" 2>/dev/null && pwd -P 2>/dev/null)" || CANON_SCRIPT="$SCRIPT_DIR"
CANON_INSTALL="$(cd "$INSTALL_DIR" 2>/dev/null && pwd -P 2>/dev/null)" || CANON_INSTALL="$INSTALL_DIR"
if [ "$CANON_SCRIPT" != "$CANON_INSTALL" ]; then
    error "Please run uninstall from the install directory:"
    echo "  bash $INSTALL_DIR/uninstall.sh"
    exit 1
fi

# ============================================================
# Step 1: Detect installed Langfuse hooks
# ============================================================
echo ""
echo -e "${BOLD}=== Agent Exporter to Langfuse — Unified Uninstaller ===${NC}"
echo ""

declare -a INSTALLED_AGENTS=()

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

if [ -d "$HOME/.codex/hooks/langfuse" ] || grep -q "langfuse-entrypoint.sh" "$HOME/.codex/hooks.json" 2>/dev/null; then
    INSTALLED_AGENTS+=("codex")
fi

# ============================================================
# Step 2: Select hooks to uninstall
# ============================================================
declare -a SELECTED_AGENTS=()

if [ ${#INSTALLED_AGENTS[@]} -eq 0 ]; then
    info "No Langfuse hooks found."
elif [ -n "$ARG_AGENTS" ]; then
    IFS=',' read -ra AGENT_LIST <<< "$ARG_AGENTS"
    for agent in "${AGENT_LIST[@]}"; do
        case "$agent" in
            claude-code|qoder|qoderwork|opencode|codex) SELECTED_AGENTS+=("$agent") ;;
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

# ============================================================
# Step 3: Determine if langstash + install dir should be cleaned
# ============================================================
UNINSTALL_LANGSTASH=false
UNINSTALL_INSTALL_DIR=false

# Count how many hooks will remain after this uninstall
REMAINING_HOOKS=$(( ${#INSTALLED_AGENTS[@]} - ${#SELECTED_AGENTS[@]} ))

if [ "$ARG_YES" = true ]; then
    UNINSTALL_LANGSTASH=true
    UNINSTALL_INSTALL_DIR=true
elif [ "$REMAINING_HOOKS" -le 0 ] && [ ${#INSTALLED_AGENTS[@]} -gt 0 ]; then
    # All hooks being removed — ask about langstash
    echo ""
    prompt_input "All hooks will be removed. Also uninstall langstash service? [y/N]: "
    read -r CONFIRM_LS
    if [[ "$CONFIRM_LS" =~ ^[Yy]$ ]]; then
        UNINSTALL_LANGSTASH=true
    fi
elif [ ${#INSTALLED_AGENTS[@]} -eq 0 ]; then
    # No hooks at all — ask about langstash
    echo ""
    prompt_input "Uninstall langstash service? [y/N]: "
    read -r CONFIRM_LS
    if [[ "$CONFIRM_LS" =~ ^[Yy]$ ]]; then
        UNINSTALL_LANGSTASH=true
    fi
else
    # Partial hook removal — langstash stays
    echo ""
    info "Some hooks will remain. langstash service will be kept."
fi

if [ "$UNINSTALL_LANGSTASH" = true ]; then
    if [ "$ARG_YES" != true ]; then
        prompt_input "Also remove install directory $INSTALL_DIR? [y/N]: "
        read -r CONFIRM_RM
        if [[ "$CONFIRM_RM" =~ ^[Yy]$ ]]; then
            UNINSTALL_INSTALL_DIR=true
        fi
    else
        UNINSTALL_INSTALL_DIR=true
    fi
fi

# ============================================================
# Step 4: Confirm and execute
# ============================================================
echo ""
echo -e "${BOLD}Uninstall plan:${NC}"
if [ ${#SELECTED_AGENTS[@]} -gt 0 ]; then
    echo "  Hooks:      ${SELECTED_AGENTS[*]}"
else
    echo "  Hooks:      (none)"
fi
echo "  langstash:  $([ "$UNINSTALL_LANGSTASH" = true ] && echo 'remove' || echo 'keep')"
echo "  $INSTALL_DIR: $([ "$UNINSTALL_INSTALL_DIR" = true ] && echo 'remove' || echo 'keep')"
echo ""

if [ "$ARG_YES" != true ]; then
    prompt_input "Proceed? [y/N]: "
    read -r CONFIRM
    if [[ ! "$CONFIRM" =~ ^[Yy]$ ]]; then
        info "Cancelled."
        exit 0
    fi
    echo ""
fi

# --- Uninstall agent hooks ---
uninstall_claude_code() {
    echo -e "${BOLD}--- Uninstalling: Claude Code ---${NC}"
    bash "$SCRIPT_DIR/hooks/claude-code/uninstall.sh"
}
uninstall_qoder() {
    echo -e "${BOLD}--- Uninstalling: Qoder ---${NC}"
    bash "$SCRIPT_DIR/hooks/qoder/uninstall.sh"
}
uninstall_qoderwork() {
    echo -e "${BOLD}--- Uninstalling: QoderWork ---${NC}"
    bash "$SCRIPT_DIR/hooks/qoderwork/uninstall.sh"
}
uninstall_opencode() {
    echo -e "${BOLD}--- Uninstalling: OpenCode ---${NC}"
    bash "$SCRIPT_DIR/hooks/opencode/uninstall.sh"
}
uninstall_codex() {
    echo -e "${BOLD}--- Uninstalling: Codex ---${NC}"
    bash "$SCRIPT_DIR/hooks/codex/uninstall.sh"
}

if [ ${#SELECTED_AGENTS[@]} -gt 0 ]; then
    for agent in "${SELECTED_AGENTS[@]}"; do
        echo ""
        case "$agent" in
            claude-code) uninstall_claude_code ;;
            qoder)       uninstall_qoder ;;
            qoderwork)   uninstall_qoderwork ;;
            opencode)    uninstall_opencode ;;
            codex)       uninstall_codex ;;
        esac
    done
fi

# --- Uninstall langstash ---
if [ "$UNINSTALL_LANGSTASH" = true ]; then
    echo ""
    echo -e "${BOLD}--- Uninstalling: langstash ---${NC}"
    if [ -f "$SCRIPT_DIR/exporter/uninstall-langstash.sh" ]; then
        bash "$SCRIPT_DIR/exporter/uninstall-langstash.sh"
    fi

    # Remove shell profile loader
    SHELL_RC=""
    if [ -n "${ZSH_VERSION:-}" ] || [ "$(basename "${SHELL:-}")" = "zsh" ]; then
        SHELL_RC="$HOME/.zshenv"
    else
        SHELL_RC="$HOME/.profile"
    fi

    if [ -f "$SHELL_RC" ] && grep -qF "agent-exporter-to-langfuse" "$SHELL_RC" 2>/dev/null; then
        python3 -c "
import sys
lines = open(sys.argv[1]).readlines()
out = []
skip_next = False
for line in lines:
    if skip_next and 'agent-exporter-to-langfuse' in line:
        skip_next = False
        continue
    if '# Agent Langfuse Exporters' in line:
        skip_next = True
        continue
    skip_next = False
    out.append(line)
open(sys.argv[1], 'w').writelines(out)
" "$SHELL_RC" 2>/dev/null && info "Removed profile loader from $SHELL_RC"
    fi
fi

# --- Remove install directory ---
if [ "$UNINSTALL_INSTALL_DIR" = true ] && [ -d "$INSTALL_DIR" ]; then
    cd "$HOME"
    rm -rf "$INSTALL_DIR"
    info "Removed $INSTALL_DIR"
fi

# ============================================================
# Summary
# ============================================================
echo ""
echo -e "${BOLD}=== Uninstall Complete ===${NC}"
echo ""
if [ ${#SELECTED_AGENTS[@]} -gt 0 ]; then
    echo "  Removed hooks: ${SELECTED_AGENTS[*]}"
fi
if [ "$UNINSTALL_LANGSTASH" = true ]; then
    echo "  Removed langstash service"
fi
if [ "$UNINSTALL_INSTALL_DIR" = true ]; then
    echo "  Removed $INSTALL_DIR"
fi
echo ""
echo "  Restart your coding agents to apply."
