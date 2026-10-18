#!/bin/bash
# Развёртывание WDTT-бота в Docker на Linux-сервере (VPS).
# Запуск:    sudo ./deploy.sh            (REPO_URL можно переопределить: REPO_URL=... ./deploy.sh)
# Настройки: sudo ./deploy.sh --configure   (открыть окно настроек позже)
# Без вопросов: NONINTERACTIVE=1 ./deploy.sh  (требуется готовый .env)
set -e

PROJECT_DIR="${PROJECT_DIR:-/opt/wdtt-bot}"
REPO_URL="${REPO_URL:-https://github.com/DanEvil/WDTT-Telegram-bot.git}"
BRANCH="${BRANCH:-main}"
CONFIGURE_ONLY=0
[ "${1:-}" = "--configure" ] && CONFIGURE_ONLY=1

COMPOSE=""
DETECTED_COMPOSE=0

detect_compose() {
    if docker compose version >/dev/null 2>&1; then
        COMPOSE="docker compose"
    elif command -v docker-compose >/dev/null 2>&1; then
        COMPOSE="docker-compose"
    else
        return 1
    fi
    DETECTED_COMPOSE=1
    return 0
}

# ---------- окно настроек ----------

# значение KEY из существующего .env (пусто, если файла/ключа нет)
env_val() {
    [ -f "$PROJECT_DIR/.env" ] && grep -E "^$1=" "$PROJECT_DIR/.env" | head -n1 | cut -d= -f2-
}

WT_GUI=0
setup_ui() {
    if command -v whiptail >/dev/null 2>&1 && [ -t 0 ] && [ -t 1 ]; then
        WT_GUI=1
    else
        WT_GUI=0
    fi
}

# wt_input TITLE DEFAULT REQUIRED(bool) MASKED(bool) -> вывод: значение
wt_input() {
    local title="$1" def="$2" req="$3" mask="$4" val=""
    while :; do
        if [ "$WT_GUI" = "1" ]; then
            local opts=(--title "Настройки WDTT-бота" --backtitle "Установка WDTT-бота")
            if [ "$mask" = "1" ]; then
                val=$(whiptail "${opts[@]}" --passwordbox "  $title" 9 66 "$def" 3>&1 1>&2 2>&3)
            else
                val=$(whiptail "${opts[@]}" --inputbox "  $title" 9 66 "$def" 3>&1 1>&2 2>&3)
            fi
            local rc=$?
            if [ $rc -ne 0 ]; then
                echo "Настройки прерваны." >&2
                exit 1
            fi
        else
            local hint=""
            [ -n "$def" ] && hint=" [$def]"
            if [ "$mask" = "1" ]; then
                read -rp "$title (скрытый ввод)$hint: " val
                [ -z "$val" ] && val="$def"
            else
                read -rp "$title$hint: " val
            fi
        fi
        if [ "$req" = "1" ] && [ -z "$val" ]; then
            if [ "$WT_GUI" = "1" ]; then
                whiptail --title "Настройки WDTT-бота" --msgbox "  Обязательное поле, введите значение." 8 60
            else
                echo "Ошибка: это поле обязательно." >&2
            fi
            continue
        fi
        printf '%s' "$val"
        return 0
    done
}

# wt_confirm TITLE -> 0 (да) / 1 (нет)
wt_confirm() {
    if [ "$WT_GUI" = "1" ]; then
        whiptail --title "Настройки WDTT-бота" --yesno "  $1" 9 66
        return $?
    fi
    read -rp "$1 [Y/n]: " a
    case "${a,,}" in n|no) return 1 ;; *) return 0 ;; esac
}

