"""Репозиторий запрета: каталог техник блокировки и их обхода.

Каталог описывает, какими способами оператор связи (DPI/РКН и подобные)
мешает доступу к сайту, как это распознать и какими средствами обойти.
Одна и та же база используется:

- десктопным приложением Windows (`Windows/main.py`) — определить, что мешает,
  и применить обход (zapret, маршруты, настройки браузера);
- сайтом и ботом (`api.py`) — отдать каталог клиентам.

Только стандартная библиотека: модуль должен работать и на сервере.
"""

from __future__ import annotations

import socket
import ssl
import time
import urllib.error
import urllib.request

# ─────────────────────────────────────────────────────────────────────────────
# Каталог техник блокировки
# ─────────────────────────────────────────────────────────────────────────────
# kind        — категория техники (для группировки в UI)
# probe       — на что смотреть при диагностике
# remedies    — способы обхода; kind определяет, каким модулем применяется:
#               zapret / direct / tunnel / browser / dns / vpn

CATALOG: list[dict] = [
    {
        "id": "dns_blackhole",
        "kind": "dns",
        "title": "Подмена или блокировка DNS",
        "description": "Резолвер отдаёт неверный IP, заглушку или не отвечает вовсе.",
        "symptoms": ["сайт не открывается вообще", "открывается не тот сайт",
                     "все браузеры ведут себя одинаково"],
        "probe": "getaddrinfo не отвечает или возвращает подозрительный адрес",
        "remedies": [
            {"id": "doh", "kind": "browser", "title": "Включить DNS-over-HTTPS в браузере",
             "detail": "Chrome/Edge: DnsOverHttpsMode=2 с шаблоном Cloudflare/Google"},
            {"id": "secure_dns", "kind": "browser", "title": "Безопасный DNS в Windows",
             "detail": "Параметры сети → DNS-запросы → автоматический (DoH)"},
            {"id": "vpn_dns", "kind": "vpn", "title": "Резолвить через туннель VPN",
             "detail": "DNS-запросы идут внутри туннеля, минуя подмену"},
        ],
    },
    {
        "id": "ip_block",
        "kind": "network",
        "title": "Блокировка по IP-адресу или подсети",
        "description": "Соединение до сервера обрывается на сетевом уровне: SYN не отвечает.",
        "symptoms": ["сайт полностью недоступен", "другие сайты на том же IP работают",
                     "ошибка connection timed out"],
        "probe": "TCP-соединение до 443 не устанавливается",
        "remedies": [
            {"id": "vpn_route", "kind": "vpn", "title": "Пустить через туннель VPN",
             "detail": "Исходящий адрес меняется на адрес VPN-сервера"},
            {"id": "other_dns", "kind": "dns", "title": "Другой DNS для этого домена",
             "detail": "Часто у блокировки есть несколько адресов — помогает смена резолвера"},
        ],
    },
    {
        "id": "sni_filter",
        "kind": "dpi",
        "title": "Фильтрация по SNI (имя домена в TLS ClientHello)",
        "description": "DPI читает имя домена из незашифрованной части TLS-рукопожатия и обрывает соединение.",
        "symptoms": ["сайт открывается только в браузере с ECH/DoH",
                     "curl и приложения не работают", "загрузка зависает на «установлении соединения»"],
        "probe": "TCP соединяется, TLS-рукопожатие зависает или сбрасывается после ClientHello",
        "remedies": [
            {"id": "zapret_fake_tls", "kind": "zapret", "title": "zapret: фейковый TLS ClientHello",
             "detail": "Подмена ClientHello ломает чтение SNI на DPI"},
            {"id": "zapret_disorder", "kind": "zapret", "title": "zapret: disorder / split",
             "detail": "Фрагментация и перестановка сегментов мешают сборке ClientHello"},
            {"id": "vpn_sni", "kind": "vpn", "title": "Скрыть SNI внутри туннеля",
             "detail": "DPI не видит имя домена — соединение идёт к VPN-серверу"},
        ],
    },
    {
        "id": "tls_fragmentation",
        "kind": "dpi",
        "title": "Дессинхронизация на границе фрагментов TLS",
        "description": "DPI не склеивает фрагменты ClientHello и не находит имя домена.",
        "symptoms": ["короткие TLS-соединения работают, длинные — нет",
                     "помогает только смена браузера или клиента"],
        "probe": "рукопожатие с длинным ClientHello падает, с коротким — проходит",
        "remedies": [
            {"id": "zapret_multisplit", "kind": "zapret", "title": "zapret: multisplit",
             "detail": "Разрезание ClientHello на несколько сегментов"},
            {"id": "zapret_fake_record", "kind": "zapret", "title": "zapret: фейковая TLS-запись",
             "detail": "Подставная запись сбивает разбор потока на DPI"},
            {"id": "vpn_frag", "kind": "vpn", "title": "Скрыть фрагментацию в туннеле",
             "detail": "Наружу уходит один поток без разрывов"},
        ],
    },
    {
        "id": "quic_blackhole",
        "kind": "dpi",
        "title": "Блокировка QUIC (UDP 443)",
        "description": "UDP-трафик HTTP/3 глушат, браузер упирается в него и не откатывается на TCP.",
        "symptoms": ["видео и сайты открываются с задержкой", "браузер не откатывается на HTTP/2",
                     "TCP работает, а QUIC — нет"],
        "probe": "UDP 443 не отвечает, TCP 443 отвечает нормально",
        "remedies": [
            {"id": "quic_off", "kind": "browser", "title": "Отключить QUIC в браузере",
             "detail": "Chrome/Edge: политика QuicAllowed=0, приложение перейдёт на HTTP/2"},
            {"id": "zapret_fake_quic", "kind": "zapret", "title": "zapret: фейковые QUIC-пакеты",
             "detail": "zapret отвечает на UDP 443 фейковым QUIC, отводя DPI в сторону"},
            {"id": "vpn_quic", "kind": "vpn", "title": "Пустить QUIC через туннель",
             "detail": "UDP инкапсулируется в туннель и не фильтруется"},
        ],
    },
    {
        "id": "tcp_throttle",
        "kind": "throttle",
        "title": "Искусственное замедление TCP",
        "description": "Скорость режут на стороне оператора: пакеты считают, окна сужают, соединение «ползёт».",
        "symptoms": ["сайт открывается очень долго", "скорость падает в разы при долгой загрузке",
                     "ping в норме, а переда��а данных медленная"],
        "probe": "соединение устанавливается, но скорость передачи заметно ниже нормы",
        "remedies": [
            {"id": "zapret_split", "kind": "zapret", "title": "zapret: split / multisplit",
             "detail": "Дробление потока сбивает счётчик пакетов шейпера"},
            {"id": "zapret_fake", "kind": "zapret", "title": "zapret: фейковые пакеты",
             "detail": "Лишние пакеты в обратном направлении обесценивают счётчик"},
            {"id": "direct_route", "kind": "direct", "title": "Прямое подключение мимо VPN",
             "detail": "Если замедляет сам туннель — домен уходит напрямую"},
            {"id": "vpn_throttle", "kind": "vpn", "title": "Сменить протокол VPN",
             "detail": "Другой транспорт (WDTT/AIVPN/OpenFlux) может идти без шейпа"},
        ],
    },
    {
        "id": "rst_injection",
        "kind": "dpi",
        "title": "Инъекция RST-пакетов",
        "description": "Оператор отправляет поддельный RST, обрывая уже установленное соединение.",
        "symptoms": ["соединение рвётся сразу после установления", "повторные попытки помогают",
                     "ошибка connection reset by peer"],
        "probe": "TLS или HTTP обрывается ошибкой ConnectionResetError",
        "remedies": [
            {"id": "zapret_fake_rst", "kind": "zapret", "title": "zapret: фейковые пакеты",
             "detail": "Фейковый ответ перебивает инъекцию RST"},
            {"id": "ignore_rst", "kind": "zapret", "title": "zapret: игнорировать RST",
             "detail": "Фильтр отбрасывает входящие RST на заданном порту"},
            {"id": "vpn_rst", "kind": "vpn", "title": "Скрыть соединение в туннеле",
             "detail": "RST-инъекция не работает по зашифрованному туннелю"},
        ],
    },
    {
        "id": "http_host_filter",
        "kind": "dpi",
        "title": "Фильтрация по HTTP Host",
        "description": "DPI разбирает HTTP-заголовок Host и блокирует запрос к нужному домену.",
        "symptoms": ["HTTPS работает, HTTP — нет", "запрос возвращает пустую страницу или 403"],
        "probe": "TLS проходит, но HTTP-запрос к сайту обрывается или подменяется",
        "remedies": [
            {"id": "zapret_fake_host", "kind": "zapret", "title": "zapret: фейковый Host-заголовок",
             "detail": "Подставной Host сбивает разбор запроса"},
            {"id": "vpn_http", "kind": "vpn", "title": "HTTP через туннель",
             "detail": "Весь HTTP-трафик шифруется внутри туннеля"},
        ],
    },
    {
        "id": "mtproto_block",
        "kind": "dpi",
        "title": "Блокировка протокола MTProto (Telegram)",
        "description": "Фильтруются UDP-пакеты MTProxy и DC-соединения Telegram.",
        "symptoms": ["Telegram не подключается, веб-версия работает",
                     "в логах ошибки connection to 149.154.* failed"],
        "probe": "UDP-соединения к DC Telegram не отвечают",
        "remedies": [
            {"id": "zapret_tg_proxy", "kind": "zapret", "title": "zapret: встроенный Telegram-прокси",
             "detail": "Локальный MTProxy переводит трафик в TCP"},
            {"id": "vpn_mtproto", "kind": "vpn", "title": "Telegram через туннель VPN",
             "detail": "MTProxy-трафик идёт внутри зашифрованного туннеля"},
        ],
    },
    {
        "id": "ttl_desync",
        "kind": "dpi",
        "title": "Манипуляция TTL (рассинхронизация маршрутов)",
        "description": "TTL пакетов урезается так, что DPI видит поток, а сервер — нет.",
        "symptoms": ["соединение обрывается через несколько секунд", "повторные попытки не помогают",
                     "помогает только смена маршрута"],
        "probe": "соединение устанавливается и тут же обрывается без ошибки приложения",
        "remedies": [
            {"id": "zapret_autottl", "kind": "zapret", "title": "zapret: autottl для фейковых пакетов",
             "detail": "Фейковые пакеты с автоподбором TTL доходят до DPI"},
            {"id": "vpn_ttl", "kind": "vpn", "title": "Скрыть поток в туннеле",
             "detail": "Наружу идёт только шифрованный поток до VPN-сервера"},
        ],
    },
]

