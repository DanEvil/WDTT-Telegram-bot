# Happ через свой OpenFlux (без аренды RU-серверов и без Yandex Cloud)

Идея: OpenFlux даёт whitelisted-трубу (carrier Yandex Docs / Mail.ru — их IP в белых списках). Клиент OpenFlux умеет поднимать **локальный SOCKS5**, а на Android/Windows loopback общий для всех приложений. Поэтому Happ (ядро Xray) может ходить **поверх этого SOCKS** — весь трафик Happ уходит через openflux-туннель на наш exit-сервер.

## Android (телефон)

1. Приложение **OpenFluxAndroid** → импорт `openflux://...` ссылки из бота → режим **«Локальный SOCKS5»** (НЕ системный VPN, чтобы не было петли). Запомни порт (обычно `127.0.0.1:10808`).
   - Если carrier `yandex` просит капчу — пройди её встроенным браузером приложения (smartcaptcha).
2. В **Happ** добавь обычную ссылку:
   ```
   socks://127.0.0.1:10808#OpenFlux
   ```
   (порт — тот, что показывает OpenFluxAndroid) и включи VPN-режим Happ.
   - Xray-TUN в Happ заворачивает трафик в outbound `socks → 127.0.0.1:10808`, openflux гонит его через whitelisted carrier на наш сервер.
   - DNS в Happ пойдёт через системный DNS; если провайдер режет внешний DNS — в настройках Happ выстави DoH `https://dns.yandex.net/dns-query` (Yandex в белом списке).

## Windows/Linux (десктоп)

1. Клиент OpenFlux в режиме SOCKS:
   ```
   openflux --role=client --inbound=socks5 --socks5 127.0.0.1:1080 <openflux://link>
   ```
2. Happ Desktop → `custom-tunnel-config` = JSON из `happ-desktop.json` (поправь порт SOCKS при необходимости), core `xray`, `tun-type: xray`.

## Почему это работает под белыми списками

- Единственное «внешнее» соединение — carrier OpenFlux (Yandex Docs/Mail.ru), их IP whitelisted у всех операторов (см. датасет openlibrecommunity/twl).
- Наши серверы (HOSTKEY NL / NetCrafters FI) в белый список не входят, поэтому прямой vless на них под БС не ходит — для «чистого» vless в Happ нужен whitelisted HTTP-фронт (Yandex Cloud Serverless Container), а здесь обходимся своим openflux.

## Ограничение

OpenFlux — свой протокол, Happ его «не видит»: связка только через локальный SOCKS (loopback общий между приложениями). Цепочку vless→dialerProxy→SOCKS тоже можно (десктоп), но смысла нет — plain socks-link проще.
