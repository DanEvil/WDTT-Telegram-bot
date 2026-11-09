"""Пошаговые инструкции по подключению (единый источник для бота, сайта и мини-аппа).

Фото лежат в static/instr/ и отдаются как /static/instr/<имя> —
один и тот же URL работает и на сайте, и в мини-аппе.
"""

ANDROID_APP_URL = "https://github.com/amurcanov/proxy-turn-vk-android/releases/download/v1.2.4/WDTT-universal.apk"
IOS_TESTFLIGHT_APP = "https://apps.apple.com/app/testflight/id899247664"
IOS_TESTFLIGHT_JOIN = "https://testflight.apple.com/join/ANm6cmDv"

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
}

INTRO = "Подключение к K.O. Gateway"


def step_img(name: str) -> str:
    """URL картинки шага (статика сайта/приложения)."""
    return f"/static/instr/{name}"
