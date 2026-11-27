"""Конвертер ссылок openflux:// -> vless://.

OpenFlux раздаёт клиентам ссылки вида openflux://v1/<base64url(raw DEFLATE(JSON))>.
Внутри JSON: {name, negotiate, codec, secret, context, transports:[{type,url,dial,...}]}.
Для direct-транспорта в поле dial лежит host:port выходной ноды.

Задача плагина: по openflux:// ссылке выходной ноды сформировать эквивалентную
vless:// ссылку на xray (VLESS+Reality), который крутится на той же ноде
(порт 443), чтобы клиенты Happ/v2rayNG могли подключаться обычным vless://.

Формат vless:// (см. bot._build_vless_url):
  vless://<uuid>@<server>:<port>?encryption=none&security=reality&type=tcp
          &flow=xtls-rprx-vision&sni=<sni>&fp=chrome&pbk=<pbk>&sid=<sid>#<remark>

Использование как модуль:
    from link_converter import openflux_to_vless, decode_openflux
    vless = openflux_to_vless(of_link, vless_params={...})

CLI:
    python link_converter.py "openflux://v1/..." [--server H] [--port P] --uuid U --pbk K --sid S --sni N
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import zlib
from urllib.parse import quote

PREFIX = "openflux://v1/"


class LinkError(ValueError):
    """Невалидная openflux:// ссылка."""


def _normalize_body(body: str) -> str:
    # как в share.Decode: терпим пробелы/переносы, padding и стандартный алфавит base64
    body = "".join(body.split())
    body = body.replace("+", "-").replace("/", "_").rstrip("=")
    return body


def decode_openflux(link: str) -> dict:
    """Парсит openflux://v1/... в dict конфигурации. Бросает LinkError."""
    link = (link or "").strip()
    if not link.startswith(PREFIX):
        raise LinkError("not an openflux://v1/ link")
    body = _normalize_body(link[len(PREFIX):])
    try:
        packed = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    except Exception as e:
        raise LinkError(f"bad base64: {e}") from e
    try:
        raw = zlib.decompress(packed, -15)  # raw DEFLATE, как в share.Encode
    except Exception as e:
        raise LinkError(f"bad deflate payload: {e}") from e
    try:
        cfg = json.loads(raw.decode("utf-8"))
    except Exception as e:
        raise LinkError(f"bad json payload: {e}") from e
    if not isinstance(cfg, dict) or not cfg.get("transports"):
        raise LinkError("payload has no transports")
    return cfg


def encode_openflux(cfg: dict) -> str:
    """Сериализует dict обратно в openflux://v1/... (зеркало share.Encode)."""
    raw = json.dumps(cfg, separators=(",", ":")).encode()
    comp = zlib.compressobj(9, zlib.DEFLATED, -15)
    packed = comp.compress(raw) + comp.flush()
    return PREFIX + base64.urlsafe_b64encode(packed).decode().rstrip("=")


def personalize_link(base_link: str, *, name: str | None = None, doc_url: str | None = None) -> str:
    """Клонирует базовую openflux:// ссылку exit-ноды под пользователя.

    name    — метка профиля (например, tg<id>); отличается у каждого клиента.
    doc_url — если у пользователя свой документ-транспорт (Yandex), он
              подставляется в yandex-транспорт и в context ссылки. Secret
              трогать нельзя: он общий с exit-нодой (иначе exit не
              расшифрует трафик).
    """
    cfg = decode_openflux(base_link)
    if name:
        cfg["name"] = name
    if doc_url:
        for t in cfg.get("transports", []):
            if t.get("type") == "yandex":
                t["url"] = doc_url
        cfg["context"] = doc_url
    return encode_openflux(cfg)


def openflux_exit_host(cfg: dict) -> str | None:
    """host выходной ноды из direct-транспорта (поле dial), иначе None."""
    for t in cfg.get("transports", []):
        dial = (t or {}).get("dial") or ""
        if dial:
            return dial.rsplit(":", 1)[0]
    return None


def build_vless_url(
    uuid: str,
    server: str,
    port: int = 443,
    *,
    security: str = "reality",
    protocol: str = "tcp",
    sni: str = "",
    fp: str = "chrome",
    pbk: str = "",
    sid: str = "",
    flow: str = "xtls-rprx-vision",
    remark: str = "OpenFlux->VLESS",
) -> str:
    """Собирает vless:// ссылку (формат совместим с bot._build_vless_url)."""
    params = [
        "encryption=none",
        f"security={security}",
        f"type={protocol}",
    ]
    if flow:
        params.append(f"flow={flow}")
    if sni:
        params.append(f"sni={quote(sni, safe='')}")
    if fp:
        params.append(f"fp={fp}")
    if security == "reality":
        if pbk:
            params.append(f"pbk={pbk}")
        if sid:
            params.append(f"sid={sid}")
    return f"vless://{uuid}@{server}:{port}?{'&'.join(params)}#{quote(remark, safe='')}"


def openflux_to_vless(
    of_link: str,
    *,
    vless_server: str,
    vless_port: int = 443,
    uuid: str,
    pbk: str = "",
    sid: str = "",
    sni: str = "",
    fp: str = "chrome",
    flow: str = "xtls-rprx-vision",
    security: str = "reality",
    remark: str = "OpenFlux->VLESS",
    strict_host: bool = False,
) -> str:
    """Конвертирует openflux:// ссылку в vless://.

    vless_server — адрес xray-ноды (обычно та же машина, что и openflux exit).
    Если strict_host=True и в ссылке есть direct dial с другим host — LinkError.
    """
    cfg = decode_openflux(of_link)
    host = openflux_exit_host(cfg)
    if strict_host and host and host != vless_server:
        raise LinkError(f"openflux exit host {host!r} != vless server {vless_server!r}")
    return build_vless_url(
        uuid, vless_server, vless_port,
        security=security, sni=sni, fp=fp, pbk=pbk, sid=sid,
        flow=flow, remark=remark,
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="openflux:// -> vless:// converter")
    p.add_argument("link", help="openflux://v1/... link")
    p.add_argument("--server", required=True, help="vless server (xray node)")
    p.add_argument("--port", type=int, default=443)
    p.add_argument("--uuid", required=True)
    p.add_argument("--pbk", default="", help="reality public key")
    p.add_argument("--sid", default="", help="reality short id")
    p.add_argument("--sni", default="")
    p.add_argument("--fp", default="chrome")
    p.add_argument("--flow", default="xtls-rprx-vision")
    p.add_argument("--security", default="reality")
    p.add_argument("--remark", default="OpenFlux->VLESS")
    p.add_argument("--strict-host", action="store_true")
    a = p.parse_args(argv)
    try:
        cfg = decode_openflux(a.link)
        print(json.dumps(cfg, ensure_ascii=False, indent=2), file=sys.stderr)
        print(openflux_to_vless(
            a.link, vless_server=a.server, vless_port=a.port, uuid=a.uuid,
            pbk=a.pbk, sid=a.sid, sni=a.sni, fp=a.fp, flow=a.flow,
            security=a.security, remark=a.remark, strict_host=a.strict_host,
        ))
        return 0
    except LinkError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
