"""Клиент REST API панели WDTT (https://github.com/ildarmaga/wdtt).

Особенности протокола панели (см. docs/API.md и исходники panel/*.go):

* Логин: ``POST /login`` (form) -> cookie сессии ``wdtt-panel`` и CSRF-cookie
  ``wdtt-csrf``. На неверный пароль панель отвечает HTTP 200 c
  ``{"success": false}``, при частых попытках — 429 (rate-limiter), поэтому
  повторный логин делаем только по 401/редиректу, без перебора.
* Все запросы к ``/panel/api/*`` идут через requireAuthCSRF: без заголовка
  ``X-Requested-With: XMLHttpRequest`` вместо JSON будет 302-редирект, а для
  не-GET методов обязателен заголовок ``X-CSRF-Token`` со значением cookie
  ``wdtt-csrf``.
* В ``GET /panel/api/users`` реальный пароль пользователя лежит в поле
  ``password_key`` (поле ``password`` — маскированное), готовая ссылка
  ``wdtt://`` — в поле ``link``.
* ``POST /panel/api/users/update`` пересобирает запись пользователя из тела
  запроса: пропущенные поля сбрасываются в значения по умолчанию, поэтому
  клиент всегда передаёт expires_at / total_gb / max_devices / active явно.
"""

import base64
import json
import logging
import re
from typing import Any

import httpx

log = logging.getLogger(__name__)

_SESSION_COOKIE = "wdtt-panel"
_CSRF_COOKIE = "wdtt-csrf"


class WdttError(RuntimeError):
    """Панель вернула success=false или HTTP-ошибку (не связанную с сессией)."""


