"""pytest-фикстуры проекта.

Интеграционный smoke_test.py запускается отдельно (python smoke_test.py) —
он поднимает фейковую панель и проверяет весь поток выдачи доступа.
Здесь — только то, что нужно unit-тестам в tests/.
"""
import os
import sys
import tempfile

# Каждый pytest-запуск использует свою временную БД — тесты не трогают bot.db
os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "pytest_bot.db"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Тесты не должны зависеть от локального .env: в CI нет ни ADMIN_IDS, ни токена
# бота, из-за чего admin-тесты получали 403, а сессии — «bad session».
# Фиксируем тестовые значения ДО импорта config (он читает .env при импорте).
os.environ["ADMIN_IDS"] = os.environ.get("ADMIN_IDS") or "1035529538"
os.environ["TELEGRAM_BOT_TOKEN"] = os.environ.get("TELEGRAM_BOT_TOKEN") or "test-token"

import asyncio

import db

# Инициализируем схему один раз при импорте conftest (event loop pytest ещё не создан)
asyncio.run(db.init_db())
