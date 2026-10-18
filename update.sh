#!/usr/bin/env bash
# update.sh — обновление WDTT-Telegram-bot на сервере.
# Основной путь: Docker (git reset + docker compose up -d --build).
# Запасной путь: venv + systemd-сервис (если Docker не найден).
# Переменные: BRANCH (ветка), REPO_URL (первая привязка/смена репозитория),
#             SERVICE (имя systemd-сервиса для fallback-режима, по умолчанию bot).
set -euo pipefail

log()  { echo -e "\e[1;34m[+] $*\e[0m"; }
warn() { echo -e "\e[1;33m[?] $*\e[0m"; }
err()  { echo -e "\e[1;31m[!] $*\e[0m" >&2; }

BRANCH="${BRANCH:-}"
REPO_URL="${REPO_URL:-}"
SERVICE="${SERVICE:-bot}"

command -v git >/dev/null 2>&1 || { err "git не установлен (apt install git)"; exit 1; }

cd "$(dirname "$(readlink -f "$0")")"

# ---------- Git ----------
if [ ! -d .git ]; then
    if [ -z "$REPO_URL" ]; then
        err "Папка не является git-репозиторием и REPO_URL не задан."
        err "Пример: REPO_URL=https://github.com/ВАШ_ЛОГИН/ВАШ_РЕПО.git ./update.sh"
        exit 1
    fi
    log "Инициализируем git-репозиторий и привязываем к $REPO_URL"
    git init -q
    git remote add origin "$REPO_URL"
elif [ -n "$REPO_URL" ]; then
    git remote set-url origin "$REPO_URL"
fi

git remote get-url origin >/dev/null 2>&1 || { err "Remote origin не настроен"; exit 1; }
git ls-remote --heads origin >/dev/null 2>&1 || { err "Не удалось обратиться к репозиторию"; exit 1; }

if [ -z "$BRANCH" ]; then
    if git ls-remote --heads origin main | grep -q .; then
        BRANCH="main"
    elif git ls-remote --heads origin master | grep -q .; then
        BRANCH="master"
    else
        err "В удалённом репозитории нет веток main/master. Укажите ветку: BRANCH=<имя> ./update.sh"
        exit 1
    fi
fi

log "Обновляем ветку $BRANCH из origin"
git fetch --prune origin
git reset --hard "origin/$BRANCH"
log "Обновлённый коммит: $(git log -1 --format='%h %s (%ci)')"

# .env и база не отслеживаются (.gitignore) — reset --hard их не трогает.

# ---------- Docker ----------
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    if [ ! -f .env ]; then
        err ".env не найден — заполните его по .env.example и повторите."
        exit 1
    fi
    log "Пересобираем и перезапускаем контейнер"
    docker compose up -d --build
    docker compose ps
    log "Логи: docker compose logs -f wdtt-bot"
    log "=== Обновление завершено ==="
    exit 0
fi

# ---------- Fallback: venv + systemd ----------
warn "Docker не найден — обновляем в режиме venv + systemd."

if [ -x ".venv/bin/python" ]; then
    PYTHON_BIN=".venv/bin/python"
elif [ -x ".venv/Scripts/python.exe" ]; then
    PYTHON_BIN=".venv/Scripts/python.exe"
else
    PYTHON_BIN=""
    for ver in 3.13 3.12 3.11 3.10; do
        if command -v "python$ver" >/dev/null 2>&1; then
            PYTHON_BIN="python$ver"
            break
        fi
    done
    [ -n "$PYTHON_BIN" ] || {
        err "Python 3.10–3.13 не найден (aiogram 3 не работает на 3.14+)."
        exit 1
    }
    log "Создаём .venv на $PYTHON_BIN"
    "$PYTHON_BIN" -m venv .venv
    PYTHON_BIN=".venv/bin/python"
fi

log "Обновляем pip и зависимости"
"$PYTHON_BIN" -m pip install --quiet --upgrade pip
"$PYTHON_BIN" -m pip install --quiet --upgrade -r requirements.txt

if command -v systemctl >/dev/null 2>&1 && \
   systemctl list-unit-files --type=service 2>/dev/null | grep -q "^${SERVICE}.service"; then
    log "Перезапускаем systemd-службу $SERVICE"
    sudo systemctl daemon-reload || true
    sudo systemctl restart "$SERVICE"
    sudo systemctl status "$SERVICE" --no-pager || true
    log "Логи сервиса: journalctl -u $SERVICE -f"
else
    warn "Systemd-служба '$SERVICE' не найдена. Запустите бота вручную:"
    echo "  $PYTHON_BIN bot.py"
fi

log "=== Обновление завершено ==="
