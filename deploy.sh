#!/bin/bash
# ============================================================
# WDTT VPN DEPLOYMENT SCRIPT
# ============================================================
# This script deploys the complete WDTT VPN stack:
# - Xray with WDTT Plugin
# - Telegram Bot
# - WDTT Panel
# - OpenFlux Exit Node
# - PostgreSQL, Redis, Nginx, Prometheus, Grafana
# ============================================================

set -euo pipefail

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info() { echo -e "${BLUE}[INFO]${NC} $*"; }
log_success() { echo -e "${GREEN}[SUCCESS]${NC} $*"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }

# Configuration
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="${PROJECT_ROOT}/build"
COMPOSE_FILE="${PROJECT_ROOT}/docker-compose.yml"

# Check prerequisites
check_prerequisites() {
    log_info "Checking prerequisites..."
    
    for cmd in docker docker-compose git openssl; do
        if ! command -v "$cmd" &> /dev/null; then
            log_error "$cmd is not installed"
            exit 1
        fi
    done
    
    log_success "Prerequisites check passed"
}

# Generate secrets
generate_secrets() {
    log_info "Generating secrets..."
    
    mkdir -p secrets
    
    # Database password
    if [ ! -f secrets/db_password.txt ]; then
        openssl rand -base64 32 > secrets/db_password.txt
        log_success "Generated DB password"
    fi
    
    # Grafana password
    if [ ! -f secrets/grafana_password.txt ]; then
        openssl rand -base64 16 > secrets/grafana_password.txt
        log_success "Generated Grafana password"
    fi
    
    # OpenFlux secret
    if [ ! -f configs/openflux/secret.key ]; then
        openssl rand -hex 32 > configs/openflux/secret.key
        log_success "Generated OpenFlux secret"
    fi
}

# Build images
build_images() {
    log_info "Building Docker images..."
    
    # Build Xray with plugin
    log_info "Building Xray with WDTT plugin..."
    docker-compose build xray
    
    # Build bot
    log_info "Building Telegram bot..."
    docker-compose build bot
    
    # Build WDTT panel
    log_info "Building WDTT panel..."
    docker-compose build wdtt-panel
    
    log_success "All images built"
}

# Initialize database
init_database() {
    log_info "Initializing database..."
    
    # Start only DB and Redis first
    docker-compose up -d db redis
    
    # Wait for DB to be ready
    log_info "Waiting for database..."
    sleep 10
    
    # Run migrations (if any)
    docker-compose run --rm bot python -m alembic upgrade head || true
    
    log_success "Database initialized"
}

# Generate SSL certificates
generate_ssl() {
    log_info "Setting up SSL certificates..."
    
    mkdir -p certs
    
    if [ ! -f certs/your-domain.com.crt ] || [ ! -f certs/your-domain.com.key ]; then
        log_warn "SSL certificates not found. You need to:"
        echo "  1. Obtain SSL certificates for your domain (Let's Encrypt recommended)"
        echo "  2. Place them in ./certs/your-domain.com.crt and ./certs/your-domain.com.key"
        echo "  3. Or run: certbot certonly --standalone -d your-domain.com"
        echo ""
        read -p "Press Enter to continue after placing certificates..."
    fi
}

# Create config files from templates
generate_configs() {
    log_info "Generating configuration files..."
    
    # Check if .env exists
    if [ ! -f configs/bot/.env ]; then
        cp configs/bot/.env.example configs/bot/.env
        log_warn "Created configs/bot/.env from template. PLEASE EDIT IT WITH YOUR VALUES!"
    fi
    
    # Xray config - check if UUID is set
    if grep -q "YOUR_UUID_HERE" configs/xray/config.json; then
        log_warn "configs/xray/config.json contains placeholder UUID. Please edit with your UUID."
    fi
    
    # Plugin config
    if grep -q "your-panel.example.com" configs/xray/plugin-config.json; then
        log_warn "configs/xray/plugin-config.json contains placeholder values. Please edit."
    fi
    
    # Nginx config
    if grep -q "your-domain.com" configs/nginx/nginx.conf; then
        log_warn "configs/nginx/nginx.conf contains placeholder domain. Please edit."
    fi
}

# Deploy
deploy() {
    log_info "Starting deployment..."
    
    # Pull latest images
    docker-compose pull
    
    # Start all services
    docker-compose up -d
    
    log_success "Deployment started"
    log_info "Checking service health..."
    sleep 15
    
    # Check service status
    docker-compose ps
    
    log_info "Checking logs..."
    docker-compose logs --tail=50
}

# Main
main() {
    echo "==============================================================================="
    echo "WDTT VPN DEPLOYMENT"
    echo "==============================================================================="
    echo ""
    
    check_prerequisites
    generate_secrets
    generate_configs
    build_images
    init_database
    generate_ssl
    deploy
    
    echo ""
    echo "==============================================================================="
    log_success "DEPLOYMENT COMPLETE"
    echo "==============================================================================="
    echo ""
    echo "Services:"
    echo "  - Xray + WDTT Plugin:     https://your-domain.com:443"
    echo "  - Telegram Bot API:       http://your-domain.com:8080"
    echo "  - WDTT Panel:             https://your-domain.com/wdtt/"
    echo "  - Grafana:                http://your-domain.com:3000"
    echo "  - Prometheus:             http://your-domain.com:9090"
    echo ""
    echo "Next steps:"
    echo "  1. Edit configs/bot/.env with your actual values"
    echo "  2. Edit configs/xray/config.json with your UUID"
    echo "  3. Edit configs/xray/plugin-config.json with your panel URL"
    echo "  4. Edit configs/nginx/nginx.conf with your domain"
    echo "  5. Place SSL certificates in ./certs/"
    echo "  6. Restart: docker-compose restart"
    echo ""
    echo "Useful commands:"
    echo "  docker-compose logs -f bot          # Bot logs"
    echo "  docker-compose logs -f xray         # Xray logs"
    echo "  docker-compose ps                   # Service status"
    echo "  docker-compose restart              # Restart all"
}

# Run main
main "$@"