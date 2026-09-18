"""Idempotent importer for the BudgetMoneyBot XLSX export.

Run inside the deployed app container, for example:
``python -m app.legacy_import /tmp/BudgetMoneyBot.xlsx --family-id 1``.
The workbook contains no currency column, so its amounts are intentionally
treated as ILS, the documented family currency at the time of migration.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
from collections import Counter
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Iterator
from xml.etree import ElementTree as ET
from zipfile import ZipFile
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Category, Family, LegacyImportRow, Transaction, User
from app.repositories import add_transaction

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
TZ = ZoneInfo("Asia/Jerusalem")

# (destination type, destination category). These are the mappings agreed with
# the family before this import; names not present in the DB are created.
MAPPING = {
    "Аренда квартиры": ("expense", "Аренда"),
    "Корзина": ("income", "Корзина"),
    "Проценты по депозиту": ("income", "Проценты по депозиту"),
    "Прочее": ("expense", "Другое"),
    "PPM": ("income", "Зарплата"),
    "Аренда": ("expense", "Аренда"),
    "Вовка": ("expense", "Детское"),
    "Хозяйство": ("expense", "Продукты"),
    "Коммуникации": ("expense", "Коммуникации"),
    "Транспорт": ("expense", "Транспорт"),
    "Коммуналка": ("expense", "Коммуналка"),
    "Страховка": ("expense", "Страховка"),
    "Подарки": ("expense", "Подарки"),
    "Барахло": ("expense", "Барахло"),
    "Zhurkovsky": ("expense", "Бухгалтер"),
    "Битуах Леуми": ("expense", "Битуах Леуми"),
    "Медицина": ("expense", "Медицина"),
    "Кафе и рестораны": ("expense", "Кафе и рестораны"),
}


def normal_comment(value: str | None) -> str:
    return " ".join((value or "").split()).casefold()


def excel_datetime(value: str) -> datetime:
    return datetime(1899, 12, 30) + timedelta(days=float(value))


def source_key(sheet: str, row: int, moment: datetime, amount: Decimal, comment: str | None) -> str:
    payload = f"budgetmoneybot-v1\0{sheet}\0{row}\0{moment.isoformat()}\0{amount}\0{normal_comment(comment)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def xlsx_rows(path: Path) -> Iterator[tuple[str, int, datetime, Decimal, str | None]]:
    with ZipFile(path) as archive:
        strings: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            strings = ["".join(part.text or "" for part in item.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t")) for item in root.findall("m:si", NS)]
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        targets = {item.attrib["Id"]: item.attrib["Target"].lstrip("/") for item in rels}
        for sheet in workbook.find("m:sheets", NS):
            name = sheet.attrib["name"]
            target = targets[sheet.attrib["{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"]]
            target = target if target.startswith("xl/") else "xl/" + target
            root = ET.fromstring(archive.read(target))
            for row in root.findall(".//m:sheetData/m:row", NS)[1:]:
                values: dict[str, str] = {}
                for cell in row.findall("m:c", NS):
                    ref = cell.attrib.get("r", "")
                    column = "".join(character for character in ref if character.isalpha())
                    value = cell.findtext("m:v", default="", namespaces=NS)
                    if cell.attrib.get("t") == "s" and value:
                        value = strings[int(value)]
                    values[column] = value
                if values.get("B") and values.get("C"):
                    yield name, int(row.attrib["r"]), excel_datetime(values["B"]), Decimal(values["C"]), values.get("D") or None


def fingerprint(day, tx_type: str, category: str, amount: Decimal, comment: str | None) -> tuple:
    return day, tx_type, category, amount.quantize(Decimal("0.01")), normal_comment(comment)


def is_already_saved_ikea(sheet: str, moment: datetime, amount: Decimal, comment: str | None) -> bool:
    return sheet == "Прочее" and moment.date().isoformat() == "2026-09-15" and amount == Decimal("1996") and normal_comment(comment) == "икеа"


async def import_workbook(path: Path, family_id: int, *, dry_run: bool = False) -> Counter:
    rows = list(xlsx_rows(path))
    missing = sorted({sheet for sheet, *_ in rows if sheet not in MAPPING})
    if missing:
        raise ValueError("Нет мэппинга для листов: " + ", ".join(missing))
    summary: Counter = Counter()
    async with SessionLocal() as session:
        family = await session.get(Family, family_id)
        if not family:
            raise ValueError("Семья не найдена.")
        actor = await session.scalar(select(User).where(User.family_id == family_id, User.role == "owner").order_by(User.id))
        if not actor:
            raise ValueError("У семьи нет владельца.")
        categories = {(category.type, category.name): category for category in (await session.scalars(select(Category).where(Category.family_id == family_id))).all()}
        for tx_type, category_name in set(MAPPING.values()):
            if (tx_type, category_name) not in categories:
                category = Category(family_id=family_id, name=category_name, type=tx_type)
                session.add(category)
                await session.flush()
                categories[tx_type, category_name] = category
                summary["categories_created"] += 1
        current = Counter()
        stored = await session.execute(select(Transaction, Category.name).join(Category, Transaction.category_id == Category.id).where(Transaction.family_id == family_id, Transaction.deleted_at.is_(None)))
        for transaction, category_name in stored.all():
            current[fingerprint(transaction.date.astimezone(TZ).date(), transaction.type, category_name, transaction.amount, transaction.comment)] += 1
        known_keys = set((await session.scalars(select(LegacyImportRow.source_key).where(LegacyImportRow.family_id == family_id))).all())
        for sheet, row, moment, amount, comment in rows:
            key = source_key(sheet, row, moment, amount, comment)
            if key in known_keys:
                summary["already_processed"] += 1
                continue
            tx_type, category_name = MAPPING[sheet]
            if is_already_saved_ikea(sheet, moment, amount, comment):
                status, transaction_id = "skipped_existing_ikea_receipt", None
                summary["skipped_ikea"] += 1
            elif current[fingerprint(moment.date(), tx_type, category_name, amount, comment)] > 0:
                current[fingerprint(moment.date(), tx_type, category_name, amount, comment)] -= 1
                status, transaction_id = "skipped_existing", None
                summary["skipped_existing"] += 1
            else:
                transaction = await add_transaction(
                    session, family_id, actor.id, categories[tx_type, category_name], amount, tx_type, comment,
                    source_amount=amount, source_currency="ILS", exchange_rate=Decimal("1"),
                    exchange_rate_date=moment.date(), exchange_rate_source="legacy_xlsx",
                )
                transaction.date = moment.replace(tzinfo=TZ)
                status, transaction_id = "imported", transaction.id
                summary["imported"] += 1
            session.add(LegacyImportRow(family_id=family_id, source_key=key, source_file=path.name, source_sheet=sheet, source_row=row, status=status, transaction_id=transaction_id))
        if dry_run:
            await session.rollback()
        else:
            await session.commit()
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("workbook", type=Path)
    parser.add_argument("--family-id", type=int, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    print(dict(sorted(asyncio.run(import_workbook(args.workbook, args.family_id, dry_run=args.dry_run)).items())))


if __name__ == "__main__":
    main()
