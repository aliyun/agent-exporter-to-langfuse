#!/usr/bin/env bash
# installer.sh — Unified installer for agent-exporter-to-langfuse
# Subcommands: install, upgrade, uninstall, rollback
set -euo pipefail

# --- Colors ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BOLD='\033[1m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

INSTALL_DIR="$HOME/.agent-exporter-to-langfuse"
DEFAULT_REPO="aliyun/agent-exporter-to-langfuse"
LANGSTASH_WRAPPER="$HOME/.local/bin/langstash"
HEALTH_URL="http://127.0.0.1:5288/health"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-60}"

# ============================================================
# Utility functions
# ============================================================

compute_sha256() {
    local file="$1"
    if command -v sha256sum &>/dev/null; then
        sha256sum "$file" | awk '{print $1}'
    elif command -v shasum &>/dev/null; then
        shasum -a 256 "$file" | awk '{print $1}'
    else
        error "Neither sha256sum nor shasum found"
        return 1
    fi
}

verify_sha256() {
    local tarball="$1" sums_file="$2" original_name="${3:-}"
    if [ -z "$original_name" ]; then
        original_name="$(basename "$tarball")"
    fi
    local expected
    expected="$(grep "$original_name" "$sums_file" | awk '{print $1}')"
    if [ -z "$expected" ]; then
        error "No checksum found for $original_name in SHA256SUMS"
        return 1
    fi
    local actual
    actual="$(compute_sha256 "$tarball")"
    if [ "$actual" != "$expected" ]; then
        error "SHA-256 mismatch: expected $expected, got $actual"
        return 1
    fi
    info "SHA-256 verified: $original_name"
}

read_pointer() {
    local name="$1"
    local file="$INSTALL_DIR/$name"
    if [ -f "$file" ]; then
        cat "$file" | tr -d '[:space:]'
    fi
}

write_pointer() {
    local name="$1" value="$2"
    local file="$INSTALL_DIR/$name"
    local tmp
    tmp="$(mktemp "$INSTALL_DIR/.pointer.XXXXXX")"
    printf '%s' "$value" > "$tmp"
    mv -f "$tmp" "$file"
}

