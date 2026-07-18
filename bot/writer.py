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
            if stripped and not stripped.startswith(";") and "  " in stripped:
                parts = re.split(r"\s{2,}", stripped)
                if parts:
                    candidate = parts[0].strip()
                    if ":" in candidate and not re.match(r"\d{4}", candidate):
                        accounts.add(candidate)
        return sorted(accounts)

    def get_recent_examples(self, n: int = 6) -> list[dict]:
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
                if parts and ":" in parts[0] and parts[0].startswith("expenses"):
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
