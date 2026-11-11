# K.O. Gateway — Android (APK)

Android-приложение экосистемы K.O. Gateway, по мотивам Telegram mini-app:
внутри WebView открывается сайт `https://auth.ko-gateway.ru` — вход через
Telegram, профиль с подпиской (тариф, срок, устройства онлайн, трафик),
ссылки подключения, оплата, рефералы, админ-панель.

Ссылки подключения `wdtt://` и `qwdtt://` перехватываются приложением:
открываются во внешнем клиенте WDTT, а если клиент не установлен —
копируются в буфер обмена. Ссылки `t.me/...` открываются в Telegram.

## Универсальная сборка (все устройства)

В `app/build.gradle` настроены ABI-splits:

- `app-universal-release.apk` — один APK на все архитектуры
- `app-arm64-v8a-release.apk`, `app-armeabi-v7a-release.apk`,
  `app-x86-release.apk`, `app-x86_64-release.apk` — раздельные APK

minSdk 24, targetSdk 34.

## Сборка на этом ПК

Инструменты уже установлены в `C:/tools` (JDK 17, Gradle 8.11.1, Android SDK):

```bat
set JAVA_HOME=C:\tools\jdk17\jdk-17
set ANDROID_HOME=C:\tools\android-sdk
C:\tools\gradle-8.11.1\bin\gradle -p C:\Users\nenas\WDTT-Telegram-bot\Android assembleRelease --no-daemon
```

Готовые APK: `Android/app/build/outputs/apk/release/`

На другой машине: установите JDK 17 + Android SDK (cmdline-tools,
`platforms;android-34`, `build-tools;34.0.0`), затем
`gradle wrapper` один раз (чтобы появился `gradlew`), далее
`./gradlew assembleRelease`.

## Подпись release

Сейчас release подписывается встроенным debug-ключом (`app/debug.keystore`
нужно создать: `keytool -genkeypair -keystore app/debug.keystore -alias androiddebugkey
-storepass android -keypass android -dname "CN=K.O. Gateway" -keyalg RSA -keysize 2048 -validity 10000`),
чтобы APK был устанавливаемым. Для публикации в Google Play замените
`signingConfig` в `app/build.gradle` на свой keystore.

## Структура

```
Android/
├── settings.gradle / build.gradle / gradle.properties
├── gradle/wrapper/gradle-wrapper.properties   (Gradle 8.11.1)
├── local.properties                           (sdk.dir — локально, не коммитить)
└── app/
    ├── build.gradle                           (AGP 8.7.3, Kotlin 2.0.21, splits ABI)
    └── src/main/
        ├── AndroidManifest.xml                (LAUNCHER + intent-filter wdtt://, qwdtt://)
        ├── java/com/kogateway/app/MainActivity.kt
        └── res/                               (иконки всех плотностей, тема, строки)
```