class WdttClient:
    def __init__(self, base_url: str, username: str, password: str, verify_ssl: bool = True):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self._client = httpx.AsyncClient(
            verify=verify_ssl,
            timeout=httpx.Timeout(40.0),
            follow_redirects=False,
        )
        self._logged_in = False

    async def close(self) -> None:
        await self._client.aclose()

    async def login(self) -> None:
        r = await self._client.post(
            f"{self.base_url}/login",
            data={"username": self.username, "password": self.password},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        if r.status_code in (301, 302, 303, 307, 308):
            raise WdttError("WDTT login: неожиданный редирект (проверьте WDTT_BASE_URL)")
        if r.status_code == 429:
            raise WdttError("WDTT login: панель заблокировала вход (rate-limit)")
        try:
            data = r.json()
        except ValueError:
            raise WdttError(f"WDTT login: не JSON (HTTP {r.status_code}) — проверьте WDTT_BASE_URL")
        if r.status_code >= 400 or not data.get("success"):
            raise WdttError(f"WDTT login failed: {data.get('msg') or f'HTTP {r.status_code}'}")
        self._logged_in = True

    @staticmethod
    def _error_msg(r: httpx.Response) -> str:
        try:
            return r.json().get("msg") or r.text[:200]
        except Exception:
            return r.text[:200] or "no message"

    async def _call(self, method: str, path: str, retry: bool = True, **kwargs: Any):
        if not self._logged_in:
            await self.login()

        headers = dict(kwargs.pop("headers", None) or {})
        headers["X-Requested-With"] = "XMLHttpRequest"
        if method.upper() not in ("GET", "HEAD", "OPTIONS"):
            csrf = self._client.cookies.get(_CSRF_COOKIE)
            if csrf:
                headers["X-CSRF-Token"] = csrf

        r = await self._client.request(method, f"{self.base_url}{path}", headers=headers, **kwargs)

        # Сессия истекла: без XHR-заголовка панель отвечала бы 302 на /login.
        if (r.status_code == 401 or r.status_code in (301, 302, 303, 307, 308)) and retry:
            log.info("WDTT-сессия истекла, перелогиниваемся (%s %s)", method, path)
            self._logged_in = False
            await self.login()
            return await self._call(method, path, retry=False, **kwargs)

        if r.status_code >= 400:
            raise WdttError(f"WDTT {method} {path} -> HTTP {r.status_code}: {self._error_msg(r)}")

        try:
            data = r.json()
        except ValueError:
            raise WdttError(f"WDTT {method} {path}: не JSON в ответе")
        if not data.get("success"):
            # Это ошибка самого запроса, а не сессии — перелогин не поможет.
            raise WdttError(f"WDTT {method} {path}: {data.get('msg')}")
        return data.get("obj")

    # ---------- пользователи VPN ----------

    async def add_user(self, *, comment: str, expires_at: int = 0, total_gb: int = 0,
                       max_devices: int = 1, vk_hash: str = "") -> str:
        """Создаёт пользователя и возвращает его пароль (он же — логин-ключ).

        Панель пересобирает запись, vk_hash передаём явно (пустая строка
        совпадает с дефолтом панели — плейсхолдер VK_HASH в ссылках).
        """
        payload = {
            "comment": comment,
            "expires_at": int(expires_at),
            "total_gb": int(total_gb),
            "max_devices": int(max_devices),
            "active": True,
            "vk_hash": vk_hash,
        }
        obj = await self._call("POST", "/panel/api/users/add", json=payload)
        password = (obj or {}).get("password") if isinstance(obj, dict) else None
        if not password:
            raise WdttError("WDTT users/add не вернул пароль")
        return password

    async def update_user(self, *, old_password: str, password: str | None = None,
                          expires_at: int = 0, total_gb: int = 0,
                          max_devices: int = 1, vk_hash: str = "") -> None:
        """Обновляет пользователя. password=None -> пароль не меняется.

        Панель пересобирает запись из запроса, поэтому все поля передаём явно
        (в т.ч. vk_hash: пропущенное поле панель обнулит).
        """
        payload = {
            "old_password": old_password,
            "password": password or "",
            "expires_at": int(expires_at),
            "total_gb": int(total_gb),
            "max_devices": int(max_devices),
            "active": True,
            "vk_hash": vk_hash,
        }
        await self._call("POST", "/panel/api/users/update", json=payload)

    async def create_vk_call(self, password: str) -> str:
        """Создаёт VK-вызов для пользователя и применяет его hash (поля Applied).

        POST /panel/api/vk/call/create {"password": ..., "apply": true}.
        Возвращает применённый vk_hash; требует, чтобы в панели были настроены
        VK-куки. Ошибка пробрасывается наружу (WdttError).
        """
        obj = await self._call("POST", "/panel/api/vk/call/create",
                               json={"password": password, "apply": True})
        hash_value = (obj or {}).get("vk_hash") if isinstance(obj, dict) else None
        if not hash_value:
            raise WdttError("WDTT vk/call/create не вернул hash")
        return str(hash_value)

    async def reset_traffic(self, password: str) -> None:
        await self._call("POST", "/panel/api/users/reset-traffic", json={"password": password})

    async def delete_user(self, password: str) -> None:
        await self._call("POST", "/panel/api/users/delete", json={"password": password})

    async def get_users(self) -> dict:
        obj = await self._call("GET", "/panel/api/users")
        return obj if isinstance(obj, dict) else {}

    async def get_inbound(self) -> dict:
        obj = await self._call("GET", "/panel/api/inbound")
        return obj if isinstance(obj, dict) else {}

    async def save_inbound(self, **fields) -> dict:
        """Сохраняет inbound (POST /panel/api/inbound/save) и перезапускает WDTT.

        Панель пересобирает конфиг из тела запроса, поэтому переданные поля
        должны быть полными: удобно сначала прочитать get_inbound() и
        overrride-ить нужные ключи.
        """
        obj = await self._call("POST", "/panel/api/inbound/save", json=fields)
        return obj if isinstance(obj, dict) else {}

    async def set_max_users(self, max_users: int) -> dict:
        """Удобный хелпер: меняет только лимит пользователей (max_users)."""
        cur = await self.get_inbound()
        payload = {
            "tag": cur.get("tag") or "wdtt-in",
            "remark": cur.get("remark") or "WDTT",
            "listen_host": cur.get("listen_host") or "0.0.0.0",
            "server_host": cur.get("server_host") or "",
            "dtls_port": int(cur.get("dtls_port") or 56000),
            "wg_port": int(cur.get("wg_port") or 56001),
            "client_port": int(cur.get("client_port") or 9000),
            "dns": cur.get("dns") or "1.1.1.1",
            "max_users": int(max_users),
        }
        return await self.save_inbound(**payload)

    # ---------- VK Creator (cookies / хеши) ----------

    async def set_vk_cookies(self, cookies: str) -> dict:
        """Загружает cookies VK-профиля в панель (POST /panel/api/vk/cookies).

        cookies — JSON-массив [{name,value}...] (cookies-vk.json) или строка
        вида "remixsid=...; ...". В панели хранится один активный профиль —
        бот ротирует профили, загружая нужный перед созданием вызова.
        """
        obj = await self._call("POST", "/panel/api/vk/cookies", json={"cookies_json": cookies})
        return obj if isinstance(obj, dict) else {}

    async def vk_status(self) -> dict:
        """GET /panel/api/vk/status: cookies_ok, cookies_present, sessions (активные звонки)."""
        obj = await self._call("GET", "/panel/api/vk/status")
        return obj if isinstance(obj, dict) else {}

    async def vk_calls(self) -> list[dict]:
        """Активные VK-звонки панели (сессии) из vk/status."""
        st = await self.vk_status()
        sessions = st.get("sessions")
        return sessions if isinstance(sessions, list) else []

    # ---------- настройки панели ----------

    async def set_panel_credentials(self, new_password: str, new_username: str | None = None) -> None:
        """Меняет логин/пароль панели (POST /panel/setting/updateUser, 3x-ui совместимый).

        После смены пароля текущая сессия протухает — клиент перелогинится
        с новыми данными при следующем _call (нужно обновить self.password).
        """
        payload = {
            "oldUsername": self.username,
            "oldPassword": self.password,
            "newUsername": new_username or self.username,
            "newPassword": new_password,
        }
        await self._call("POST", "/panel/setting/updateUser", json=payload)
        self.password = new_password
        if new_username:
            self.username = new_username
        self._logged_in = False  # сессия с новым паролем не валидна

    async def get_user_info(self, password: str) -> dict | None:
        """Запись пользователя из списка панели (total_gb, max_devices и т.д.)."""
        for u in (await self.get_users()).get("users") or []:
            if u.get("password_key") == password or u.get("password") == password:
                return u
        return None

    async def find_link(self, password: str) -> str | None:
        """Готовая ссылка wdtt:// пользователя с данным паролем.

        Если в payload ссылки нет hash (или стоит панельный плейсхолдер
        'VK_HASH'), вписываем vk_hash из записи пользователя.
        """
        u = await self.get_user_info(password)
        if not u:
            return None
        link = u.get("link")
        if link:
            link = self._ensure_payload_hash(str(link), str(u.get("vk_hash") or ""))
        return link

    @staticmethod
    def _ensure_payload_hash(link: str, vk_hash: str) -> str:
        """Прописывает vk_hash в универсальную wdtt:// (base64 JSON payload).

        Ссылку собирает панель; ключ hash попадает туда только при наличии
        VK-хэша у пользователя. Если ключа нет (или это плейсхолдер) и хэш
        известен — декодируем, вписываем и пересобираем ссылку. При ошибке
        декодирования возвращаем исходную ссылку без изменений.
        """
        if not link or not link.startswith("wdtt://"):
            return link
        hashes = WdttClient._clean_hash_pieces(vk_hash)
        if not hashes:
            return link
        import base64
        import json
        b64 = link[len("wdtt://"):]
        try:
            data = base64.b64decode(b64 + "=" * (-len(b64) % 4)).decode("utf-8")
            obj = json.loads(data)
            if not isinstance(obj, dict):
                return link
        except Exception:
            return link
        existing = WdttClient._clean_hash_pieces(str(obj.get("hash") or ""))
        if existing:
            return link
        obj["hash"] = ",".join(hashes)
        encoded = base64.b64encode(
            json.dumps(obj, separators=(",", ":")).encode("utf-8")
        ).decode("ascii")
        return "wdtt://" + encoded

    async def platform_links(self, password: str) -> dict[str, str]:
        """Ссылки под мобильные платформы из панели.

        Панель собирает их в buildAllSubscriptionLinks и отдаёт на HTML-странице
        подписки (GET sub_url?html=1) в скрытых textarea #subscription-links и
        #subscription-link-titles (ссылки и заголовки — построчно, пары "заголовок
        -> ссылка"). Используем: "iOS — VK Turn Proxy" и "Android — WDTT"
        (desktop/qWDTT/WDTT JSON не включаем; универсальную отдаёт find_link).

        Возвращает {"ios": ..., "android": ...}; платформы, для которых ссылки
        нет, пропускаются.
        """
        u = await self.get_user_info(password)
        if not u:
            return {}
        vk_hash = str(u.get("vk_hash") or "").strip()
        sub_url = str(u.get("sub_url") or "").strip()
        if not sub_url:
            return {}
        sep = "&" if "?" in sub_url else "?"
        r = await self._client.get(sub_url + sep + "html=1", headers={"Accept": "text/html"})
        if r.status_code >= 400:
            raise WdttError(f"WDTT subscription page -> HTTP {r.status_code}")
        page = r.text
        m_links = re.search(r'id="subscription-links"[^>]*>(.*?)</textarea>', page, re.S)
        m_titles = re.search(r'id="subscription-link-titles"[^>]*>(.*?)</textarea>', page, re.S)
        if not m_links or not m_titles:
            return {}
        links = [x.strip() for x in m_links.group(1).splitlines() if x.strip()]
        titles = [x.strip() for x in m_titles.group(1).splitlines() if x.strip()]
        result: dict[str, str] = {}
        for title, link in zip(titles, links):
            if not link:
                continue
            t = title.lower()
            if "ios" in t or "turn" in t:
                result.setdefault("ios",
                                  self._ensure_hash(link, vk_hash, hash_limit=1))
            elif "android" in t:
                result.setdefault("android", self._ensure_hash(link, vk_hash))
        return result

    @staticmethod
    def _clean_hash_pieces(value: str) -> list[str]:
        """Кусочки хэша из colon-ссылки: пустые и плейсхолдер 'VK_HASH' отбрасываются.

        Панель без VK-вызова вписывает в поле хэша литерал "VK_HASH"
        (pkg/vkhash.Placeholder) — считать его реальным хэшем нельзя.
        """
        out = []
        for piece in value.split(","):
            piece = piece.strip()
            if piece and piece.upper() != "VK_HASH":
                out.append(piece)
        return out

    @staticmethod
    def _ensure_hash(link: str, vk_hash: str, hash_limit: int = 0) -> str:
        """Прописывает vk_hash из панели в colon-ссылку, если он не указан.

        Формат: wdtt://host:dtls:wg:local:pass:hash[,hash…][#имя].
        Сегмент хэша заменяем значением из записи пользователя
        (GET /panel/api/users -> vk_hash), если он пуст или содержит
        плейсхолдер "VK_HASH". hash_limit=1 (iOS) берёт только первый хэш,
        как делает панель (vkhash.HashLimit).
        """
        if not link or not link.startswith("wdtt://"):
            return link
        hashes = WdttClient._clean_hash_pieces(vk_hash)
        if not hashes:
            return link  # в записи панели нет реального хэша — вписывать нечего
        if hash_limit and hash_limit > 0:
            hashes = hashes[:hash_limit]
        core, sep, name = link[len("wdtt://"):].partition("#")
        fields = core.split(":")
        if len(fields) < 6:
            return link
        if WdttClient._clean_hash_pieces(":".join(fields[5:])):
            return link  # реальный хэш уже прописан панелью
        fields = fields[:5] + [",".join(hashes)]
        out = "wdtt://" + ":".join(fields)
        return out + ("#" + name if sep else "")
