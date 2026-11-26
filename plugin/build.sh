#!/bin/bash
set -e

# =============================================================================
# WDTT Outbound Plugin & Custom Xray Build Script
# =============================================================================
# This script:
# 1. Builds the WDTT outbound plugin
# 2. Builds Xray with -tags=plugin
# 3. Copies the plugin to Xray plugins directory
# 4. Creates example config.json for 3x-ui
# =============================================================================

# Configuration
PLUGIN_NAME="wdtt-outbound"
PLUGIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="${PLUGIN_DIR}/build"
XRAY_DIR="${BUILD_DIR}/xray"
XRAY_VERSION="${XRAY_VERSION:-v1.8.10}"
PLUGIN_OUTPUT="${BUILD_DIR}/${PLUGIN_NAME}"
XRAY_OUTPUT="${XRAY_DIR}/xray"
PLUGINS_DIR="${BUILD_DIR}/plugins"
CONFIG_DIR="${BUILD_DIR}/config"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log_info() { echo -e "${BLUE}[INFO]${NC} $*"; }
log_success() { echo -e "${GREEN}[SUCCESS]${NC} $*"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }

# -----------------------------------------------------------------------------
# Step 1: Build the plugin
# -----------------------------------------------------------------------------
build_plugin() {
    log_info "Building WDTT outbound plugin..."
    mkdir -p "${BUILD_DIR}"

    cd "${PLUGIN_DIR}"
    go build -ldflags="-s -w" -o "${PLUGIN_OUTPUT}" .

    if [[ -f "${PLUGIN_OUTPUT}" ]]; then
        local size=$(du -h "${PLUGIN_OUTPUT}" | cut -f1)
        log_success "Plugin built: ${PLUGIN_OUTPUT} (${size})"
    else
        log_error "Plugin build failed"
        exit 1
    fi
}

# -----------------------------------------------------------------------------
# Step 2: Build Xray with -tags=plugin
# -----------------------------------------------------------------------------
build_xray() {
    log_info "Building Xray with plugin support (tags=plugin)..."
    mkdir -p "${XRAY_DIR}"

    if [[ ! -d "${XRAY_DIR}/.git" ]]; then
        log_info "Cloning Xray-core repository (version: ${XRAY_VERSION})..."
        git clone --depth 1 --branch "${XRAY_VERSION}" https://github.com/XTLS/Xray-core.git "${XRAY_DIR}"
    else
        log_info "Xray repository already exists, updating..."
        cd "${XRAY_DIR}"
        git fetch --depth 1 origin "${XRAY_VERSION}"
        git checkout "${XRAY_VERSION}"
    fi

    cd "${XRAY_DIR}"

    # Build Xray with plugin tag
    log_info "Compiling Xray with -tags=plugin..."
    go build -tags=plugin -ldflags="-s -w" -o "${XRAY_OUTPUT}" ./main

    if [[ -f "${XRAY_OUTPUT}" ]]; then
        local size=$(du -h "${XRAY_OUTPUT}" | cut -f1)
        log_success "Xray built: ${XRAY_OUTPUT} (${size})"
    else
        log_error "Xray build failed"
        exit 1
    fi
}

# -----------------------------------------------------------------------------
# Step 3: Copy plugin to Xray plugins directory
# -----------------------------------------------------------------------------
copy_plugin() {
    log_info "Copying plugin to Xray plugins directory..."
    mkdir -p "${PLUGINS_DIR}"

    cp "${PLUGIN_OUTPUT}" "${PLUGINS_DIR}/${PLUGIN_NAME}"

    # Make executable
    chmod +x "${PLUGINS_DIR}/${PLUGIN_NAME}"

    log_success "Plugin copied to: ${PLUGINS_DIR}/${PLUGIN_NAME}"
}

