# K.O. Gateway — iOS / iPadOS

Нативное приложение экосистемы K.O. Gateway для всех Apple-устройств
(iPhone, iPad, iPod touch) — по мотивам Telegram mini-app.

Внутри `WKWebView` открывается сайт `https://auth.ko-gateway.ru`: вход через
Telegram, профиль с подпиской (тариф, срок, устройства онлайн, трафик),
ссылки подключения, оплата, рефералы, админ-панель.

Ссылки подключения `wdtt://` / `qwdtt://` открываются во внешнем клиенте
WDTT, а если клиент не установлен — копируются в буфер обмена.
Ссылки `t.me/...` открываются в Telegram.

## Универсальность (все Apple-девайсы)

- `TARGETED_DEVICE_FAMILY = "1,2"` → iPhone (1) и iPad (2), один target
- `UIDeviceFamily` в Info.plist заполняется Xcode автоматически
- Ориентации: iPhone — портрет + ландшафт, iPad — все четыре
- iPad multitasking включён (не `UIRequiresFullScreen`)
- Минимальная версия: iOS 15.0
- Универсальный бинарник: arm64 (устройства) + x86_64/arm64 (симулятор)

## Сборка

Требуется **macOS + Xcode 15+** (на Windows/iOS-сборка невозможна —
нужен Apple SDK и codesign).

```bash
open KOGateway.xcodeproj
# выбрать схему KOGateway → iPhone/iPad
# Product → Run (⌘R) — запуск на симуляторе/устройстве
# Product → Archive → Distribute App — сборка .ipa для TestFlight/App Store
```

Для установки на устройство без магазина: Xcode → Signing & Capabilities →
выбрать свою Team (Apple ID), подключить iPhone и нажать Run.

## Структура

```
IOS/
├── KOGateway.xcodeproj/project.pbxproj   (target KOGateway, iPhone+iPad)
├── KOGateway/
│   ├── AppDelegate.swift                 (точка входа, сцены)
│   ├── SceneDelegate.swift               (окно + навигация)
│   ├── WebViewController.swift           (WKWebView, обработка wdtt://, qwdtt://, t.me)
│   ├── Info.plist                        (сцены, схемы URL, ориентации, ATS)
│   ├── Base.lproj/LaunchScreen.storyboard
│   └── Assets.xcassets/                  (иконка 1024px + акцент)
└── README.md
```
