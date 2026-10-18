# =============================================================================
# update.ps1 – PowerShell‑скрипт обновления WDTT‑Telegram‑bot на Windows‑сервере
#   * Синхронизация с Git‑репозиторием
#   * Обновление зависимостей
#   * Перезапуск systemd‑службы (или инструкция manual)
#   * Помощные сообщения и цветной вывод
# =============================================================================
# Устанавливаем параметры, которые можно переопределять через переменные окружения
$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent -Path $MyInvocation.MyCommand.Path
Set-Location -Path $ScriptDir

$Branch   = $env:BRANCH   # optional, auto‑detect if $null
$RepoUrl  = $env:REPO_URL # required on first run
$Service  = $env:SERVICE  # name of the systemd service (default: bot)

# ---------- Helper functions ----------
function Write-Info  { Write-Host "[+] $PSItem" -ForegroundColor Cyan }
function Write-Warn  { Write-Host "[?] $PSItem" -ForegroundColor Yellow }
function Write-ErrorMsg { Write-Host "[!] $PSItem" -ForegroundColor Red }

# ---------- Sanity checks ----------
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-ErrorMsg "git не установлен (https://git-scm.com)."
    exit 1
}

# ---------- Initialize / update Git remote ----------
if (-not (Test-Path -PathType Container -Path ".git")) {
    if (-not $RepoUrl) {
        Write-ErrorMsg "Папка не является репозиторием Git и URL репозитория не задан."
        Write-Host "Пример: `$env:REPO_URL = 'https://github.com/ВАШ_ЛОГИН/ВАШ_РЕПО.git'; .\update.ps1"
        exit 1
    }
    Write-Info "Инициализируем Git‑репозиторий и привязываем к $RepoUrl"
    git init -q
    git remote add origin $RepoUrl
}
else {
    if ($RepoUrl) {
        git remote set-url origin $RepoUrl
    }
}

git remote get-url origin > $null
if ($LASTEXITCODE -ne 0) {
    Write-ErrorMsg "Remote origin не настроен. Укажите `$env:REPO_URL`."
    exit 1
}

git remote get-url origin > $null
if ($LASTEXITCODE -ne 0) {
    Write-ErrorMsg "Не удалось получить URL remote."
    exit 1
}

git ls-remote --heads origin > $null
if ($LASTEXITCODE -ne 0) {
    Write-ErrorMsg "Не удалось обратиться к репозиторию (проверьте `$env:REPO_URL` и сеть)."
    exit 1
}

# ---------- Determine branch ----------
if (-not $Branch) {
    if (git ls-remote --heads origin main) { $Branch = "main" }
    elseif (git ls-remote --heads origin master) { $Branch = "master" }
    else {
        Write-ErrorMsg "В удалённом репозитории нет веток main/master. Укажите `$env:BRANCH`."
        exit 1
    }
}

Write-Info "Обновляем ветку $Branch из origin/$Branch"
git fetch --prune origin
git reset --hard "origin/$Branch"
Write-Host "Коммит: $(git log -1 --format='%h %s (%ci)')"

# ---------- Python virtual environment ----------
if (Test-Path -PathType Container -Path ".venv\Scripts\python.exe") {
    $PythonBin = ".venv\Scripts\python.exe"
}
else {
    # ищем любой из поддерживаемых Python (3.10‑3.13)
    $found = $false
    foreach ($ver in @("3.13","3.12","3.11","3.10")) {
        if (Get-Command "python$ver" -ErrorAction SilentlyContinue) {
            $PythonBin = "python$ver"
            $found = $true
            break
        }
    }
    if (-not $found) {
        Write-ErrorMsg "Python 3.10‑3.13 не найден. Установите одну из поддерживаемых версий."
        exit 1
    }
    Write-Info "Создаём виртуальное окружение (.venv) на $PythonBin"
    & $PythonBin -m venv .venv
    $PythonBin = ".venv\Scripts\python.exe"
}

Write-Info "Активируем виртуальное окружение"
& $PythonBin -m venv --help > $null

Write-Info "Обновляем pip и зависимости"
& $PythonBin -m pip install --quiet --upgrade pip
& $PythonBin -m pip install --quiet --upgrade -r requirements.txt

# ---------- Restart systemd service ----------
# Поскольку systemd управляется в Linux, эта часть будет выполнена только в WSL или через SSH.
# На чистом Windows она просто выведет предупреждение.
if (Get-Command systemctl -ErrorAction SilentlyContinue) {
    if (Get-Service -Name $Service -ErrorAction SilentlyContinue) {
        Write-Info "Перезапускаем systemd‑службу $Service"
        & sudo systemctl daemon-reload | Out-Null
        & sudo systemctl restart $Service | Out-Null
        & sudo systemctl status $Service --no-pager | Out-Null
        Write-Info "Логи сервиса: `sudo journalctl -u $Service -f`"
    }
    else {
        Write-Warn "Systemd‑служба '$Service' не найдена. Запустите бота вручную:"
        Write-Host "  `$PythonBin bot.py"
        Write-Host " (или создайте unit‑файл согласно инструкциям в README)"
    }
}
else {
    Write-Warn "Systemd‑служба '$Service' не найдена. Запустите бота вручную:"
    Write-Host "  `$PythonBin bot.py"
    Write-Host " (или создайте unit‑файл согласно инструкциям в README)"
fi

Write-Host "`n=== Обновление завершено ==="
Exit 0