download_file() {
    local url="$1" dest="$2"
    if [[ "$url" == file://* ]]; then
        local path="${url#file://}"
        cp "$path" "$dest"
        return
    fi
    if command -v curl &>/dev/null; then
        curl -fsSL -o "$dest" "$url"
    elif command -v wget &>/dev/null; then
        wget -q -O "$dest" "$url"
    else
        error "Neither curl nor wget found"
        return 1
    fi
}

get_version_dir() {
    echo "$INSTALL_DIR/versions/$1"
}

read_hook_state() {
    local file="$INSTALL_DIR/hook-state.json"
    if [ -f "$file" ]; then
        cat "$file"
    else
        echo '{}'
    fi
}

write_hook_state_entry() {
    local agent="$1" version="$2" status="$3" err="${4:-}"
    local file="$INSTALL_DIR/hook-state.json"
    local tmp
    tmp="$(mktemp "$INSTALL_DIR/.hook-state.XXXXXX")"

    python3 -c "
import json, sys
agent, version, status, err = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
path = sys.argv[5]
try:
    data = json.load(open(path))
except (FileNotFoundError, json.JSONDecodeError):
    data = {}
entry = {'status': status}
if version:
    entry['version'] = version
if err:
    entry['error'] = err
data[agent] = entry
json.dump(data, open(sys.argv[6], 'w'), indent=2)
" "$agent" "$version" "$status" "$err" "$file" "$tmp"

    mv -f "$tmp" "$file"
}

get_hook_version() {
    local agent="$1"
    local file="$INSTALL_DIR/hook-state.json"
    if [ ! -f "$file" ]; then
        return
    fi
    python3 -c "
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    v = d.get(sys.argv[2], {}).get('version', '')
    print(v, end='')
except: pass
" "$file" "$agent"
}

list_known_agents() {
    local ver_dir="$1"
    if [ -d "$ver_dir/hooks" ]; then
        for d in "$ver_dir/hooks"/*/; do
            [ -d "$d" ] || continue
            local name
            name="$(basename "$d")"
            if [ -f "$d/install.sh" ]; then
                echo "$name"
            fi
        done
    fi
}

install_detected_hooks() {
    local ver_dir="$1"
    local agents
    agents="$(list_known_agents "$ver_dir")"
    if [ -z "$agents" ]; then
        return
    fi
    info "Installing hooks for detected agents ..."
    for agent in $agents; do
        local script="$ver_dir/hooks/$agent/install.sh"
        if [ -f "$script" ]; then
            info "Installing hook: $agent"
            bash "$script" --upgrade -y 2>&1 || warn "Hook $agent installation had warnings"
            write_hook_state_entry "$agent" "$(read_pointer current)" "installed"
        fi
    done
}

stop_langstash() {
    if [ "$(uname)" = "Darwin" ]; then
        launchctl unload "$HOME/Library/LaunchAgents/com.langstash.plist" 2>/dev/null || true
    else
        systemctl --user stop langstash 2>/dev/null || true
    fi
}

start_langstash() {
    if [ "$(uname)" = "Darwin" ]; then
        launchctl load "$HOME/Library/LaunchAgents/com.langstash.plist" 2>/dev/null || true
    else
        systemctl --user start langstash 2>/dev/null || true
    fi
}

restart_langstash() {
    stop_langstash
    sleep 1
    start_langstash
}

wait_health() {
    local timeout="${1:-$HEALTH_TIMEOUT}"
    local elapsed=0
    info "Waiting for langstash health check ..."
    while [ "$elapsed" -lt "$timeout" ]; do
        if curl -sf "$HEALTH_URL" >/dev/null 2>&1; then
            info "langstash is healthy"
            return 0
        fi
        sleep 2
        elapsed=$((elapsed + 2))
    done
    error "langstash health check timed out after ${timeout}s"
    return 1
}

# ============================================================
# install subcommand
# ============================================================

cmd_install() {
    local version="" package_url="" skip_verify=false

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --version) version="$2"; shift 2 ;;
            --package-url) package_url="$2"; shift 2 ;;
            --secret-key)  LANGFUSE_SECRET_KEY="$2"; shift 2 ;;
            --public-key)  LANGFUSE_PUBLIC_KEY="$2"; shift 2 ;;
            --base-url)    LANGFUSE_BASE_URL="$2"; shift 2 ;;
            --user-id)     LANGFUSE_USER_ID="$2"; shift 2 ;;
            --tags)        LANGFUSE_TAGS="$2"; shift 2 ;;
            --skip-verify) skip_verify=true; shift ;;
            *) error "install: unknown option: $1"; exit 1 ;;
        esac
    done

    export LANGFUSE_SECRET_KEY="${LANGFUSE_SECRET_KEY:-}"
    export LANGFUSE_PUBLIC_KEY="${LANGFUSE_PUBLIC_KEY:-}"
    export LANGFUSE_BASE_URL="${LANGFUSE_BASE_URL:-}"
    export LANGFUSE_USER_ID="${LANGFUSE_USER_ID:-}"
    export LANGFUSE_TAGS="${LANGFUSE_TAGS:-}"

    mkdir -p "$INSTALL_DIR/versions" "$INSTALL_DIR/config" "$INSTALL_DIR/data" "$INSTALL_DIR/logs"

    # Determine download URL and version
    local tarball_url="" sums_url=""
    if [ -n "$package_url" ]; then
        tarball_url="$package_url"
        if [ -z "$version" ]; then
            # Try to extract version from filename
            local fname
            fname="$(basename "$package_url")"
            version="$(echo "$fname" | sed -n 's/agent-exporter-to-langfuse-\(.*\)\.tar\.gz/\1/p')"
            if [ -z "$version" ]; then
                error "Cannot determine version from package URL. Use --version."
                exit 1
            fi
        fi
        # SHA256SUMS next to tarball
        local base_dir
        if [[ "$package_url" == file://* ]]; then
            base_dir="$(dirname "${package_url#file://}")"
            sums_url="file://${base_dir}/SHA256SUMS"
        else
            base_dir="$(echo "$package_url" | sed 's|/[^/]*$||')"
            sums_url="${base_dir}/SHA256SUMS"
        fi
    else
        if [ -z "$version" ] || [ "$version" = "latest" ]; then
            info "Querying latest release version ..."
            local api_url="https://api.github.com/repos/${DEFAULT_REPO}/releases?per_page=1"
            local release_json
            release_json="$(curl -fsSL "$api_url")" || {
                error "Failed to query GitHub releases API"
                exit 1
            }
            version="$(echo "$release_json" | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['tag_name'].lstrip('v'))")" || {
                error "No releases found for ${DEFAULT_REPO}"
                exit 1
            }
            info "Latest version: $version"
        fi
        tarball_url="https://github.com/${DEFAULT_REPO}/releases/download/v${version}/agent-exporter-to-langfuse-${version}.tar.gz"
        sums_url="https://github.com/${DEFAULT_REPO}/releases/download/v${version}/SHA256SUMS"
    fi

    # Download
    local tmp_dir
    tmp_dir="$(mktemp -d)"
    # shellcheck disable=SC2064
    trap "rm -rf '$tmp_dir'" EXIT

    local tarball_basename
    tarball_basename="$(basename "$tarball_url")"
    info "Downloading package ..."
    local tarball_file="$tmp_dir/$tarball_basename"
    local sums_file="$tmp_dir/SHA256SUMS"
    download_file "$tarball_url" "$tarball_file"

    # SHA-256 verification: fail-closed for remote URLs, warn for file://
    if [ "$skip_verify" = true ]; then
        warn "SHA-256 verification skipped (--skip-verify)"
    elif download_file "$sums_url" "$sums_file" 2>/dev/null; then
        verify_sha256 "$tarball_file" "$sums_file" "$tarball_basename"
    elif [[ "$tarball_url" == file://* ]]; then
        warn "SHA256SUMS not found next to local package, skipping verification"
    else
        error "SHA256SUMS download failed — cannot verify package integrity"
        error "Use --skip-verify to install without integrity verification"
        exit 1
    fi

    # Extract
    info "Extracting ..."
    tar xzf "$tarball_file" -C "$tmp_dir"

    # Find extracted directory — it should be agent-exporter-to-langfuse-<version>/
    local extracted_dir=""
    for d in "$tmp_dir"/agent-exporter-to-langfuse-*/; do
        [ -d "$d" ] && extracted_dir="$d" && break
    done
    if [ -z "$extracted_dir" ]; then
        extracted_dir="$tmp_dir"
    fi

    # Read actual version from VERSION file
    if [ -f "$extracted_dir/VERSION" ]; then
        version="$(cat "$extracted_dir/VERSION" | tr -d '[:space:]')"
    fi

    local ver_dir
    ver_dir="$(get_version_dir "$version")"

    if [ -d "$ver_dir" ]; then
        warn "Version $version already exists, overwriting"
        rm -rf "$ver_dir"
    fi

    mkdir -p "$(dirname "$ver_dir")"
    mv "$extracted_dir" "$ver_dir"

    # Write current pointer
    write_pointer "current" "$version"
    info "Installed version $version"

    # Run langstash install from the version directory
    if [ -f "$ver_dir/exporter/install-langstash.sh" ]; then
        info "Installing langstash service ..."
        INSTALL_DIR="$INSTALL_DIR" \
        EXPORTER_DIR="$ver_dir/exporter" \
        LANGFUSE_PUBLIC_KEY="${LANGFUSE_PUBLIC_KEY:-}" \
        LANGFUSE_SECRET_KEY="${LANGFUSE_SECRET_KEY:-}" \
        LANGFUSE_BASE_URL="${LANGFUSE_BASE_URL:-}" \
        bash "$ver_dir/exporter/install-langstash.sh" || warn "langstash service installation skipped"
    fi

    # Install hooks for detected agents
    install_detected_hooks "$ver_dir"

    # Install CLI wrapper
    install_wrapper

    info "Installation complete: v$version"
}

# ============================================================
# uninstall subcommand
# ============================================================

cmd_uninstall() {
    local purge=false

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --purge) purge=true; shift ;;
            *) error "uninstall: unknown option: $1"; exit 1 ;;
        esac
    done

    local current
    current="$(read_pointer "current")"

    # Stop langstash service
    info "Stopping langstash service ..."
    stop_langstash

    # Uninstall hooks
    if [ -n "$current" ]; then
        local ver_dir
        ver_dir="$(get_version_dir "$current")"
        if [ -d "$ver_dir/hooks" ]; then
            for agent_dir in "$ver_dir/hooks"/*/; do
                [ -d "$agent_dir" ] || continue
                local agent
                agent="$(basename "$agent_dir")"
                if [ -f "$agent_dir/uninstall.sh" ]; then
                    info "Uninstalling hook: $agent"
                    bash "$agent_dir/uninstall.sh" || warn "Failed to uninstall $agent hook"
                fi
            done
        fi
    fi

    # Uninstall langstash service
    if [ -f "$INSTALL_DIR/exporter/uninstall-langstash.sh" ]; then
        bash "$INSTALL_DIR/exporter/uninstall-langstash.sh" 2>/dev/null || true
    elif [ -n "$current" ]; then
        local ver_dir
        ver_dir="$(get_version_dir "$current")"
        if [ -f "$ver_dir/exporter/uninstall-langstash.sh" ]; then
            bash "$ver_dir/exporter/uninstall-langstash.sh" 2>/dev/null || true
        fi
    fi

    # Remove versions, pointers, hook-state
    rm -rf "$INSTALL_DIR/versions"
    rm -f "$INSTALL_DIR/current" "$INSTALL_DIR/previous" "$INSTALL_DIR/hook-state.json"
    info "Removed versions and pointers"

    # Remove CLI wrapper
    rm -f "$LANGSTASH_WRAPPER"
    info "Removed CLI wrapper"

    # Remove launchd/systemd service files
    rm -f "$HOME/Library/LaunchAgents/com.langstash.plist" 2>/dev/null || true
    rm -f "$HOME/.config/systemd/user/langstash.service" 2>/dev/null || true
    if command -v systemctl &>/dev/null; then
        systemctl --user daemon-reload 2>/dev/null || true
    fi

    # Remove shell profile loader
    for rc_file in "$HOME/.zshenv" "$HOME/.profile" "$HOME/.bashrc"; do
        if [ -f "$rc_file" ] && grep -qF "agent-exporter-to-langfuse" "$rc_file" 2>/dev/null; then
            python3 -c "
import sys
lines = open(sys.argv[1]).readlines()
out = [l for l in lines if 'agent-exporter-to-langfuse' not in l]
open(sys.argv[1], 'w').writelines(out)
" "$rc_file" 2>/dev/null && info "Removed profile loader from $rc_file"
        fi
    done

    # Remove per-agent env LaunchAgents
    for plist in "$HOME/Library/LaunchAgents"/com.*.langfuse-env.plist; do
        if [ -f "$plist" ]; then
            launchctl unload "$plist" 2>/dev/null || true
            rm -f "$plist"
            info "Removed $(basename "$plist")"
        fi
    done

    if [ "$purge" = true ]; then
        info "Purging config, data, and logs ..."
        rm -rf "$INSTALL_DIR"
        info "Purge complete: $INSTALL_DIR removed"
    else
        info "Uninstall complete. Config/data/logs preserved in $INSTALL_DIR"
    fi
}

# ============================================================
# upgrade subcommand
# ============================================================

cmd_upgrade() {
    local version="" package_url="" retry_hooks=false retry_agent="" skip_verify=false

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --version) version="$2"; shift 2 ;;
            --package-url) package_url="$2"; shift 2 ;;
            --retry-hooks) retry_hooks=true; shift ;;
            --agent) retry_agent="$2"; shift 2 ;;
            --skip-verify) skip_verify=true; shift ;;
            *) error "upgrade: unknown option: $1"; exit 1 ;;
        esac
    done

    # Retry-hooks mode: just re-run hook upgrades
    if [ "$retry_hooks" = true ]; then
        retry_failed_hooks "$retry_agent"
        return
    fi

    local current
    current="$(read_pointer "current")"
    if [ -z "$current" ]; then
        # Check for legacy git layout
        if [ -d "$INSTALL_DIR/.git" ]; then
            info "Detected legacy git layout, migrating ..."
            migrate_legacy_layout
            current="$(read_pointer "current")"
        else
            error "No current version found. Run 'installer.sh install' first."
            exit 1
        fi
    fi

    # Determine download URL and target version
    local tarball_url="" sums_url=""
    if [ -n "$package_url" ]; then
        tarball_url="$package_url"
        if [ -z "$version" ]; then
            local fname
            fname="$(basename "$package_url")"
            version="$(echo "$fname" | sed -n 's/agent-exporter-to-langfuse-\(.*\)\.tar\.gz/\1/p')"
            if [ -z "$version" ]; then
                error "Cannot determine version from package URL. Use --version."
                exit 1
            fi
        fi
        local base_dir
        if [[ "$package_url" == file://* ]]; then
            base_dir="$(dirname "${package_url#file://}")"
            sums_url="file://${base_dir}/SHA256SUMS"
        else
            base_dir="$(echo "$package_url" | sed 's|/[^/]*$||')"
            sums_url="${base_dir}/SHA256SUMS"
        fi
    else
        if [ -z "$version" ]; then
            error "Must specify --version or --package-url for upgrade"
            exit 1
        fi
        tarball_url="https://github.com/${DEFAULT_REPO}/releases/download/v${version}/agent-exporter-to-langfuse-${version}.tar.gz"
        sums_url="https://github.com/${DEFAULT_REPO}/releases/download/v${version}/SHA256SUMS"
    fi

    if [ "$version" = "$current" ]; then
        info "Already at version $version"
        return
    fi

    # Download and verify
    local tmp_dir
    tmp_dir="$(mktemp -d)"
    # shellcheck disable=SC2064
    trap "rm -rf '$tmp_dir'" EXIT

    local tarball_basename
    tarball_basename="$(basename "$tarball_url")"
    info "Downloading version $version ..."
    local tarball_file="$tmp_dir/$tarball_basename"
    local sums_file="$tmp_dir/SHA256SUMS"
    download_file "$tarball_url" "$tarball_file"

    # SHA-256 verification: fail-closed for remote URLs, warn for file://
    if [ "$skip_verify" = true ]; then
        warn "SHA-256 verification skipped (--skip-verify)"
    elif download_file "$sums_url" "$sums_file" 2>/dev/null; then
        verify_sha256 "$tarball_file" "$sums_file" "$tarball_basename"
    elif [[ "$tarball_url" == file://* ]]; then
        warn "SHA256SUMS not found next to local package, skipping verification"
    else
        error "SHA256SUMS download failed — cannot verify package integrity"
        error "Use --skip-verify to upgrade without integrity verification"
        exit 1
    fi

    # Extract
    info "Extracting ..."
    tar xzf "$tarball_file" -C "$tmp_dir"

    local extracted_dir=""
    for d in "$tmp_dir"/agent-exporter-to-langfuse-*/; do
        [ -d "$d" ] && extracted_dir="$d" && break
    done
    if [ -z "$extracted_dir" ]; then
        extracted_dir="$tmp_dir"
    fi

    if [ -f "$extracted_dir/VERSION" ]; then
        version="$(cat "$extracted_dir/VERSION" | tr -d '[:space:]')"
    fi

    local ver_dir
    ver_dir="$(get_version_dir "$version")"

    if [ -d "$ver_dir" ]; then
        rm -rf "$ver_dir"
    fi
    mkdir -p "$(dirname "$ver_dir")"
    mv "$extracted_dir" "$ver_dir"

    # Pre-install langstash dependencies in new version
    if [ -f "$ver_dir/exporter/install-langstash.sh" ]; then
        info "Pre-installing langstash dependencies ..."
        if command -v uv &>/dev/null; then
            local uv_extras=""
            if [ "$(uname)" = "Darwin" ]; then
                uv_extras="--extra macos"
            fi
            (cd "$ver_dir/exporter" && uv sync $uv_extras 2>&1) || warn "uv sync failed"
        fi
    fi

    # Pointer swap: previous <- current, current <- new
    write_pointer "previous" "$current"
    write_pointer "current" "$version"
    info "Pointer swap: $current → $version"

    # Restart langstash
    info "Restarting langstash ..."

    # Update service files to point to new version
    local langstash_bin="$ver_dir/exporter/.venv/bin/langstash"
    update_service_files "$langstash_bin"

    restart_langstash

    if ! wait_health; then
        error "langstash failed to start after upgrade"
        write_hook_state_entry "__upgrade__" "$version" "error" "langstash health check failed"
        exit 1
    fi

    # Upgrade hooks
    upgrade_hooks "$version" "$current"

    # GC old versions
    gc_versions

    info "Upgrade complete: v$current → v$version"
}

