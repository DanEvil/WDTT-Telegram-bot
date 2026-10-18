#!/usr/bin/env bash
# Обновление бота WDTT-Telegram-bot на VDS из GitHub:
# git-синхронизация + зависимости + перезапуск systemd-сервиса.
#
# ВАЖНО: локальные изменения в отслеживаемых файлах будут ПЕРЕЗАПИСАНЫ
# версией из репозитория. Файлы .env и bot.db перечислены в .gitignore,
# поэтому не отслеживаются и сохраняются.
#
# Использование:
#   ./update.sh                        # ветка main/master (автоопределение)
#   BRANCH=dev ./update.sh             # принудительно ветка dev
#   SERVICE=my-bot ./update.sh         # другое имя systemd-сервиса (по умолчанию bot)
#
# Если папка на VDS ещё не git-репозиторий (например, залита архивом),
# при ПЕРВОМ запуске укажите адрес своего репозитория:
#   REPO_URL=https://github.com/ВАШ_ЛОГИН/ВАШ_РЕПО.git ./update.sh
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")"

BRANCH="${BRANCH:-}"
SERVICE="${SERVICE:-bot}"
REPO_URL="${REPO_URL:-}"

command -v git >/dev/null 2>&1 || { echo "ОШИБКА: git не установлен (apt install git)"; exit 1; }

# Привязываем папку к репозиторию, если это ещё не git-репозиторий.
# Remote перезаписываем ТОЛЬКО когда REPO_URL задан явно — иначе используем
# тот, что уже настроен (не подменяем его молча на дефолтный).
if [ ! -d .git ]; then
  if [ -z "$REPO_URL" ]; then
    echo "ОШИБКА: папка не связана с git (нет .git). Укажите адрес репозитория:"
    echo "  REPO_URL=https://github.com/ВАШ_ЛОГИН/ВАШ_РЕПО.git ./update.sh"
    exit 1
  fi
  echo "==> git init + привязка к $REPO_URL"
  git init -q
  git remote add origin "$REPO_URL"
elif [ -n "$REPO_URL" ]; then
  if git remote get-url origin >/dev/null 2>&1; then
    git remote set-url origin "$REPO_URL"
  else
    git remote add origin "$REPO_URL"
  fi
fi

if ! git remote get-url origin >/dev/null 2>&1; then
  echo "ОШИБКА: remote origin не настроен. Укажите REPO_URL:"
  echo "  REPO_URL=https://github.com/ВАШ_ЛОГИН/ВАШ_РЕПО.git ./update.sh"
  exit 1
fi

if ! git ls-remote --heads origin >/dev/null 2>&1; then
  echo "ОШИБКА: не удалось обратиться к репозиторию (проверьте REPO_URL и сеть)"
  exit 1
fi

# Ветка: заданная явно, иначе main, иначе master
if [ -z "$BRANCH" ]; then
  if git ls-remote --heads origin main | grep -q .; then
    BRANCH="main"
  elif git ls-remote --heads origin master | grep -q .; then
    BRANCH="master"
  else
    echo "ОШИБКА: в репозитории нет ветки main/master — укажите BRANCH=<имя>"
    exit 1
  fi
fi

echo "==> git fetch origin --prune"
git fetch origin --prune

echo "==> сброс кода на origin/$BRANCH (.env и bot.db сохраняются)"
git reset --hard "origin/$BRANCH"
echo "Код на коммите: $(git log -1 --format='%h %s (%ci)' "origin/$BRANCH")"

# Python: сначала .venv проекта, иначе системный Python 3.10-3.13.
# Явно перебираем версии 3.10-3.13: aiogram 3 требует aiohttp<3.13
# и не работает на Python 3.14/3.15.
if [ -x ".venv/bin/python" ]; then
  PY=".venv/bin/python"
elif [ -x ".venv/Scripts/python.exe" ]; then
  PY=".venv/Scripts/python.exe"
else
  SYS_PY="$(command -v python3.13 || command -v python3.12 || command -v python3.11 || command -v python3.10 || command -v python3 || command -v python || true)"
  if [ -z "$SYS_PY" ]; then
    echo "ОШИБКА: python не найден. Установите Python 3.10-3.13."
    exit 1
  fi
  echo "==> создание виртуального окружения .venv ($SYS_PY)"
  "$SYS_PY" -m venv .venv
  PY=".venv/bin/python"
fi

echo "==> обновление зависимостей ($PY)"
"$PY" -m pip install --quiet --upgrade pip
"$PY" -m pip install --upgrade -r requirements.txt

echo "==> перезапуск бота"
sudo_cmd() { if [ "$(id -u)" -eq 0 ]; then "$@"; else sudo "$@"; fi; }
if command -v systemctl >/dev/null 2>&1 && \
   systemctl list-unit-files --type=service 2>/dev/null | grep -q "^${SERVICE}.service"; then
  sudo_cmd systemctl restart "$SERVICE"
  sudo_cmd systemctl --no-pager status "$SERVICE" || true
  echo "Готово. Логи: journalctl -u $SERVICE -f"
else
  echo "⚠️  systemd-сервис '$SERVICE' не найден — перезапустите вручную:"
  echo "    $PY bot.py"
  echo "    (unit-файл из раздела 6 README обычно лежит в /etc/systemd/system/$SERVICE.service)"
fi
