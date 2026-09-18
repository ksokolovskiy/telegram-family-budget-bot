"""Receipt acquisition and PDF preparation without persisting remote content."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from html.parser import HTMLParser
import ipaddress
from urllib.parse import urljoin, urlsplit

import aiohttp

MAX_RECEIPT_BYTES = 10 * 1024 * 1024
MAX_URL_LENGTH = 2_048
MAX_REDIRECTS = 4
MAX_PDF_PAGES = 5
# Kept off while the dynamic web-receipt renderer is being stabilized. The
# implementation remains in this module and can be enabled without a rewrite.
ENABLE_RECEIPT_URLS = False


class ReceiptError(ValueError):
    pass


@dataclass(frozen=True)
class ReceiptPayload:
    kind: str  # image, pdf or text
    content: bytes | str
    mime_type: str | None = None


def is_http_url(value: str) -> bool:
    parsed = urlsplit(value.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname)


async def fetch_receipt_url(value: str) -> ReceiptPayload:
    """Fetch a public receipt URL with conservative bounds and redirect checks."""
    url = value.strip()
    if len(url) > MAX_URL_LENGTH or not is_http_url(url):
        raise ReceiptError("Нужна корректная публичная ссылка http:// или https://.")
    timeout = aiohttp.ClientTimeout(total=20, connect=8, sock_read=12)
    headers = {"User-Agent": "FamilyBudgetReceiptReader/1.0", "Accept": "text/html,application/pdf,image/*"}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as client:
        for _ in range(MAX_REDIRECTS + 1):
            await _require_public_host(url)
            async with client.get(url, allow_redirects=False) as response:
                if response.status in {301, 302, 303, 307, 308}:
                    target = response.headers.get("Location")
                    if not target:
                        raise ReceiptError("Ссылка перенаправляет без адреса чека.")
                    url = urljoin(url, target)
                    if not is_http_url(url):
                        raise ReceiptError("Ссылка перенаправляет на недопустимый адрес.")
                    continue
                if response.status >= 400:
                    raise ReceiptError(f"Сайт чека вернул ошибку {response.status}.")
                raw = await _read_limited(response)
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
                path = urlsplit(url).path.lower()
                if content_type == "application/pdf" or path.endswith(".pdf"):
                    return ReceiptPayload("pdf", raw, "application/pdf")
                if content_type.startswith("image/"):
                    return ReceiptPayload("image", raw, content_type)
                if content_type in {"text/html", "application/xhtml+xml"} or not content_type:
                    text = html_to_text(raw.decode(response.charset or "utf-8", errors="replace"))
                    # Many receipt services (including modern SPA storefronts)
                    # return only a JavaScript shell to an HTTP client.
                    if len(text) < 80:
                        return await render_dynamic_receipt(url)
                    return ReceiptPayload("text", text)
                raise ReceiptError("Поддерживаются ссылки на изображение, PDF или веб-страницу чека.")
    raise ReceiptError("Слишком много перенаправлений по ссылке чека.")


async def render_dynamic_receipt(url: str) -> ReceiptPayload:
    """Render a public SPA receipt page and return its visual content to AI."""
    try:
        from playwright.async_api import async_playwright
    except ImportError as error:  # pragma: no cover - deployment dependency
        raise ReceiptError("Для динамических чеков не установлен браузерный модуль.") from error
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 1280, "height": 900}, device_scale_factor=1)

            async def guard(route) -> None:
                try:
                    await _require_public_host(route.request.url)
                except ReceiptError:
                    await route.abort()
                else:
                    await route.continue_()

            await page.route("**/*", guard)
            await page.goto(url, wait_until="networkidle", timeout=25_000)
            screenshot = await page.screenshot(type="jpeg", quality=80, full_page=True)
            await browser.close()
    except Exception as error:
        raise ReceiptError("Не удалось открыть динамическую страницу чека.") from error
    if not screenshot or len(screenshot) > MAX_RECEIPT_BYTES:
        raise ReceiptError("Страница чека слишком велика для распознавания.")
    return ReceiptPayload("image", screenshot, "image/jpeg")


async def _require_public_host(url: str) -> None:
    hostname = urlsplit(url).hostname
    if not hostname:
        raise ReceiptError("У ссылки отсутствует домен.")
    try:
        addresses = await asyncio.get_running_loop().getaddrinfo(hostname, None, type=0)
    except OSError as error:
        raise ReceiptError("Не удалось найти сайт чека.") from error
    if not addresses:
        raise ReceiptError("Не удалось найти сайт чека.")
    for _, _, _, _, sockaddr in addresses:
        if not ipaddress.ip_address(sockaddr[0]).is_global:
            raise ReceiptError("Допустимы только публичные ссылки на чек.")


async def _read_limited(response: aiohttp.ClientResponse) -> bytes:
    declared = response.content_length
    if declared and declared > MAX_RECEIPT_BYTES:
        raise ReceiptError("Чек по ссылке больше 10 МБ.")
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.content.iter_chunked(64 * 1024):
        total += len(chunk)
        if total > MAX_RECEIPT_BYTES:
            raise ReceiptError("Чек по ссылке больше 10 МБ.")
        chunks.append(chunk)
    return b"".join(chunks)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._ignored = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in {"script", "style", "noscript", "svg"}:
            self._ignored += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg"} and self._ignored:
            self._ignored -= 1

    def handle_data(self, data: str) -> None:
        if not self._ignored:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    return " ".join(" ".join(parser.parts).split())[:40_000]


def render_pdf_pages(content: bytes) -> list[tuple[bytes, str]]:
    if len(content) > MAX_RECEIPT_BYTES:
        raise ReceiptError("PDF-чек больше 10 МБ.")
    try:
        import fitz
        document = fitz.open(stream=content, filetype="pdf")
    except Exception as error:
        raise ReceiptError("Не удалось открыть PDF-чек.") from error
    try:
        if not document.page_count:
            raise ReceiptError("PDF-чек не содержит страниц.")
        pages: list[tuple[bytes, str]] = []
        for index in range(min(document.page_count, MAX_PDF_PAGES)):
            # Receipt fonts are often only a few pixels high at 1x.  Render at
            # 2x so the vision model can read a price independently of the PDF
            # text layer (which is absent in scans and sometimes malformed).
            pixmap = document.load_page(index).get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
            pages.append((pixmap.tobytes("jpeg"), "image/jpeg"))
        return pages
    finally:
        document.close()


def extract_pdf_text(content: bytes) -> str:
    """Return a PDF's native text layer, if it has one, without OCR.

    Digital receipts often contain an exact text layer.  Giving that text to
    the model alongside the page image makes rows and totals much less prone
    to visual sampling differences; scans simply return an empty string.
    """
    if len(content) > MAX_RECEIPT_BYTES:
        raise ReceiptError("PDF-чек больше 10 МБ.")
    try:
        import fitz
        document = fitz.open(stream=content, filetype="pdf")
    except Exception as error:
        raise ReceiptError("Не удалось открыть PDF-чек.") from error
    try:
        return "\n".join(document.load_page(index).get_text("text") for index in range(min(document.page_count, MAX_PDF_PAGES)))[:40_000].strip()
    finally:
        document.close()