# ============================================================
# rollback subcommand
# ============================================================

cmd_rollback() {
    local current previous
    current="$(read_pointer "current")"
    previous="$(read_pointer "previous")"

    if [ -z "$previous" ]; then
        error "No previous version to rollback to"
        exit 1
    fi

    local ver_dir
    ver_dir="$(get_version_dir "$previous")"
    local langstash_bin="$ver_dir/exporter/.venv/bin/langstash"

    if [ ! -x "$langstash_bin" ]; then
        error "Previous version binary not executable: $langstash_bin"
        exit 1
    fi

    info "Rolling back: v$current → v$previous"

    # Swap pointers
    write_pointer "current" "$previous"
    write_pointer "previous" "$current"
    update_service_files "$langstash_bin"

    restart_langstash

    if ! wait_health; then
        error "langstash failed to start after rollback"
        exit 1
    fi

    # Rollback hooks
    local new_current="$previous"
    local agents
    agents="$(list_known_agents "$ver_dir")"
    for agent in $agents; do
        local hook_ver
        hook_ver="$(get_hook_version "$agent")"
        if [ -z "$hook_ver" ] || [ "$hook_ver" = "$new_current" ]; then
            continue
        fi
        upgrade_single_hook "$agent" "$new_current" "$hook_ver"
    done

    info "Rollback complete: now at v$previous"
}

