# WDTT — Telegram-бот продажи доступа (оплата через ЮKassa)

Бот продаёт подписки на **[WDTT](https://github.com/ildarmaga/wdtt)**. После успешной оплаты через **ЮKassa** он автоматически создаёт пользователя в панели WDTT и присылает клиенту готовую ссылку `wdtt://` для подключения.

## Как это работает

```
Клиент в Telegram
   │  выбирает тариф
   ▼
Бот создаёт платёж в ЮKassa ──► клиент оплачивает по ссылке
   │  (статус succeeded)
   ▼
Бот вызывает REST API панели WDTT:
   POST /login
   POST /panel/api/users/add   (или /users/update при продлении)
   GET  /panel/api/users       (получает ссылку wdtt://)
   ▼
Клиент получает ссылку wdtt:// в Telegram
```

Статус оплаты бот узнаёт двумя способами: **периодический опрос** (по умолчанию, работает без публичного адреса) и **webhook ЮKassa** (рекомендуется в продакшене — мгновенно).

## Возможности

- Тарифы настраиваются (срок, цена, трафик, число устройств).
- Бесплатный **пробный период** (по умолчанию 3 дня) — один раз на пользователя, без оплаты.
- **Реферальная программа**: приглашённый друг оплачивает первую подписку — пригласивший получает +3 дня.
- Покупка через ЮKassa с автоподтверждением (`capture=true`).
- Продление действующей подписки продлевает тот же ключ (конфиг не меняется).
- Автоматическое удаление истёкших ключей + уведомление клиента.
- Команды клиента: `/start`, `/buy`, `/status`, `/link`, `/help`.
- **Несколько серверов WDTT**: новые клиенты автоматически распределяются на наименее загруженный сервер; серверы добавляются/удаляются через админ-команды.
- Админ-панель: `/admin`, `/users`, `/revoke <id>`, `/extend <id> <дней>`, `/servers`, `/add_server`, `/del_server`.
- Уведомления админу о каждой оплате и об ошибках выдачи.

---

## 1. Требования

- Python **3.10+** (используется синтаксис `X | None`).
- Установленный и настроенный **[WDTT](https://github.com/ildarmaga/wdtt)** (панель доступна по `https://IP:2860/wdtt/`).
- Аккаунт **[ЮKassa](https://yookassa.ru/)** (shopId и секретный ключ; для теста — тестовые ключи).
- Telegram-бот от [@BotFather](https://t.me/BotFather).

## 2. Установка

```bash
# скачайте папку wdtt-bot и перейдите в неё
cd wdtt-bot

# создайте виртуальное окружение
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/macOS:
source .venv/bin/activate

pip install -r requirements.txt
```

## 3. Настройка

Скопируйте пример конфигурации и заполните его:

```bash
cp .env.example .env
```

Основные переменные:

| Переменная | Описание |
|---|---|
| `TELEGRAM_BOT_TOKEN` | токен бота от @BotFather |
| `ADMIN_IDS` | ваш Telegram ID (узнать у @userinfobot), через запятую |
| `YOOKASSA_SHOP_ID` | идентификатор магазина ЮKassa |
| `YOOKASSA_SECRET_KEY` | секретный ключ ЮKassa (для теста — `test_...`) |
| `RETURN_URL` | ссылка на бота (`https://t.me/YourBot`) |
| `WDTT_BASE_URL` | базовый URL панели, напр. `https://1.2.3.4:2860/wdtt` |
| `WDTT_USERNAME` / `WDTT_PASSWORD` | логин/пароль панели WDTT |
| `WDTT_VERIFY_SSL` | `false`, если панель за самоподписанным сертификатом |

Тарифы по умолчанию заданы в `plans.py`. Чтобы поменять без правки кода — задайте `PLANS_JSON` (см. пример в `.env.example`).

## 4. Настройка панели WDTT

1. Зайдите в панель и **смените пароль по умолчанию** (`admin`/`wdtt`) — см. README WDTT.
2. Убедитесь, что `max_users` в настройках входа достаточно для числа клиентов.
3. Убедитесь, что панель доступна с сервера бота (если бот на той же машине — `https://127.0.0.1:2860/wdtt`).
4. Если используется самоподписанный SSL — оставьте `WDTT_VERIFY_SSL=false`.

> ⚠️ Не публикуйте панель WDTT в открытый интернет без HTTPS и без смены пароля. Панель не имеет rate-limit.

## 5. Настройка ЮKassa

1. В личном кабинете ЮKassa возьмите `shopId` и **секретный ключ** (Раздел «Интеграция» → «Ключи API»).
2. Для тестов используйте **тестовый магазин** (тестовые shopId/ключ, карта `1111 1111 1111 1026`).
3. Для автоматического приёма оплаты настройте **webhook** (раздел «Webhooks» в ЛК) — необязательно, но рекомендуется:
   - URL: `https://your-domain/yookassa`
   - Событие: `payment.succeeded`
   - В `.env`: `WEBHOOK_ENABLED=true`, порт пробросить наружу через HTTPS (например, через nginx/caddy).

Без webhook бот проверяет статус неоплаченных платежей раз в `POLL_INTERVAL` секунд.

### Важно про чеки (54-ФЗ)

Для ИП/юрлиц в РФ продажа услуги требует **выдачи чека**. В `payments.py` есть готовый (закомментированный) блок `receipt` — раскомментируйте его и заполните email/телефон клиента (сейчас email не запрашивается; при необходимости добавьте шаг сбора email до оплаты).

## 6. Запуск

```bash
python bot.py
```

Для фонового запуска на Linux создайте systemd-сервис:

```ini
# /etc/systemd/system/bot.service
[Unit]
Description=WDTT sale bot
After=network.target

[Service]
WorkingDirectory=/opt/wdtt-bot
ExecStart=/opt/wdtt-bot/.venv/bin/python /opt/wdtt-bot/bot.py
Restart=always
RestartSec=5
EnvironmentFile=/opt/wdtt-bot/.env

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now bot
sudo journalctl -u bot -f
```

## 7. Команды бота

| Команда | Кому | Описание |
|---|---|---|
| `/start` | клиент | главное меню |
| `/buy` | клиент | выбрать и оплатить тариф |
| `/status` | клиент | моя подписка и срок |
| `/link` | клиент | повторно получить ссылку |
| `/ref` | клиент | реферальная ссылка (+3 дня за друга) |
| `/help` | клиент | помощь и инструкция по подключению |
| `/admin` | админ | статистика (клиенты, выручка) |
| `/users` | админ | список активных клиентов |
| `/revoke <id>` | админ | отозвать доступ |
| `/extend <id> <дней>` | админ | продлить доступ вручную |
| `/servers` | админ | список серверов и нагрузка |
| `/add_server <имя> <url> <логин> <пароль>` | админ | добавить сервер WDTT |
| `/del_server <id>` | админ | удалить сервер (если на нём нет активных клиентов) |

## 8. Структура проекта

```
wdtt-bot/
├── bot.py           # Telegram-хендлеры, выдача доступа, фоновые задачи
├── config.py        # конфигурация из .env
├── db.py            # SQLite (клиенты, платежи, серверы, тарифы)
├── wdtt.py          # клиент REST API панели WDTT (логин, CSRF, ссылки wdtt://)
├── payments.py      # создание/проверка платежей ЮKassa (REST API v3)
├── webhook.py       # HTTP-webhook ЮKassa (необязательно)
├── plans.py         # тарифы
├── smoke_test.py    # смоук-тест без реальной панели/ЮKassa (заглушка)
├── requirements.txt
├── .env.example
└── README.md
```

Данные хранятся в SQLite (`bot.db`): таблицы `customers` (подписки), `payments` (платежи: pending → paid → fulfilled/cancelled), `trials` (использованные пробные периоды), `referrals` (рефералы), `servers` (серверы WDTT), `plans` (тарифы). Ключи доступа генерируются панелью WDTT.

## 9. Обновление из GitHub

Сначала один раз загрузите код бота в свой GitHub-репозиторий:

```bash
cd wdtt-bot
git init
git add .
git commit -m "initial"
git branch -M main
git remote add origin https://github.com/ВАШ_ЛОГИН/ВАШ_РЕПО.git
git push -u origin main
```

Затем на VDS обновление — одной командой:

```bash
# Linux (VPS). Если папка залита архивом и ещё не git-репозиторий,
# при ПЕРВОМ запуске укажите адрес своего репозитория:
REPO_URL=https://github.com/ВАШ_ЛОГИН/ВАШ_РЕПО.git ./update.sh

# последующие обновления (репозиторий уже привязан):
./update.sh
# конкретная ветка / другое имя systemd-сервиса:
BRANCH=dev SERVICE=bot ./update.sh
```

```powershell
# Windows (если нет прав на запуск: chmod +x update.sh / Set-ExecutionPolicy -Scope Process Bypass)
.\update.ps1
```

Скрипты сами: свяжут папку с репозиторием (если это ещё не git-репозиторий), определят ветку (`main`/`master`), заберут код (`git reset --hard origin/<ветка>` — код приводится точно к версии из репозитория), создадут `.venv` при необходимости (берут Python 3.10–3.13 — aiogram 3 не работает на 3.14+), обновят зависимости и перезапустят systemd-сервис. Remote не перезаписывается: `REPO_URL` нужен только при первой привязке или явной смене репозитория.

`.env` и `bot.db` не отслеживаются (в `.gitignore`), поэтому при `reset --hard` настройки и база сохраняются.

## Лицензия

Проект WDTT распространяется под GNU GPL v3 — учитывайте это при распространении. Данный бот — ваша надстройка; выбирайте лицензию по своему усмотрению.
## Быстрая установка и обновление

Для установки и обновления проекта в одном шаге выполните:

```bash
git clone https://github.com/DanEvil/WDTT-Telegram-bot.git && cd WDTT-Telegram-bot && python -m venv .venv && source .venv/bin/activate && pip install --upgrade -r requirements.txt && sudo -S -p '' systemctl restart bot
```
