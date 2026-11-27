#!/bin/bash
# Перезаливает список клиентов VLESS в xray после перезапуска.
# Пользователи, добавленные через xray API, живут только в памяти процесса,
# поэтому после рестарта список берётся из /etc/xray-vless/users.json.
# Клиенты заливаются в три инбаунда: vless-in (REALITY, 443), vless-xhttp-in
# (XHTTP, 20500, путь /xhttp -> wdtt-out) и vless-xhttp-of-in (XHTTP, 20501,
# путь /xhttp-of -> openflux-out), без flow — XHTTP его не поддерживает.
#
# Установка: см. deploy.sh / xray-vless-sync.service
set -u
X=/usr/local/xray-vless/bin/xray
S=127.0.0.1:62790
F=/etc/xray-vless/users.json
TAG=vless-in
PORT=443
TAG2=vless-xhttp-in
PORT2=20500
TAG3=vless-xhttp-of-in
PORT3=20501

[ -f "$F" ] || { echo "нет файла $F — нечего синхронизировать"; exit 0; }

python3 - "$F" "$TAG" "$PORT" "$TAG2" "$PORT2" "$TAG3" "$PORT3" > /tmp/xray-adu.json <<'PY'
import json, sys
f, tag, port, tag2, port2, tag3, port3 = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4], int(sys.argv[5]), sys.argv[6], int(sys.argv[7])
try:
    clients = json.load(open(f))
except Exception:
    print("[]")
    sys.exit(0)
if isinstance(clients, dict):
    clients = clients.get("clients", [])
clients = [c for c in clients if isinstance(c, dict) and c.get("id")]
# XHTTP не поддерживает flow (vision) — снимаем его для xhttp-инбаундов
clients_noflow = [{k: v for k, v in c.items() if k != "flow"} for c in clients]
def ib(t, p, cl):
    return {"tag": t, "listen": "0.0.0.0", "port": p, "protocol": "vless",
            "settings": {"clients": cl, "decryption": "none"}}
print(json.dumps({"inbounds": [ib(tag, port, clients), ib(tag2, port2, clients_noflow), ib(tag3, port3, clients_noflow)]}))
PY

N=$(python3 -c "import json;print(len(json.load(open('/tmp/xray-adu.json'))['inbounds'][0]['settings']['clients']))")
[ "$N" = "0" ] && { echo "список клиентов пуст"; exit 0; }

for i in 1 2 3 4 5; do
  out=$($X api adu --server=$S /tmp/xray-adu.json 2>&1) && break
  sleep 2
done
echo "$out"
rm -f /tmp/xray-adu.json