# ============================================================
# Hook upgrade helpers
# ============================================================

upgrade_hooks() {
    local new_version="$1" old_version="$2"
    local ver_dir
    ver_dir="$(get_version_dir "$new_version")"
    local agents
    agents="$(list_known_agents "$ver_dir")"

    for agent in $agents; do
        local hook_ver
        hook_ver="$(get_hook_version "$agent")"
        if [ -z "$hook_ver" ]; then
            # Not previously installed, skip upgrade (use install flow instead)
            continue
        fi
        upgrade_single_hook "$agent" "$new_version" "$hook_ver"
    done
}

upgrade_single_hook() {
    local agent="$1" new_version="$2" old_version="$3"

    info "Upgrading hook: $agent ($old_version → $new_version)"

    local old_dir new_dir
    old_dir="$(get_version_dir "$old_version")"
    new_dir="$(get_version_dir "$new_version")"

    # Step 1: Uninstall using old version's script
    if [ -f "$old_dir/hooks/$agent/uninstall.sh" ]; then
        if ! bash "$old_dir/hooks/$agent/uninstall.sh" 2>&1; then
            warn "Failed to uninstall $agent hook (old version $old_version)"
        fi
    fi

    # Step 2: Install using new version's script
    if [ -f "$new_dir/hooks/$agent/install.sh" ]; then
        if bash "$new_dir/hooks/$agent/install.sh" --upgrade -y 2>&1; then
            write_hook_state_entry "$agent" "$new_version" "installed"
            info "Hook upgraded: $agent → v$new_version"
            return
        fi
    fi

    # Step 3: Install failed — rollback to old version
    warn "Failed to install $agent hook (new version $new_version), rolling back ..."
    if [ -f "$old_dir/hooks/$agent/install.sh" ]; then
        if bash "$old_dir/hooks/$agent/install.sh" --upgrade -y 2>&1; then
            write_hook_state_entry "$agent" "$old_version" "error" "upgrade to $new_version failed, rolled back to $old_version"
        else
            write_hook_state_entry "$agent" "$old_version" "error" "upgrade to $new_version failed, rollback also failed"
        fi
    else
        write_hook_state_entry "$agent" "$old_version" "error" "upgrade to $new_version failed, no rollback script"
    fi
}

