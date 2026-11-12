#!/usr/bin/env bash
# Развёртывание всех 3 VPN протоколов на сервере 144.48.10.130
# Выполнять под root: bash deploy_vpn.sh
set -euo pipefail

SERVER_IP="144.48.10.130"
WDTT_PASS="${WDTT_PASS:-test123}"

echo "=== Развёртывание VPN протоколов на $SERVER_IP ==="

# ---------- 0. Подготовка системы ----------
apt-get update -y
apt-get install -y curl wget git build-essential iptables iproute2

# Go (для OpenFlux)
if ! command -v go &>/dev/null; then
    curl -L https://go.dev/dl/go1.27.0.linux-amd64.tar.gz -o /tmp/go.tar.gz
    rm -rf /usr/local/go && tar -C /usr/local -xzf /tmp/go.tar.gz
    echo 'export PATH=$PATH:/usr/local/go/bin' >> /etc/profile.d/go.sh
    export PATH=$PATH:/usr/local/go/bin
fi

# ---------- 1. WDTT ----------
echo "--- WDTT ---"
if [ ! -f /root/wdtt-test-amd64 ]; then
    echo "WDTT бинарник не найден. Скопируйте wdtt на сервер и запустите:"
    echo "  ./wdtt-test-amd64 -password $WDTT_PASS"
else
    # Проверяем, запущен ли WDTT
    if ! pgrep -f wdtt-test-amd64 >/dev/null; then
        nohup /root/wdtt-test-amd64 -password "$WDTT_PASS" > /var/log/wdtt.log 2>&1 &
        echo "WDTT запущен (PID $!)"
    else
        echo "WDTT уже запущен"
    fi
fi

# ---------- 2. AIVPN exit node ----------
echo "--- AIVPN ---"
if [ ! -d /root/aivpn ]; then
    git clone https://github.com/infosave2007/aivpn.git /root/aivpn
fi
cd /root/aivpn
cargo build --release 2>/dev/null || cargo build --release

# AIVPN exit на порту 443 (маскировка под HTTPS)
nohup ./target/release/aivpn-server \
    --role exit \
    --bind 0.0.0.0:443 \
    > /var/log/aivpn.log 2>&1 &
echo "AIVPN exit запущен (PID $!) на 443"

# ---------- 3. OpenFlux exit node ----------
echo "--- OpenFlux ---"
if [ ! -d /root/OpenFlux ]; then
    git clone https://github.com/p1neappleXpress/OpenFlux.git /root/OpenFlux
fi
cd /root/OpenFlux
go build -o /usr/local/bin/openflux .

# Секрет для шифрования (общий для клиентов)
OPENFLUX_SECRET="${OPENFLUX_SECRET:-$(openssl rand -hex 32)}"
echo "$OPENFLUX_SECRET" > /etc/openflux.key
chmod 600 /etc/openflux.key

# OpenFlux exit на порту 56000 (L4 режим, gVisor, не требует root для сети, но работает и с root)
nohup openflux \
    --role=exit \
    --mode=l4 \
    --transport=direct \
    --direct-listen=0.0.0.0:56000 \
    --encryption-key-file=/etc/openflux.key \
    --share-host="$SERVER_IP" \
    > /var/log/openflux.log 2>&1 &
echo "OpenFlux exit запущен (PID $!) на 56000"

# ---------- 4. Firewall ----------
echo "--- Firewall ---"
iptables -A INPUT -p tcp --dport 22 -j ACCEPT      # SSH
iptables -A INPUT -p tcp --dport 443 -j ACCEPT     # AIVPN
iptables -A INPUT -p tcp --dport 56000 -j ACCEPT   # OpenFlux
iptables -A INPUT -p udp --dport 56000:56003 -j ACCEPT  # WDTT
iptables -A INPUT -j ACCEPT
iptables-save > /etc/iptables/rules.v4 2>/dev/null || true

# ---------- 5. systemd сервисы ----------
cat > /etc/systemd/system/wdtt.service <<EOF
[Unit]
Description=WDTT VPN
After=network.target

[Service]
Type=simple
ExecStart=/root/wdtt-test-amd64 -password $WDTT_PASS
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/aivpn.service <<EOF
[Unit]
Description=AIVPN Exit Node
After=network.target

[Service]
Type=simple
WorkingDirectory=/root/aivpn
ExecStart=/root/aivpn/target/release/aivpn-server --role exit --bind 0.0.0.0:443
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/openflux.service <<EOF
[Unit]
Description=OpenFlux Exit Node
After=network.target

[Service]
Type=simple
ExecStart=/usr/local/bin/openflux --role=exit --mode=l4 --transport=direct --direct-listen=0.0.0.0:56000 --encryption-key-file=/etc/openflux.key --share-host=$SERVER_IP
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable wdtt aivpn openflux 2>/dev/null || true

echo ""
echo "=== Готово ==="
echo "WDTT:      порты 56000/56001/56003 UDP, 2860/2096/2861 TCP"
echo "AIVPN:     порт 443 TCP"
echo "OpenFlux:  порт 56000 TCP (direct, L4)"
echo "Секрет OpenFlux: /etc/openflux.key"
echo ""
echo "Проверить: ss -tlnp | grep -E '443|56000|2860|2096|2861'"
