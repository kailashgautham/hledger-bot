import json
from pathlib import Path
from typing import Optional


class MerchantMap:
    def __init__(self, path: Path):
        self.path = path
        self._data: dict[str, str] = self._load()

    def _load(self) -> dict[str, str]:
        if self.path.exists():
            with open(self.path) as f:
                return json.load(f)
        return {}

    def reload(self) -> None:
        """Re-read from disk; the file is also written by imports."""
        self._data = self._load()

    def lookup(self, description: str) -> Optional[str]:
        desc_upper = description.upper()
        for key, account in self._data.items():
            if key.upper() in desc_upper:
                return account
        return None

    def save(self, key: str, account: str) -> None:
        self._data[key.upper()] = account
        self._write()

    def to_dict(self) -> dict[str, str]:
        return dict(self._data)

    def delete(self, key: str) -> bool:
        """Remove a rule. Returns False if it was not there."""
        if self._data.pop(key.upper(), None) is None:
            return False
        self._write()
        return True

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w") as f:
            json.dump(self._data, f, indent=2, sort_keys=True)
