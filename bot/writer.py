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

    def transaction_exists(self, date: str, description: str, amount: float) -> bool:
        """Check for an existing entry with same date+description+amount."""
        if not self.path.exists():
            return False
        pattern = f"{date} {description}"
        amount_str = f"{self.currency} {amount:.2f}"
        text = self.path.read_text()
        return pattern in text and amount_str in text

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------

    def append_transactions(
        self, transactions: list[dict], card_config: dict
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

        liability = card_config["liability_account"]
        blocks: list[str] = []
        skipped: list[dict] = []

        for tx in sorted(transactions, key=lambda t: t["date"]):
            if self.transaction_exists(tx["date"], tx["description"], tx["amount"]):
                skipped.append(tx)
                continue
            blocks.append(self._format_entry(tx, liability))

        if blocks:
            existing = self.path.read_text()
            separator = "\n" if existing and not existing.endswith("\n\n") else ""
            self.path.write_text(existing + separator + "\n".join(blocks) + "\n")

        return skipped

    def _format_entry(self, tx: dict, liability: str) -> str:
        account = tx.get("account")
        amount_str = f"{self.currency} {tx['amount']:.2f}"

        if account:
            debit_line = f"    {account:<40}{amount_str}"
            credit_line = f"    {liability}"
            return (
                f"{tx['date']} {tx['description']}\n"
                f"{debit_line}\n"
                f"{credit_line}\n"
            )
        else:
            # TODO entry — still parseable by hledger
            debit_line = f"    expenses:unknown              {amount_str}  ; TODO"
            credit_line = f"    {liability}"
            return (
                f"{tx['date']} {tx['description']}  ; TODO\n"
                f"{debit_line}\n"
                f"{credit_line}\n"
            )
