"""Применение способов обхода блокировок в Windows-приложении.

Работает с каталогом `block_methods.py` (репозиторий запрета) и умеет:

- диагностировать, какая техника блокировки мешает конкретному сайту;
- добавлять домен в список zapret2 (`myhostlist.txt`) и перезапускать zapret;
- включать/выключать QUIC в браузерах Chrome/Edge через реестр;
- переключать DNS-over-HTTPS в браузере;
- отправлять домен в прямое подключение (мимо VPN) или в принудительный туннель.

Все действия, меняющие систему, требуют прав администратора — там, где это
невозможно, метод возвращает понятный текст вместо ошибки.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import winreg

# Пути zapret2 (установка Stable)
ZAPRET_DIR = r"C:\Zapret\Stable"
ZAPRET_HOSTLIST = os.path.join(ZAPRET_DIR, "lists", "myhostlist.txt")
ZAPRET_EXCLUDE = os.path.join(ZAPRET_DIR, "lists", "list-exclude.txt")
ZAPRET_EXE = os.path.join(ZAPRET_DIR, "exe", "winws2.exe")
ZAPRET_GUI = os.path.join(ZAPRET_DIR, "_internal", "Zapret.exe")
ZAPRET_SERVICE = "Monkey"

# Реестр Chrome/Edge: политики QUIC и DoH
CHROME_POLICY = r"SOFTWARE\Policies\Google\Chrome"
EDGE_POLICY = r"SOFTWARE\Policies\Microsoft\Edge"

DOH_TEMPLATES = {
    "cloudflare": "https://chrome.cloudflare-dns.com/dns-query",
    "google": "https://dns.google/dns-query",
    "yandex": "https://common.dot.dns.yandex.net/dns-query",
}


def is_admin() -> bool:
    """True, если процесс запущен с правами администратора."""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# zapret2
# ─────────────────────────────────────────────────────────────────────────────

def zapret_installed() -> bool:
    return os.path.isdir(ZAPRET_DIR)


def zapret_hostlist_path() -> str | None:
    """Путь к пользовательскому списку zapret (None, если zapret не установлен)."""
    return ZAPRET_HOSTLIST if zapret_installed() else None


def zapret_add_host(domain: str) -> tuple[bool, str]:
    """Добавляет домен в myhostlist.txt zapret2.

    Возвращает (успешно, сообщение). Домен должен быть без схемы и слеша.
    """
    domain = (domain or "").strip().lower()
    if not domain:
        return False, "Пустой домен"
    if "://" in domain:
        domain = domain.split("://", 1)[1]
    domain = domain.split("/", 1)[0].split(":", 1)[0]
    if not domain:
        return False, "Не удалось разобрать домен"

    if not zapret_installed():
        return False, "zapret2 не установлен (C:\\Zapret\\Stable)"

    try:
        os.makedirs(os.path.dirname(ZAPRET_HOSTLIST), exist_ok=True)
        existing = []
        if os.path.isfile(ZAPRET_HOSTLIST):
            with open(ZAPRET_HOSTLIST, "r", encoding="utf-8", errors="replace") as f:
                existing = [ln.strip() for ln in f if ln.strip()]
        if domain in existing:
            return True, f"{domain} уже есть в списке zapret"
        existing.append(domain)
        with open(ZAPRET_HOSTLIST, "w", encoding="utf-8") as f:
            f.write("\n".join(existing) + "\n")
        return True, f"{domain} добавлен в myhostlist.txt"
    except Exception as e:
        return False, f"Не удалось записать список: {e}"


def zapret_remove_host(domain: str) -> tuple[bool, str]:
    """Убирает домен из myhostlist.txt zapret2."""
    domain = (domain or "").strip().lower()
    if not zapret_installed() or not os.path.isfile(ZAPRET_HOSTLIST):
        return False, "zapret2 не установлен"
    try:
        with open(ZAPRET_HOSTLIST, "r", encoding="utf-8", errors="replace") as f:
            lines = [ln.strip() for ln in f if ln.strip()]
        if domain not in lines:
            return True, f"{domain} не было в списке"
        lines = [ln for ln in lines if ln.lower() != domain]
        with open(ZAPRET_HOSTLIST, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + ("\n" if lines else ""))
        return True, f"{domain} удалён из myhostlist.txt"
    except Exception as e:
        return False, f"Не удалось изменить список: {e}"


def normalize_domain(domain: str) -> str:
    """Приводит домен к виду без схемы, пути и порта."""
    domain = (domain or "").strip().lower()
    if "://" in domain:
        domain = domain.split("://", 1)[1]
    domain = domain.split("/", 1)[0].split(":", 1)[0]
    return domain


def domain_in_list(domain: str, hosts: list[str]) -> bool:
    """Домен или его родительский домен есть в списке.

    Шаблон "example.com" покрывает "www.example.com" и "a.b.example.com";
    шаблон "*.example.com" покрывает поддомены, но не сам example.com.
    """
    domain = normalize_domain(domain)
    if not domain:
        return False
    for raw in (hosts or []):
        pat = normalize_domain(raw)
        if not pat:
            continue
        if pat.startswith("*."):
            base = pat[2:]
            if domain == base or domain.endswith("." + base):
                return True
        elif domain == pat or domain.endswith("." + pat):
            return True
    return False


def zapret_covers(domain: str) -> bool:
    """Домен обслуживается zapret — он есть в myhostlist.txt."""
    if not zapret_installed():
        return False
    return domain_in_list(domain, zapret_hosts())


def zapret_state() -> dict:
    """Состояние zapret2: установлен, работает, сколько доменов в списке."""
    hosts = zapret_hosts() if zapret_installed() else []
    return {
        "installed": zapret_installed(),
        "running": zapret_running(),
        "hosts_count": len(hosts),
        "hosts": hosts,
    }


def zapret_hosts() -> list[str]:
    """Текущий список доменов zapret2."""
    if not zapret_installed() or not os.path.isfile(ZAPRET_HOSTLIST):
        return []
    try:
        with open(ZAPRET_HOSTLIST, "r", encoding="utf-8", errors="replace") as f:
            return [ln.strip() for ln in f if ln.strip()]
    except Exception:
        return []


def zapret_is_excluded(domain: str) -> bool:
    """Домен в list-exclude.txt — zapret его намеренно не обходит."""
    domain = (domain or "").strip().lower()
    if not zapret_installed() or not os.path.isfile(ZAPRET_EXCLUDE):
        return False
    try:
        with open(ZAPRET_EXCLUDE, "r", encoding="utf-8", errors="replace") as f:
            for ln in f:
                ln = ln.strip().lower()
                if not ln or ln.startswith("#"):
                    continue
                if ln == domain or domain.endswith("." + ln):
                    return True
    except Exception:
        pass
    return False


def zapret_running() -> bool:
    """Запущен ли сейчас zapret (winws2.exe)."""
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq winws2.exe"],
                             capture_output=True, timeout=15)
        text = (out.stdout or b"").decode("utf-8", "replace").lower()
        return "winws2.exe" in text
    except Exception:
        return False


def zapret_restart() -> tuple[bool, str]:
    """Перезапускает zapret2, чтобы изменения списка применились.

    Порядок: сервис Monkey → GUI Zapret.exe → winws2 напрямую.
    Изменения списка не действуют на лету — нужен перезапуск.
    """
    if not zapret_installed():
        return False, "zapret2 не установлен"

    # 1) Служба Monkey (устанавливается GUI при старте с правами админа)
    try:
        q = subprocess.run(["sc", "query", ZAPRET_SERVICE],
                           capture_output=True, timeout=15)
        if q.returncode == 0 and b"EXISTS" in (q.stdout or b"").upper() or \
           (q.returncode == 0 and b"RUNNING" in (q.stdout or b"").upper()):
            subprocess.run(["sc", "stop", ZAPRET_SERVICE], capture_output=True, timeout=20)
            subprocess.run(["sc", "start", ZAPRET_SERVICE], capture_output=True, timeout=20)
            return True, "zapret2 перезапущен через службу"
    except Exception:
        pass

    # 2) GUI — она сама поднимает winws с настройками пользователя.
    #    GUI требует повышения прав, поэтому запускаем через UAC.
    if os.path.isfile(ZAPRET_GUI):
        if _elevate(ZAPRET_GUI, cwd=os.path.dirname(ZAPRET_GUI)):
            return True, "zapret2 GUI запущен с повышенными правами — проверьте, что обход включён"
        try:
            subprocess.Popen([ZAPRET_GUI], cwd=os.path.dirname(ZAPRET_GUI),
                             creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
            return True, "zapret2 GUI запущен — проверьте, что обход включён"
        except Exception as e:
            return False, f"Не удалось запустить GUI zapret2: {e}"

    # 3) winws2 напрямую (нужен конфиг с актуальными параметрами)
    if os.path.isfile(ZAPRET_EXE):
        cfg = _latest_zapret_config()
        if cfg:
            try:
                subprocess.Popen([ZAPRET_EXE, f"@{cfg}"], cwd=ZAPRET_DIR,
                                 creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
                return True, "zapret2 перезапущен (winws2)"
            except Exception as e:
                return False, f"Не удалось запустить winws2: {e}"
        return False, "zapret2 запущен вручную: нет сохранённого конфига"

    return False, "Не удалось перезапустить zapret2"


def _elevate(exe_path: str, cwd: str | None = None) -> bool:
    """Запускает программу с повышенными правами через UAC (ShellExecute runas).

    Возвращает True, если запуск инициирован (пользователь подтвердит UAC).
    Если процесс уже запущен с правами администратора — просто запускаем.
    """
    if is_admin():
        try:
            subprocess.Popen([exe_path], cwd=cwd,
                             creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
            return True
        except Exception:
            return False
    try:
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe_path, None, cwd, 1)
        # ShellExecuteW возвращает >32 при успехе, <=32 при ошибке/отказе
        return rc > 32
    except Exception:
        return False


def _latest_zapret_config() -> str | None:
    """Путь к последнему сохранённому конфигу winws2 (если есть)."""
    tmp_dir = os.path.join(ZAPRET_DIR, "user", "tmp", "winws2_at_config")
    if not os.path.isdir(tmp_dir):
        return None
    try:
        files = [os.path.join(tmp_dir, fn) for fn in os.listdir(tmp_dir)
                 if fn.endswith(".txt")]
        if not files:
            return None
        return max(files, key=os.path.getmtime)
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Браузер: QUIC и DNS-over-HTTPS
# ─────────────────────────────────────────────────────────────────────────────

def _set_policy(key_path: str, name: str, value, kind=winreg.REG_DWORD) -> bool:
    """Записывает значение в ветку реестра (создавая её)."""
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path, 0,
                                winreg.KEY_SET_VALUE | winreg.KEY_READ) as key:
            winreg.SetValueEx(key, name, 0, kind, value)
        return True
    except Exception:
        return False


def browser_disable_quic() -> tuple[bool, str]:
    """Отключает QUIC в Chrome и Edge через политику реестра.

    Нужен перезапуск браузера. Требует прав администратора для записи в
    ветку Policies (иначе пишем в HKCU — этого достаточно для Chrome/Edge).
    """
    done = []
    for path, name in ((CHROME_POLICY, "Chrome"), (EDGE_POLICY, "Edge")):
        if _set_policy(path, "QuicAllowed", 0):
            done.append(name)
    if done:
        return True, ("QUIC отключён в " + ", ".join(done) +
                      " — перезапустите браузер")
    return False, "Не удалось записать политику QUIC (нужны права администратора)"


def browser_enable_quic() -> tuple[bool, str]:
    """Возвращает QUIC в Chrome и Edge (QuicAllowed=1)."""
    done = []
    for path, name in ((CHROME_POLICY, "Chrome"), (EDGE_POLICY, "Edge")):
        if _set_policy(path, "QuicAllowed", 1):
            done.append(name)
    if done:
        return True, "QUIC включён в " + ", ".join(done) + " — перезапустите браузер"
    return False, "Не удалось записать политику QUIC"


def browser_set_doh(provider: str = "cloudflare") -> tuple[bool, str]:
    """Включает DNS-over-HTTPS в Chrome и Edge."""
    template = DOH_TEMPLATES.get(provider)
    if not template:
        return False, f"Неизвестный DNS-провайдер: {provider}"
    ok = True
    for path in (CHROME_POLICY, EDGE_POLICY):
        ok &= _set_policy(path, "DnsOverHttpsMode", 2)
        ok &= _set_policy(path, "DnsOverHttpsTemplates",
                          template, winreg.REG_SZ)
    if ok:
        return True, f"DNS-over-HTTPS ({provider}) включён — перезапустите браузер"
    return False, "Не удалось включить DNS-over-HTTPS"


def browser_doh_state() -> dict:
    """Текущее состояние политик QUIC/DoH в Chrome и Edge."""
    out = {}
    for path, name in ((CHROME_POLICY, "Chrome"), (EDGE_POLICY, "Edge")):
        state = {}
        for key, kind in (("QuicAllowed", "dword"), ("DnsOverHttpsMode", "dword")):
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as k:
                    val, _ = winreg.QueryValueEx(k, key)
                state[key] = val
            except FileNotFoundError:
                state[key] = None
            except Exception:
                state[key] = None
        out[name] = state
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Применение обхода по «рецепту» из каталога
# ─────────────────────────────────────────────────────────────────────────────

def apply_remedy(remedy: dict, domain: str, drm=None,
                 config: dict | None = None) -> tuple[bool, str]:
    """Применяет один способ обхода к домену.

    remedy — запись из каталога (block_methods.CATALOG): kind = zapret/direct/
    tunnel/browser/dns/vpn. drm — DirectRoutesManager (для direct/tunnel).
    """
    kind = (remedy or {}).get("kind", "")
    title = (remedy or {}).get("title", "обход")
    domain = (domain or "").strip().lower()
    if "://" in domain:
        domain = domain.split("://", 1)[1]
    domain = domain.split("/", 1)[0].split(":", 1)[0]

    if kind == "zapret":
        ok, msg = zapret_add_host(domain)
        if not ok:
            return False, f"{title}: {msg}"
        restarted, rmsg = zapret_restart()
        if restarted:
            return True, f"{title}: {msg}; {rmsg}"
        return True, f"{title}: {msg}; перезапустите zapret2 вручную ({rmsg})"

    if kind == "direct":
        if drm is None:
            return False, f"{title}: нет менеджера маршрутов"
        drm.direct_add("domains", domain)
        drm.save()
        return True, f"{title}: {domain} добавлен в прямое подключение (мимо VPN)"

    if kind == "tunnel":
        if drm is None:
            return False, f"{title}: нет менеджера маршрутов"
        drm.tunnel_add("domains", domain)
        drm.save()
        return True, f"{title}: {domain} отправлен через туннель VPN"

    if kind == "browser":
        rid = (remedy or {}).get("id", "")
        if rid == "quic_off":
            ok, msg = browser_disable_quic()
            return ok, f"{title}: {msg}"
        if rid == "doh":
            ok, msg = browser_set_doh("cloudflare")
            return ok, f"{title}: {msg}"
        if rid == "secure_dns":
            return False, (f"{title}: откройте Параметры → Сеть и Интернет → "
                           "Свойства адаптера → DNS-серверы → «Автоматически (DoH)»")
        return False, f"{title}: примените вручную — {remedy.get('detail', '')}"

    if kind == "dns":
        return False, (f"{title}: укажите другой DNS в настройках сети "
                       f"({remedy.get('detail', '')})")

    if kind == "vpn":
        return False, (f"{title}: подключитесь через профиль VPN в приложении "
                       f"({remedy.get('detail', '')})")

    return False, f"Неизвестный способ обхода: {kind}"


def apply_all_remedies(diagnosis: dict, drm=None, limit: int = 3) -> list[dict]:
    """Применяет несколько способов обхода подряд (до limit успешных)."""
    out = []
    done = 0
    for remedy in diagnosis.get("remedies", []) or []:
        if done >= limit:
            break
        ok, msg = apply_remedy(remedy, diagnosis.get("host", ""), drm=drm)
        out.append({"remedy": remedy, "ok": ok, "message": msg})
        if ok:
            done += 1
    return out
