import difflib
import json
import re
from pathlib import Path
from typing import Optional


def match_card_name(name: str, candidates: list[str]) -> Optional[str]:
    """Return the candidate card name that best matches `name`, or None.

    Matching is case/format-insensitive and tries, in order:
      1. exact match (after normalising punctuation/case),
      2. containment (most specific candidate first),
      3. fuzzy match (SequenceMatcher ratio >= 0.7).

    This keeps the AI-detected card name mapping to a single canonical key
    even though the model returns slightly different spellings each run
    (e.g. "UOB ONE CARD", "UOB Card", "UOB One Card").
    """
    if not name:
        return None

    def norm(s: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", s.lower())

    target = norm(name)
    if not target:
        return None

    seen: dict[str, str] = {}
    for c in candidates:
        n = norm(c)
        if n and n not in seen:
            seen[n] = c

    if target in seen:
        return seen[target]

    for n in sorted(seen, key=len, reverse=True):
        if target in n or n in target:
            return seen[n]

    best, best_ratio = None, 0.0
    for n, c in seen.items():
        ratio = difflib.SequenceMatcher(None, target, n).ratio()
        if ratio > best_ratio:
            best, best_ratio = c, ratio
    return best if best_ratio >= 0.7 else None


class StateManager:
    def __init__(self, path: Path):
        self.path = path
        self._data = self._load()

    def _load(self) -> dict:
        if self.path.exists():
            with open(self.path) as f:
                return json.load(f)
        return {"last_date": None, "card_last_dates": {}}

    def canonical_card_name(
        self, name: str, canonical_names: Optional[list[str]] = None
    ) -> str:
        """Map an AI-detected card name to a stable canonical key.

        Configured card names (config `cards:`) always win, so the same
        physical card is tracked under a single key regardless of how the
        model spells it. Falls back to existing state keys, then to the raw
        name for cards not seen before.
        """
        if not name:
            return name
        if canonical_names:
            match = match_card_name(name, canonical_names)
            if match:
                return match
        match = match_card_name(name, list(self._data.get("card_last_dates", {}).keys()))
        return match or name

    def get_last_date(self, card: Optional[str] = None) -> Optional[str]:
        if card:
            return self._data.get("card_last_dates", {}).get(card)
        return self._data.get("last_date")

    def set_last_date(
        self,
        date_str: str,
        card: Optional[str] = None,
        canonical_names: Optional[list[str]] = None,
    ) -> None:
        self._data["last_date"] = date_str
        if card:
            if canonical_names:
                card = self.canonical_card_name(card, canonical_names)
            self._data.setdefault("card_last_dates", {})[card] = date_str
        self._save()

    def set_reconciled(self, account: str, balance: float, date_str: Optional[str] = None) -> None:
        reconciled = self._data.setdefault("reconciled", {})
        reconciled[account] = {
            "date": date_str or self._today(),
            "balance": round(balance, 2),
        }
        self._save()

    def get_reconciled(self) -> dict:
        return dict(self._data.get("reconciled", {}))

    @staticmethod
    def _today() -> str:
        from datetime import date

        return date.today().isoformat()

    def to_dict(self) -> dict:
        return dict(self._data)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w") as f:
            json.dump(self._data, f, indent=2)