CATALOG_BY_ID = {item["id"]: item for item in CATALOG}


def get_catalog() -> list[dict]:
    """Каталог техник блокировки (копия — чтобы вызывающий не испортил исходный)."""
    return [dict(item) for item in CATALOG]


def get_method(method_id: str) -> dict | None:
    """Описание техники по id."""
    return dict(CATALOG_BY_ID[method_id]) if method_id in CATALOG_BY_ID else None


# ─────────────────────────────────────────────────────────────────────────────
# Диагностика
# ─────────────────────────────────────────────────────────────────────────────

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) K.O.Gateway/1.0"


def _host_of(url: str) -> str:
    if "://" in url:
        url = url.split("://", 1)[1]
    return url.split("/", 1)[0].split(":", 1)[0]


def probe_host(url: str, timeout: float = 6.0) -> dict:
    """Пробирует доступ к сайту по фазам и возвращает результаты диагностики.

    Фазы: DNS → TCP → TLS → HTTP. По тому, на какой фазе всё сломалось,
    определяется техника блокировки.
    """
    host = _host_of(url)
    res: dict = {"url": url, "host": host, "dns_ms": None, "tcp_ms": None,
                 "tls_ms": None, "http_ms": None, "dns_ok": False, "tcp_ok": False,
                 "tls_ok": False, "http_ok": False, "error": None, "phase": None}

    # 1. DNS
    t0 = time.time()
    try:
        socket.setdefaulttimeout(timeout)
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        res["dns_ok"] = True
        res["dns_ms"] = int((time.time() - t0) * 1000)
        res["ips"] = sorted({i[4][0] for i in infos})
    except Exception as e:
        res["dns_ms"] = int((time.time() - t0) * 1000)
        res["error"] = f"dns: {type(e).__name__}"
        res["phase"] = "dns"
        return res

    # 2. TCP
    t0 = time.time()
    try:
        with socket.create_connection((host, 443), timeout=timeout) as sock:
            res["tcp_ok"] = True
            res["tcp_ms"] = int((time.time() - t0) * 1000)
            # 3. TLS
            t1 = time.time()
            # Сертификат не проверяем: проба меряет ЗАДЕРЖКУ по фазам
            # (DNS/TCP/TLS/HTTP), а не проверяет подлинность сервера.
            # С отключённой проверкой handshake одинаково проходит и на
            # сайтах с самоподписанным сертификатом — иначе такие сайты
            # ошибочно диагностировались бы как «фильтрация по SNI».
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            try:
                with ctx.wrap_socket(sock, server_hostname=host) as tls:
                    res["tls_ok"] = True
                    res["tls_ms"] = int((time.time() - t1) * 1000)
                    try:
                        res["tls_version"] = tls.version()
                    except Exception:
                        pass
                    # 4. HTTP
                    t2 = time.time()
                    req = urllib.request.Request(
                        f"https://{host}/", headers={"User-Agent": UA},
                        method="GET")
                    try:
                        with urllib.request.urlopen(req, timeout=timeout) as r:
                            r.read(1)
                            res["http_ok"] = True
                            res["http_ms"] = int((time.time() - t2) * 1000)
                            res["http_code"] = r.status
                    except urllib.error.HTTPError as e:
                        # Сервер ответил — значит HTTP-фаза работает
                        res["http_ok"] = True
                        res["http_ms"] = int((time.time() - t2) * 1000)
                        res["http_code"] = e.code
                    except Exception as e:
                        res["error"] = f"http: {type(e).__name__}: {e}"
                        res["phase"] = "http"
            except Exception as e:
                res["error"] = f"tls: {type(e).__name__}: {e}"
                res["phase"] = "tls"
    except Exception as e:
        res["tcp_ms"] = int((time.time() - t0) * 1000)
        res["error"] = f"tcp: {type(e).__name__}: {e}"
        res["phase"] = "tcp"
        return res

    if res["http_ok"] and res["http_ms"] is None:
        res["http_ms"] = 0
    return res