retry_failed_hooks() {
    local target_agent="${1:-}"
    local current
    current="$(read_pointer "current")"
    if [ -z "$current" ]; then
        error "No current version"
        exit 1
    fi

    local file="$INSTALL_DIR/hook-state.json"
    if [ ! -f "$file" ]; then
        info "No hook state file, nothing to retry"
        return
    fi

    local agents_to_retry
    if [ -n "$target_agent" ]; then
        local status
        status="$(python3 -c "
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    print(d.get(sys.argv[2], {}).get('status', ''))
except: pass
" "$file" "$target_agent")"
        if [ "$status" != "error" ]; then
            info "Agent '$target_agent' has no failed hook to retry"
            return
        fi
        agents_to_retry="$target_agent"
    else
        agents_to_retry="$(python3 -c "
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    for k, v in d.items():
        if v.get('status') == 'error':
            print(k)
except: pass
" "$file")"
    fi

    if [ -z "$agents_to_retry" ]; then
        info "No failed hooks to retry"
        return
    fi

    for agent in $agents_to_retry; do
        local hook_ver
        hook_ver="$(get_hook_version "$agent")"
        if [ -z "$hook_ver" ]; then
            hook_ver="$current"
        fi
        upgrade_single_hook "$agent" "$current" "$hook_ver"
    done
}

