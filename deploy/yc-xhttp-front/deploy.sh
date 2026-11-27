#!/bin/bash
# Deploy streaming reverse proxy to Yandex Serverless Containers.
# Result: https://<container-id>.containers.yandexcloud.net -> http://$TARGET_IP:20500 (xray XHTTP)
#
# The containers.yandexcloud.net domain resolves to Yandex Cloud (AS200350) IPs,
# which are present in RU whitelists of all major providers — so this endpoint
# is reachable from Russia even under whitelist (БС) filtering, no RU server rented.
#
# Usage:
#   export TARGET_IP=78.17.16.23
#   ./deploy.sh
# Then set in bot .env:
#   VLESS_XHTTP_HOST=<container-id>.containers.yandexcloud.net
#   VLESS_XHTTP_PORT=443
#   VLESS_XHTTP_PATH=/xhttp            (WDTT exit via VK TURN)
#   VLESS_XHTTP_OF_PATH=/xhttp-of      (OpenFlux exit)
set -euo pipefail

TARGET_IP="${TARGET_IP:-78.17.16.23}"
NAME="${NAME:-ko-xhttp-front}"
REGISTRY_NAME="${REGISTRY_NAME:-ko-front}"

yc config profile activate 2>/dev/null || true
FOLDER_ID=$(yc config get folder-id)

# registry
REGISTRY_ID=$(yc container registry list --format json | python3 -c \
  "import sys,json; rs=json.load(sys.stdin); print(next((r['id'] for r in rs if r['name']=='$REGISTRY_NAME'), ''))")
if [ -z "$REGISTRY_ID" ]; then
  REGISTRY_ID=$(yc container registry create "$REGISTRY_NAME" --format json | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
fi
echo "registry: $REGISTRY_ID"

# build & push
yc container image build --file Dockerfile --tag "cr.yandex/$REGISTRY_ID/$NAME:v1" . >/dev/null
yc container image push "cr.yandex/$REGISTRY_ID/$NAME:v1"

# container (create or update)
CID=$(yc serverless container list --format json | python3 -c \
  "import sys,json; cs=json.load(sys.stdin); print(next((c['id'] for c in cs if c['name']=='$NAME'), ''))")
SPEC="spec:
  cores: 1
  coreFraction: 100
  memory: 512Mb
  executionTimeout: 600s
  concurrency: 8
  serviceAccountId: $(yc iam service-account list --format json | python3 -c "import sys,json; sas=json.load(sys.stdin); print(next(s['id'] for s in sas if s['name']=='default-sa'))")
  imageSpec:
    imageUrl: cr.yandex/$REGISTRY_ID/$NAME:v1
  env:
    - key: TARGET
      value: http://$TARGET_IP:20500
  resources:
    memory: 512Mb
    cores: 1
"
if [ -z "$CID" ]; then
  yc serverless container create --name "$NAME" --format json >/dev/null
  CID=$(yc serverless container list --format json | python3 -c "import sys,json; cs=json.load(sys.stdin); print(next(c['id'] for c in cs if c['name']=='$NAME'))")
fi
echo "$SPEC" | yc serverless container update "$CID" --from-yaml - >/dev/null || \
  yc serverless container deploy --id "$CID" >/dev/null
yc serverless container allow-unauthenticated-access "$CID" >/dev/null 2>&1 || true

DOMAIN=$(yc serverless container get "$CID" --format json | python3 -c "import sys,json; print(json.load(sys.stdin)['domain'])")
echo
echo "=== READY ==="
echo "Front URL: https://$DOMAIN"
echo "Set in bot .env: VLESS_XHTTP_HOST=$DOMAIN"
