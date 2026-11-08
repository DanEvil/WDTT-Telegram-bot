"""Тесты валидации initData Telegram Mini Apps: обе схемы подписи + edge cases."""
import hashlib
import hmac
import json
import os
import sys
import time
from urllib.parse import urlencode

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api import validate_init_data  # noqa: E402

TOKEN = "123456:TEST-TOKEN-abcdefghijklmnopqrstuvwxyz"


def _sign(pairs: dict, secret: bytes) -> str:
    check = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    return hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()


def _make_init_data(user: dict, secret: bytes, extra: dict | None = None) -> str:
    """Собирает initData. Хеш считается по полям БЕЗ signature (как делает Telegram),
    а extra (например, signature) добавляется уже после подписи."""
    pairs = {
        "auth_date": str(int(time.time())),
        "query_id": "AAH_test",
        "user": json.dumps(user, ensure_ascii=False, separators=(",", ":")),
    }
    pairs["hash"] = _sign(pairs, secret)
    if extra:
        pairs.update(extra)
    return urlencode(pairs)


def test_modern_schema_hmac_webappdata():
    """Актуальная схема Mini Apps: secret = HMAC(bot_token, 'WebAppData')."""
    secret = hmac.new(TOKEN.encode(), b"WebAppData", hashlib.sha256).digest()
    init_data = _make_init_data({"id": 42, "first_name": "Иван"}, secret)
    user = validate_init_data(init_data, TOKEN)
    assert user is not None, "современная схема подписи не принята"
    assert user["id"] == 42


def test_legacy_schema_sha256():
    """Старая схема: secret = SHA256(bot_token)."""
    secret = hashlib.sha256(TOKEN.encode()).digest()
    init_data = _make_init_data({"id": 43, "first_name": "Пётр"}, secret)
    user = validate_init_data(init_data, TOKEN)
    assert user is not None, "старая схема подписи не принята"
    assert user["id"] == 43


def test_signature_field_excluded():
    """Поле signature не входит в data-check-string — хеш должен сойтись."""
    secret = hmac.new(TOKEN.encode(), b"WebAppData", hashlib.sha256).digest()
    init_data = _make_init_data({"id": 44}, secret, extra={"signature": "ed25519-blob"})
    assert validate_init_data(init_data, TOKEN) is not None


def test_plus_in_user_json():
    """Плюс в данных пользователя не должен ломать подпись."""
    secret = hmac.new(TOKEN.encode(), b"WebAppData", hashlib.sha256).digest()
    init_data = _make_init_data({"id": 45, "username": "a+b"}, secret)
    user = validate_init_data(init_data, TOKEN)
    assert user is not None, "initData с '+' не принята"
    assert user["id"] == 45


def test_wrong_token_rejected():
    secret = hmac.new(TOKEN.encode(), b"WebAppData", hashlib.sha256).digest()
    init_data = _make_init_data({"id": 46}, secret)
    assert validate_init_data(init_data, "999:OTHER-TOKEN") is None


def test_empty_init_data_rejected():
    assert validate_init_data("", TOKEN) is None


def test_expired_rejected():
    secret = hmac.new(TOKEN.encode(), b"WebAppData", hashlib.sha256).digest()
    pairs = {
        "auth_date": str(int(time.time()) - 90000),
        "user": json.dumps({"id": 47}),
    }
    pairs["hash"] = _sign(pairs, secret)
    assert validate_init_data(urlencode(pairs), TOKEN) is None
