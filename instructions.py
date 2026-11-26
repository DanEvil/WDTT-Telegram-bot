"""Пошаговые инструкции по подключению через VLESS (единый источник для бота, сайта и мини-аппа).

Поддерживаемые приложения для VLESS (Reality):
- Android: v2rayNG, Shadowrocket, Clash Meta, NekoBox, SagerNet
- iOS: Shadowrocket, Karing, V2RC, sing-box MT, Quantumult X
- Windows: v2rayN, NekoBox, Clash Verge, FlClash
- macOS: Clash Verge, FoXray, Stash
- Linux: v2rayA, NekoBox, Clash Verge

iOS-ссылки отобраны по фактической доступности в App Store России и стран СНГ
(проверено через iTunes lookup API для storefront'ов ru/kz/by/uz/am/ge/md/tj).
Streisand, FoXray и Stash удалены из App Store (404) — не предлагаем их клиентам.

Фото лежат в static/instr/ и отдаются как /static/instr/<имя> —
один и тот же URL работает и на сайте, и в мини-аппе.
Шаг может быть без фото (img=None) — тогда рисуется просто текст.
"""
import os

# URLs приложений (поддержка VLESS)
V2RAYNG_URL = "https://github.com/2dust/v2rayNG/releases/latest/download/app-universal-release.apk"
SHADOWROCKET_IOS = "https://apps.apple.com/app/id932747118"
KARING_IOS = "https://apps.apple.com/app/id6472431552"
V2RC_IOS = "https://apps.apple.com/app/id6748362312"
SINGBOX_MT_IOS = "https://apps.apple.com/app/id6785326793"
QUANTUMULT_X_IOS = "https://apps.apple.com/app/id1443988620"
CLASH_META_URL = "https://github.com/MetaCubeX/ClashMetaForAndroid/releases/latest/download/app-release.apk"
NEKOBOX_ANDROID = "https://github.com/MatsuriDayo/NekoBoxForAndroid/releases/latest/download/app-universal-release.apk"
V2RAYN_URL = "https://github.com/2dust/v2rayN/releases/latest/download/v2rayN.zip"
CLASH_VERGE_URL = "https://github.com/zzzgydi/clash-verge-rev/releases/latest/download/Clash.Verge_1.7.7_x64-setup.exe"

