# wdtt-socks — headless WDTT-клиент (VK TURN) с локальным SOCKS5

Зачем: VLESS-сервер (xray-vless на 78.17.16.23:443) должен выпускать трафик
**через WDTT/VK TURN** (обход белых списков), а не напрямую. Штатный
`pwdtt-client-linux-amd64` — GUI (Wails/GTK/WebKit), на headless-сервере падает
(`panic: failed to init GTK`), поэтому здесь headless-вариант на движке
`wg-turn-client/core` из репозитория `ildarmaga/pwdtt-client`.

Схема:

```
клиент → VLESS+Reality (78.17.16.23:443) → xray-vless
   → outbound socks 10.255.254.2:1080 (veth в netns wdttx)
   → SOCKS5 в netns (default route через TUN wdttx0)
   → TUN ↔ UDP 127.0.0.1:19000 ↔ ядро wg-turn-client
   → VK TURN relay → wdtt-сервер (DTLS 56000 / direct RAW 56003) → интернет
```

Ядро (core) работает на хосте и сам ходит в VK/peer по обычной маршрутизации —
петли нет: TUN живёт в отдельном netns, туда default route.

## Сборка

```bash
# на сервере: Go >= 1.25 (модуль клиента: go 1.25.0)
git clone --depth 1 https://github.com/ildarmaga/pwdtt-client /root/pwdtt-client
mkdir -p /root/pwdtt-client/client/cmd/wdtt-socks
cp main.go /root/pwdtt-client/client/cmd/wdtt-socks/main.go
cd /root/pwdtt-client/client
go build -buildvcs=false -o /usr/local/wdtt-socks/wdtt-socks ./cmd/wdtt-socks
```

Зависимости — только из `client/go.mod` (proxy.golang.org).

## Запуск

Нужна ссылка выделенного пользователя панели WDTT (`vless-exit`, бессрочный,
с vk_hash). Colon-формат: `wdtt://IP:dtls:wg_port:client_port:password:vk_hash`.

```bash
echo 'wdtt://78.17.16.23:56000:56001:9000:PASS:HASH' > /root/wdtt-exit-link.txt
systemctl enable --now wdtt-socks   # юнит в wdtt-socks.service
```

Сервис поднимает ядро, ждёт `raw_config` (IP клиента в туннеле), создаёт TUN в
netns `wdttx`, veth `veth-wdttx-h` (host 10.255.254.1/24) ↔ `veth-wdttx-n` (ns
10.255.254.2/24), default route в ns через TUN, и SOCKS5 на `10.255.254.2:1080`.

## xray-vless

`/etc/xray-vless/config.json`: outbound `socks → 10.255.254.2:1080` стоит первым
(default outbound), правила: `geoip:private → blocked`, `udp/53 → direct`.
Юнит `xray-vless.service` зависит от `wdtt-socks.service` (After/Wants).

## Pitfalls

- **Direct RAW порт = DTLS+3 (56003), а не wg_port из ссылки** (parts[2]=56001 —
  это WG-over-DTLS). `RawDirectPort: 0` в Config → авто DTLS+3. С 56001 —
  RAWCONF уходит на WG-порт, сервер молчит, `i/o timeout`.
- Catch-all правило `{"type":"field","outboundTag":...}` без полей — xray ругается
  `this rule has no effective fields`. Default outbound = первый в списке outbounds.
- SOCKS5 здесь TCP-only (CONNECT): UDP/QUIC через цепочку не ходит, браузеры
  фолбэкаются на TCP. UDP/53 ausdrücklich → direct.
- GUI-клиенту нужны `libayatana-appindicator3-1`, `libwebkit2gtk-4.1-0` и дисплей —
  headless проще на ядре, чем через xvfb.
- VK-куки панели должны быть валидны (`GET /panel/api/vk/status` → cookies_ok),
  иначе `vk/call/create` и TURN-креды не выдадутся.
