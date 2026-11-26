query = "&".join(params)
    # URL encode the remark
    import urllib.parse
    remark_encoded = urllib.parse.quote(name)

    return f"vless://{uuid}@{server}:{port}?{query}#{remark_encoded}"


def get_vless_only_links() -> dict:
    """Возвращает только VLESS ссылки для новых пользователей (не в базе).

    Используется при первом подключении по VLESS ссылке.
    """
    if not config.VLESS_ENABLED:
        return {}
    vless_url = _build_vless_url()
    if not vless_url:
        return {}
    return {"vless": vless_url}


async def _links_section(client, pwd: str, sub_id: str | None = None, include_vless: bool = True) -> str | None:
    """Блок со ссылками подключения для iOS и Android.

    Если настроен SUB_DOMAIN и есть sub_id — отдаём единую ссылку на подписку.
    Иначе платформенные ссылки берём со страницы подписки панели; если их нет —
    откатываемся на универсальную wdtt:// ссылку.

    Если include_vless=True, добавляет VLESS ссылку (для существующих пользователей).
    """
    parts = []

    # VLESS ссылка (добавляем первой, если включена)
    if include_vless and config.VLESS_ENABLED:
        vless_url = _build_vless_url()
        if vless_url:
            parts.append(f"<b>🔐 VLESS (Reality)</b>\n<code>{html.escape(vless_url)}</code>")

    # Если есть единый домен подписки и sub_id — отдаём унифицированную ссылку,
    # но только если Redis доступен (иначе nginx не найдёт маппинг)
    if config.SUB_DOMAIN and sub_id and redis_client:
        try:
            mapped = await redis_client.get(f"sub:{sub_id}")
            if mapped:
                sub_path = config.SUB_PATH.rstrip("/")
                unified_url = f"{config.SUB_DOMAIN.rstrip('/')}{sub_path}/{sub_id}"
                parts.append(f"<b>🔗 Единая ссылка подписки:</b>\n<code>{html.escape(unified_url)}</code>")
        except Exception as e:
            log.warning("Redis недоступен при проверке sub_id, fallback на прямые ссылки: %s", e)

    try:
        pl = await client.platform_links(pwd)
    except Exception:
        log.warning("платформенные ссылки недоступны, используем универсальную")
        pl = {}
    labels = (("ios", "🍏 iOS"), ("android", "🤖 Android"), ("windows", "🪟 Windows"))
    for key, label in labels:
        if pl.get(key):
            parts.append(f"<b>{label}</b>\n<code>{html.escape(pl[key])}</code>")

    if parts:
        return "\n\n".join(parts)

    try:
        univers = await client.find_link(pwd)
    except Exception:
        return None
    if univers:
        parts.append(f"<code>{html.escape(univers)}</code>")
        return "\n\n".join(parts)
    return None


async def links_json(client, pwd: str, sub_id: str | None = None, include_vless: bool = True) -> dict:
    """Ссылки подключения в JSON для Mini App: {unified?, ios?, android?, windows?, universal?, vless?}."""
    out: dict = {}

    # VLESS ссылка
    if include_vless and config.VLESS_ENABLED:
        vless_url = _build_vless_url()
        if vless_url:
            out["vless"] = vless_url

    if config.SUB_DOMAIN and sub_id and redis_client:
        try:
            mapped = await redis_client.get(f"sub:{sub_id}")
            if mapped:
                sub_path = config.SUB_PATH.rstrip("/")
                out["unified"] = f"{config.SUB_DOMAIN.rstrip('/')}{sub_path}/{sub_id}"
        except Exception as e:
            log.warning("Redis недоступен при проверке sub_id: %s", e)
    if "unified" not in out:
        try:
            pl = await client.platform_links(pwd)
            if pl.get("ios"):
                out["ios"] = pl["ios"]
            if pl.get("android"):
                out["android"] = pl["android"]
            if pl.get("windows"):
                out["windows"] = pl["windows"]
        except Exception:
            pass
        if not out:
            try:
                u = await client.find_link(pwd)
                if u:
                    out["universal"] = u
            except Exception:
                pass
    return out