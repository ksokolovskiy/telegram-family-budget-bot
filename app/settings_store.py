"""Persisted, owner-managed application settings.

The database URL is deliberately not represented here: it is needed to open
the database that holds these values. BOT_TOKEN and OWNER_TELEGRAM_ID are also
deployment secrets/identity anchors and remain environment-only.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import base64
import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AppSetting, Family

ENCRYPTION_VERSION = "bot-token-hkdf-v1"
_FERNET_SALT = b"telegram-family-budget-bot/app-settings/fernet/v1"
_FERNET_INFO = b"openai_api_key"

LOG_LEVEL = "log_level"
APP_ENV = "app_env"
RECEIPT_STORAGE = "receipt_storage"

DEFAULTS = {
    LOG_LEVEL: "INFO",
    APP_ENV: "local",
    RECEIPT_STORAGE: "retain_file_id",
}
EDITABLE_KEYS = tuple(DEFAULTS)
LABELS = {
    LOG_LEVEL: "Уровень логирования",
    APP_ENV: "Окружение приложения",
    RECEIPT_STORAGE: "Хранение идентификатора чека",
}
HELP = {
    LOG_LEVEL: "Введите один из уровней: DEBUG, INFO, WARNING, ERROR, CRITICAL.",
    APP_ENV: "Введите короткое имя окружения, например local или production.",
    RECEIPT_STORAGE: "retain_file_id — позволит повторно распознать чек; do_not_retain — после распознавания не сохранять Telegram file_id.",
}


@dataclass(frozen=True)
class RuntimeSettings:
    log_level: str
    app_env: str


@dataclass(frozen=True)
class FamilyAISettings:
    openai_api_key: str | None
    openai_model: str


async def get_values(session: AsyncSession) -> dict[str, str]:
    rows = await session.scalars(select(AppSetting).where(AppSetting.key.in_(EDITABLE_KEYS)))
    values = dict(DEFAULTS)
    for row in rows:
        values[row.key] = row.value
    return values


async def get_runtime_settings(session: AsyncSession) -> RuntimeSettings:
    values = await get_values(session)
    return RuntimeSettings(
        log_level=values[LOG_LEVEL],
        app_env=values[APP_ENV],
    )


async def get_family_ai_settings(session: AsyncSession, family_id: int) -> FamilyAISettings:
    family = await session.get(Family, family_id)
    if family is None:
        raise ValueError("Семья не найдена.")
    key = None
    if family.openai_api_key:
        if not family.openai_api_key_encryption_version:
            raise RuntimeError("Ключ OpenAI семьи не зашифрован. Задайте его заново.")
        key = decrypt_family_openai_api_key(
            family.openai_api_key, family.openai_api_key_encryption_version, family.id
        )
    return FamilyAISettings(openai_api_key=key, openai_model=family.openai_model)


async def set_family_openai_api_key(session: AsyncSession, family: Family, value: str) -> None:
    value = value.strip()
    if not value or len(value) > 512:
        raise ValueError("Укажите корректный OpenAI API key длиной до 512 символов.")
    family.openai_api_key = encrypt_family_openai_api_key(value, family.id)
    family.openai_api_key_encryption_version = ENCRYPTION_VERSION
    family.openai_api_key_encrypted_at = datetime.now(timezone.utc)


async def set_family_openai_model(session: AsyncSession, family: Family, model_id: str) -> None:
    model_id = model_id.strip()
    if not model_id.startswith("gpt-") or len(model_id) > 120:
        raise ValueError("Некорректная модель OpenAI.")
    family.openai_model = model_id


def validate_value(key: str, value: str) -> str:
    value = value.strip()
    if key not in EDITABLE_KEYS:
        raise ValueError("Неизвестная настройка.")
    if not value:
        raise ValueError("Значение не может быть пустым.")
    if key == LOG_LEVEL:
        value = value.upper()
        if value not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("Допустимы DEBUG, INFO, WARNING, ERROR или CRITICAL.")
    elif key == APP_ENV:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", value):
            raise ValueError("Используйте до 40 латинских букв, цифр, «-» или «_».")
    elif key == RECEIPT_STORAGE and value not in {"retain_file_id", "do_not_retain"}:
        raise ValueError("Используйте retain_file_id или do_not_retain.")
    return value


async def set_value(session: AsyncSession, key: str, value: str) -> str:
    value = validate_value(key, value)
    row = await session.get(AppSetting, key)
    if row is None:
        row = AppSetting(key=key, value=value)
        session.add(row)
    else:
        row.value = value
    row.encryption_version = None
    row.encrypted_at = None
    return value


def _fernet_for_bot_token():
    """Build a Fernet key from the deployment's BOT_TOKEN using HKDF-SHA256."""
    from app.config import settings

    if not settings.bot_token:
        raise RuntimeError("BOT_TOKEN is required to encrypt or decrypt the OpenAI API key.")
    try:
        from cryptography.fernet import Fernet
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    except ImportError as error:  # pragma: no cover - dependency is installed in deployment
        raise RuntimeError("cryptography dependency is required for encrypted settings.") from error
    material = HKDF(algorithm=hashes.SHA256(), length=32, salt=_FERNET_SALT, info=_FERNET_INFO).derive(
        settings.bot_token.encode("utf-8")
    )
    return Fernet(base64.urlsafe_b64encode(material))


def encrypt_openai_api_key(value: str) -> str:
    return _fernet_for_bot_token().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_openai_api_key(value: str, version: str) -> str:
    if version != ENCRYPTION_VERSION:
        raise RuntimeError(f"Неподдерживаемая версия шифрования настройки: {version}")
    try:
        return _fernet_for_bot_token().decrypt(value.encode("ascii")).decode("utf-8")
    except Exception as error:
        # Never fall back to treating a malformed ciphertext as a live API key.
        raise RuntimeError("Не удалось расшифровать OpenAI API key. Проверьте BOT_TOKEN.") from error


def encrypt_family_openai_api_key(value: str, family_id: int) -> str:
    return _fernet_for_family(family_id).encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_family_openai_api_key(value: str, version: str, family_id: int) -> str:
    if version != ENCRYPTION_VERSION:
        raise RuntimeError(f"Неподдерживаемая версия шифрования настройки: {version}")
    try:
        return _fernet_for_family(family_id).decrypt(value.encode("ascii")).decode("utf-8")
    except Exception as error:
        raise RuntimeError("Не удалось расшифровать ключ OpenAI семьи. Проверьте BOT_TOKEN.") from error


def _fernet_for_family(family_id: int):
    from app.config import settings

    if not settings.bot_token:
        raise RuntimeError("BOT_TOKEN is required to encrypt or decrypt the OpenAI API key.")
    try:
        from cryptography.fernet import Fernet
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    except ImportError as error:  # pragma: no cover
        raise RuntimeError("cryptography dependency is required for encrypted settings.") from error
    material = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_FERNET_SALT,
        info=f"openai_api_key:family:{family_id}".encode(),
    ).derive(settings.bot_token.encode("utf-8"))
    return Fernet(base64.urlsafe_b64encode(material))


def display_value(key: str, value: str) -> str:
    return value