run_settings_wizard() {
    setup_ui
    cd "$PROJECT_DIR"

    # $2 = переменная .env, $3 = значение по умолчанию
    def() {
        local x
        x=$(env_val "$2")
        printf '%s' "${x:-$3}"
    }

    local token admin_ids yk_shop yk_secret return_url wdtt_url wdtt_user wdtt_pass webhook_enabled webhook_port
    token=$(wt_input "TELEGRAM_BOT_TOKEN — токен бота от @BotFather:" "$(def x TELEGRAM_BOT_TOKEN)" 1 0)
    admin_ids=$(wt_input "ADMIN_IDS — Telegram ID администраторов через запятую:" "$(def x ADMIN_IDS)" 1 0)
    yk_shop=$(wt_input "YOOKASSA_SHOP_ID — ID магазина в ЮKassa:" "$(def x YOOKASSA_SHOP_ID)" 1 0)
    yk_secret=$(wt_input "YOOKASSA_SECRET_KEY — секретный ключ ЮKassa (sk_...):" "$(def x YOOKASSA_SECRET_KEY)" 1 1)
    return_url=$(wt_input "RETURN_URL — куда ведёт чек «оплатить» (например, https://t.me/YourBot):" "$(def x RETURN_URL https://t.me/YourBot)" 1 0)
    wdtt_url=$(wt_input "WDTT_BASE_URL — адрес панели WDTT (например, https://1.2.3.4:2860/wdtt):" "$(def x WDTT_BASE_URL)" 1 0)
    wdtt_user=$(wt_input "WDTT_USERNAME — логин панели:" "$(def x WDTT_USERNAME)" 1 0)
    wdtt_pass=$(wt_input "WDTT_PASSWORD — пароль панели:" "$(def x WDTT_PASSWORD)" 1 1)
    webhook_enabled=$(wt_input "WEBHOOK_ENABLED — платить через webhook? false/true:" "$(def x WEBHOOK_ENABLED false)" 1 0)
    webhook_port=$(wt_input "WEBHOOK_PORT — порт webhook (нужно только при true):" "$(def x WEBHOOK_PORT 8080)" 0 0)

    local summary
    summary=$'Проверьте введённые данные:\n'
    summary+="TELEGRAM_BOT_TOKEN: ${token:0:8}***"$'\n'
    summary+="ADMIN_IDS: $admin_ids"$'\n'
    summary+="YOOKASSA_SHOP_ID: $yk_shop"$'\n'
    summary+="YOOKASSA_SECRET_KEY: ${yk_secret:0:3}***"$'\n'
    summary+="RETURN_URL: $return_url"$'\n'
    summary+="WDTT_BASE_URL: $wdtt_url"$'\n'
    summary+="WDTT_USERNAME: $wdtt_user"$'\n'
    summary+="WDTT_PASSWORD: ${wdtt_pass:0:1}***"$'\n'
    summary+="WEBHOOK_ENABLED: $webhook_enabled"$'\n'
    summary+="WEBHOOK_PORT: $webhook_port"

    if ! wt_confirm "$summary"; then
        echo "Сохранение отменено — настройки не изменены." >&2
        return 1
    fi

    cat <<EOF > "$PROJECT_DIR/.env"
TELEGRAM_BOT_TOKEN=$token
ADMIN_IDS=$admin_ids
YOOKASSA_SHOP_ID=$yk_shop
YOOKASSA_SECRET_KEY=$yk_secret
RETURN_URL=$return_url
WDTT_BASE_URL=$wdtt_url
WDTT_USERNAME=$wdtt_user
WDTT_PASSWORD=$wdtt_pass
WDTT_VERIFY_SSL=false
WEBHOOK_ENABLED=$webhook_enabled
WEBHOOK_HOST=0.0.0.0
WEBHOOK_PORT=$webhook_port
WEBHOOK_PATH=/yookassa
EOF
    chmod 600 "$PROJECT_DIR/.env"
    echo ".env сохранён: $PROJECT_DIR/.env"
    return 0
}

# ---------- основной сценарий ----------

if [ "$CONFIGURE_ONLY" = "1" ]; then
    cd "$PROJECT_DIR" 2>/dev/null || { echo "Проект не установлен: $PROJECT_DIR не найден." >&2; exit 1; }
    [ -f .env ] && chmod 600 .env
    if run_settings_wizard; then
        if detect_compose && docker ps --format '{{.Names}}' | grep -q wdtt-bot; then
            echo "Перезапускаю контейнер с новыми настройками..."
            $COMPOSE up -d
        else
            echo "Контейнер не запущен — примените настройки: sudo ./deploy.sh"
        fi
    fi
    exit 0
fi

echo "--- 1. Проверка Docker ---"
if ! command -v docker >/dev/null 2>&1; then
    echo "Docker не найден, устанавливаю..."
    curl -fsSL https://get.docker.com | sh
    systemctl enable --now docker
fi
if docker compose version >/dev/null 2>&1; then
    COMPOSE="docker compose"
else
    COMPOSE="docker-compose"
fi

echo "--- 2. Клонирование репозитория ($BRANCH) ---"
if [ -d "$PROJECT_DIR/.git" ]; then
    cd "$PROJECT_DIR"
    git fetch origin
    git reset --hard "origin/$BRANCH"
else
    git clone --branch "$BRANCH" "$REPO_URL" "$PROJECT_DIR"
    cd "$PROJECT_DIR"
fi

echo "--- 3. Окно настроек ---"
if [ -f .env ] && [ "${NONINTERACTIVE:-0}" = "0" ] && [ -t 0 ]; then
    # .env уже есть — всё равно открываем настройки (значения подставлены).
    if wt_confirm "Найден существующий .env. Открыть окно настроек?"; then
        run_settings_wizard
    else
        echo ".env не изменён: $PROJECT_DIR/.env"
    fi
elif [ -f .env ]; then
    echo ".env уже существует ($PROJECT_DIR/.env) — неинтерактивный режим, пропускаю."
else
    run_settings_wizard
fi

echo "--- 4. Сборка и запуск контейнера ---"
$COMPOSE up -d --build

echo "-----------------------------------------------------------"
echo "РАЗВЁРТЫВАНИЕ ЗАВЕРШЕНО!"
echo "Статус:        $COMPOSE ps"
echo "Логи:          $COMPOSE logs -f wdtt-bot"
echo "Перезапуск:    $COMPOSE restart"
echo "Обновление:    git pull && $COMPOSE up -d --build"
echo "Настройки:     sudo ./deploy.sh --configure"
echo "-----------------------------------------------------------"
if grep -q '^WEBHOOK_ENABLED=true' "$PROJECT_DIR/.env" 2>/dev/null; then
    echo "Внимание: WEBHOOK_ENABLED=true, но порт в docker-compose.yml"
    echo "закомментирован — раскомментируйте блок ports и выполните"
    echo "$COMPOSE up -d"
fi
