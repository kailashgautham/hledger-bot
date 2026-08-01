import json
from pathlib import Path
from typing import Optional


class StateManager:
    def __init__(self, path: Path):
        self.path = path
        self._data = self._load()

    def _load(self) -> dict:
        if self.path.exists():
            with open(self.path) as f:
                return json.load(f)
        return {"last_date": None, "card_last_dates": {}}

    def get_last_date(self, card: Optional[str] = None) -> Optional[str]:
        if card:
            return self._data.get("card_last_dates", {}).get(card)
        return self._data.get("last_date")

    def set_last_date(self, date_str: str, card: Optional[str] = None) -> None:
        self._data["last_date"] = date_str
        if card:
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
