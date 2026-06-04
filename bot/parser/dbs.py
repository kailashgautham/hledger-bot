"""
DBS / POSB credit card statement parser.

Expected layout (pdfplumber table or text):
  Transaction Date | Description | Amount (SGD)
  01 May 25        | GRABFOOD    | 12.50

Credits (refunds) are typically marked "CR" after the amount or appear
in a separate credit column — we skip those (they reduce liability, not an expense).
"""
import logging
import re
from datetime import datetime

import pdfplumber  # type: ignore

from .base import BaseParser, Transaction

logger = logging.getLogger(__name__)

_DATE_PATTERNS = [
    "%d %b %y",   # 01 May 25
    "%d %b %Y",   # 01 May 2025
    "%d/%m/%y",   # 01/05/25
    "%d/%m/%Y",   # 01/05/2025
]


def _parse_date(raw: str) -> str | None:
    raw = raw.strip()
    for fmt in _DATE_PATTERNS:
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _parse_amount(raw: str) -> float | None:
    raw = raw.strip()
    if not raw:
        return None
    is_credit = raw.upper().endswith("CR") or raw.startswith("-")
    raw = re.sub(r"[^\d.]", "", raw)
    try:
        val = float(raw)
        return -val if is_credit else val  # credits are negative (skip later)
    except ValueError:
        return None


class DBSParser(BaseParser):
    IDENTIFIERS = ["DBS BANK", "POSB", "DBS ALTITUDE", "DBS BANK LTD"]
    card_name = "DBS Altitude"

    def parse(self, pdf_path: str) -> list[Transaction]:
        transactions: list[Transaction] = []
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                transactions.extend(self._parse_page(page))
        seen: set[tuple] = set()
        deduped: list[Transaction] = []
        for tx in transactions:
            key = (tx["date"], tx["description"], tx["amount"])
            if key not in seen:
                seen.add(key)
                deduped.append(tx)
        return deduped

    def _parse_page(self, page) -> list[Transaction]:
        # Try structured table extraction first
        for table in page.extract_tables() or []:
            result = self._try_table(table)
            if result:
                return result
        # Fall back to raw text regex
        return self._parse_text(page.extract_text() or "")

    def _try_table(self, table: list[list]) -> list[Transaction]:
        """Parse a pdfplumber table. Returns [] if table doesn't look like transactions."""
        if not table or len(table[0]) < 3:
            return []
        transactions: list[Transaction] = []
        for row in table:
            if not row or len(row) < 3:
                continue
            date_raw = (row[0] or "").strip()
            desc = (row[1] or "").strip()
            amount_raw = (row[-1] or "").strip()
            date = _parse_date(date_raw)
            if not date or not desc:
                continue
            amount = _parse_amount(amount_raw)
            if amount is None or amount <= 0:
                continue
            transactions.append(
                Transaction(date=date, description=desc, amount=round(amount, 2))
            )
        return transactions

    def _parse_text(self, text: str) -> list[Transaction]:
        """
        Regex fallback. Matches lines like:
          01 May 25   GRABFOOD*SG123   18.50
          01/05/25    NETFLIX.COM      10.98
        """
        pattern = re.compile(
            r"(\d{1,2}[\s/]\w+[\s/]\d{2,4})\s{2,}(.+?)\s{2,}([\d,]+\.\d{2}\s*(?:CR)?)\s*$",
            re.MULTILINE,
        )
        transactions: list[Transaction] = []
        for m in pattern.finditer(text):
            date = _parse_date(m.group(1))
            if not date:
                continue
            desc = m.group(2).strip()
            amount = _parse_amount(m.group(3))
            if amount is None or amount <= 0:
                continue
            transactions.append(
                Transaction(date=date, description=desc, amount=round(amount, 2))
            )
        return transactions