PLATFORMS = {
    "android": {
        "title": "🤖 Android",
        "intro": (
            "Для Android рекомендуем <b>v2rayNG</b> или <b>NekoBox</b> — "
            "оба поддерживают VLESS (Reality) и работают без Root.\n\n"
            "1. Скачайте приложение по кнопке ниже.\n"
            "2. Скопируйте VLESS-ссылку из профиля.\n"
            "3. В приложении: «+» → «Импорт из буфера обмена» → Подключиться."
        ),
        "app_url": V2RAYNG_URL,
        "app_label": "Скачать v2rayNG (APK)",
        "alt_apps": [
            ("NekoBox", NEKOBOX_ANDROID, "Современный клиент с Material You"),
            ("Clash Meta", CLASH_META_URL, "Поддержка правил и TUN"),
        ],
        "steps": [
            (None, "Установите v2rayNG (APK) или NekoBox и откройте приложение."),
            (None, "Скопируйте VLESS-ссылку из профиля (кнопка «👤 Моя подписка» в боте или на сайте)."),
            (None, "В v2rayNG: нажмите «+» → «Импорт из буфера обмена». В NekoBox: «+» → «Импорт из буфера обмена».\nСсылка определится автоматически."),
            (None, "Нажмите на созданную конфигурацию → переключите переключатель вверху → статус «Подключено»."),
        ],
    },
    "ios": {
        "title": "🍎 iOS / iPadOS",
        "intro": (
            "Для iOS рекомендуем <b>Shadowrocket</b> (249 ₽) — самый удобный клиент для "
            "VLESS Reality.\nБесплатные альтернативы: <b>Karing</b>, <b>V2RC</b>, <b>sing-box MT</b>.\n\n"
            "Все приложения ниже доступны в App Store России и стран СНГ.\n"
            "Если приложение не находится: Настройки → [ваше имя] → Медиаматериалы и покупки → "
            "Учётная запись → Страна/регион → Россия.\n\n"
            "1. Установите приложение из App Store.\n"
            "2. Скопируйте VLESS-ссылку из профиля.\n"
            "3. В приложении: «+» → «Импорт из буфера обмена» → «Готово» → Включите VPN."
        ),
        "links": [
            (SHADOWROCKET_IOS, "Shadowrocket (рекомендуем, 249 ₽)"),
            (KARING_IOS, "Karing (бесплатно, V2Ray/sing-box)"),
            (V2RC_IOS, "V2RC (бесплатно, Xray + VLESS)"),
            (SINGBOX_MT_IOS, "sing-box MT (бесплатно)"),
            (QUANTUMULT_X_IOS, "Quantumult X (899 ₽, для продвинутых)"),
        ],
        "steps": [
            (None, "Установите Shadowrocket или Karing из App Store — ссылки на все варианты выше (все доступны в РФ и СНГ)."),
            (None, "Скопируйте VLESS-ссылку из профиля (кнопка «👤 Моя подписка» в боте или на сайте)."),
            (None, "В приложении: нажмите «+» (плюс вверху справа) → «Импорт из буфера обмена» → конфигурация добавится автоматически."),
            (None, "Включите переключатель VPN → разрешите добавление конфигурации VPN → статус «Подключено»."),
        ],
    },
    "windows": {
        "title": "🪟 Windows",
        "intro": (
            "Для Windows рекомендуем <b>v2rayN</b> или <b>NekoBox</b> — "
            "оба полностью поддерживают VLESS (Reality).\n\n"
            "1. Скачайте и распакуйте v2rayN (или установите NekoBox).\n"
            "2. Скопируйте VLESS-ссылку из профиля.\n"
            "3. В v2rayN: «Сервер» → «Добавить из буфера обмена» → «Активировать»."
        ),
        "app_url": V2RAYN_URL,
        "app_label": "Скачать v2rayN (ZIP)",
        "alt_apps": [
            ("NekoBox", "https://github.com/MatsuriDayo/NekoBoxForDesktop/releases/latest/download/NekoBox-Setup.exe", "Кроссплатформенный GUI клиент"),
            ("Clash Verge", CLASH_VERGE_URL, "Красивый GUI на Tauri/React"),
        ],
        "steps": [
            (None, "Скачайте v2rayN (ZIP), распакуйте в любую папку и запустите v2rayN.exe."),
            (None, "Скопируйте VLESS-ссылку из профиля (кнопка «👤 Моя подписка» в боте или на сайте)."),
            (None, "В v2rayN: меню «Сервер» → «Добавить из буфера обмена» (Ctrl+V) → конфигурация появится в списке."),
            (None, "Дважды кликните по конфигурации → значок в трее станет зелёным → «System Proxy» → «Auto Config»."),
        ],
    },
}

INTRO = (
    "🔐 <b>Подключение через VLESS (Reality)</b>\n\n"
    "VLESS — современный протокол с поддержкой Reality (маскировка под TLS).\n"
    "Работает быстрее и стабильнее WDTT, не требует VK-хешей.\n\n"
    "Выберите вашу платформу:"
)

# Версия без HTML-тегов: подзаголовок страницы/шапки экранируется на сайте и
# в мини-аппе, поэтому с тегами там появлялись литералы «<b>».
INTRO_PLAIN = (
    "🔐 Подключение через VLESS (Reality)\n\n"
    "VLESS — современный протокол с поддержкой Reality (маскировка под TLS).\n"
    "Работает быстрее и стабильнее WDTT, не требует VK-хешей.\n\n"
    "Выберите вашу платформу:"
)


def platforms_with_windows_app(url: str) -> dict:
    """Копия PLATFORMS с подменённой ссылкой на приложение Windows."""
    import copy
    plats = copy.deepcopy(PLATFORMS)
    plats["windows"]["app_url"] = url
    return plats


def step_img(name: str) -> str:
    """URL картинки шага (статика сайта/приложения)."""
    return f"/static/instr/{name}"
