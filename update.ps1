# update.ps1 — обновление WDTT-Telegram-bot на Windows.
# Основной путь: Docker Desktop (git reset + docker compose up -d --build).
# Запасной путь: venv (обновление зависимостей).
# Переменные: $env:BRANCH (ветка), $env:REPO_URL (первая привязка/смена репозитория).
$ErrorActionPreference = "Stop"

$Branch  = $env:BRANCH
$RepoUrl = $env:REPO_URL

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

function Write-Info { Write-Host "[+] $args" -ForegroundColor Cyan }
function Write-Warn { Write-Host "[?] $args" -ForegroundColor Yellow }
function Write-ErrorMsg { Write-Host "[!] $args" -ForegroundColor Red }

# ---------- Git ----------
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-ErrorMsg "git не установлен (https://git-scm.com)."
    exit 1
}

if (-not (Test-Path -PathType Container ".git")) {
    if (-not $RepoUrl) {
        Write-ErrorMsg "Папка не является git-репозиторием и REPO_URL не задан."
        Write-Host "Пример: `$env:REPO_URL = 'https://github.com/ВАШ_ЛОГИН/ВАШ_РЕПО.git'; .\update.ps1"
        exit 1
    }
    Write-Info "Инициализируем git-репозиторий и привязываем к $RepoUrl"
    git init -q
    git remote add origin $RepoUrl
}
elseif ($RepoUrl) {
    git remote set-url origin $RepoUrl
}

if (-not (git remote get-url origin 2>$null)) {
    Write-ErrorMsg "Remote origin не настроен. Укажите `$env:REPO_URL."
    exit 1
}
git ls-remote --heads origin > $null 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-ErrorMsg "Не удалось обратиться к репозиторию (проверьте сеть и REPO_URL)."
    exit 1
}

if (-not $Branch) {
    if (git ls-remote --heads origin main 2>$null) { $Branch = "main" }
    elseif (git ls-remote --heads origin master 2>$null) { $Branch = "master" }
    else {
        Write-ErrorMsg "В удалённом репозитории нет веток main/master. Укажите `$env:BRANCH."
        exit 1
    }
}

Write-Info "Обновляем ветку $Branch из origin"
git fetch --prune origin
git reset --hard "origin/$Branch"
Write-Info "Обновлённый коммит: $(git log -1 --format='%h %s (%ci)')"

# .env и база не отслеживаются (.gitignore) — reset --hard их не трогает.

# ---------- Docker ----------
$composeOk = $false
if (Get-Command docker -ErrorAction SilentlyContinue) {
    docker compose version > $null 2>&1
    if ($LASTEXITCODE -eq 0) { $composeOk = $true }
}

if ($composeOk) {
    if (-not (Test-Path ".env")) {
        Write-ErrorMsg ".env не найден — заполните его по .env.example и повторите."
        exit 1
    }
    Write-Info "Пересобираем и перезапускаем контейнер"
    docker compose up -d --build
    docker compose ps
    Write-Info "Логи: docker compose logs -f wdtt-bot"
    Write-Host "=== Обновление завершено ==="
    exit 0
}

# ---------- Fallback: venv ----------
Write-Warn "Docker не найден — обновляем зависимости в venv (запустите бота вручную)."

if (Test-Path ".venv\Scripts\python.exe") {
    $PythonBin = ".venv\Scripts\python.exe"
}
else {
    $found = $false
    foreach ($ver in @("3.13", "3.12", "3.11", "3.10")) {
        if (Get-Command "python$ver" -ErrorAction SilentlyContinue) {
            & "python$ver" -m venv .venv
            $found = $true
            break
        }
    }
    if (-not $found) {
        # последний вариант — py launcher с конкретной версией
        foreach ($ver in @("3.13", "3.12", "3.11", "3.10")) {
            & py "-$ver" --version > $null 2>&1
            if ($LASTEXITCODE -eq 0) {
                & py "-$ver" -m venv .venv
                $found = $true
                break
            }
        }
    }
    if (-not $found) {
        Write-ErrorMsg "Python 3.10–3.13 не найден (aiogram 3 не работает на 3.14+)."
        exit 1
    }
    $PythonBin = ".venv\Scripts\python.exe"
}

Write-Info "Обновляем pip и зависимости"
& $PythonBin -m pip install --quiet --upgrade pip
& $PythonBin -m pip install --quiet --upgrade -r requirements.txt

Write-Warn "Запустите бота вручную: $PythonBin bot.py"
Write-Host "=== Обновление завершено ==="
Exit 0
