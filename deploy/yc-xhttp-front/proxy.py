#!/usr/bin/env python3
"""Streaming reverse proxy for Xray XHTTP inbound + VLESS subscription endpoint.

Runs inside Yandex Serverless Container (domain <id>.containers.yandexcloud.net
resolves to Yandex Cloud AS200350 IPs, which are in RU whitelists), so it is
reachable from Russia under whitelist (БС) filtering without renting an RU server.

Routes:
  GET /vless-sub  -> base64 list of vless:// links (Happ/v2ray subscription shows
                     them as expandable servers). Built from env VLESS_UUID,
                     FRONT_HOST, SUB_PATHS, SUB_REMARKS.
  everything else -> streamed to TARGET (xray XHTTP inbound).

Env:
  TARGET       - base URL of xray XHTTP inbound, e.g. http://78.17.16.23:20500
  LISTEN       - listen address, default 0.0.0.0:8080 (container port)
  FRONT_HOST   - public host of this container, e.g. <id>.containers.yandexcloud.net
  VLESS_UUID   - shared vless UUID (must exist on xray inbounds 20500/20501)
  SUB_PATHS    - comma-separated xhttp paths, default "/xhttp,/xhttp-of"
  SUB_REMARKS  - comma-separated server remarks, default "WDTT,OpenFlux"
"""
import base64
import os
import urllib.parse

from aiohttp import ClientSession, ClientTimeout, web

TARGET = os.environ.get("TARGET", "http://127.0.0.1:20500").rstrip("/")
LISTEN = os.environ.get("LISTEN", "0.0.0.0:8080")
FRONT_HOST = os.environ.get("FRONT_HOST", "").strip()
VLESS_UUID = os.environ.get("VLESS_UUID", "").strip()
SUB_PATHS = [p.strip() for p in os.environ.get("SUB_PATHS", "/xhttp,/xhttp-of").split(",") if p.strip()]
SUB_REMARKS = [r.strip() for r in os.environ.get("SUB_REMARKS", "WDTT,OpenFlux").split(",")]

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}

client: ClientSession | None = None


def build_subscription() -> str:
    links = []
    for i, path in enumerate(SUB_PATHS):
        remark = SUB_REMARKS[i] if i < len(SUB_REMARKS) else f"Server {i + 1}"
        params = [
            "encryption=none",
            "type=xhttp",
            "security=tls",
            f"sni={FRONT_HOST}",
            f"path={urllib.parse.quote(path)}",
            f"host={FRONT_HOST}",
            "mode=packet-up",
            "uplinkDataPlacement=header",
            "uplinkChunkSize=16384",
            "scMaxEachPostBytes=32768",
            "scMinPostsIntervalMs=10",
        ]
        links.append(f"vless://{VLESS_UUID}@{FRONT_HOST}:443?{'&'.join(params)}#{urllib.parse.quote(remark)}")
    return base64.b64encode("\n".join(links).encode()).decode()


async def vless_sub(request: web.Request) -> web.Response:
    if not VLESS_UUID or not FRONT_HOST:
        return web.Response(status=500, text="VLESS_UUID/FRONT_HOST not configured")
    return web.Response(text=build_subscription(), content_type="text/plain")


async def handle(request: web.Request) -> web.StreamResponse:
    assert client is not None
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP}
    url = TARGET + request.path_qs
    try:
        resp = await client.request(
            request.method, url,
            headers=headers,
            data=request.content,          # streams the request body
            allow_redirects=False,
            timeout=ClientTimeout(total=None, sock_read=300),
        )
    except Exception as e:
        return web.Response(status=502, text=f"upstream error: {e}")
    out = web.StreamResponse(status=resp.status, headers={
        k: v for k, v in resp.headers.items() if k.lower() not in HOP_BY_HOP
    })
    await out.prepare(request)
    async for chunk in resp.content.iter_any():
        await out.write(chunk)            # streams the response body
    await out.write_eof()
    return out


async def on_startup(app: web.Application):
    global client
    client = ClientSession()


async def on_cleanup(app: web.Application):
    if client:
        await client.close()


app = web.Application()
app.on_startup.append(on_startup)
app.on_cleanup.append(on_cleanup)
app.router.add_get("/vless-sub", vless_sub)
app.router.add_route("*", "/{tail:.*}", handle)

if __name__ == "__main__":
    host, port = LISTEN.rsplit(":", 1)
    web.run_app(app, host=host, port=int(port), print=None)
