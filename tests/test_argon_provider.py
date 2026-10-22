import json
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.google_ai import GoogleClient, GoogleAIError
from app.receipt_argon import Scan, recognize, to_transaction, validate_scan
from app.receipt_progress import receipt_progress
from app.settings_store import (
    FamilyAISettings,
    _fernet_for_family,
    set_family_ai_key,
    set_family_ai_model,
)


def scan_data():
    return dict(
        currency="ILS",
        printed_total=18,
        rows=[
            dict(
                name_original="מגבונים",
                barcode="123",
                quantity=2,
                unit_price=10,
                signed_amount=20,
                kind="product",
                source_images=[1],
            ),
            dict(name_original="הנחה", signed_amount=-2, kind="discount", source_images=[1]),
        ],
    )


def labels():
    return dict(
        categories=[dict(id=1, category="Дом", needs_review=False)],
        deductions=[dict(id=2, product_ids=[1])],
    )


def reply(data):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(data)))]
    )


async def test_two_calls_no_translation_original_names_and_progress():
    create = AsyncMock(side_effect=[reply(scan_data()), reply(labels())])
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    progress = AsyncMock()
    result = await recognize(
        client, "gemini-4-argon", [(b"photo", "image/jpeg")], ["Дом"], progress=progress
    )
    assert create.await_count == 2
    assert result.items[0].name == "מגבונים"
    assert result.items[0].display_name is None
    assert result.items[0].amount == Decimal(18)
    assert progress.await_count == 4


async def test_bad_ocr_retries_before_classification_without_balancing():
    bad = scan_data()
    bad["printed_total"] = 19
    create = AsyncMock(return_value=reply(bad))
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    progress = AsyncMock()
    with pytest.raises(ValueError, match="sum"):
        await recognize(client, "test", [(b"x", "image/jpeg")], ["Дом"], progress=progress)
    assert create.await_count == 2
    assert any("Повторно" in c.args[0] for c in progress.await_args_list)


@pytest.mark.parametrize(
    "change", ["amount", "sign", "source", "quantity", "duplicate", "missing", "target"]
)
def test_untrusted_rows_and_labels_validated(change):
    data = scan_data()
    labs = labels()
    if change == "amount":
        data["printed_total"] = 99
    if change == "sign":
        data["rows"][1]["signed_amount"] = 2
    if change == "source":
        data["rows"][0]["source_images"] = [2]
    if change == "quantity":
        data["rows"][0]["quantity"] = 1
    if change == "duplicate":
        labs["categories"] *= 2
    if change == "missing":
        labs["deductions"] = []
    if change == "target":
        labs["deductions"][0]["product_ids"] = [8]
    with pytest.raises(ValueError):
        scan = Scan.model_validate(data)
        validate_scan(scan, 1)
        to_transaction(scan, labs, ["Дом"], 1)


async def test_google_client_preserves_image_bytes_and_rejects_truncation():
    client = GoogleClient("secret", "gemini-4-argon")
    client.request = AsyncMock(
        return_value={
            "candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "{}"}]}}]
        }
    )
    await client.create(
        messages=[
            {
                "content": [
                    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,eA=="}}
                ]
            }
        ]
    )
    body = client.request.call_args.args[2]
    assert body["contents"][0]["parts"][0]["inlineData"] == {
        "mimeType": "image/jpeg",
        "data": "eA==",
    }
    client.request.return_value = {"candidates": [{"finishReason": "MAX_TOKENS"}]}
    with pytest.raises(GoogleAIError):
        await client.create(messages=[])


async def test_provider_keys_and_models_are_independent(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "bot_token", "123456:test-token")
    family = SimpleNamespace(
        id=17, openai_api_key="existing-ciphertext", openai_model="gpt-5.4", ai_provider="openai"
    )
    await set_family_ai_key(None, family, "google", "google-test-key")
    await set_family_ai_model(None, family, "google", "gemini-4-argon")
    assert family.openai_api_key == "existing-ciphertext"
    assert family.openai_model == "gpt-5.4"
    assert family.google_api_key != "google-test-key"
    assert (
        _fernet_for_family(17, "google").decrypt(family.google_api_key.encode())
        == b"google-test-key"
    )
    for fid, provider in [(18, "google"), (17, "openai")]:
        with pytest.raises(Exception):
            _fernet_for_family(fid, provider).decrypt(family.google_api_key.encode())
    runtime = FamilyAISettings("openai-secret", "gpt-5.4", "google", "google-secret")
    assert runtime.api_key == "google-secret" and runtime.model == "gemini-4-argon"
    assert "secret" not in repr(runtime)


@pytest.mark.parametrize("fails", [False, True])
async def test_progress_uses_one_message_and_finishes_on_error(fails):
    rich = SimpleNamespace(send=AsyncMock(return_value={"message_id": 42}), edit=AsyncMock())
    try:
        async with receipt_progress(object(), 1, rich) as progress:
            await progress("Распознаю…")
            await progress("Классифицирую…")
            if fails:
                raise ValueError("failure")
    except ValueError:
        pass
    assert rich.send.await_count == 1
    assert rich.edit.await_count == 3
    assert ("остановлена" if fails else "завершена") in rich.edit.call_args.args[3]


async def test_telegram_progress_failure_does_not_abort_recognition():
    rich = SimpleNamespace(send=AsyncMock(side_effect=RuntimeError), edit=AsyncMock())
    async with receipt_progress(object(), 1, rich) as progress:
        await progress("Распознаю…")
