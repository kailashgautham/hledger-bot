import calendar
import math
import re
from pathlib import Path
from typing import Optional


class JournalWriter:
    def __init__(self, journal_path: str, currency: str):
        self.path = Path(journal_path)
        self.currency = currency

    # ------------------------------------------------------------------
    # Reading helpers
    # ------------------------------------------------------------------

    def get_accounts(self) -> list[str]:
        """Return all unique accounts found in the journal."""
        if not self.path.exists():
            return []
        accounts: set[str] = set()
        for line in self.path.read_text().splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith(";") and line.startswith(" "):
                parts = re.split(r"\s{2,}", stripped) if "  " in stripped else [stripped]
                if parts:
                    candidate = parts[0].strip()
                    if ":" in candidate and not re.match(r"\d{4}", candidate):
                        accounts.add(candidate)
        return sorted(accounts)

    def get_recent_examples(self, n: int = 10) -> list[dict]:
        """Return the last n categorised transactions as {description, account} dicts."""
        if not self.path.exists():
            return []
        text = self.path.read_text()
        examples: list[dict] = []
        payee = None
        for line in text.splitlines():
            # hledger transaction header: "2025-06-01 Payee"
            if re.match(r"\d{4}-\d{2}-\d{2}\s", line):
                payee = " ".join(line.split()[1:]).strip()
            elif payee and line.strip() and not line.strip().startswith(";"):
                parts = re.split(r"\s{2,}", line.strip())
                if parts and ":" in parts[0] and (parts[0].startswith("expenses") or parts[0].startswith("income")):
                    examples.append({"description": payee, "account": parts[0]})
                    payee = None
        return examples[-n:]

    def transaction_exists(self, date: str, original_description: str, amount: float) -> bool:
        """Check for duplicate by date + original AI description (in ; orig: comment) + amount."""
        if not self.path.exists():
            return False
        amount_str = f"{self.currency} {amount:.2f}"
        text = self.path.read_text()
        # Match against stored original description comment
        orig_pattern = f"; orig: {original_description}".lower()
        if orig_pattern in text.lower() and amount_str in text:
            return True
        # Fallback: match display name for entries written before this change
        display_pattern = f"{date} {original_description}".lower()
        return display_pattern in text.lower() and amount_str in text

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------

    def append_transactions(
        self, transactions: list[dict], offset_account: str
    ) -> list[dict]:
        """
        Append transactions to the journal.

        Each transaction dict must have: date, description, amount, account.
        account == None means ; TODO entry.

        Returns list of skipped (duplicate) transactions.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text("")

        blocks: list[str] = []
        skipped: list[dict] = []

        for tx in sorted(transactions, key=lambda t: t["date"]):
            original = tx.get("original_description") or tx["description"]
            if self.transaction_exists(tx["date"], original, tx["amount"]):
                skipped.append(tx)
                continue
            blocks.append(self._format_entry(tx, offset_account))

        if blocks:
            existing = self.path.read_text()
            separator = "\n" if existing and not existing.endswith("\n\n") else ""
            self.path.write_text(existing + separator + "\n".join(blocks) + "\n")

        return skipped

    def append_reconciliation_entry(
        self, date_str: str, account: str, adjustment: float, actual_balance: float
    ) -> None:
        """Append a reconciliation adjustment entry with a balance assertion."""
        if abs(adjustment) < 0.005:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text("")

        amount_str = self._fmt_amount(adjustment)
        offset_str = self._fmt_amount(-adjustment)
        actual_num = f"{'-' if actual_balance < 0 else ''}{abs(actual_balance):.2f}"
        block = (
            f"{date_str} Reconcile {account}\n"
            f"    {account:<40}{amount_str}  = {self.currency} {actual_num}\n"
            f"    {'equity:reconciling':<40}{offset_str}\n"
        )
        existing = self.path.read_text()
        separator = "\n" if existing and not existing.endswith("\n\n") else ""
        self.path.write_text(existing + separator + block)

    def _fmt_amount(self, value: float) -> str:
        sign = "-" if value < 0 else ""
        return f"{self.currency} {sign}{abs(value):.2f}"

    # ------------------------------------------------------------------
    # Editing existing transactions
    # ------------------------------------------------------------------

    def find_block_by_line(self, line_no: int):
        """Locate a transaction block by its starting line number (1-indexed).

        Returns (start_line, end_line) indices or None.
        """
        if not self.path.exists():
            return None
        lines = self.path.read_text().splitlines(keepends=True)
        idx = line_no - 1
        if idx < 0 or idx >= len(lines):
            return None
        if not re.match(r"\d{4}-\d{2}-\d{2}\s", lines[idx]):
            return None
        starts = [i for i, line in enumerate(lines)
                  if re.match(r"\d{4}-\d{2}-\d{2}\s", line)]
        starts.append(len(lines))
        for k in range(len(starts) - 1):
            if starts[k] == idx:
                return idx, starts[k + 1]
        return None

    def _find_block(self, date_str: str, description: str, amount: float):
        """Locate a transaction block by date + description + amount.

        Returns (start_line, end_line) indices or None.
        """
        if not self.path.exists():
            return None
        lines = self.path.read_text().splitlines(keepends=True)
        amount_token = f"{self.currency} {abs(amount):.2f}"
        starts = [i for i, line in enumerate(lines)
                  if re.match(r"\d{4}-\d{2}-\d{2}\s", line)]
        starts.append(len(lines))
        for k in range(len(starts) - 1):
            start, end = starts[k], starts[k + 1]
            hm = re.match(
                r"(\d{4}-\d{2}-\d{2})\s+(.*?)(?:\s+;\s*(.*))?$",
                lines[start].rstrip("\n"),
            )
            if not hm:
                continue
            hdate, hdesc = hm.group(1), hm.group(2).strip()
            if hdate == date_str and hdesc == description:
                if amount_token in "".join(lines[start:end]):
                    return start, end
        return None

    def edit_transaction(
        self, date_str: str, description: str, amount: float, changes: dict,
        line_no: int | None = None,
    ) -> bool:
        """Rewrite a transaction found by line_no (preferred) or date+description+amount.

        changes may contain: date, description, amount, account, offset_account.
        Returns True if found and edited.
        """
        found = self.find_block_by_line(line_no) if line_no else self._find_block(date_str, description, amount)
        if not found:
            return False
        start, end = found
        lines = self.path.read_text().splitlines(keepends=True)

        hm = re.match(
            r"(\d{4}-\d{2}-\d{2})\s+(.*?)(?:\s+;\s*(.*))?$",
            lines[start].rstrip("\n"),
        )
        hdate, hdesc, hcomment = hm.group(1), hm.group(2).strip(), hm.group(3)

        new_date = changes.get("date", hdate)
        new_desc = changes.get("description", hdesc)
        new_amount = changes.get("amount")
        new_account = changes.get("account")
        new_offset = changes.get("offset_account")

        hdr = f"{new_date} {new_desc}"
        if hcomment:
            hdr += f"  ; {hcomment}"

        out = [hdr + "\n"]
        posting_idx = 0
        for ln in lines[start + 1:end]:
            s = ln.rstrip("\n")
            stripped = s.strip()
            if not stripped or stripped.startswith(";"):
                out.append(ln)
                continue
            m = re.match(r"(\s+)(\S+)(.*)$", s)
            if not m:
                out.append(ln)
                continue
            indent, acct, rest = m.group(1), m.group(2), m.group(3)

            if posting_idx == 0 and new_account:
                acct = new_account
            elif posting_idx == 1 and new_offset:
                acct = new_offset
            posting_idx += 1
            if new_amount is not None and self.currency in rest:
                sign_m = re.search(rf"{re.escape(self.currency)}\s*(-?)", rest)
                sign = sign_m.group(1) if sign_m else ""
                rest = re.sub(
                    rf"({re.escape(self.currency)})\s*-?\s*[\d,]+\.\d{{2}}",
                    rf"\g<1> {sign}{new_amount:.2f}", rest, count=1,
                )
            out.append(f"{indent}{acct}{rest}\n")

        lines[start:end] = out
        self.path.write_text("".join(lines))
        return True

    def delete_transaction(self, date_str: str, description: str, amount: float,
                           line_no: int | None = None) -> bool:
        """Remove a transaction found by line_no (preferred) or date+description+amount.

        Returns True if found and removed.
        """
        found = self.find_block_by_line(line_no) if line_no else self._find_block(date_str, description, amount)
        if not found:
            return False
        start, end = found
        lines = self.path.read_text().splitlines(keepends=True)
        del lines[start:end]
        self.path.write_text("".join(lines))
        return True

    def amortize_transaction(
        self, date_str: str, description: str, amount: float, months: float,
        line_no: int | None = None,
    ) -> bool:
        """Replace a lump expense with a prepaid asset + monthly amortisation entries.

        months may be fractional (e.g. 2.5): full months pay an equal share and a
        final partial month pays the remainder. Returns True if found and rewritten.
        """
        found = self.find_block_by_line(line_no) if line_no else self._find_block(date_str, description, amount)
        if not found:
            return False
        start, end = found
        lines = self.path.read_text().splitlines(keepends=True)

        hm = re.match(
            r"(\d{4}-\d{2}-\d{2})\s+(.*?)(?:\s+;\s*(.*))?$",
            lines[start].rstrip("\n"),
        )
        hdate, hdesc, hcomment = hm.group(1), hm.group(2).strip(), hm.group(3)

        entries = math.ceil(months)
        per = round(amount / months, 2)
        if per <= 0:
            return False

        offset = None
        cat = "expenses:unknown"
        for ln in lines[start + 1:end]:
            m = re.match(r"\s*(\S+)", ln)
            if not m:
                continue
            acct = m.group(1)
            if acct.startswith("expenses:"):
                cat = acct
            elif offset is None and (
                acct.startswith("assets:") or acct.startswith("liabilities:")
            ):
                offset = acct
        if offset is None:
            return False

        prepaid = "assets:prepaid:" + cat.split(":", 1)[1]
        orig = f"  ; {hcomment}" if hcomment else ""
        out = [
            f"{hdate} {hdesc}{orig}  ; prepaid\n",
            f"    {prepaid:<40}{self.currency} {amount:.2f}\n",
            f"    {offset}\n",
        ]

        y, mth, d = int(hdate[:4]), int(hdate[5:7]), int(hdate[8:10])
        month_label = f"{months:g}"
        for i in range(1, entries + 1):
            ddate = self._add_months(y, mth, d, i - 1)
            amt = per if i < entries else round(amount - per * (entries - 1), 2)
            out.append(f"{ddate} {hdesc}  ; amortize {i}/{month_label}\n")
            out.append(f"    {cat:<40}{self.currency} {amt:.2f}\n")
            out.append(f"    {prepaid}\n")

        lines[start:end] = out
        self.path.write_text("".join(lines))
        return True

    @staticmethod
    def _add_months(y: int, m: int, d: int, delta: int) -> str:
        total = y * 12 + (m - 1) + delta
        yy, mm = divmod(total, 12)
        mm += 1
        last = calendar.monthrange(yy, mm)[1]
        return f"{yy:04d}-{mm:02d}-{min(d, last):02d}"

    def _format_entry(self, tx: dict, offset_account: str) -> str:
        account = tx.get("account")
        amount_str = f"{self.currency} {tx['amount']:.2f}"
        is_income = tx.get("type") == "income"
        original = tx.get("original_description") or tx["description"]
        orig_comment = f"  ; orig: {original}" if original != tx["description"] else ""

        if is_income:
            income_account = account or "income:unknown"
            return (
                f"{tx['date']} {tx['description']}{orig_comment}\n"
                f"    {offset_account:<40}{amount_str}\n"
                f"    {income_account}\n"
            )
        elif account:
            return (
                f"{tx['date']} {tx['description']}{orig_comment}\n"
                f"    {account:<40}{amount_str}\n"
                f"    {offset_account}\n"
            )
        else:
            return (
                f"{tx['date']} {tx['description']}  ; TODO\n"
                f"    expenses:unknown              {amount_str}  ; TODO\n"
                f"    {offset_account}\n"
            )