# ============================================================
# Legacy migration
# ============================================================

migrate_legacy_layout() {
    if [ ! -d "$INSTALL_DIR/.git" ]; then
        return
    fi

    local version=""
    if [ -f "$INSTALL_DIR/VERSION" ]; then
        version="$(cat "$INSTALL_DIR/VERSION" | tr -d '[:space:]')"
    fi
    if [ -z "$version" ]; then
        version="0.0.0-legacy"
    fi

    info "Migrating from git layout to versioned layout (v$version) ..."

    local ver_dir
    ver_dir="$(get_version_dir "$version")"
    mkdir -p "$INSTALL_DIR/versions"

    # Move files into version directory (exclude shared dirs and .git)
    mkdir -p "$ver_dir"
    for item in "$INSTALL_DIR"/*; do
        local name
        name="$(basename "$item")"
        case "$name" in
            versions|config|data|logs|current|previous|hook-state.json) continue ;;
            .*) continue ;;
        esac
        mv "$item" "$ver_dir/" 2>/dev/null || cp -R "$item" "$ver_dir/"
    done

    # Remove .git
    rm -rf "$INSTALL_DIR/.git"

    # Create shared dirs if not exist
    mkdir -p "$INSTALL_DIR/config" "$INSTALL_DIR/data" "$INSTALL_DIR/logs"

    # Write pointer
    write_pointer "current" "$version"

    info "Migration complete: v$version"
}

# ============================================================
# GC old versions
# ============================================================

gc_versions() {
    local current previous
    current="$(read_pointer "current")"
    previous="$(read_pointer "previous")"

    if [ ! -d "$INSTALL_DIR/versions" ]; then
        return
    fi

    for ver_dir in "$INSTALL_DIR/versions"/*/; do
        [ -d "$ver_dir" ] || continue
        local ver
        ver="$(basename "$ver_dir")"
        if [ "$ver" = "$current" ] || [ "$ver" = "$previous" ]; then
            continue
        fi
        info "GC: removing version $ver"
        rm -rf "$ver_dir"
    done
}

