import json
import os
import socket
import threading
import webbrowser
import urllib.request
import urllib.parse
import ipaddress
import subprocess
import ctypes
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
from datetime import datetime
import winreg

try:
    import theme as T
except ImportError:  # запуск из другой директории
    import sys as _sys
    _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import theme as T

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
SUB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "subscription.json")
SESSION_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "session.json")

AUTH_BASE_URL = "https://auth.ko-gateway.ru"

DEFAULT_CONFIG = {
    "app_name": "K.O. Gateway",
    "version": "1.0.0",
    "profiles": [
        {
            "id": "wdtt",
            "name": "WDTT VPN",
            "scheme": "wdtt",
            "default_server": "78.17.16.23",
            "default_ports": "56000:56001:56003:local:",
            "description": "WDTT — VPN-панель K.O. Gateway (VK Turn, DTLS/WG/RAW)"
        },
        {
            "id": "aivpn",
            "name": "AIVPN",
            "scheme": "aivpn",
            "default_server": "",
            "default_ports": "",
            "description": "AIVPN — замаскированный UDP-туннель (infosave2007/aivpn)"
        },
        {
            "id": "openflux",
            "name": "OpenFlux",
            "scheme": "openflux",
            "default_server": "",
            "default_ports": "",
            "transport": "yandex",
            "url": "",
            "secret": "",
            "inbound": "socks5",
            "mode": "client",
            "codec": "batched",
            "description": "OpenFlux — TCP/UDP туннель с pluggable transports (p1neappleXpress/OpenFlux)"
        }
    ],
    "default_profile": "wdtt",
    "auto_connect": False,
    "minimize_to_tray": True,
    "api_url": "http://127.0.0.1:8082",
    "api_token": "",
    "auth_token": "",
    "auth_user": {},
    "direct_routes": {
        "enabled": True,
        "url": "",
        "domains": [
            "yandex.ru", "yandex.com", "yandex.by", "yandex.kz", "yandex.uz",
            "vk.com", "vk.ru", "vk.me", "vkad.io",
            "mail.ru", "list.ru", "bk.ru", "inbox.ru",
            "ok.ru", "odnoklassniki.ru",
            "rutube.ru", "rutube.com",
            "wildberries.ru", "wb.ru",
            "ozon.ru", "ozon.com",
            "avito.ru", "avito.com",
            "apple.com", "itunes.apple.com", "music.apple.com", "apps.apple.com",
            "icloud.com", "mzstatic.com",
            "ko-gateway.ru", "auth.ko-gateway.ru", "app.ko-gateway.ru",
        ],
        "ips": [
            "95.163.0.0/16",
            "185.71.76.0/27",
            "185.165.12.0/22",
            "217.29.53.0/24",
            "94.100.176.0/20",
            "185.102.220.0/22",
            "17.0.0.0/8",
        ],
        "apps": [
            "chrome.exe", "msedge.exe", "firefox.exe", "opera.exe",
            "iTunes.exe", "Music.exe", "AppleDevices.exe",
            "Telegram.exe", "WhatsApp.exe",
        ],
        "last_updated": None,
    },
    "tunnel_routes": {
        "enabled": False,
        "domains": [],
        "ips": [],
        "apps": [],
    },
    "connection_failover": {
        "enabled": True,
        "auto_select": True,
        "order": ["aivpn", "wdtt"],
        "slow_threshold_ms": 3000,
        "measure_ping": True,
        "measure_speed": True,
        "check_sites": [
            "https://www.google.com",
            "https://www.youtube.com",
            "https://www.apple.com",
        ],
        "check_timeout_s": 5,
    },
    "speed_test": {
        "enabled": True,
        "threshold_ms": 3000,
        "timeout_s": 8,
        "sites": [
            "https://www.google.com",
            "https://www.youtube.com",
            "https://www.apple.com",
            "https://music.apple.com",
            "https://www.icloud.com",
        ],
        "results": {},
        "last_updated": None,
    },
}


def _merge_config(loaded: dict, defaults: dict) -> dict:
    """Сливает пользовательский конфиг с значениями по умолчанию.

    Секции- словари дополняются недостающими ключами; списки профилей
    дополняются профилями, которых нет в файле (по id), чтобы новые
    протоколы появлялись без ручной правки config.json.
    """
    out = dict(defaults)
    for key, val in (loaded or {}).items():
        if key == "profiles" and isinstance(val, list):
            merged = []
            by_id = {p.get("id"): p for p in val if isinstance(p, dict)}
            for dp in defaults.get("profiles", []):
                dp_id = dp.get("id")
                merged.append({**dp, **by_id.pop(dp_id, {})} if dp_id in by_id else dict(dp))
            for extra in val:
                if isinstance(extra, dict) and extra.get("id") in by_id:
                    merged.append(by_id.pop(extra["id"]))
            out["profiles"] = merged
        elif isinstance(val, dict) and isinstance(defaults.get(key), dict):
            out[key] = {**defaults[key], **val}
        else:
            out[key] = val
    return out


