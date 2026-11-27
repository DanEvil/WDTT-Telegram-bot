# Whitelisted front for VLESS+XHTTP (no RU server needed)

Goal: let Happ / v2rayNG / v2ray clients connect under RU whitelist (БС) mode
to the VPN node `78.17.16.23` (HOSTKEY, NL — NOT in whitelists, L3-dropped).

Chain:

```
Happ/v2rayNG (vless://...@<front>:443, type=xhttp, security=tls)
        │  TLS to <id>.containers.yandexcloud.net  (Yandex Cloud AS200350 — whitelisted)
        ▼
Yandex Serverless Container: streaming reverse proxy (proxy.py)
        │  plain HTTP, any Host accepted (xray xhttpSettings.host="")
        ▼
78.17.16.23:20500  vless-xhttp-in  path=/xhttp     -> outbound wdtt-out    (WDTT over VK TURN)
78.17.16.23:20501  vless-xhttp-of-in path=/xhttp-of -> outbound openflux-out (OpenFlux via Yandex.Docs/direct)
```

Server side (already applied on 78.17.16.23):
- `xhttpSettings.host = ""` on both XHTTP inbounds — xray skips the Host check
  when host is empty (transport/internet/splithttp/hub.go: `if len(h.host) > 0 …`),
  so any front domain works. UUID auth still required.
- `openflux-client.service` — openflux client on 127.0.0.1:11081 → local exit :56004
  (transports direct,yandex; key /etc/openflux/secret.key).

Deploy:

```bash
yc login                       # your Yandex account (free tier is enough)
export TARGET_IP=78.17.16.23
cd deploy/yc-xhttp-front
./deploy.sh                    # prints https://<id>.containers.yandexcloud.net
```

Bot (.env on 2.56.120.156):

```
VLESS_XHTTP_ENABLED=true
VLESS_XHTTP_HOST=<id>.containers.yandexcloud.net
VLESS_XHTTP_PORT=443
VLESS_XHTTP_PATH=/xhttp
VLESS_XHTTP_OF_PATH=/xhttp-of
```

Then users get two links in the Mini App / bot:
- `vless_xhttp`     — exit via WDTT (VK TURN relay)
- `vless_xhttp_of`  — exit via OpenFlux (Yandex.Docs carrier + direct)

Note: Cloud Functions (functions.yandexcloud.net) are NOT suitable — they buffer
request/response (3.5 MB cap), while XHTTP packet-up needs long-lived streaming.
Serverless Containers stream fine and their domain is Yandex Cloud AS200350.
