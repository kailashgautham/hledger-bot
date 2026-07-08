from abc import ABC, abstractmethod
from typing import TypedDict


class Transaction(TypedDict, total=False):
    date: str        # ISO format: 2025-06-01
    description: str
    amount: float    # always positive
    type: str        # "expense" (money out) or "income" (money in)


class BaseParser(ABC):
    # Strings that identify this bank in the PDF text
    IDENTIFIERS: list[str] = []
    card_name: str = ""
    offset_account: str = "liabilities:creditcard:unknown"

    def detect(self, text: str) -> bool:
        upper = text.upper()
        return any(s.upper() in upper for s in self.IDENTIFIERS)

    @abstractmethod
    def parse(self, pdf_path: str) -> list[Transaction]:
        ...