# -----------------------------------------------------------------------------
# Step 4: Create example config.json for 3x-ui
# -----------------------------------------------------------------------------
create_3xui_config() {
    log_info "Creating example config.json for 3x-ui..."
    mkdir -p "${CONFIG_DIR}"

    cat > "${CONFIG_DIR}/config.json" << 'EOF'
{
  "log": {
    "access": "",
    "error": "",
    "loglevel": "warning"
  },
  "inbounds": [
    {
      "listen": "127.0.0.1",
      "port": 10808,
      "protocol": "socks",
      "settings": {
        "auth": "noauth",
        "udp": true,
        "userLevel": 8
      },
      "sniffing": {
        "enabled": true,
        "destOverride": ["http", "tls", "quic"],
        "routeOnly": false
      }
    },
    {
      "listen": "127.0.0.1",
      "port": 10809,
      "protocol": "http",
      "settings": {
        "userLevel": 8
      }
    }
  ],
  "outbounds": [
    {
      "protocol": "plugin",
      "tag": "wdtt-outbound",
      "settings": {
        "plugin": "wdtt-outbound",
        "args": [],
        "pluginOpts": {
          "panel_url": "https://your-panel.example.com",
          "panel_username": "your-username",
          "panel_password": "your-password",
          "verify_ssl": true,
          "vk_hash_mode": "auto",
          "fixed_vk_hash": "",
          "pool_size": 10,
          "vk_hash_refresh_interval": "5m"
        }
      },
      "streamSettings": {
        "network": "tcp",
        "security": "none"
      }
    },
    {
      "protocol": "freedom",
      "tag": "direct"
    },
    {
      "protocol": "blackhole",
      "tag": "blocked"
    }
  ],
  "routing": {
    "domainStrategy": "IPIfNonMatch",
    "rules": [
      {
        "type": "field",
        "ip": [
          "geoip:private"
        ],
        "outboundTag": "direct"
      },
      {
        "type": "field",
        "domain": [
          "geosite:category-ads-all"
        ],
        "outboundTag": "blocked"
      }
    ]
  },
  "policy": {
    "levels": {
      "8": {
        "handshake": 4,
        "connIdle": 300,
        "uplinkOnly": 2,
        "downlinkOnly": 5,
        "statsUserUplink": false,
        "statsUserDownlink": false
      }
    },
    "system": {
      "statsInboundUplink": false,
      "statsInboundDownlink": false,
      "statsOutboundUplink": false,
      "statsOutboundDownlink": false
    }
  },
  "dns": {
    "servers": [
      "https+local://1.1.1.1/dns-query",
      "8.8.8.8",
      "1.1.1.1",
      "localhost"
    ],
    "hosts": {},
    "tag": "dns_inbound"
  }
}
EOF

    # Also create a minimal config for quick testing
    cat > "${CONFIG_DIR}/config.minimal.json" << 'EOF'
{
  "log": { "loglevel": "warning" },
  "inbounds": [
    {
      "listen": "127.0.0.1",
      "port": 10808,
      "protocol": "socks",
      "settings": { "auth": "noauth", "udp": true }
    }
  ],
  "outbounds": [
    {
      "protocol": "plugin",
      "tag": "wdtt-outbound",
      "settings": {
        "plugin": "wdtt-outbound",
        "pluginOpts": {
          "panel_url": "https://your-panel.example.com",
          "panel_username": "your-username",
          "panel_password": "your-password",
          "verify_ssl": true,
          "vk_hash_mode": "auto",
          "pool_size": 10,
          "vk_hash_refresh_interval": "5m"
        }
      }
    },
    { "protocol": "freedom", "tag": "direct" }
  ],
  "routing": {
    "rules": [
      { "type": "field", "ip": ["geoip:private"], "outboundTag": "direct" }
    ]
  }
}
EOF

    log_success "3x-ui configs created:"
    log_success "  - ${CONFIG_DIR}/config.json (full)"
    log_success "  - ${CONFIG_DIR}/config.minimal.json (minimal)"
}

# -----------------------------------------------------------------------------
# Print summary
# -----------------------------------------------------------------------------
print_summary() {
    echo
    echo "============================================================================="
    echo "BUILD COMPLETE"
    echo "============================================================================="
    echo
    echo "Artifacts:"
    echo "  Plugin:     ${PLUGIN_OUTPUT}"
    echo "  Xray:       ${XRAY_OUTPUT}"
    echo "  Plugins:    ${PLUGINS_DIR}/${PLUGIN_NAME}"
    echo "  Configs:    ${CONFIG_DIR}/"
    echo
    echo "Directory structure:"
    tree "${BUILD_DIR}" 2>/dev/null || find "${BUILD_DIR}" -type f | sort | sed 's|^|  |'
    echo
    echo "To run with 3x-ui:"
    echo "  1. Copy ${PLUGINS_DIR}/${PLUGIN_NAME} to your Xray plugins directory"
    echo "  2. Use ${CONFIG_DIR}/config.json as your Xray configuration"
    echo "  3. Update panel_url, panel_username, panel_password in the config"
    echo
    echo "To run standalone:"
    echo "  ${XRAY_OUTPUT} -config ${CONFIG_DIR}/config.json"
    echo
}

# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
main() {
    echo "============================================================================="
    echo "WDTT Outbound Plugin & Custom Xray Build"
    echo "============================================================================="
    echo

    # Check for Go
    if ! command -v go &> /dev/null; then
        log_error "Go is not installed or not in PATH"
        exit 1
    fi

    log_info "Go version: $(go version)"

    # Check for Git
    if ! command -v git &> /dev/null; then
        log_error "Git is not installed or not in PATH"
        exit 1
    fi

    build_plugin
    build_xray
    copy_plugin
    create_3xui_config
    print_summary
}

main "$@"