# ============================================================
# Service file updater
# ============================================================

update_service_files() {
    local langstash_bin="$1"

    if [ "$(uname)" = "Darwin" ]; then
        local plist="$HOME/Library/LaunchAgents/com.langstash.plist"
        if [ -f "$plist" ]; then
            # Update ProgramArguments to new binary path
            python3 -c "
import plistlib, sys
path = sys.argv[1]
new_bin = sys.argv[2]
with open(path, 'rb') as f:
    pl = plistlib.load(f)
pl['ProgramArguments'] = [new_bin]
with open(path, 'wb') as f:
    plistlib.dump(pl, f)
" "$plist" "$langstash_bin" 2>/dev/null || true
        fi
    else
        local svc="$HOME/.config/systemd/user/langstash.service"
        if [ -f "$svc" ]; then
            # Update ExecStart line
            if command -v sed &>/dev/null; then
                sed -i.bak "s|^ExecStart=.*|ExecStart=${langstash_bin} --server-only|" "$svc" 2>/dev/null || true
                rm -f "${svc}.bak" 2>/dev/null || true
            fi
            systemctl --user daemon-reload 2>/dev/null || true
        fi
    fi
}

# ============================================================
# CLI wrapper installer
# ============================================================

install_wrapper() {
    mkdir -p "$(dirname "$LANGSTASH_WRAPPER")"

    cat > "$LANGSTASH_WRAPPER" << 'WRAPPEREOF'
#!/usr/bin/env bash
# langstash CLI wrapper — dynamically resolves current version
set -euo pipefail

INSTALL_DIR="$HOME/.agent-exporter-to-langfuse"
POINTER_FILE="$INSTALL_DIR/current"

if [ ! -f "$POINTER_FILE" ]; then
    echo "ERROR: langstash not installed (no current version pointer)" >&2
    exit 1
fi

CURRENT="$(cat "$POINTER_FILE" | tr -d '[:space:]')"
if [ -z "$CURRENT" ]; then
    echo "ERROR: current version pointer is empty" >&2
    exit 1
fi

LANGSTASH_BIN="$INSTALL_DIR/versions/$CURRENT/exporter/.venv/bin/langstash"
if [ ! -x "$LANGSTASH_BIN" ]; then
    echo "ERROR: langstash binary not found at $LANGSTASH_BIN" >&2
    exit 1
fi

exec "$LANGSTASH_BIN" "$@"
WRAPPEREOF

    chmod +x "$LANGSTASH_WRAPPER"
    info "CLI wrapper installed: $LANGSTASH_WRAPPER"

    # Check if ~/.local/bin is in PATH
    case ":$PATH:" in
        *":$HOME/.local/bin:"*) ;;
        *) warn "$HOME/.local/bin is not in PATH. Add it to use 'langstash' directly." ;;
    esac
}

