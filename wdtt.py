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

import logging
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
                       max_devices: int = 1) -> str:
        """Создаёт пользователя и возвращает его пароль (он же — логин-ключ)."""
        payload = {
            "comment": comment,
            "expires_at": int(expires_at),
            "total_gb": int(total_gb),
            "max_devices": int(max_devices),
            "active": True,
        }
        obj = await self._call("POST", "/panel/api/users/add", json=payload)
        password = (obj or {}).get("password") if isinstance(obj, dict) else None
        if not password:
            raise WdttError("WDTT users/add не вернул пароль")
        return password

    async def update_user(self, *, old_password: str, password: str | None = None,
                          expires_at: int = 0, total_gb: int = 0,
                          max_devices: int = 1) -> None:
        """Обновляет пользователя. password=None -> пароль не меняется.

        Панель пересобирает запись из запроса, поэтому все поля передаём явно.
        """
        payload = {
            "old_password": old_password,
            "password": password or "",
            "expires_at": int(expires_at),
            "total_gb": int(total_gb),
            "max_devices": int(max_devices),
            "active": True,
        }
        await self._call("POST", "/panel/api/users/update", json=payload)

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

    async def get_user_info(self, password: str) -> dict | None:
        """Запись пользователя из списка панели (total_gb, max_devices и т.д.)."""
        for u in (await self.get_users()).get("users") or []:
            if u.get("password_key") == password or u.get("password") == password:
                return u
        return None

    async def find_link(self, password: str) -> str | None:
        """Готовая ссылка wdtt:// пользователя с данным паролем.

        Ссылка из панели универсальна для всех клиентов (устройства
        привязываются автоматически до лимита max_devices).
        """
        u = await self.get_user_info(password)
        return u.get("link") if u else None