def classify(probe: dict, slow_threshold_ms: int = 3000) -> dict:
    """Определяет технику блокировки по результатам пробы.

    Возвращает dict: method_id (id техники или None), confidence (0..1),
    severity ('blocked' | 'degraded' | 'ok'), reason (текст для UI).
    """
    err = (probe.get("error") or "").lower()
    host = probe.get("host", "")

    if not probe.get("dns_ok"):
        return {"method_id": "dns_blackhole", "confidence": 0.8, "severity": "blocked",
                "reason": f"DNS не отвечает для {host}"}

    if not probe.get("tcp_ok"):
        if "reset" in err:
            return {"method_id": "rst_injection", "confidence": 0.6, "severity": "blocked",
                    "reason": "соединение сброшено на уровне TCP"}
        return {"method_id": "ip_block", "confidence": 0.75, "severity": "blocked",
                "reason": f"TCP-соединение до {host}:443 не устанавливается"}

    if not probe.get("tls_ok"):
        if "reset" in err:
            return {"method_id": "rst_injection", "confidence": 0.75, "severity": "blocked",
                    "reason": "RST-инъекция: соединение сброшено во время TLS-рукопожатия"}
        if "timeout" in err or "timed out" in err:
            return {"method_id": "sni_filter", "confidence": 0.7, "severity": "blocked",
                    "reason": "TLS-рукопожатие зависло после ClientHello — похоже на фильтрацию по SNI"}
        return {"method_id": "tls_fragmentation", "confidence": 0.5, "severity": "blocked",
                "reason": f"TLS-рукопожатие не завершилось ({probe.get('error') or 'ошибка'})"}

    if not probe.get("http_ok"):
        return {"method_id": "http_host_filter", "confidence": 0.6, "severity": "blocked",
                "reason": "TLS работает, но HTTP-запрос не проходит"}

    http_ms = probe.get("http_ms") or 0
    if http_ms > slow_threshold_ms:
        return {"method_id": "tcp_throttle", "confidence": 0.65, "severity": "degraded",
                "reason": f"соединение установлено, но ответ {http_ms} мс — есть замедление"}

    return {"method_id": None, "confidence": 0.0, "severity": "ok",
            "reason": f"доступ без задержек ({http_ms} мс)"}


def diagnose(url: str, slow_threshold_ms: int = 3000, timeout: float = 6.0) -> dict:
    """Полная диагностика сайта: проба + определение техники + рекомендации."""
    probe = probe_host(url, timeout=timeout)
    verdict = classify(probe, slow_threshold_ms)
    method = CATALOG_BY_ID.get(verdict["method_id"]) if verdict.get("method_id") else None
    return {
        "url": url,
        "host": probe.get("host"),
        "probe": probe,
        "verdict": verdict,
        "method": method,
        "remedies": list(method.get("remedies", [])) if method else [],
    }
