#!/usr/bin/env bash
# =============================================================================
# update.sh – Скрипт обновления WDTT‑Telegram‑bot на VPS.
#   * Синхронизация с удалённым репозиторием (Git)
#   * Обновление зависимостей
#   * Перезапуск systemd‑службы (или инструкция manual)
#   * Помощные сообщения и цветной вывод
# =============================================================================
set -euo pipefail

# ---------- Helper functions ----------
log()   { echo -e "\e[1;34m[+] $*\e[0m"; }
warn()  { echo -e "\e[1;33m[?] $*\e[0m"; }
err()   { echo -e "\e[1;31m[!] $*\e[0m" >&2; }

# ---------- Configuration ----------
BRANCH="${BRANCH:-}"          # автоопределяется, если пусто
SERVICE="${SERVICE:-bot}"      # имя systemd‑службы
REPO_URL="${REPO_URL:-}"       # при первом запуске обязательна
REMOTE_NAME="origin"

# ---------- Sanity checks ----------
command -v git >/dev/null 2>&1 || { err "git не установлен (apt install git)"; exit 1; }

# ---------- Ensure we are in the script directory ----------
SCRIPT_DIR="$(dirname "$(readlink -f "$0")")"
cd "$SCRIPT_DIR"

# ---------- Initialize / update Git remote ----------
if [ ! -d .git ]; then
    if [ -z "$REPO_URL" ]; then
        err "Директория не является Git‑репозиторием и URL репозитория не задан."
        err "Пример: REPO_URL=https://github.com/ВАШ_ЛОГИН/ВАШ_РЕПО.git ./update.sh"
        exit 1
    fi
    log "Инициализируем Git‑репозиторий и привязываем к $REPO_URL"
    git init -q
    git remote add "$REMOTE_NAME" "$REPO_URL"
else
    # If a new URL is explicitly supplied, override the existing remote
    if [ -n "$REPO_URL" ]; then
        git remote set-url "$REMOTE_NAME" "$REPO_URL"
    fi
fi

git remote get-url "$REMOTE_NAME" >/dev/null 2>&1 || { err "Remote $REMOTE_NAME не настроен"; exit 1; }
git ls-remote --heads "$REMOTE_NAME" >/dev/null 2>&1 || { err "Не удалось обратиться к remote‑репозиторию"; exit 1; }

# ---------- Determine branch ----------
if [ -z "$BRANCH" ]; then
    if git ls-remote --heads "$REMOTE_NAME" main | grep -q .; then
        BRANCH="main"
    elif git ls-remote --heads "$REMOTE_NAME" master | grep -q .; then
        BRANCH="master"
    else
        err "В удалённом репозитории нет веток main/master. Укажите ветку через BRANCH= имя"
        exit 1
    fi
fi

log "Обновляем ветку $BRANCH из $REMOTE_NAME"
git fetch --prune "$REMOTE_NAME"
git reset --hard "origin/$BRANCH"
log "Обновлённый коммит: $(git log -1 --format='%h %s (%ci)')"

# ---------- Python virtual environment ----------
if [ -x ".venv/bin/python" ]; then
    PYTHON_BIN=".venv/bin/python"
elif [ -x ".venv/Scripts/python.exe" ]; then
    PYTHON_BIN=".venv/Scripts/python.exe"
else
    # Поиск любой из поддерживаемых Python (3.10‑3.13)
    for ver in 3.13 3.12 3.11 3.10; do
        if command -v "python$ver" >/dev/null 2>&1; then
            PYTHON_BIN="python$ver"
            break
        fi
    done
    if [ -z "$PYTHON_BIN" ]; then
        err "Python 3.10‑3.13 не найден. Установите одну из поддерживаемых версий."
        exit 1
    fi
    log "Создаём виртуальное окружение (.venv) на $PYTHON_BIN"
    python3 -m venv .venv
    PYTHON_BIN=".venv/bin/python"
fi

log "Активируем виртуальное окружение"
source "$PYTHON_BIN/python -m venv --help" >/dev/null || true

log "Обновляем pip и зависимости"
"$PYTHON_BIN" -m pip install --quiet --upgrade pip
"$PYTHON_BIN" -m pip install --quiet --upgrade -r requirements.txt

# ---------- Restart systemd service ----------
if command -v systemctl >/dev/null 2>&1 && \
   systemctl list-unit-files --type=service 2>/dev/null | grep -q "^${SERVICE}.service"; then
    log "Перезапускаем systemd‑службу $SERVICE"
    sudo systemctl daemon-reload || true
    sudo systemctl restart "$SERVICE"
    sudo systemctl status "$SERVICE" --no-pager || true
    log "Логи сервиса: journalctl -u $SERVICE -f"
else
    warn "Systemd‑служба '$SERVICE' не найдена. Запустите бота вручную:"
    echo "  $PYTHON_BIN bot.py"
    echo " (или создайте unit‑файл согласно инструкциям в README)"
fi

log "=== Обновление завершено ==="
exit 0