# Обновление бота WDTT-Telegram-bot из GitHub (Windows / PowerShell).
#
# ВАЖНО: локальные изменения в отслеживаемых файлах будут ПЕРЕЗАПИСАНЫ.
# .env и bot.db перечислены в .gitignore и сохраняются.
#
# Использование:
#   .\update.ps1                       # ветка main/master (автоопределение)
#   $env:BRANCH = "dev"; .\update.ps1
#
# Если папка ещё не git-репозиторий (например, распакована из ZIP),
# при ПЕРВОМ запуске укажите адрес своего репозитория:
#   $env:REPO_URL = "https://github.com/ВАШ_ЛОГИН/ВАШ_РЕПО.git"; .\update.ps1
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$Branch  = $env:BRANCH
$RepoUrl = $env:REPO_URL

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Host "ОШИБКА: git не установлен (https://git-scm.com)" -ForegroundColor Red
    exit 1
}

# Привязываем папку к репозиторию, если это ещё не git-репозиторий.
# Remote перезаписываем ТОЛЬКО когда REPO_URL задан явно.
if (-not (Test-Path ".git")) {
    if (-not $RepoUrl) {
        Write-Host "ОШИБКА: папка не связана с git (нет .git). Укажите адрес репозитория:" -ForegroundColor Red
        Write-Host '  $env:REPO_URL = "https://github.com/ВАШ_ЛОГИН/ВАШ_РЕПО.git"; .\update.ps1'
        exit 1
    }
    Write-Host "==> git init + привязка к $RepoUrl"
    git init -q
    git remote add origin $RepoUrl
} elseif ($RepoUrl) {
    git remote get-url origin *> $null
    if ($LASTEXITCODE -eq 0) { git remote set-url origin $RepoUrl }
    else { git remote add origin $RepoUrl }
}

git remote get-url origin *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "ОШИБКА: remote origin не настроен. Укажите REPO_URL." -ForegroundColor Red
    exit 1
}

if (-not (git ls-remote --heads origin 2>$null)) {
    Write-Host "ОШИБКА: не удалось обратиться к репозиторию (проверьте REPO_URL и сеть)" -ForegroundColor Red
    exit 1
}

# Ветка: заданная явно, иначе main, иначе master
if (-not $Branch) {
    if (git ls-remote --heads origin main) { $Branch = "main" }
    elseif (git ls-remote --heads origin master) { $Branch = "master" }
    else {
        Write-Host "ОШИБКА: в репозитории нет ветки main/master — укажите `$env:BRANCH" -ForegroundColor Red
        exit 1
    }
}

Write-Host "==> git fetch origin --prune"
git fetch origin --prune
if ($LASTEXITCODE -ne 0) { exit 1 }

Write-Host "==> сброс кода на origin/$Branch (.env и bot.db сохраняются)"
git reset --hard "origin/$Branch"
git log -1 --format="Код на коммите: %h %s (%ci)"

# Python: сначала .venv проекта, иначе системный Python 3.10-3.13
# (aiogram 3 требует aiohttp<3.13 и не работает на Python 3.14+).
if (Test-Path ".venv\Scripts\python.exe") {
    $py = ".venv\Scripts\python.exe"
} else {
    $sysPy = $null
    foreach ($c in @("python3.13", "python3.12", "python3.11", "python3.10", "python3", "python", "py")) {
        if (Get-Command $c -ErrorAction SilentlyContinue) { $sysPy = $c; break }
    }
    if (-not $sysPy) {
        Write-Host "ОШИБКА: python не найден. Установите Python 3.10-3.13." -ForegroundColor Red
        exit 1
    }
    Write-Host "==> создание виртуального окружения .venv ($sysPy)"
    & $sysPy -m venv .venv
    if ($LASTEXITCODE -ne 0) { exit 1 }
    $py = ".venv\Scripts\python.exe"
}

Write-Host "==> обновление зависимостей"
& $py -m pip install --upgrade -r requirements.txt
if ($LASTEXITCODE -ne 0) {
    Write-Host "ОШИБКА установки зависимостей. Требуется Python 3.10-3.13 (aiogram 3 несовместим с 3.14+)." -ForegroundColor Red
    exit 1
}

Write-Host "Готово. Запуск бота: $py bot.py"
