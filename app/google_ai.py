"""Bounded Gemini requests, with a small internal chat-client adapter."""

import asyncio
import base64
import json
from types import SimpleNamespace

import aiohttp


class GoogleAIError(RuntimeError):
    pass


class GoogleClient:
    def __init__(self, api_key, model):
        self._key, self.model = api_key, model
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def request(self, method, path, payload=None):
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180)) as session:
                async with session.request(
                    method,
                    "https://generativelanguage.googleapis.com/v1beta/" + path,
                    json=payload,
                    headers={"x-goog-api-key": self._key},
                ) as response:
                    if response.status != 200:
                        # Never include provider bodies, headers or keys in errors/logs.
                        raise GoogleAIError(
                            f"Google API: HTTP {response.status}. Проверьте ключ, доступ и квоту."
                        )
                    return await response.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as error:
            raise GoogleAIError(
                "Google API временно недоступен. Повторите распознавание."
            ) from error

    async def create(self, *, messages, max_completion_tokens=16000, **kwargs):
        parts = []
        for message in messages:
            content = message["content"]
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            for part in content:
                if part["type"] == "text":
                    parts.append({"text": part["text"]})
                elif part["type"] == "image_url":
                    header, encoded = part["image_url"]["url"].split(",", 1)
                    if not header.startswith("data:") or not header.endswith(";base64"):
                        raise ValueError("Only inline receipt images are supported")
                    base64.b64decode(encoded, validate=True)
                    parts.append({"inlineData": {"mimeType": header[5:-7], "data": encoded}})
        schema = kwargs.get("response_format", {}).get("json_schema", {}).get("schema")
        if schema:
            parts.append({"text": "Return JSON matching this schema:\n" + json.dumps(schema)})
        data = await self.request(
            "POST",
            f"models/{self.model}:generateContent",
            {
                "contents": [{"role": "user", "parts": parts}],
                "generationConfig": {
                    "responseMimeType": "application/json",
                    "maxOutputTokens": max_completion_tokens,
                },
            },
        )
        candidates = data.get("candidates") or []
        if not candidates or candidates[0].get("finishReason") != "STOP":
            raise GoogleAIError(
                "Google не завершил ответ. Чек не записан; повторите распознавание."
            )
        text = "".join(
            p.get("text", "")
            for p in candidates[0].get("content", {}).get("parts", [])
            if not p.get("thought")
        )
        json.loads(text)  # Reject truncated/malformed responses before domain code.
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])

    async def model_ids(self):
        from urllib.parse import urlencode

        result, token = set(), None
        for _ in range(20):
            path = "models" + ("?" + urlencode({"pageToken": token}) if token else "")
            data = await self.request("GET", path)
            for model in data.get("models", []):
                name = model["name"].removeprefix("models/")
                if (
                    name.startswith("gemini-")
                    and "generateContent" in model.get("supportedGenerationMethods", [])
                    and not any(
                        word in name
                        for word in ("image", "tts", "transcribe", "robotics", "live", "embedding")
                    )
                ):
                    result.add(name)
            token = data.get("nextPageToken")
            if not token:
                return sorted(result, reverse=True)
        raise GoogleAIError("Слишком много страниц списка моделей.")

    async def close(self):
        pass
