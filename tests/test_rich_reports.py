from datetime import datetime, timezone

from app.reports import period_bounds, shift_anchor
from app.rich import link
from app.models import TransactionDraft
from app.charts import ReportChartService


def test_rich_action_is_a_link_style_tg_button():
    assert link("r:opaque:s:fact", "Факт") == (
        '<tg-button type="callback_data" style="link" data="r:opaque:s:fact">Факт</tg-button>'
    )


def test_quarter_and_year_periods_include_all_budget_months():
    start, end, label, months = period_bounds("quarter", "2026-05")
    assert (start, end, label, months) == (
        datetime(2026, 4, 1, tzinfo=timezone.utc),
        datetime(2026, 7, 1, tzinfo=timezone.utc),
        "Q2 2026",
        ["2026-04", "2026-05", "2026-06"],
    )
    _, _, _, year_months = period_bounds("year", "2026-05")
    assert year_months == [f"2026-{month:02d}" for month in range(1, 13)]
    assert shift_anchor("2026-01", "quarter", -1) == "2025-10"


def test_transaction_draft_is_bound_to_the_rendered_chat_message():
    assert {"chat_id", "message_id"}.issubset(TransactionDraft.__table__.columns.keys())


def test_transaction_draft_has_integrity_fields():
    assert {"revision", "status", "receipt_file_id", "receipt_mime_type"}.issubset(
        TransactionDraft.__table__.columns.keys()
    )


def test_chart_is_a_real_png_attachment_source():
    image = ReportChartService().png([("Продукты", 1200), ("Транспорт", 500)])
    assert image is not None
    assert image.startswith(b"\x89PNG\r\n\x1a\n")
