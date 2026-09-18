import pytest

from app.settings_store import (
    APP_ENV,
    ENCRYPTION_VERSION,
    LOG_LEVEL,
    decrypt_family_openai_api_key,
    encrypt_family_openai_api_key,
    validate_value,
)


def test_log_level_is_normalized_and_validated():
    assert validate_value(LOG_LEVEL, " warning ") == "WARNING"
    with pytest.raises(ValueError):
        validate_value(LOG_LEVEL, "verbose")


def test_app_environment_has_a_safe_short_format():
    assert validate_value(APP_ENV, "production-eu_1") == "production-eu_1"
    with pytest.raises(ValueError):
        validate_value(APP_ENV, "production environment")


def test_unknown_encryption_version_never_falls_back_to_plaintext():
    with pytest.raises(RuntimeError, match="версия"):
        decrypt_family_openai_api_key("not-a-key", "unknown-v0", 1)


def test_family_openai_key_ciphertext_cannot_be_reused_by_another_family(monkeypatch):
    pytest.importorskip("cryptography.fernet")
    from app.config import settings

    monkeypatch.setattr(settings, "bot_token", "123456:stable-test-token-with-enough-entropy")
    encrypted = encrypt_family_openai_api_key("test-api-key", 17)
    assert encrypted != "test-api-key"
    assert decrypt_family_openai_api_key(encrypted, ENCRYPTION_VERSION, 17) == "test-api-key"
    with pytest.raises(RuntimeError, match="расшифровать"):
        decrypt_family_openai_api_key(encrypted, ENCRYPTION_VERSION, 18)
