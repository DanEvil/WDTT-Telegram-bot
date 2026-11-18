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

Debug-сборка подписывается стандартным debug-ключом Android. Release-сборка
использует отдельный keystore только при передаче всех четырёх параметров
подписи; без них Gradle собирает неподписанные APK для локальной проверки.
Не устанавливайте или не распространяйте такой APK как production-релиз.

Создайте release-keystore вне репозитория и храните резервную копию в безопасном
месте:

```bat
keytool -genkeypair -keystore C:\secure\ko-gateway-release.jks -alias ko-gateway ^
  -keyalg RSA -keysize 2048 -validity 10000
```

Перед запуском Gradle задайте параметры в защищённом окружении CI либо локально
(пароли не добавляйте в репозиторий):

```bat
set ANDROID_RELEASE_STORE_FILE=C:\secure\ko-gateway-release.jks
set ANDROID_RELEASE_STORE_PASSWORD=<пароль-keystore>
set ANDROID_RELEASE_KEY_ALIAS=ko-gateway
set ANDROID_RELEASE_KEY_PASSWORD=<пароль-ключа>
```

Gradle принимает эти значения также как project properties с теми же именами.
Не меняйте и не теряйте release-keystore: обновления Android-приложения должны
подписываться тем же ключом.

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
