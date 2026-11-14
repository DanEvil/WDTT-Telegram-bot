"""Пошаговые инструкции по подключению (единый источник для бота, сайта и мини-аппа).

Фото лежат в static/instr/ и отдаются как /static/instr/<имя> —
один и тот же URL работает и на сайте, и в мини-аппе.
Шаг может быть без фото (img=None) — тогда рисуется просто текст.
"""
import os

ANDROID_APP_URL = "https://github.com/amurcanov/proxy-turn-vk-android/releases/download/v1.2.4/WDTT-universal.apk"
IOS_TESTFLIGHT_APP = "https://apps.apple.com/app/testflight/id899247664"
IOS_TESTFLIGHT_JOIN = "https://testflight.apple.com/join/ANm6cmDv"
# Ссылка на приложение для Windows — та же, что и на сайте (config.APP_WINDOWS_URL).
WINDOWS_APP_URL = os.getenv(
    "APP_WINDOWS_URL",
    "https://github.com/ildarmaga/wdtt/releases/download/v1.5.76/pwdtt-client-windows-amd64.exe",
).strip()

PLATFORMS = {
    "android": {
        "title": "🤖 Android",
        "intro": (
            "Установите приложение <b>WDTT</b> (APK), добавьте конфигурацию по ссылке "
            "<code>wdtt://</code> из профиля и подключитесь."
        ),
        "app_url": ANDROID_APP_URL,
        "app_label": "Скачать WDTT для Android (APK)",
        "steps": [
            ("IMG_6833.jpeg", 'Откройте приложение и перейдите во вкладку «Туннель»'),
            ("IMG_6834.jpeg", 'Включите переключатель «Режим ссылки»'),
            ("IMG_6835.jpeg", 'Вставьте вашу ссылку wdtt:// и нажмите «Подключить»'),
        ],
    },
    "ios": {
        "title": "🍎 iOS",
        "intro": (
            "Установите <b>TestFlight</b> из App Store, откройте ссылку-приглашение, "
            "установите клиент и подключитесь, как показано на скриншотах ниже."
        ),
        "links": [
            (IOS_TESTFLIGHT_APP, "TestFlight в App Store"),
            (IOS_TESTFLIGHT_JOIN, "Приглашение в TestFlight"),
        ],
        "steps": [
            ("IMG_6820.jpeg", 'Откройте приложение VK Turn Proxy и нажмите внизу «Settings»'),
            ("IMG_6821.jpg", 'В разделе «Backup & Restore» выберите «Import from Connection Link…»'),
            ("IMG_6822.jpg", 'В открывшемся окне нажмите «Разрешить вставку» — ссылка вставится из Telegram'),
        ],
    },
    "windows": {
        "title": "🪟 Windows",
        "intro": (
            "Установите приложение <b>PWDTT</b> для Windows, добавьте конфигурацию по ссылке "
            "<code>wdtt://</code> из профиля и подключитесь."
        ),
        "app_url": WINDOWS_APP_URL,
        "app_label": "Скачать PWDTT для Windows (EXE)",
        "steps": [
            (None, "Скачайте и установите PWDTT для Windows по кнопке выше."),
            (None, "Скопируйте вашу ссылку wdtt://... из профиля (в приложении или на сайте)."),
            (None, "Вставьте ссылку в PWDTT в поле «Ссылка» и нажмите «Подключить»."),
        ],
    },
}

INTRO = "Подключение к K.O. Gateway"

# Совместимые клиенты и форматы ссылок — как в README апстрима ildarmaga/wdtt.
CLIENTS = [
    {"client": "VK Turn Proxy", "platform": "iOS",
     "format": "wdtt:// (colon), 1-й хеш из ссылки или VK_HASH"},
    {"client": "WDTT", "platform": "Android",
     "format": "wdtt:// (colon), до 4 хешей через запятую или VK_HASH"},
    {"client": "PWDTT", "platform": "Desktop Win/Linux",
     "format": "wdtt:// (colon), до 4 хешей — клиент из релизов WDTT"},
    {"client": "WDTT", "platform": "Windows",
     "format": "wdtt:// (colon), до 4 хешей через запятую или VK_HASH"},
    {"client": "qWDTT", "platform": "Android (fork)",
     "format": "qwdtt://, hashes=h1,h2,… (до 4) или VK_HASH"},
    {"client": "WDTT (qwdtt)", "platform": "Windows",
     "format": "qwdtt://, hashes=h1,h2,… (до 4) или VK_HASH"},
]

# Про VK-хеш — как в апстриме: создаётся в панели (Настройки → VK Creator).
VK_HASH_NOTE = (
    "VK-хеш для подключения создаётся автоматически в панели WDTT "
    "(Настройки → VK Creator) и попадает в вашу ссылку подключения — "
    "вводить его вручную не нужно."
)


def clients_table_html() -> str:
    """Таблица совместимых клиентов для сайта и мини-аппа."""
    import html as _html
    rows = "".join(
        "<tr><td><b>{}</b></td><td>{}</td><td><code>{}</code></td></tr>".format(
            _html.escape(c["client"]), _html.escape(c["platform"]), _html.escape(c["format"])
        )
        for c in CLIENTS
    )
    return (
        '<table class="clients-table"><thead><tr>'
        "<th>Клиент</th><th>Платформа</th><th>Формат ссылки</th>"
        f"</tr></thead><tbody>{rows}</tbody></table>"
    )


def clients_text() -> str:
    """Та же таблица plain-текстом для Telegram-бота (без <table>)."""
    lines = ["📱 Совместимые клиенты:"]
    for c in CLIENTS:
        lines.append(f"• <b>{c['client']}</b> ({c['platform']}): <code>{c['format']}</code>")
    lines.append("")
    lines.append(VK_HASH_NOTE)
    return "\n".join(lines)


def platforms_with_windows_app(url: str) -> dict:
    """Копия PLATFORMS с подменённой ссылкой на приложение Windows (значение из админ-панели)."""
    import copy
    plats = copy.deepcopy(PLATFORMS)
    plats["windows"]["app_url"] = url
    return plats


def step_img(name: str) -> str:
    """URL картинки шага (статика сайта/приложения)."""
    return f"/static/instr/{name}"
