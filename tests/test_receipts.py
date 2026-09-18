import pytest

from app.receipts import ReceiptError, extract_pdf_text, html_to_text, is_http_url, render_pdf_pages


def test_only_http_urls_are_receipt_url_candidates():
    assert is_http_url("https://receipts.example/receipt?id=1")
    assert is_http_url("http://example.org/receipt")
    assert not is_http_url("receipt.pdf")
    assert not is_http_url("file:///etc/passwd")


def test_html_receipt_text_excludes_script_content():
    content = html_to_text("<html><body><h1>Receipt</h1><p>Total 42.50</p><script>secret()</script></body></html>")
    assert "Receipt" in content
    assert "Total 42.50" in content
    assert "secret" not in content


def test_pdf_receipt_is_rendered_to_an_image():
    fitz = pytest.importorskip("fitz")
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "Receipt total 42.50")
    content = document.tobytes()
    document.close()

    pages = render_pdf_pages(content)

    assert len(pages) == 1
    assert pages[0][1] == "image/jpeg"
    assert pages[0][0].startswith(b"\xff\xd8")


def test_pdf_text_layer_is_extracted_when_present():
    import fitz

    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "TOTAL 1996")
    payload = document.tobytes()
    document.close()

    assert "TOTAL 1996" in extract_pdf_text(payload)


def test_invalid_pdf_has_a_clear_error():
    with pytest.raises(ReceiptError, match="PDF"):
        render_pdf_pages(b"not a pdf")