# ============================================================
# Main dispatch
# ============================================================

usage() {
    cat <<EOF
Usage: installer.sh <command> [OPTIONS]

Commands:
  install     Install agent-exporter-to-langfuse
  upgrade     Upgrade to a new version
  uninstall   Uninstall (--purge to remove all data)
  rollback    Rollback to the previous version

Install Options:
  --version VER       Install specific version
  --package-url URL   Use a custom package URL (supports file://)
  --skip-verify       Skip SHA-256 integrity verification
  --secret-key KEY    Langfuse Secret Key
  --public-key KEY    Langfuse Public Key
  --base-url URL      Langfuse Base URL
  --user-id ID        Langfuse User ID (optional)
  --tags TAGS         Extra tags, comma-separated (optional)

  Environment variables LANGFUSE_SECRET_KEY, LANGFUSE_PUBLIC_KEY,
  LANGFUSE_BASE_URL, LANGFUSE_USER_ID, LANGFUSE_TAGS are also accepted.

Upgrade Options:
  --version VER       Upgrade to specific version
  --package-url URL   Use a custom package URL
  --skip-verify       Skip SHA-256 integrity verification
  --retry-hooks       Retry failed hook upgrades only
  --agent NAME        With --retry-hooks, retry only this agent's hook

Uninstall Options:
  --purge             Remove config, data, and logs too
EOF
    exit 0
}

if [ $# -eq 0 ]; then
    usage
fi

COMMAND="$1"; shift

case "$COMMAND" in
    install)   cmd_install "$@" ;;
    upgrade)   cmd_upgrade "$@" ;;
    uninstall) cmd_uninstall "$@" ;;
    rollback)  cmd_rollback "$@" ;;
    -h|--help) usage ;;
    *) error "Unknown command: $COMMAND"; usage ;;
esac
