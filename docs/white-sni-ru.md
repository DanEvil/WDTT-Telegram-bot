# Белые списки РФ: выбор SNI для VLESS+REALITY

В режиме «белых списков» мобильных операторов РФ DPI пропускает TCP/443 только к
whitelisted-доменам (проверка по SNI). Поэтому `realitySettings.dest/serverNames`
на сервере и параметр `sni=` в клиентских vless:// ссылках должны быть одним из
этих доменов — иначе handshake не проходит.

- Дефолт в этом проекте: **`ya.ru`** (см. `config.py` VLESS_SNI и
  `/etc/xray-vless/config.json` на сервере, `VLESS_SNI` в `/root/vless-params.txt`).
- Проверенные whitelisted-домены: `ya.ru`, `yandex.ru`, `yandex.net`, `rutube.ru`,
  `static.rutube.ru`, `gosuslugi.ru`, `sberbank.ru`, `sun6-21.userapi.com`,
  `api.vk.com`, `download.cdn.yandex.net`.
- Полный список доменов: `docs/white-domains-ru.txt`
  (источник: github.com/hxehex/russia-mobile-internet-whitelist, whitelist.txt).
- SNI-подписка с рабочими VLESS+REALITY конфигами под белые списки:
  github.com/SeamniZ/vpn-configs-for-russia → `WHITE-SNI-RU-all.txt`.

Роутинг на exit-ноде (белые списки РФ): `geoip:ru` + Apple-домены + DNS → `direct`,
всё остальное → `wdtt-out` (VPN через VK TURN). См. `routing.rules` в
`/etc/xray-vless/config.json`.
