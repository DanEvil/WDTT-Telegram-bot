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


def platforms_with_windows_app(url: str) -> dict:
    """Копия PLATFORMS с подменённой ссылкой на приложение Windows (значение из админ-панели)."""
    import copy
    plats = copy.deepcopy(PLATFORMS)
    plats["windows"]["app_url"] = url
    return plats


def step_img(name: str) -> str:
    """URL картинки шага (статика сайта/приложения)."""
    return f"/static/instr/{name}"