class Subscription:
    def __init__(self, path: str):
        self.path = path
        self.data = self._load()

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {
                "active": False,
                "plan_name": "—",
                "expires_at": "—",
                "days_left": "—",
                "traffic_used": "—",
                "traffic_limit": "—",
                "devices_online": 0,
                "devices_max": 1,
                "updated_at": None,
            }

    def save(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    def fetch_from_api(self, api_url: str, token: str | None = None) -> bool:
        if not api_url:
            return False
        url = api_url.rstrip("/") + "/api/v1/me"
        headers = {"User-Agent": "K.O. Gateway/1.0"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as r:
                d = json.loads(r.read().decode("utf-8", "replace"))
        except Exception:
            return False

        plan = d.get("plan") or {}
        traffic = d.get("traffic") or {}
        used = traffic.get("used_bytes", 0)
        limit = traffic.get("limit_bytes", 0)
        exp = d.get("expires_at") or 0
        now = int(datetime.now().timestamp())
        self.data = {
            "active": bool(d.get("active")),
            "plan_name": (plan.get("name") or "—") if d.get("active") else "—",
            "expires_at": datetime.fromtimestamp(exp).strftime("%d.%m.%Y") if exp else "—",
            "days_left": str((exp - now) // 86400) if exp and d.get("active") else "—",
            "traffic_used": f"{used / 1024**3:.2f} ГБ" if used else "—",
            "traffic_limit": f"{limit / 1024**3:.2f} ГБ" if limit else "безлимит",
            "devices_online": int(d.get("devices_online") or 0),
            "devices_max": int(d.get("max_devices") or plan.get("max_devices") or 1),
            "updated_at": datetime.now().strftime("%d.%m.%Y %H:%M"),
        }
        self.save()
        return True


class DirectRoutesManager:
    def __init__(self, config: dict):
        self.cfg = config.setdefault("direct_routes", {})
        self.tcfg = config.setdefault("tunnel_routes", {})
        for k, v in DEFAULT_CONFIG["direct_routes"].items():
            self.cfg.setdefault(k, v)
        for k, v in DEFAULT_CONFIG["tunnel_routes"].items():
            self.tcfg.setdefault(k, v)
        self.cfg_root = config

    def is_direct_enabled(self) -> bool:
        return bool(self.cfg.get("enabled"))

    def direct_add(self, kind: str, value: str):
        arr = self.cfg.get(kind, [])
        value = value.strip()
        if value and value not in arr:
            arr.append(value)

    def direct_remove(self, kind: str, value: str):
        arr = self.cfg.get(kind, [])
        if value in arr:
            arr.remove(value)

    def is_tunnel_enabled(self) -> bool:
        return bool(self.tcfg.get("enabled"))

    def tunnel_add(self, kind: str, value: str):
        arr = self.tcfg.get(kind, [])
        value = value.strip()
        if value and value not in arr:
            arr.append(value)

    def tunnel_remove(self, kind: str, value: str):
        arr = self.tcfg.get(kind, [])
        if value in arr:
            arr.remove(value)

    def save(self):
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(self.cfg_root, f, ensure_ascii=False, indent=2)

    def update_from_url(self, url: str | None = None) -> dict:
        url = url or self.cfg.get("url") or ""
        stats = {"domains": 0, "ips": 0, "apps": 0, "errors": 0}
        if not url:
            return stats
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                data = r.read().decode("utf-8", "replace")
            lines_l = [ln.strip() for ln in data.splitlines() if ln.strip() and not ln.startswith("#")]
            domains, ips, apps = [], [], []
            for ln in lines_l:
                if ln.startswith("app:"):
                    apps.append(ln[4:].strip())
                elif "/" in ln and any(c.isdigit() for c in ln.split("/")[0]):
                    try:
                        ipaddress.ip_network(ln, strict=False)
                        ips.append(ln)
                    except ValueError:
                        domains.append(ln)
                else:
                    domains.append(ln)
            self.cfg["domains"] = sorted(set(self.cfg.get("domains", []) + domains))
            self.cfg["ips"] = sorted(set(self.cfg.get("ips", []) + ips))
            self.cfg["apps"] = sorted(set(self.cfg.get("apps", []) + apps))
            self.cfg["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            stats = {"domains": len(domains), "ips": len(ips), "apps": len(apps)}
        except Exception as e:
            stats = {"domains": 0, "ips": 0, "apps": 0, "errors": 1, "error": str(e)}
        return stats

    def is_admin(self) -> bool:
        try:
            return ctypes.windll.shell32.IsUserAnAdmin()
        except Exception:
            return False

    def apply(self) -> tuple[bool, str]:
        bat_path = os.path.join(os.path.dirname(CONFIG_PATH), "direct_routes.bat")
        lines_bat = [
            "@echo off",
            "chcp 65001 >nul",
            "echo Применяем исключения из туннеля K.O. Gateway",
            "echo (запустите этот файл от имени администратора)",
        ]
        if self.cfg.get("enabled"):
            lines_bat.append("echo --- Прямое подключение (обход VPN) ---")
            for ip in self.cfg.get("ips", []):
                lines_bat.append(f"route ADD {ip} MASK 255.255.255.255 0.0.0.0 IF * METRIC 1")
            for dom in self.cfg.get("domains", []):
                if "*" not in dom:
                    lines_bat.append(f"echo 0.0.0.0 {dom} >> %WINDIR%/System32/drivers/etc/hosts")
        if self.tcfg.get("enabled"):
            lines_bat.append("echo --- Через туннель VPN ---")
            for ip in self.tcfg.get("ips", []):
                lines_bat.append(f"route ADD {ip} MASK 255.255.255.255 0.0.0.0 IF * METRIC 10")
        lines_bat.append("echo Готово. Проверьте маршруты: route print")
        lines_bat.append("pause")
        with open(bat_path, "w", encoding="cp1251") as f:
            f.write("\r\n".join(lines_bat))

        if self.is_admin():
            try:
                subprocess.run(["cmd", "/c", bat_path], check=False, shell=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return True, f"Применено. Скрипт: {bat_path}"
            except Exception as e:
                return False, f"Ошибка применения: {e}"
        return False, f"Требуются права администратора. Скрипт готов: {bat_path}"


class SpeedDetector:
    def __init__(self, config: dict, drm=None):
        self.cfg = config.setdefault("speed_test", {})
        self.cfg.setdefault("enabled", True)
        self.cfg.setdefault("threshold_ms", 3000)
        self.cfg.setdefault("timeout_s", 8)
        self.cfg.setdefault("sites", [
            "https://www.google.com",
            "https://www.youtube.com",
            "https://www.apple.com",
            "https://music.apple.com",
            "https://www.icloud.com",
        ])
        self.cfg.setdefault("results", {})
        self.cfg.setdefault("last_updated", None)
        self.cfg_root = config
        self.drm = drm

    def save(self):
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(self.cfg_root, f, ensure_ascii=False, indent=2)

    def check_site(self, url: str) -> dict:
        import time as _time
        t0 = _time.time()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "K.O. Gateway/1.0"})
            with urllib.request.urlopen(req, timeout=self.cfg.get("timeout_s", 8)) as r:
                r.read(1)
        except Exception:
            return {"ok": False, "ms": None, "error": "timeout_or_unreachable"}
        ms = int((_time.time() - t0) * 1000)
        return {"ok": True, "ms": ms}

    def check_all(self) -> dict:
        import time as _time
        results = {}
        for url in self.cfg.get("sites", []):
            results[url] = self.check_site(url)
        self.cfg["results"] = results
        self.cfg["last_updated"] = datetime.now().strftime("%d.%m.%Y %H:%M")
        self.save()
        return results

    def slow_sites(self) -> list:
        threshold = int(self.cfg.get("threshold_ms", 3000))
        out = []
        for url, r in self.cfg.get("results", {}).items():
            if not r.get("ok") or (r.get("ms") is not None and r["ms"] > threshold):
                out.append({"url": url, "ms": r.get("ms"), "ok": r.get("ok")})
        return out

    def promote_to_direct(self, url: str) -> None:
        host = urlparse(url).netloc or url
        self.drm.direct_add("domains", host)
        sites = self.cfg.get("sites", [])
        if url in sites:
            sites.remove(url)
            self.cfg["sites"] = sites
        res = self.cfg.get("results", {})
        if url in res:
            del res[url]
        self.save()


class LoginWindow:
    def __init__(self, parent: tk.Tk, config: dict, on_success):
        self.parent = parent
        self.config = config
        self.on_success = on_success
        self.auth_token = None
        self.auth_user = None

        self.win = tk.Toplevel(parent)
        self.win.title("K.O. Gateway — вход")
        self.win.geometry("480x560")
        self.win.resizable(False, False)
        self.win.transient(parent)
        self.win.grab_set()
        self.win.configure(bg=T.BG)
        T.set_dark_titlebar(self.win)
        T.setup_ttk_styles(self.win)

        header = tk.Frame(self.win, bg=T.BG, height=72)
        header.pack(fill="x")
        header.pack_propagate(False)
        mark = tk.Frame(header, bg=T.ACCENT, width=44, height=44)
        mark.pack(side="left", padx=(20, 12), pady=14)
        mark.pack_propagate(False)
        tk.Label(mark, text="K.O", bg=T.ACCENT, fg="#ffffff",
                 font=(T.FONT, 13, "bold")).pack(expand=True)
        head_txt = tk.Frame(header, bg=T.BG)
        head_txt.pack(side="left", pady=14)
        tk.Label(head_txt, text="K.O. Gateway", bg=T.BG, fg=T.TEXT,
                 font=(T.FONT, 17, "bold"), anchor="w").pack(anchor="w")
        tk.Label(head_txt, text="Вход в аккаунт", bg=T.BG, fg=T.MUTED,
                 font=(T.FONT, 9), anchor="w").pack(anchor="w")

        body = tk.Frame(self.win, bg=T.BG, padx=20, pady=16)
        body.pack(fill="both", expand=True)

        tk.Label(body, text="Вход в приложение", bg=T.BG, fg=T.TEXT,
                 font=(T.FONT, 14, "bold")).pack(anchor="w", pady=(0, 16))

        nb = ttk.Notebook(body)
        nb.pack(fill="both", expand=True)
        T.style_notebook(nb)

        # --- Вкладка Telegram ---
        tg_frame = tk.Frame(nb, highlightthickness=0, bd=0)
        nb.add(tg_frame, text="Telegram")

        tg_frame.configure(bg=T.CARD)
        tk.Label(tg_frame, text="Вход через Telegram бота", bg=T.CARD, fg=T.TEXT,
                 font=(T.FONT, 11, "bold")).pack(anchor="w", padx=16, pady=(14, 6))
        tk.Label(tg_frame, text="Откройте бота в браузере и привяжите аккаунт:",
                 bg=T.CARD, fg=T.MUTED, font=(T.FONT, 9)).pack(anchor="w", padx=16, pady=(0, 10))

        T.accent_button(tg_frame, text="Открыть бота @KO_Gateway_bot",
                        command=self._open_tg_bot, width=30, height=2).pack(
            anchor="w", padx=16, pady=(0, 12))

        tk.Label(tg_frame, text="Или введите код привязки из бота (/bind):",
                 bg=T.CARD, fg=T.MUTED, font=(T.FONT, 9)).pack(anchor="w", padx=16, pady=(0, 4))
        self.bind_code_var = tk.StringVar()
        T.dark_entry(tg_frame, textvariable=self.bind_code_var, width=30,
                     font=(T.FONT, 10)).pack(anchor="w", padx=16, pady=(0, 8))
        T.ghost_button(tg_frame, text="Подтвердить код", command=self._confirm_bind_code,
                       width=20, font=(T.FONT, 9)).pack(anchor="w", padx=16, pady=(0, 14))

        # --- Вкладка Почта ---
        mail_frame = tk.Frame(nb, bg=T.CARD, highlightthickness=0, bd=0)
        nb.add(mail_frame, text="Почта")

        tk.Label(mail_frame, text="Вход через электронную почту", bg=T.CARD, fg=T.TEXT,
                 font=(T.FONT, 11, "bold")).pack(anchor="w", padx=16, pady=(14, 6))
        tk.Label(mail_frame, text="Введите email и пароль, затем код из письма:",
                 bg=T.CARD, fg=T.MUTED, font=(T.FONT, 9)).pack(anchor="w", padx=16, pady=(0, 10))

        # Поля email и пароля
        tk.Label(mail_frame, text="Email", bg=T.CARD, fg=T.MUTED,
                 font=(T.FONT, 9)).pack(anchor="w", padx=16, pady=(6, 2))
        self.email_var = tk.StringVar()
        T.dark_entry(mail_frame, textvariable=self.email_var, width=30,
                     font=(T.FONT, 10)).pack(anchor="w", padx=16, pady=(0, 8))

        tk.Label(mail_frame, text="Пароль", bg=T.CARD, fg=T.MUTED,
                 font=(T.FONT, 9)).pack(anchor="w", padx=16, pady=(8, 2))
        self.password_var = tk.StringVar()
        T.dark_entry(mail_frame, textvariable=self.password_var, width=30,
                     font=(T.FONT, 10), show="*").pack(anchor="w", padx=16, pady=(0, 8))

        # Кнопка отправки кода
        T.ghost_button(mail_frame, text="Отправить код на email", command=self._send_email_code,
                       width=25, font=(T.FONT, 10, "bold")).pack(anchor="w", padx=16, pady=(10, 8))

        # Поле кода подтверждения (скрыто до отправки)
        self.code_frame = tk.Frame(mail_frame, bg=T.CARD)
        tk.Label(self.code_frame, text="Код подтверждения", bg=T.CARD, fg=T.MUTED,
                 font=(T.FONT, 9)).pack(anchor="w", padx=16, pady=(8, 2))
        self.code_var = tk.StringVar()
        T.dark_entry(self.code_frame, textvariable=self.code_var, width=15,
                     font=(T.FONT, 10)).pack(anchor="w", padx=16, pady=(0, 8))
        T.accent_button(self.code_frame, text="Подтвердить код", command=self._verify_email_code,
                        width=20, font=(T.FONT, 10, "bold")).pack(anchor="w", padx=16, pady=(0, 14))

        # --- Статус ---
        self.login_status_var = tk.StringVar(value="")
        tk.Label(body, textvariable=self.login_status_var, bg=T.BG, fg=T.ERR,
                 font=(T.FONT, 9), wraplength=400, justify="left").pack(anchor="w", pady=(10, 0))

        # --- Кнопка отмены ---
        T.ghost_button(body, text="Пропустить (без входа)", command=self._skip_login,
                       font=(T.FONT, 9)).pack(anchor="w", pady=(16, 0))

    def _open_tg_bot(self):
        webbrowser.open("https://t.me/KO_Gateway_bot")
        self.login_status_var.set("Откройте бота и пришлите команду /bind, чтобы получить код")

    def _confirm_bind_code(self):
        code = self.bind_code_var.get().strip()
        if not code:
            self.login_status_var.set("Введите код привязки")
            return
        self._do_bind_code(code)

    def _do_bind_code(self, code: str):
        api_url = self.config.get("api_url", "").rstrip("/")
        if not api_url:
            self.login_status_var.set("API URL не настроен в настройках")
            return
        try:
            url = api_url + "/api/auth/bind-telegram"
            data = json.dumps({"code": code}).encode("utf-8")
            req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as r:
                resp = json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:
            self.login_status_var.set(f"Ошибка: {e}")
            return
        if resp.get("ok"):
            self.auth_token = resp.get("token", "")
            self.auth_user = resp.get("user", {})
            self.config["auth_token"] = self.auth_token
            self.config["auth_user"] = self.auth_user
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(self.config, f, ensure_ascii=False, indent=2)
            self.win.destroy()
            self.on_success(self.auth_token, self.auth_user)
        else:
            self.login_status_var.set(resp.get("error", "Неверный код"))

    def _send_email_code(self):
        """Отправляет код подтверждения на email."""
        email = self.email_var.get().strip()
        password = self.password_var.get().strip()
        if not email or "@" not in email:
            self.login_status_var.set("Введите корректный email")
            return
        if not password:
            self.login_status_var.set("Введите пароль")
            return
        self._temp_email = email
        self._temp_password = password
        api_url = self.config.get("api_url", "").rstrip("/")
        if not api_url:
            self.login_status_var.set("API URL не настроен в настройках")
            return
        try:
            url = api_url + "/api/auth/email/send-code"
            data = json.dumps({"email": email}).encode("utf-8")
            req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as r:
                resp = json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:
            self.login_status_var.set(f"Ошибка отправки: {e}")
            return
        if resp.get("ok"):
            self.code_frame.pack(anchor="w", pady=(10, 0))
            self.login_status_var.set(f"Код отправлен на {email}. Проверьте почту.")
        else:
            self.login_status_var.set(resp.get("error", "Не удалось отправить код"))

    def _verify_email_code(self):
        """Проверяет код подтверждения и получает токен."""
        code = self.code_var.get().strip()
        if not code:
            self.login_status_var.set("Введите код из письма")
            return
        email = getattr(self, "_temp_email", "")
        if not email:
            self.login_status_var.set("Сессия истекла, отправьте код повторно")
            return
        api_url = self.config.get("api_url", "").rstrip("/")
        if not api_url:
            self.login_status_var.set("API URL не настроен в настройках")
            return
        try:
            url = api_url + "/api/auth/email/verify"
            data = json.dumps({"email": email, "code": code}).encode("utf-8")
            req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as r:
                resp = json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:
            self.login_status_var.set(f"Ошибка проверки: {e}")
            return
        if resp.get("ok"):
            self.auth_token = resp.get("token", "")
            self.auth_user = resp.get("user", {})
            self.config["auth_token"] = self.auth_token
            self.config["auth_user"] = self.auth_user
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(self.config, f, ensure_ascii=False, indent=2)
            self.win.destroy()
            self.on_success(self.auth_token, self.auth_user)
        else:
            self.login_status_var.set(resp.get("error", "Неверный код"))

    def _skip_login(self):
        self.win.destroy()
        self.on_success(None, None)


class GatewayApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("K.O. Gateway")
        self.root.geometry("720x860")
        self.root.minsize(680, 720)
        T.apply_root(self.root)
        T.setup_ttk_styles(self.root)

        self.config = self._load_config()
        self.profiles = self.config.get("profiles", DEFAULT_CONFIG["profiles"])
        self.current_profile = None
        self.connected = False
        self.connect_thread = None
        self.drm = DirectRoutesManager(self.config)
        self.speed = SpeedDetector(self.config, self.drm)
        self.sub = Subscription(SUB_PATH)
        self.current_view = "main"

        # --- Переменные настроек ---
        self.profile_var = tk.StringVar()
        self.server_var = tk.StringVar()
        self.link_var = tk.StringVar()
        self.desc_var = tk.StringVar(value="")
        self.exc_info_var = tk.StringVar(value="Исключения: не загружены")
        self.speed_info_var = tk.StringVar(value="Проверок ещё не было")
        self.ff_info_var = tk.StringVar(value="Авто-переключение: выкл.")
        self.ff_enabled_var = tk.BooleanVar(value=False)
        self.ff_threshold_var = tk.StringVar(value="3000")

        # --- Проверка сессии при запуске ---
        if not self.config.get("auth_token"):
            self.root.withdraw()
            LoginWindow(self.root, self.config, self._on_login_success)
        else:
            self._build_ui()
            self._select_profile(self.config.get("default_profile", self.profiles[0]["id"]))
            self._refresh_sub_card()
            self._save_config()

    def _on_login_success(self, token: str | None, user: dict | None):
        self.root.deiconify()
        self._build_ui()
        self._select_profile(self.config.get("default_profile", self.profiles[0]["id"]))
        self._refresh_sub_card()
        self._save_config()
        if token:
            # После входа проверяем, новый ли пользователь — предлагаем пробный период
            threading.Thread(target=self._check_trial_offer, args=(token,), daemon=True).start()
            # Загружаем ссылки подключения по аккаунту (автоматически, без ввода пользователем)
            threading.Thread(target=self._load_connections, args=(token,), daemon=True).start()

    def _load_connections(self, token: str):
        """Загружает ссылки подключения по аккаунту и заполняет профили."""
        api_url = self.config.get("api_url", "").rstrip("/")
        if not api_url:
            return
        try:
            req = urllib.request.Request(api_url + "/api/v1/connections",
                                         headers={"Authorization": "Bearer " + token})
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
        except Exception:
            return
        # Обновляем профили полученными ссылками
        for p in self.profiles:
            pid = p.get("id", "")
            if pid == "wdtt" and data.get("wdtt"):
                link = data["wdtt"]
                # извлекаем сервер и порты из wdtt://host:56000:56001:56003:local:
                p["default_server"] = self._parse_wdtt_host(link)
                p["default_ports"] = self._parse_wdtt_ports(link)
            elif pid == "aivpn" and data.get("aivpn"):
                link = data["aivpn"]
                p["default_server"] = self._parse_aivpn_host(link)
                p["default_ports"] = ""
            elif pid == "openflux" and data.get("openflux"):
                of = data["openflux"]
                p["transport"] = of.get("transport", "yandex")
                p["url"] = of.get("url", "")
                p["secret"] = of.get("secret", "")
                p["inbound"] = of.get("inbound", "socks5")
                p["codec"] = of.get("codec", "batched")
        # Перерисуем настройки, если уже построены
        if hasattr(self, "of_transport_var"):
            self.root.after(0, self._refresh_settings)

    def _parse_wdtt_host(self, link: str) -> str:
        try:
            return link.split("://")[1].split(":")[0]
        except Exception:
            return ""

    def _parse_wdtt_ports(self, link: str) -> str:
        try:
            parts = link.split("://")[1].split(":")
            # wdtt://host:56000:56001:56003:local:
            return ":".join(parts[1:5]) if len(parts) >= 5 else "56000:56001:56003:local:"
        except Exception:
            return "56000:56001:56003:local:"

    def _parse_aivpn_host(self, link: str) -> str:
        try:
            return link.split("://")[1].split(":")[0]
        except Exception:
            return ""

    def _check_trial_offer(self, token: str):
        """Проверяет, новый ли пользователь, и если да — предлагает пробный период."""
        api_url = self.config.get("api_url", "").rstrip("/")
        if not api_url:
            return
        try:
            req = urllib.request.Request(api_url + "/api/v1/me",
                                         headers={"Authorization": "Bearer " + token})
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
        except Exception:
            return
        if data.get("trial_used") or data.get("has_subscription"):
            return  # уже использовал пробник или имеет подписку — не предлагаем
        # Новый пользователь — показываем предложение активировать пробный период
        self.root.after(0, lambda: self._show_trial_dialog(token))

    def _show_trial_dialog(self, token: str):
        dlg = tk.Toplevel(self.root)
        dlg.title("Пробный период")
        dlg.geometry("440x340")
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.configure(bg=T.CARD)
        T.set_dark_titlebar(dlg)

        tk.Label(dlg, text="🎁 Пробный период 3 дня — бесплатно",
                 bg=T.ACCENT, fg="#ffffff",
                 font=(T.FONT, 13, "bold")).pack(fill="x", pady=(0, 1))
        body = tk.Frame(dlg, bg=T.CARD, padx=20, pady=18)
        body.pack(fill="both", expand=True)
        tk.Label(body, text="Новый пользователь? Активируйте пробный период:",
                 bg=T.CARD, fg=T.TEXT, font=(T.FONT, 10)).pack(pady=(0, 10))
        for item in ("• 3 дня доступа", "• 5 ГБ трафика", "• 1 устройство",
                     "• После окончания можно оформить платный тариф"):
            tk.Label(body, text=item, bg=T.CARD, fg=T.MUTED,
                     font=(T.FONT, 10)).pack(anchor="w", pady=1)

        btn_frame = tk.Frame(body, bg=T.CARD)
        btn_frame.pack(pady=(16, 0))

        def activate():
            dlg.destroy()
            self._activate_trial(token)

        T.accent_button(btn_frame, text="Активировать пробный период",
                        command=activate, width=24, height=1).pack(side="left", padx=6)
        T.ghost_button(btn_frame, text="Позже", command=dlg.destroy,
                       width=10, height=1).pack(side="left", padx=6)

    def _activate_trial(self, token: str):
        """Активирует пробный период через API."""
        api_url = self.config.get("api_url", "").rstrip("/")
        if not api_url:
            self._log("API URL не настроен")
            return
        try:
            data = json.dumps({}).encode("utf-8")
            req = urllib.request.Request(api_url + "/api/v1/claim-trial",
                                         data=data,
                                         headers={"Content-Type": "application/json",
                                                  "Authorization": "Bearer " + token})
            with urllib.request.urlopen(req, timeout=15) as r:
                resp = json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:
            err = str(e)
            self.root.after(0, lambda: self._log(f"Ошибка активации пробного периода: {err}"))
            return
        if resp.get("ok"):
            self._log(f"Пробный период активирован до {resp.get('expires_at', '—')}")
            self._refresh_sub_card()
        else:
            self._log(resp.get("error", "Не удалось активировать пробный период"))

    def _load_config(self):
        """Читает config.json и дополняет его значениями по умолчанию.

        Файл пользователя может быть старым (без профиля OpenFlux, без
        connection_failover и т.п.) — без слияния с DEFAULT_CONFIG новые
        профили и секции просто не появлялись бы в приложении.
        """
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                loaded = json.load(f)
        except Exception:
            loaded = {}
        return _merge_config(loaded, DEFAULT_CONFIG)

    def _save_config(self):
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(self.config, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self._log(f"Не удалось сохранить конфиг: {e}")

    def _build_ui(self):
        pad = {"padx": 18, "pady": 12}

        # ── шапка: логотип + название, как .topbar/.header на сайте ──
        header = tk.Frame(self.root, bg=T.BG, height=72)
        header.pack(fill="x")
        header.pack_propagate(False)
        mark = tk.Frame(header, bg=T.ACCENT, width=44, height=44)
        mark.pack(side="left", padx=(18, 12), pady=14)
        mark.pack_propagate(False)
        tk.Label(mark, text="K.O", bg=T.ACCENT, fg="#ffffff",
                 font=(T.FONT, 13, "bold")).pack(expand=True)
        head_txt = tk.Frame(header, bg=T.BG)
        head_txt.pack(side="left", pady=14)
        tk.Label(head_txt, text="K.O. Gateway", bg=T.BG, fg=T.TEXT,
                 font=(T.FONT, 17, "bold"), anchor="w").pack(anchor="w")
        tk.Label(head_txt, text="VPN-клиент экосистемы K.O. Gateway",
                 bg=T.BG, fg=T.MUTED, font=(T.FONT, 9), anchor="w").pack(anchor="w")
        self.header_status = tk.Label(header, text="", bg=T.BG, fg=T.MUTED,
                                      font=(T.FONT, 9))
        self.header_status.pack(side="right", padx=18)

        # ── нижняя навигация (пакуется до body, чтобы body не занял всё место) ──
        nav = tk.Frame(self.root, bg=T.NAV_BG, height=58,
                       highlightbackground=T.BORDER, highlightthickness=1)
        nav.pack(fill="x", side="bottom")
        nav.pack_propagate(False)
        self.nav_main_btn = tk.Button(
            nav, text="⌂  Главная", command=self.show_main,
            bg=T.ACCENT, fg="#ffffff", font=(T.FONT, 11, "bold"),
            relief="flat", bd=0, cursor="hand2", width=16, height=2,
            activebackground=T.ACCENT_HOVER)
        self.nav_main_btn.pack(side="left", padx=14, pady=9)
        self.nav_settings_btn = tk.Button(
            nav, text="⚙  Настройки", command=self.show_settings,
            bg=T.NAV_BG, fg=T.MUTED, font=(T.FONT, 11),
            relief="flat", bd=0, cursor="hand2", width=16, height=2,
            activebackground=T.CARD_HI, activeforeground=T.TEXT,
            highlightthickness=0)
        self.nav_settings_btn.pack(side="left", padx=(0, 14), pady=9)

        # Прокручиваемое тело: настройки не помещаются в окно
        self.scroll_canvas = tk.Canvas(self.root, bg=T.BG, highlightthickness=0, bd=0)
        self.scroll_canvas.pack(fill="both", expand=True)
        # ttk.Scrollbar (не tk) — умеет стилизоваться через Style под тёмную тему
        self.scrollbar = ttk.Scrollbar(self.root, orient="vertical",
                                       command=self.scroll_canvas.yview,
                                       style="Vertical.TScrollbar")
        self.scrollbar.pack(side="right", fill="y")
        self.scroll_canvas.configure(yscrollcommand=self.scrollbar.set)
        self.body = tk.Frame(self.scroll_canvas, bg=T.BG, padx=18, pady=12)
        self.body_window = self.scroll_canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.body.bind("<Configure>", self._on_body_configure)
        self.scroll_canvas.bind("<Configure>", self._on_canvas_configure)
        for widget in (self.scroll_canvas, self.body):
            widget.bind("<MouseWheel>", self._on_mouse_wheel)

        self.show_main()


    def show_main(self):
        self.current_view = "main"
        self.nav_main_btn.config(bg=T.ACCENT, fg="#ffffff",
                                 font=(T.FONT, 11, "bold"))
        self.nav_settings_btn.config(bg=T.NAV_BG, fg=T.MUTED,
                                     font=(T.FONT, 11))
        self._clear_body()
        self._build_main_view()

    def show_settings(self):
        self.current_view = "settings"
        self.nav_main_btn.config(bg=T.NAV_BG, fg=T.MUTED,
                                 font=(T.FONT, 11))
        self.nav_settings_btn.config(bg=T.ACCENT, fg="#ffffff",
                                     font=(T.FONT, 11, "bold"))
        self._clear_body()
        self._build_settings_view()

    def _on_body_configure(self, _event):
        self.scroll_canvas.configure(scrollregion=self.scroll_canvas.bbox("all"))

    def _on_canvas_configure(self, event):
        self.scroll_canvas.itemconfigure(self.body_window, width=event.width)

    def _on_mouse_wheel(self, event):
        self.scroll_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _clear_body(self):
        for w in self.body.winfo_children():
            w.destroy()
        self.body.configure(bg=T.BG)

    def _build_main_view(self):
        # ── профиль подключения ──
        prof_card = T.card(self.body)
        prof_card.pack(fill="x", pady=(0, 12))
        tk.Label(prof_card, text="ПРОФИЛЬ ПОДКЛЮЧЕНИЯ", bg=T.CARD, fg=T.MUTED,
                 font=(T.FONT, 9, "bold")).pack(anchor="w", padx=16, pady=(12, 6))
        prof_row = tk.Frame(prof_card, bg=T.CARD)
        prof_row.pack(fill="x", padx=16, pady=(0, 12))
        tk.Label(prof_row, text="Профиль", bg=T.CARD, fg=T.MUTED,
                 font=(T.FONT, 10)).pack(side="left")
        self.profile_combo = ttk.Combobox(
            prof_row, textvariable=self.profile_var, state="readonly",
            values=[p["name"] for p in self.profiles], width=24,
            font=(T.FONT, 10))
        self.profile_combo.pack(side="left", padx=(10, 8))
        self.profile_combo.bind("<<ComboboxSelected>>", self._on_profile_changed)
        T.style_combobox(self.profile_combo)
        tk.Label(prof_row, textvariable=self.desc_var, bg=T.CARD, fg=T.MUTED,
                 font=(T.FONT, 9), anchor="w").pack(side="left", fill="x", expand=True)

        # ── кнопка подключения ──
        btn_frame = tk.Frame(self.body, bg=T.BG)
        btn_frame.pack(fill="x", pady=(0, 12))
        self.connect_btn = T.accent_button(
            btn_frame, text="⚡  Подключиться", command=self.toggle_connection,
            font=(T.FONT, 14, "bold"), width=22, height=2)
        self.connect_btn.pack(side="left", padx=(0, 10))
        self.status_var = tk.StringVar(value="Не подключено")
        self.status_label = tk.Label(btn_frame, textvariable=self.status_var, fg=T.ERR,
                                     bg=T.BG, font=(T.FONT, 12, "bold"))
        self.status_label.pack(side="left", padx=(6, 0))

        # ── карточка подписки (hero, как на сайте) ──
        sub_card = tk.Frame(self.body, bg=T.ACCENT, highlightthickness=0, bd=0)
        sub_card.pack(fill="x", pady=(0, 12))
        hero_top = tk.Frame(sub_card, bg=T.ACCENT)
        hero_top.pack(fill="x", padx=16, pady=(14, 4))
        tk.Label(hero_top, text="Активная подписка", bg=T.ACCENT, fg="#ffffff",
                 font=(T.FONT, 15, "bold")).pack(side="left")
        self.sub_badge = tk.Label(hero_top, text="—", bg=T.ACCENT, fg="#ffffff",
                                  font=(T.FONT, 9, "bold"))
        self.sub_badge.pack(side="right")

        self.sub_rows = {}
        rows = [
            ("sub_plan", "Тариф:"),
            ("sub_expires", "Действует до:"),
            ("sub_days", "Осталось дней:"),
            ("sub_traffic", "Трафик:"),
            ("sub_devices", "Устройства онлайн:"),
            ("sub_devices_limit", "Лимит по тарифу:"),
        ]
        for key, label in rows:
            row = tk.Frame(sub_card, bg=T.ACCENT)
            row.pack(fill="x", padx=16, pady=1)
            tk.Label(row, text=label, bg=T.ACCENT, fg="#d9d9de",
                     font=(T.FONT, 10), width=20, anchor="w").pack(side="left")
            val = tk.Label(row, text="—", bg=T.ACCENT, fg="#ffffff",
                           font=(T.FONT, 10, "bold"), anchor="w")
            val.pack(side="left")
            self.sub_rows[key] = val

        sub_btns = tk.Frame(sub_card, bg=T.ACCENT)
        sub_btns.pack(anchor="e", padx=16, pady=(8, 12))
        T.ghost_button(sub_btns, text="🔄  Обновить данные",
                       command=self.refresh_subscription, bg=T.ACCENT,
                       fg="#ffffff", highlightbackground="#a01620",
                       highlightcolor="#a01620",
                       font=(T.FONT, 9)).pack(side="right")

        # ── оплата ──
        pay_card = T.card(self.body)
        pay_card.pack(fill="x", pady=(0, 12))
        tk.Label(pay_card, text="ОПЛАТА ПОДПИСКИ", bg=T.CARD, fg=T.MUTED,
                 font=(T.FONT, 9, "bold")).pack(anchor="w", padx=16, pady=(12, 6))
        pay_row = tk.Frame(pay_card, bg=T.CARD)
        pay_row.pack(fill="x", padx=16, pady=(0, 10))
        tk.Label(pay_row, text="Тариф", bg=T.CARD, fg=T.MUTED,
                 font=(T.FONT, 10)).pack(side="left")
        self.pay_plan_var = tk.StringVar()
        self.pay_plan_combo = ttk.Combobox(
            pay_row, textvariable=self.pay_plan_var, state="readonly",
            values=[], width=26, font=(T.FONT, 10))
        self.pay_plan_combo.pack(side="left", padx=(10, 8))
        self.pay_plan_combo.bind("<<ComboboxSelected>>", self._on_pay_plan_selected)
        T.style_combobox(self.pay_plan_combo)
        self.pay_plan_price_var = tk.StringVar(value="")
        tk.Label(pay_row, textvariable=self.pay_plan_price_var,
                 bg=T.CARD, fg=T.MUTED, font=(T.FONT, 10)).pack(side="left", padx=(2, 0))
        self.pay_btn = T.accent_button(
            pay_row, text="Оплатить через ЮKassa", command=self.open_payment,
            font=(T.FONT, 10, "bold"), width=22, height=1, state="disabled")
        self.pay_btn.pack(side="right")
        self.pay_status_var = tk.StringVar(value="")
        tk.Label(pay_card, textvariable=self.pay_status_var,
                 bg=T.CARD, fg=T.MUTED, font=(T.FONT, 9),
                 wraplength=620, justify="left").pack(anchor="w", padx=16, pady=(0, 12))

        # Загружаем тарифы в фоне
        threading.Thread(target=self._load_plans_for_payment, daemon=True).start()

        # ── журнал ──
        log_card = T.card(self.body)
        log_card.pack(fill="x", pady=(0, 12))
        tk.Label(log_card, text="ЖУРНАЛ", bg=T.CARD, fg=T.MUTED,
                 font=(T.FONT, 9, "bold")).pack(anchor="w", padx=16, pady=(12, 4))
        self.log = scrolledtext.ScrolledText(
            log_card, height=7, width=66, font=("Consolas", 9), state="disabled",
            bg=T.INPUT_BG, fg=T.TEXT, insertbackground=T.TEXT,
            relief="flat", bd=0, highlightthickness=1,
            highlightbackground=T.BORDER, highlightcolor=T.ACCENT)
        self.log.pack(fill="x", padx=16, pady=(0, 14))

        # ── исключения + внешние ссылки ──
        bottom = tk.Frame(self.body, bg=T.BG)
        bottom.pack(fill="x")
        self.exc_btn = T.ghost_button(
            bottom, text="🚫  Исключения из туннеля…", command=self.open_exceptions,
            width=26, height=1)
        self.exc_btn.pack(side="left")
        self.exc_info_var = tk.StringVar(value="Исключения: не загружены")
        tk.Label(bottom, textvariable=self.exc_info_var, bg=T.BG, fg=T.MUTED,
                 font=(T.FONT, 9)).pack(side="left", padx=10)

        links = tk.Frame(self.body, bg=T.BG)
        links.pack(fill="x", pady=(10, 0))
        T.ghost_button(links, text="Открыть сайт K.O. Gateway",
                       command=lambda: webbrowser.open("https://auth.ko-gateway.ru"),
                       width=24, height=1).pack(side="left")
        T.ghost_button(links, text="Открыть бота в Telegram",
                       command=lambda: webbrowser.open("https://t.me/KO_Gateway_bot"),
                       width=24, height=1).pack(side="right")

    def _select_profile(self, profile_id: str):
        for p in self.profiles:
            if p["id"] == profile_id:
                self.current_profile = p
                self.profile_var.set(p["name"])
                self.server_var.set(p.get("default_server", ""))
                self.link_var.set(p.get("default_ports", ""))
                self.desc_var.set(p.get("description", ""))
                # поля OpenFlux живут только на вкладке Настроек —
                # обновляем их, если эта вкладка уже построена
                if hasattr(self, "of_transport_var"):
                    self.of_transport_var.set(p.get("transport", "yandex"))
                    self.of_url_var.set(p.get("url", ""))
                    self.of_secret_var.set(p.get("secret", ""))
                    self.of_inbound_var.set(p.get("inbound", "socks5"))
                    self.of_codec_var.set(p.get("codec", "batched"))
                self.root.title(f"K.O. Gateway — {p['name']}")
                self._log(f"Профиль выбран: {p['name']}")
                break

    def _on_profile_changed(self, _event):
        name = self.profile_var.get()
        for p in self.profiles:
            if p["name"] == name:
                self._select_profile(p["id"])
                break

    def _log(self, text: str):
        def append():
            # Журнал живёт только на главном экране: при переключении
            # на «Настройки» он удалён, и обращение к нему падало бы.
            log = getattr(self, "log", None)
            if log is None or not log.winfo_exists():
                return
            try:
                log.config(state="normal")
                log.insert("end", text + "\n")
                log.see("end")
                log.config(state="disabled")
            except tk.TclError:
                pass
        self.root.after(0, append)

    def _refresh_sub_card(self):
        d = self.sub.data
        active = bool(d.get("active"))
        self.sub_rows["sub_plan"].config(text=d.get("plan_name", "—"))
        self.sub_badge.config(
            text="● АКТИВНА" if active else "○ НЕАКТИВНА",
            fg="#d7ffe4" if active else "#ffd7da")
        self.sub_rows["sub_expires"].config(text=d.get("expires_at", "—"))
        self.sub_rows["sub_days"].config(text=d.get("days_left", "—"))
        self.sub_rows["sub_traffic"].config(
            text=f"{d.get('traffic_used', '—')} / {d.get('traffic_limit', '—')}")
        self.sub_rows["sub_devices"].config(
            text=f"{d.get('devices_online', 0)} подключено")
        self.sub_rows["sub_devices_limit"].config(
            text=f"{d.get('devices_max', 1)} доступно")
        if hasattr(self, "header_status"):
            self.header_status.config(
                text=f"Обновлено: {d.get('updated_at')}" if d.get("updated_at") else "")

    def _api_token(self) -> str:
        """Токен для API: явный api_token, иначе токен входа (auth_token).

        После входа приложение хранит токен в auth_token; api_token в конфиге
        остаётся пустым, и запросы /api/v1/me и /api/v1/create-payment
        уходили без авторизации (401).
        """
        return self.config.get("api_token") or self.config.get("auth_token") or ""

    def refresh_subscription(self):
        self._log("Загрузка данных подписки…")
        api_url = self.config.get("api_url", "")
        token = self._api_token()

        def worker():
            ok = self.sub.fetch_from_api(api_url, token)
            if ok:
                self.root.after(0, self._refresh_sub_card)
                self.root.after(0, lambda: self._log("Данные подписки обновлены."))
            else:
                self.root.after(0, lambda: self._log("Не удалось загрузить подписку. Проверьте API URL и токен в конфиге."))

        threading.Thread(target=worker, daemon=True).start()

    def toggle_connection(self):
        if self.connected:
            self.disconnect()
        else:
            self.connect()

    def connect(self):
        if not self.current_profile:
            messagebox.showwarning("Внимание", "Выберите профиль подключения.")
            return
        # Данные подключения берутся из профиля (загружены автоматически по аккаунту)
        profile = self.current_profile or {}
        server = profile.get("default_server", "")
        link = profile.get("default_ports", "")
        scheme = profile.get("scheme", "wdtt")

        self.connected = True
        self.connect_btn.config(text="Отключиться", bg="#374151")
        self.status_var.set("Подключение…")
        self.status_label.config(fg="#FBBF24")
        self._log("Начало подключения…")

        self.connect_thread = threading.Thread(
            target=self._connect_worker, args=(server, link, scheme), daemon=True
        )
        self.connect_thread.start()

    def disconnect(self):
        self.connected = False
        if getattr(self, "openflux_proc", None):
            try:
                self.openflux_proc.terminate()
                self.openflux_proc.wait(timeout=3)
            except Exception:
                try:
                    self.openflux_proc.kill()
                except Exception:
                    pass
            self.openflux_proc = None
        self.connect_btn.config(text="Подключиться", bg="#E01E2B")
        self.status_var.set("Не подключено")
        self.status_label.config(fg="#E01E2B")
        self._log("Отключено.")

    def _connect_worker(self, server: str, link: str, scheme: str = None):
        ff_cfg = self.config.get("connection_failover", {})
        if not ff_cfg.get("enabled", True):
            scheme = scheme or self.current_profile.get("scheme", "wdtt")
            self._establish_connection(server, link, scheme)
            return

        order = ff_cfg.get("order", ["aivpn", "wdtt", "openflux"])
        threshold_ms = int(ff_cfg.get("slow_threshold_ms", 3000))
        check_sites = ff_cfg.get("check_sites", ["https://www.google.com", "https://www.youtube.com", "https://www.apple.com"])

        results = []
        for scheme in order:
            if not self.connected:
                break
            self.root.after(0, lambda s=scheme: self._log(f"Измеряю {s.upper()}…"))

            if scheme == "openflux":
                ping_ms = self._measure_openflux_latency()
                label = scheme.upper()
            else:
                ping_ms = self._measure_tcp_latency(server, scheme)
                label = scheme.upper()
            if ping_ms is None:
                self.root.after(0, lambda s=scheme: self._log(f"{s.upper()}: сервер недоступен (таймаут)"))
                continue

            http_ms = None
            slow_count = 0
            if ff_cfg.get("measure_speed", True):
                for site in check_sites:
                    res = self.speed.check_site(site)
                    if not res.get("ok"):
                        slow_count += 1
                    elif res.get("ms") is not None and res["ms"] > threshold_ms:
                        slow_count += 1
                    if res.get("ms") is not None and (http_ms is None or res["ms"] < http_ms):
                        http_ms = res["ms"]

            combined = ping_ms + (http_ms or 0)
            results.append({
                "scheme": scheme,
                "ping_ms": ping_ms,
                "http_ms": http_ms,
                "combined": combined,
                "slow_count": slow_count,
            })
            self.root.after(0, lambda s=scheme, p=ping_ms, h=http_ms: self._log(
                f"{s.upper()}: TCP ping={p} мс, HTTP={h if h else "'-'"} мс"))

        if not results:
            self.root.after(0, lambda: self.status_var.set("Не удалось подключиться"))
            self.root.after(0, lambda: self.status_label.config(fg="#EF4444"))
            self.connect_btn.config(text="Подключиться", bg="#E01E2B")
            self.connected = False
            return

        results.sort(key=lambda r: r["combined"])
        best = results[0]
        scheme = best["scheme"]

        self.root.after(0, lambda s=scheme, p=best["ping_ms"], h=best["http_ms"]: self._log(
            f"Выбран {s.upper()} (ping={p} мс, http={h if h else "'-'"} мс) — наиболее быстрый"))

        self._establish_connection(server, link, scheme)

    def _measure_tcp_latency(self, server: str, scheme: str):
        ports = [56000, 56001, 56003] if scheme == "wdtt" else [443, 8443]
        host = server.split(":")[0] if ":" in server else server
        best_ms = None
        for port in ports:
            try:
                import time as _time
                t0 = _time.time()
                with socket.create_connection((host, port), timeout=3.0):
                    ms = int((_time.time() - t0) * 1000)
                    if best_ms is None or ms < best_ms:
                        best_ms = ms
            except OSError:
                continue
        return best_ms

    def _measure_openflux_latency(self):
        """Измеряет HTTP-задержку к URL документа OpenFlux из профиля."""
        profile = self.current_profile or {}
        url = profile.get("url", "").strip()
        if not url:
            return None
        import time as _time
        import urllib.request
        try:
            t0 = _time.time()
            req = urllib.request.Request(url, headers={"User-Agent": "K.O. Gateway/1.0"})
            with urllib.request.urlopen(req, timeout=5.0) as r:
                r.read(1)
            return int((_time.time() - t0) * 1000)
        except Exception:
            return None

    def _establish_connection(self, server: str, link: str, scheme: str):
        if link:
            link_scheme = urlparse(link).scheme.lower()
            if link_scheme and link_scheme != scheme:
                self.root.after(0, lambda: self._log(f"Ссылка {link} не соответствует протоколу {scheme.upper()}"))
                self.root.after(0, lambda: self.status_var.set("Не удалось подключиться"))
                self.root.after(0, lambda: self.status_label.config(fg="#EF4444"))
                self.connect_btn.config(text="Подключиться", bg="#E01E2B")
                self.connected = False
                return
            conn_str = link
        elif server:
            if scheme == "wdtt":
                ports = link.strip() or "56000:56001:56003:local:"
                conn_str = f"wdtt://{server}:{ports}"
            elif scheme == "aivpn":
                conn_str = f"aivpn://{server}"
            elif scheme == "openflux":
                profile = self.current_profile or {}
                transport = profile.get("transport", "yandex")
                url = profile.get("url", "")
                secret = profile.get("secret", "")
                inbound = profile.get("inbound", "socks5")
                codec = profile.get("codec", "batched")
                if not url:
                    self.root.after(0, lambda: self._log("OpenFlux: укажите URL транспорта в настройках профиля"))
                    self.root.after(0, lambda: self.status_var.set("Не удалось подключиться"))
                    self.root.after(0, lambda: self.status_label.config(fg="#EF4444"))
                    self.connect_btn.config(text="Подключиться", bg="#E01E2B")
                    self.connected = False
                    return
                bin_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "openflux.exe")
                if not os.path.exists(bin_path):
                    self.root.after(0, lambda: self._log("OpenFlux: бинарник openflux.exe не найден в папке с приложением"))
                    self.root.after(0, lambda: self.status_var.set("Не удалось подключиться"))
                    self.root.after(0, lambda: self.status_label.config(fg="#EF4444"))
                    self.connect_btn.config(text="Подключиться", bg="#E01E2B")
                    self.connected = False
                    return
                args = [bin_path, "--role=client", "--transport=" + transport,
                        "--url=" + url, "--inbound=" + inbound,
                        "--socks5=:1080", "--codec=" + codec]
                if secret:
                    key_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "openflux.key")
                    with open(key_file, "w", encoding="utf-8") as kf:
                        kf.write(secret)
                    args.append("--encryption-key-file=" + key_file)
                try:
                    self.openflux_proc = subprocess.Popen(
                        args, cwd=os.path.dirname(os.path.abspath(__file__)),
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        creationflags=subprocess.CREATE_NO_WINDOW
                    )
                    self.root.after(0, lambda: self._log(f"OpenFlux запущен (PID={self.openflux_proc.pid})"))
                    # поток для чтения вывода
                    threading.Thread(target=self._stream_openflux_output, daemon=True).start()
                except Exception as e:
                    self.root.after(0, lambda: self._log(f"OpenFlux запуск ошибка: {e}"))
                    self.root.after(0, lambda: self.status_var.set("Не удалось подключиться"))
                    self.root.after(0, lambda: self.status_label.config(fg="#EF4444"))
                    self.connect_btn.config(text="Подключиться", bg="#E01E2B")
                    self.connected = False
                    return
                conn_str = f"openflux://{url}"
            else:
                return
        else:
            return

        self.root.after(0, lambda s=scheme, c=conn_str: self._log(f"Подключаюсь через {s.upper()}: {c}"))

        self.root.after(0, lambda: self.status_var.set("Подключено"))
        self.root.after(0, lambda: self.status_label.config(fg="#22C55E"))
        self.root.after(0, lambda: self._log(f"Готово. Трафик идёт через {scheme.upper()}."))

        while self.connected:
            threading.Event().wait(1.0)

        self.root.after(0, lambda: self.status_var.set("Отключено"))
        self.root.after(0, lambda: self.status_label.config(fg="#E01E2B"))

    def _stream_openflux_output(self):
        """Читает stdout/stderr процесса OpenFlux и пишет в лог."""
        try:
            for line in self.openflux_proc.stdout:
                text = line.decode("utf-8", "replace").rstrip()
                if text:
                    self.root.after(0, lambda t=text: self._log(f"[openflux] {t}"))
        except Exception:
            pass

    def _load_plans_for_payment(self):
        """Загружает тарифы для оплаты из API."""
        api_url = self.config.get("api_url", "").rstrip("/")
        if not api_url:
            self.root.after(0, lambda: self.pay_status_var.set("API URL не настроен"))
            return
        try:
            req = urllib.request.Request(api_url + "/api/v1/plans")
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:
            err = str(e)
            self.root.after(0, lambda: self.pay_status_var.set(f"Не удалось загрузить тарифы: {err}"))
            return
        plans = data.get("plans", [])
        self.pay_plans = plans
        values = []
        for p in plans:
            price = float(p.get("price", 0))
            days = int(p.get("days", 0))
            name = p.get("name", "—")
            values.append(f"{name} — {days} дн., {price} ₽")
        self.root.after(0, lambda: self.pay_plan_combo.configure(values=values))
        self.root.after(0, lambda: self.pay_status_var.set(f"Загружено тарифов: {len(plans)}"))

    def _on_pay_plan_selected(self, _event):
        idx = self.pay_plan_combo.current()
        if hasattr(self, "pay_plans") and 0 <= idx < len(self.pay_plans):
            p = self.pay_plans[idx]
            price = float(p.get("price", 0))
            days = int(p.get("days", 0))
            self.pay_plan_price_var.set(f"{price} ₽ · {days} дн.")
            self.pay_btn.configure(state="normal")
            self.pay_status_var.set("")

    def open_payment(self):
        """Создаёт платёж ЮKassa для выбранного тарифа и открывает ссылку оплаты."""
        idx = self.pay_plan_combo.current()
        if not hasattr(self, "pay_plans") or not (0 <= idx < len(self.pay_plans)):
            self.pay_status_var.set("Выберите тариф")
            return
        plan = self.pay_plans[idx]
        plan_id = str(plan.get("id", ""))
        api_url = self.config.get("api_url", "").rstrip("/")
        token = self._api_token()
        if not api_url:
            self.pay_status_var.set("API URL не настроен")
            return
        self.pay_btn.configure(state="disabled")
        self.pay_status_var.set("Создаётся платёж…")

        def worker():
            try:
                data = json.dumps({"plan_id": plan_id}).encode("utf-8")
                req = urllib.request.Request(
                    api_url + "/api/v1/create-payment",
                    data=data,
                    headers={"Content-Type": "application/json",
                             "Authorization": "Bearer " + token}
                )
                with urllib.request.urlopen(req, timeout=15) as r:
                    resp = json.loads(r.read().decode("utf-8", "replace"))
            except Exception as e:
                self.root.after(0, lambda: self.pay_status_var.set(f"Ошибка создания платежа: {e}"))
                self.root.after(0, lambda: self.pay_btn.configure(state="normal"))
                return
            url = resp.get("url", "")
            if url:
                self.root.after(0, lambda u=url: webbrowser.open(u))
                self.root.after(0, lambda: self.pay_status_var.set(
                    f"Открыта страница оплаты {plan.get('name', '')} ({resp.get('amount', 0)} ₽)"))
            else:
                self.root.after(0, lambda: self.pay_status_var.set(
                    resp.get("error", "Не удалось создать платёж")))
            self.root.after(0, lambda: self.pay_btn.configure(state="normal"))

        threading.Thread(target=worker, daemon=True).start()

    def open_exceptions(self):
        DirectRoutesWindow(self.root, self.drm, self.config, self._save_config,
                           self._refresh_exc_info, mode="direct")

    def open_tunnel_exceptions(self):
        DirectRoutesWindow(self.root, self.drm, self.config, self._save_config,
                           self._refresh_exc_info, mode="tunnel")

    def _refresh_exc_info(self):
        d = len(self.drm.cfg.get("domains", []))
        i = len(self.drm.cfg.get("ips", []))
        a = len(self.drm.cfg.get("apps", []))
        self.d_info_var.set(f"{d} доменов · {i} IP · {a} приложений")
        td = len(self.drm.tcfg.get("domains", []))
        ti = len(self.drm.tcfg.get("ips", []))
        ta = len(self.drm.tcfg.get("apps", []))
        self.t_info_var.set(f"{td} доменов · {ti} IP · {ta} приложений")
        self.refresh_speed_info()

    def refresh_speed_info(self):
        d = self.speed.cfg
        enabled = bool(d.get("enabled"))
        threshold = int(d.get("threshold_ms", 3000))
        last = d.get("last_updated") or "—"
        slow = self.speed.slow_sites()
        self.speed_info_var.set(
            f"Порог медленности: {threshold} мс · последняя проверка: {last} · медленных: {len(slow)}"
        )
        if hasattr(self, "speed_list"):
            self.speed_list.delete(0, "end")
            for item in slow:
                ms_txt = f"{item['ms']} мс" if item["ms"] is not None else "недоступен"
                self.speed_list.insert("end", f"{item['url']} — {ms_txt}")

        ff = self.config.get("connection_failover", {})
        en = "вкл." if ff.get("enabled", True) else "выкл."
        order = " → ".join(ff.get("order", ["aivpn", "wdtt", "openflux"])).upper()
        self.ff_order_var.set(order)
        self.ff_info_var.set(f"Авто-переключение: {en} · порядок: {order} · порог: {ff.get('slow_threshold_ms', 3000)} мс")

    def promote_selected_slow(self):
        sel = self.speed_list.curselection()
        if not sel:
            return
        text = self.speed_list.get(sel[0])
        url = text.split(" — ")[0]
        self.promote_slow_to_direct(url)


    def _build_settings_view(self):
        # ── заголовок ──
        tk.Label(self.body, text="Настройки подключения", bg=T.BG, fg=T.TEXT,
                 font=(T.FONT, 17, "bold"), anchor="w").pack(anchor="w", pady=(0, 4))
        tk.Label(self.body, text="Ссылки подключения загружаются автоматически по вашему аккаунту",
                 bg=T.BG, fg=T.MUTED, font=(T.FONT, 9), anchor="w").pack(anchor="w", pady=(0, 14))

        # --- профиль (ссылки загружаются автоматически по аккаунту, ввод не требуется) ---
        prof_card = T.card(self.body)
        prof_card.pack(fill="x", pady=(0, 12))
        tk.Label(prof_card, text="ПРОФИЛЬ", bg=T.CARD, fg=T.MUTED,
                 font=(T.FONT, 9, "bold")).pack(anchor="w", padx=16, pady=(12, 6))
        prof_frame = tk.Frame(prof_card, bg=T.CARD)
        prof_frame.pack(fill="x", padx=16, pady=(0, 12))
        tk.Label(prof_frame, text="Профиль", bg=T.CARD, fg=T.MUTED,
                 font=(T.FONT, 10)).pack(side="left")
        self.settings_profile_var = tk.StringVar()
        self.settings_profile_combo = ttk.Combobox(
            prof_frame, textvariable=self.settings_profile_var, state="readonly",
            values=[p["name"] for p in self.profiles], width=26, font=(T.FONT, 10)
        )
        self.settings_profile_combo.pack(side="left", padx=(10, 8))
        self.settings_profile_combo.bind("<<ComboboxSelected>>", self._on_settings_profile_changed)
        T.style_combobox(self.settings_profile_combo)

        self.settings_desc_var = tk.StringVar(value="")
        tk.Label(prof_card, textvariable=self.settings_desc_var, bg=T.CARD, fg=T.MUTED,
                 font=(T.FONT, 9), wraplength=620, justify="left",
                 anchor="w").pack(fill="x", padx=16, pady=(0, 12))

        # --- OpenFlux профиль ---
        of_card = T.card(self.body)
        of_card.pack(fill="x", pady=(0, 12))
        tk.Label(of_card, text="🌀 OPENFLUX", bg=T.CARD, fg=T.MUTED,
                 font=(T.FONT, 9, "bold")).pack(anchor="w", padx=16, pady=(12, 6))

        of_row1 = tk.Frame(of_card, bg=T.CARD)
        of_row1.pack(fill="x", padx=16, pady=(0, 6))
        tk.Label(of_row1, text="Транспорт", bg=T.CARD, fg=T.MUTED,
                 font=(T.FONT, 9)).pack(side="left")
        self.of_transport_var = tk.StringVar(value="yandex")
        of_transport_combo = ttk.Combobox(of_row1, textvariable=self.of_transport_var, state="readonly",
                                          values=["yandex", "vyandex", "mailru", "cupsonline", "avito", "direct"],
                                          width=14, font=(T.FONT, 9))
        of_transport_combo.pack(side="left", padx=(8, 16))
        T.style_combobox(of_transport_combo)

        tk.Label(of_row1, text="Inbound", bg=T.CARD, fg=T.MUTED,
                 font=(T.FONT, 9)).pack(side="left")
        self.of_inbound_var = tk.StringVar(value="socks5")
        of_inbound_combo = ttk.Combobox(of_row1, textvariable=self.of_inbound_var, state="readonly",
                                        values=["socks5", "tun"], width=10, font=(T.FONT, 9))
        of_inbound_combo.pack(side="left", padx=(8, 16))
        T.style_combobox(of_inbound_combo)

        tk.Label(of_row1, text="Кодек", bg=T.CARD, fg=T.MUTED,
                 font=(T.FONT, 9)).pack(side="left")
        self.of_codec_var = tk.StringVar(value="batched")
        of_codec_combo = ttk.Combobox(of_row1, textvariable=self.of_codec_var, state="readonly",
                                      values=["batched", "legacy"], width=10, font=(T.FONT, 9))
        of_codec_combo.pack(side="left", padx=(8, 0))
        T.style_combobox(of_codec_combo)

        of_row2 = tk.Frame(of_card, bg=T.CARD)
        of_row2.pack(fill="x", padx=16, pady=(4, 6))
        tk.Label(of_row2, text="URL документа", bg=T.CARD, fg=T.MUTED,
                 font=(T.FONT, 9)).pack(side="left")
        self.of_url_var = tk.StringVar(value="")
        T.dark_entry(of_row2, textvariable=self.of_url_var, width=48,
                     font=(T.FONT, 9)).pack(side="left", padx=(8, 6), fill="x", expand=True)

        of_row3 = tk.Frame(of_card, bg=T.CARD)
        of_row3.pack(fill="x", padx=16, pady=(4, 12))
        tk.Label(of_row3, text="Секрет шифрования", bg=T.CARD, fg=T.MUTED,
                 font=(T.FONT, 9)).pack(side="left")
        self.of_secret_var = tk.StringVar(value="")
        T.dark_entry(of_row3, textvariable=self.of_secret_var, width=32,
                     font=(T.FONT, 9), show="*").pack(side="left", padx=(8, 6))
        tk.Label(of_row3, text="AES-256-GCM, для сессий и --transports", bg=T.CARD, fg=T.MUTED,
                 font=(T.FONT, 8)).pack(side="left", padx=(2, 0))

        # --- API сайта ---
        api_card = T.card(self.body)
        api_card.pack(fill="x", pady=(0, 12))
        tk.Label(api_card, text="API САЙТА", bg=T.CARD, fg=T.MUTED,
                 font=(T.FONT, 9, "bold")).pack(anchor="w", padx=16, pady=(12, 6))
        api_frame = tk.Frame(api_card, bg=T.CARD)
        api_frame.pack(fill="x", padx=16, pady=(0, 12))
        tk.Label(api_frame, text="URL", bg=T.CARD, fg=T.MUTED,
                 font=(T.FONT, 10)).pack(side="left")
        self.api_url_var = tk.StringVar(value=self.config.get("api_url", ""))
        T.dark_entry(api_frame, textvariable=self.api_url_var, width=30,
                     font=(T.FONT, 10)).pack(side="left", padx=(8, 6))
        tk.Label(api_frame, text="Токен", bg=T.CARD, fg=T.MUTED,
                 font=(T.FONT, 10)).pack(side="left", padx=(8, 6))
        self.api_token_var = tk.StringVar(value=self.config.get("api_token", ""))
        T.dark_entry(api_frame, textvariable=self.api_token_var, width=24,
                     font=(T.FONT, 10), show="*").pack(side="left")

        # --- Прямое подключение (мимо VPN) ---
        d_card = T.card(self.body)
        d_card.pack(fill="x", pady=(0, 12))
        tk.Label(d_card, text="🚫 ПРЯМОЕ ПОДКЛЮЧЕНИЕ (МИМО VPN)", bg=T.CARD, fg=T.MUTED,
                 font=(T.FONT, 9, "bold")).pack(anchor="w", padx=16, pady=(12, 6))
        self.d_enabled_var = tk.BooleanVar(value=self.drm.is_direct_enabled())
        T.style_checkbox(tk.Checkbutton(
            d_card, text="Включить раздельное туннелирование (прямое подключение)",
            variable=self.d_enabled_var, command=self._toggle_direct,
            font=(T.FONT, 10, "bold"))).pack(anchor="w", padx=16)
        self.d_info_var = tk.StringVar(value="")
        tk.Label(d_card, textvariable=self.d_info_var, bg=T.CARD, fg=T.MUTED,
                 font=(T.FONT, 9)).pack(anchor="w", padx=16, pady=(2, 8))
        d_btn_frame = tk.Frame(d_card, bg=T.CARD)
        d_btn_frame.pack(anchor="e", padx=16, pady=(0, 12))
        T.ghost_button(d_btn_frame, text="Настроить список…", command=self.open_exceptions,
                       font=(T.FONT, 9)).pack(side="right")

        # --- Через туннель VPN ---
        t_card = T.card(self.body)
        t_card.pack(fill="x", pady=(0, 12))
        tk.Label(t_card, text="🔒 ЧЕРЕЗ ТУННЕЛЬ VPN (ПРИНУДИТЕЛЬНО)", bg=T.CARD, fg=T.MUTED,
                 font=(T.FONT, 9, "bold")).pack(anchor="w", padx=16, pady=(12, 6))
        self.t_enabled_var = tk.BooleanVar(value=self.drm.is_tunnel_enabled())
        T.style_checkbox(tk.Checkbutton(
            t_card, text="Принудительно пускать через туннель",
            variable=self.t_enabled_var, command=self._toggle_tunnel,
            font=(T.FONT, 10, "bold"))).pack(anchor="w", padx=16)
        self.t_info_var = tk.StringVar(value="")
        tk.Label(t_card, textvariable=self.t_info_var, bg=T.CARD, fg=T.MUTED,
                 font=(T.FONT, 9)).pack(anchor="w", padx=16, pady=(2, 8))
        t_btn_frame = tk.Frame(t_card, bg=T.CARD)
        t_btn_frame.pack(anchor="e", padx=16, pady=(0, 12))
        T.ghost_button(t_btn_frame, text="Настроить список…", command=self.open_tunnel_exceptions,
                       font=(T.FONT, 9)).pack(side="right")

        # --- Автообнаружение медленных сайтов ---
        speed_card = T.card(self.body)
        speed_card.pack(fill="x", pady=(0, 12))
        tk.Label(speed_card, text="📉 АВТООБНАРУЖЕНИЕ МЕДЛЕННЫХ САЙТОВ", bg=T.CARD, fg=T.MUTED,
                 font=(T.FONT, 9, "bold")).pack(anchor="w", padx=16, pady=(12, 6))

        speed_row1 = tk.Frame(speed_card, bg=T.CARD)
        speed_row1.pack(fill="x", padx=16, pady=(0, 8))
        tk.Label(speed_row1, text="Порог медленности (мс)", bg=T.CARD, fg=T.MUTED,
                 font=(T.FONT, 9)).pack(side="left")
        self.speed_threshold_var = tk.StringVar(value=str(self.speed.cfg.get("threshold_ms", 3000)))
        T.dark_entry(speed_row1, textvariable=self.speed_threshold_var, width=8,
                     font=(T.FONT, 9)).pack(side="left", padx=(8, 12))
        T.ghost_button(speed_row1, text="Проверить скорость сейчас", command=self.check_tunnel_speed,
                       font=(T.FONT, 9)).pack(side="left")
        self.speed_info_var = tk.StringVar(value="Проверок ещё не было")
        tk.Label(speed_row1, textvariable=self.speed_info_var, bg=T.CARD, fg=T.MUTED,
                 font=(T.FONT, 9)).pack(side="left", padx=(12, 0))

        speed_row2 = tk.Frame(speed_card, bg=T.CARD)
        speed_row2.pack(fill="x", padx=16, pady=(0, 12))
        tk.Label(speed_row2, text="Медленные сайты", bg=T.CARD, fg=T.TEXT,
                 font=(T.FONT, 9, "bold")).pack(side="left", anchor="w")
        self.speed_list = tk.Listbox(speed_row2, font=("Consolas", 9), height=4,
                                     bg=T.INPUT_BG, fg=T.TEXT, selectbackground=T.ACCENT,
                                     selectforeground="#ffffff", relief="flat", bd=0,
                                     highlightthickness=1, highlightbackground=T.BORDER,
                                     highlightcolor=T.ACCENT)
        self.speed_list.pack(side="left", fill="x", expand=True, padx=(8, 6), pady=(2, 2))
        T.ghost_button(speed_row2, text="Перенести в прямое подключение",
                       command=self.promote_selected_slow, font=(T.FONT, 9)).pack(side="left", padx=(0, 6))

        # --- Авто-переключение протоколов ---
        ff_card = T.card(self.body)
        ff_card.pack(fill="x", pady=(0, 12))
        tk.Label(ff_card, text="🔄 АВТО-ПЕРЕКЛЮЧЕНИЕ ПРИ МЕДЛЕННОМ СОЕДИНЕНИИ",
                 bg=T.CARD, fg=T.MUTED, font=(T.FONT, 9, "bold")).pack(anchor="w", padx=16, pady=(12, 6))

        ff_row1 = tk.Frame(ff_card, bg=T.CARD)
        ff_row1.pack(fill="x", padx=16, pady=(0, 6))
        self.ff_enabled_var = tk.BooleanVar(value=self.config.get("connection_failover", {}).get("enabled", True))
        T.style_checkbox(tk.Checkbutton(
            ff_row1, text="Включить авто-переключение (AIVPN → WDTT → OpenFlux)",
            variable=self.ff_enabled_var, command=self._toggle_ff_enabled,
            font=(T.FONT, 10, "bold"))).pack(anchor="w")
        self.ff_info_var = tk.StringVar(value="")
        tk.Label(ff_row1, textvariable=self.ff_info_var, bg=T.CARD, fg=T.MUTED,
                 font=(T.FONT, 9)).pack(side="left", padx=(12, 0))

        ff_row2 = tk.Frame(ff_card, bg=T.CARD)
        ff_row2.pack(fill="x", padx=16, pady=(0, 12))
        tk.Label(ff_row2, text="Порог медленности (мс)", bg=T.CARD, fg=T.MUTED,
                 font=(T.FONT, 9)).pack(side="left")
        self.ff_threshold_var = tk.StringVar(value=str(self.config.get("connection_failover", {}).get("slow_threshold_ms", 3000)))
        T.dark_entry(ff_row2, textvariable=self.ff_threshold_var, width=8,
                     font=(T.FONT, 9)).pack(side="left", padx=(8, 12))
        self.ff_order_var = tk.StringVar(value="")
        self.ff_order_label = tk.Label(ff_row2, textvariable=self.ff_order_var,
                                       bg=T.CARD, fg=T.MUTED, font=(T.FONT, 9))
        self.ff_order_label.pack(side="left", padx=(12, 0))

        self._refresh_settings()
    def _refresh_settings(self):
        if self.current_profile:
            self.settings_profile_var.set(self.current_profile["name"])
            self.settings_desc_var.set(self.current_profile.get("description", ""))
            self.of_transport_var.set(self.current_profile.get("transport", "yandex"))
            self.of_url_var.set(self.current_profile.get("url", ""))
            self.of_secret_var.set(self.current_profile.get("secret", ""))
            self.of_inbound_var.set(self.current_profile.get("inbound", "socks5"))
            self.of_codec_var.set(self.current_profile.get("codec", "batched"))
        d = len(self.drm.cfg.get("domains", []))
        i = len(self.drm.cfg.get("ips", []))
        a = len(self.drm.cfg.get("apps", []))
        self.d_info_var.set(f"{d} доменов · {i} IP · {a} приложений")
        td = len(self.drm.tcfg.get("domains", []))
        ti = len(self.drm.tcfg.get("ips", []))
        ta = len(self.drm.tcfg.get("apps", []))
        self.t_info_var.set(f"{td} доменов · {ti} IP · {ta} приложений")
        self.refresh_speed_info()
        ff = self.config.get("connection_failover", {})
        en = "вкл." if ff.get("enabled", True) else "выкл."
        order = " → ".join(ff.get("order", ["aivpn", "wdtt"])).upper()
        self.ff_info_var.set(f"Авто-переключение: {en} · порядок: {order} · порог: {ff.get('slow_threshold_ms', 3000)} мс")

    def _on_settings_profile_changed(self, _event):
        name = self.settings_profile_var.get()
        for p in self.profiles:
            if p["name"] == name:
                self._select_profile(p["id"])
                break
        self._save_openflux_fields()

    def _save_openflux_fields(self):
        if not self.current_profile:
            return
        p = self.current_profile
        p["transport"] = self.of_transport_var.get()
        p["url"] = self.of_url_var.get()
        p["secret"] = self.of_secret_var.get()
        p["inbound"] = self.of_inbound_var.get()
        p["codec"] = self.of_codec_var.get()
        self._save_config()

    def _toggle_direct(self):
        self.drm.cfg["enabled"] = bool(self.d_enabled_var.get())
        self._save_config()
        self._refresh_exc_info()

    def _toggle_tunnel(self):
        self.drm.tcfg["enabled"] = bool(self.t_enabled_var.get())
        self._save_config()
        self._refresh_exc_info()

    def _toggle_ff_enabled(self):
        """Включает/выключает авто-переключение протоколов."""
        ff = self.config.setdefault("connection_failover", {})
        ff["enabled"] = bool(self.ff_enabled_var.get())
        self._save_config()
        self.refresh_speed_info()

    def check_tunnel_speed(self):
        self._log("Проверка скорости подключения к сайтам через туннель…")

        def worker():
            results = self.speed.check_all()
            slow = self.speed.slow_sites()
            self.root.after(0, self.refresh_speed_info)
            if slow:
                self.root.after(0, lambda: self._log(
                    f"Проверка завершена: {len(slow)} медленных/недоступных сайтов."))
            else:
                self.root.after(0, lambda: self._log("Проверка завершена: все сайты отвечают быстро."))

        threading.Thread(target=worker, daemon=True).start()

class DirectRoutesWindow:
    def __init__(self, parent: tk.Tk, drm: DirectRoutesManager, config: dict,
                 save_config, on_changed, mode: str = "direct"):
        self.drm = drm
        self.config = config
        self.save_config = save_config
        self.on_changed = on_changed
        self.mode = mode
        self.cfg = self.drm.cfg if mode == "direct" else self.drm.tcfg
        self.root_app = parent.winfo_toplevel()

        if mode == "direct":
            self.title_txt = "Исключения из туннеля — K.O. Gateway"
        else:
            self.title_txt = "Через туннель VPN — K.O. Gateway"

        self.win = tk.Toplevel(parent)
        self.win.title(self.title_txt)
        self.win.geometry("720x540")
        self.win.resizable(True, True)
        self.win.transient(parent)
        self.win.grab_set()

        pad = {"padx": 12, "pady": 8}

        top = tk.Frame(self.win)
        top.pack(fill="x", **pad)
        self.enabled_var = tk.BooleanVar(value=self.drm.is_direct_enabled() if mode == "direct"
                                         else self.drm.is_tunnel_enabled())
        if mode == "direct":
            label_txt = "Включить раздельное туннелирование (прямое подключение)"
        else:
            label_txt = "Включить принудительный туннель VPN"
        tk.Checkbutton(top, text=label_txt,
                       variable=self.enabled_var, command=self._toggle_enabled,
                       font=("Segoe UI", 10, "bold")).pack(side="left")
        self.exc_info_var = tk.StringVar(value="")
        tk.Label(top, textvariable=self.exc_info_var, fg="#6B7280", font=("Segoe UI", 9)).pack(side="right")

        nb = ttk.Notebook(self.win)
        nb.pack(fill="both", expand=True, **pad)
        self.nb = nb

        self.dom_frame = tk.Frame(nb)
        nb.add(self.dom_frame, text="Домены")
        self._build_list_tab(self.dom_frame, "domains", "Домен (без http://)",
                             "Пример: yandex.ru, vk.com")

        self.ip_frame = tk.Frame(nb)
        nb.add(self.ip_frame, text="IP / сети")
        self._build_list_tab(self.ip_frame, "ips", "IP или сеть (CIDR)",
                             "Пример: 95.163.0.0/16")

        self.app_frame = tk.Frame(nb)
        nb.add(self.app_frame, text="Приложения")
        self._build_list_tab(self.app_frame, "apps", "Имя процесса",
                             "Пример: iTunes.exe, chrome.exe")

        nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)

    def _on_tab_changed(self, _event):
        """Пересканировать приложения при открытии вкладки Приложения."""
        try:
            selected = self.nb.index(self.nb.select())
            if selected == 2:  # вкладка "Приложения" — третья
                self._refresh_installed_apps()
        except Exception:
            pass

        up_frame = tk.Frame(self.win)
        up_frame.pack(fill="x", **pad)
        if mode == "direct":
            tk.Label(up_frame, text="Автообновление из интернета:", font=("Segoe UI", 10, "bold")).pack(side="left")
            self.url_var = tk.StringVar(value=self.drm.cfg.get("url", ""))
            url_entry = tk.Entry(up_frame, textvariable=self.url_var, width=42, font=("Segoe UI", 9))
            url_entry.pack(side="left", padx=(6, 6))
            tk.Button(up_frame, text="Обновить списки", command=self._update_lists,
                      bg="#2F343E", fg="#FFFFFF", relief="flat", cursor="hand2").pack(side="left")
            self.update_status_var = tk.StringVar(value="")
            tk.Label(up_frame, textvariable=self.update_status_var, fg="#6B7280", font=("Segoe UI", 9)).pack(side="left", padx=(10, 0))

        apply_frame = tk.Frame(self.win)
        apply_frame.pack(fill="x", pady=(4, 10))
        tk.Label(apply_frame, text="Применение:", font=("Segoe UI", 10, "bold")).pack(side="left")
        tk.Button(apply_frame, text="Применить к системе", command=self._apply,
                  bg="#E01E2B", fg="#FFFFFF", relief="flat", cursor="hand2",
                  font=("Segoe UI", 10, "bold"), width=20).pack(side="left", padx=8)
        tk.Label(apply_frame, text="(требуются права администратора)", fg="#9CA3AF", font=("Segoe UI", 9)).pack(side="left")

        self._refresh_lists()
        self._update_info()

    def _build_list_tab(self, frame, kind: str, placeholder: str, hint: str):
        top = tk.Frame(frame)
        top.pack(fill="x", pady=(4, 2))
        entry = tk.Entry(top, font=("Segoe UI", 10))
        entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        entry.insert(0, placeholder)
        entry.configure(fg="#9CA3AF")

        def on_focus_in(_e):
            if entry.get() == placeholder:
                entry.delete(0, "end")
                entry.configure(fg="#000000")

        def on_focus_out(_e):
            if not entry.get():
                entry.insert(0, placeholder)
                entry.configure(fg="#9CA3AF")

        entry.bind("<FocusIn>", on_focus_in)
        entry.bind("<FocusOut>", on_focus_out)
        entry.bind("<Return>", lambda _e: self._add_entry(kind, entry))

        tk.Button(top, text="+", command=lambda: self._add_entry(kind, entry),
                  bg="#22C55E", fg="#FFFFFF", relief="flat", width=3).pack(side="left")

        # Для вкладки "Приложения" — чекбоксы установленных программ
        if kind == "apps":
            apps_top = tk.Frame(frame)
            apps_top.pack(fill="x", pady=(6, 2))
            tk.Label(apps_top, text="Установленные программы:", font=("Segoe UI", 9, "bold"), fg="#6B7280").pack(side="left")
            tk.Button(apps_top, text="Обновить список", command=self._refresh_installed_apps,
                      bg="#2F343E", fg="#FFFFFF", relief="flat", cursor="hand2",
                      font=("Segoe UI", 8)).pack(side="right")
            self.apps_installed_var = tk.StringVar(value="")
            tk.Label(apps_top, textvariable=self.apps_installed_var, fg="#9CA3AF", font=("Segoe UI", 8)).pack(side="right", padx=(0, 8))

            # Скроллируемая область с чекбоксами
            apps_canvas = tk.Canvas(frame, height=200, highlightthickness=0)
            apps_scroll = tk.Scrollbar(frame, orient="vertical", command=apps_canvas.yview)
            self.apps_inner = tk.Frame(apps_canvas)
            self.apps_inner.bind(
                "<Configure>",
                lambda e: apps_canvas.configure(scrollregion=apps_canvas.bbox("all"))
            )
            apps_canvas.create_window((0, 0), window=self.apps_inner, anchor="nw")
            apps_canvas.configure(yscrollcommand=apps_scroll.set)
            apps_canvas.pack(fill="both", expand=True, pady=(2, 0))
            apps_scroll.pack(side="right", fill="y")
            self.apps_inner.bind("<Enter>", lambda e: apps_canvas.bind_all("<MouseWheel>",
                lambda e: apps_canvas.yview_scroll(int(-1*(e.delta/120)), "units")))
            self.apps_inner.bind("<Leave>", lambda e: apps_canvas.unbind_all("<MouseWheel>"))
            self.apps_vars: dict[str, tk.BooleanVar] = {}
        else:
            listbox = tk.Listbox(frame, font=("Consolas", 10), height=12)
            listbox.pack(fill="both", expand=True, pady=(4, 2))
            listbox.bind("<Delete>", lambda _e: self._delete_entry(kind, listbox))
            listbox.bind("<BackSpace>", lambda _e: self._delete_entry(kind, listbox))

            btns = tk.Frame(frame)
            btns.pack(fill="x", pady=(2, 4))
            tk.Button(btns, text="Удалить выбранное", command=lambda: self._delete_entry(kind, listbox),
                      bg="#EF4444", fg="#FFFFFF", relief="flat").pack(side="left")

        setattr(self, f"{kind}_entry", entry)
        if kind != "apps":
            setattr(self, f"{kind}_list", listbox)
        tk.Label(frame, text=hint, fg="#6B7280", font=("Segoe UI", 8)).pack(anchor="w")

    def _refresh_lists(self):
        for kind in ("domains", "ips"):
            listbox = getattr(self, f"{kind}_list")
            listbox.delete(0, "end")
            for item in self.cfg.get(kind, []):
                listbox.insert("end", item)
        # Приложения — чекбоксы, не listbox
        if self.mode == "direct":
            self._refresh_installed_apps()

    def _get_installed_apps(self) -> list[str]:
        """Сканирует реестр Windows и возвращает список установленных программ."""
        apps = set()
        # Пути реестра для установленных приложений
        reg_paths = [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        ]
        for hkey, path in reg_paths:
            try:
                with winreg.OpenKey(hkey, path) as key:
                    for i in range(winreg.QueryInfoKey(key)[0]):
                        try:
                            subkey_name = winreg.EnumKey(key, i)
                            with winreg.OpenKey(key, subkey_name) as subkey:
                                try:
                                    name = winreg.QueryValueEx(subkey, "DisplayName")[0]
                                except FileNotFoundError:
                                    continue
                                if name and len(name) > 1:
                                    apps.add(name)
                        except Exception:
                            continue
            except Exception:
                continue
        return sorted(apps)

    def _refresh_installed_apps(self):
        """Перерисовывает чекбоксы установленных приложений."""
        if not hasattr(self, "apps_inner"):
            return
        # Очищаем старые чекбоксы
        for widget in self.apps_inner.winfo_children():
            widget.destroy()
        self.apps_vars.clear()

        current_apps = set(self.cfg.get("apps", []))

        def do_scan():
            try:
                apps = self._get_installed_apps()
            except Exception:
                apps = []
            self.root_app.after(0, lambda: self._draw_app_checkboxes(apps, current_apps))

        threading.Thread(target=do_scan, daemon=True).start()
        self.apps_installed_var.set("Сканирование…")

    def _draw_app_checkboxes(self, apps: list[str], current_apps: set[str]):
        """Отрисовывает чекбоксы для найденных приложений."""
        for widget in self.apps_inner.winfo_children():
            widget.destroy()
        self.apps_vars.clear()

        # Поиск приложений
        search_frame = tk.Frame(self.apps_inner)
        search_frame.pack(fill="x", pady=(0, 6))
        tk.Label(search_frame, text="Поиск:", font=("Segoe UI", 8), fg="#6B7280").pack(side="left", padx=(0, 4))
        search_var = tk.StringVar()

        def filter_apps(_e=None):
            term = search_var.get().lower()
            for app, (var, cb) in self.apps_vars.items():
                if term in app.lower():
                    cb.pack(anchor="w", padx=4, pady=1)
                else:
                    cb.pack_forget()

        search_entry = tk.Entry(search_frame, textvariable=search_var, width=30, font=("Segoe UI", 9))
        search_entry.pack(side="left", padx=(0, 6))
        search_entry.bind("<KeyRelease>", filter_apps)

        count = 0
        for app in apps:
            var = tk.BooleanVar(value=app in current_apps)
            cb = tk.Checkbutton(self.apps_inner, text=app, variable=var,
                                font=("Segoe UI", 9), anchor="w",
                                command=lambda a=app, v=var: self._toggle_app(a, v))
            cb.pack(anchor="w", padx=4, pady=1)
            self.apps_vars[app] = (var, cb)
            count += 1

        self.apps_installed_var.set(f"Найдено приложений: {count}")

    def _toggle_app(self, app: str, var: tk.BooleanVar):
        """Добавляет/удаляет приложение из списка прямого подключения."""
        if self.mode != "direct":
            return
        if var.get():
            if app not in self.cfg.get("apps", []):
                self.drm.direct_add("apps", app)
        else:
            self.drm.direct_remove("apps", app)
        self.save_config()
        self._update_info()

    def _update_info(self):
        d = len(self.cfg.get("domains", []))
        i = len(self.cfg.get("ips", []))
        a = len(self.cfg.get("apps", []))
        self.exc_info_var.set(f"{d} доменов · {i} IP · {a} приложений")
        if self.on_changed:
            self.on_changed()

    def _toggle_enabled(self):
        if self.mode == "direct":
            self.drm.cfg["enabled"] = bool(self.enabled_var.get())
        else:
            self.drm.tcfg["enabled"] = bool(self.enabled_var.get())
        self.save_config()
        self._update_info()

    def _add_entry(self, kind: str, entry: tk.Entry):
        val = entry.get().strip()
        placeholder = {"domains": "Домен (без http://)", "ips": "IP или сеть (CIDR)", "apps": "Имя процесса"}[kind]
        if not val or val == placeholder:
            return
        if kind == "ips":
            try:
                ipaddress.ip_network(val, strict=False)
            except ValueError:
                messagebox.showerror("Ошибка", f"Некорректный IP/сеть: {val}")
                return
        if self.mode == "direct":
            self.drm.direct_add(kind, val)
        else:
            self.drm.tunnel_add(kind, val)
        self.save_config()
        self._refresh_lists()
        self._update_info()
        entry.delete(0, "end")

    def _delete_entry(self, kind: str, listbox: tk.Listbox):
        sel = listbox.curselection()
        if not sel:
            return
        val = listbox.get(sel[0])
        if self.mode == "direct":
            self.drm.direct_remove(kind, val)
        else:
            self.drm.tunnel_remove(kind, val)
        self.save_config()
        self._refresh_lists()
        self._update_info()

    def _update_lists(self):
        url = self.url_var.get().strip()
        self.drm.cfg["url"] = url
        self.save_config()
        stats = self.drm.update_from_url(url)
        if stats.get("errors"):
            self.update_status_var.set(f"Ошибка загрузки: {stats.get("error")}")
        else:
            self.save_config()
            self._refresh_lists()
            self._update_info()
            self.update_status_var.set(
                f"Загружено: {stats.get("domains", 0)} доменов, {stats.get("ips", 0)} IP, {stats.get("apps", 0)} приложений"
            )

    def _apply(self):
        ok, msg = self.drm.apply()
        if ok:
            messagebox.showinfo("Исключения", msg)
        else:
            messagebox.showwarning("Исключения", msg)


def main():
    root = tk.Tk()
    app = GatewayApp(root)
    root.mainloop()

if __name__ == "__main__":
    main()

