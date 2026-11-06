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

import asyncio

import db

# Инициализируем схему один раз при импорте conftest (event loop pytest ещё не создан)
asyncio.run(db.init_db())
