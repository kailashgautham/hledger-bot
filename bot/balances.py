"""Parse a simple hledger journal and compute per-account balances.

Only the subset of hledger syntax this bot writes (plus a few common
variations) is handled. Multi-commodity transactions are balanced on the
commodity with the largest known amount.
"""
import re
from collections import defaultdict

_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:=\d{4}-\d{2}-\d{2})?\s+")
_AMOUNT_RE = re.compile(
    r"\A(?P<c1>[A-Za-z$€£¥₹][\w.$€£¥₹]*)?\s*"
    r"(?P<num>[+\-]?\d[\d,]*(?:\.\d+)?)\s*"
    r"(?P<c2>[A-Za-z$€£¥₹][\w.$€£¥₹]*)?\Z"
)
_STATUS_RE = re.compile(r"^[!*]\s*")


def parse_amount(text: str):
    """Return (amount, commodity) or None if text holds no amount."""
    text = re.sub(r"\s*=\s*\[.*?\]\s*$", "", text.strip())
    text = re.sub(r"\s*=\s*\S.*$", "", text)
    if ";" in text:
        text = text.split(";", 1)[0].strip()
    if not text:
        return None
    m = _AMOUNT_RE.match(text)
    if not m:
        return None
    commodity = m.group("c1") or m.group("c2") or ""
    return float(m.group("num").replace(",", "")), commodity


def _parse_posting(line: str):
    line = _STATUS_RE.sub("", line.strip())
    if not line or line.startswith(";"):
        return None
    parts = [p for p in re.split(r"\s{2,}", line) if p.strip() and not p.strip().startswith("=")]
    if not parts:
        return None
    account = parts[0].strip()
    amount = parse_amount(" ".join(parts[1:])) if len(parts) > 1 else None
    return account, amount


def _apply(postings, balances) -> None:
    known = [p for p in postings if p[1] is not None]
    missing = [p for p in postings if p[1] is None]

    if missing:
        # Best-effort balancing: sum known amounts, pick the dominant commodity.
        totals: dict[str, float] = defaultdict(float)
        for _, (amount, commodity) in known:
            totals[commodity] += amount
        if totals:
            commodity = max(totals, key=lambda c: abs(totals[c]))
            missing_amount = -totals[commodity]
        else:
            commodity, missing_amount = "", 0.0
        for account, _ in missing:
            balances[account][commodity] += missing_amount

    for account, (amount, commodity) in known:
        balances[account][commodity] += amount


def account_balances(journal_text: str) -> dict[str, dict[str, float]]:
    """Return {account: {commodity: amount}} across the whole journal."""
    balances: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    postings = None

    def flush() -> None:
        nonlocal postings
        if postings is not None:
            _apply(postings, balances)
            postings = None

    for line in journal_text.splitlines():
        if _DATE_RE.match(line):
            flush()
            postings = []
        elif postings is not None and line[:1].isspace():
            posting = _parse_posting(line)
            if posting:
                postings.append(posting)
        elif postings is not None:
            flush()
    flush()

    return {account: dict(comm) for account, comm in balances.items()}


def account_total(balances: dict[str, dict[str, float]], account: str, currency: str) -> float:
    amounts = balances.get(account, {})
    if currency in amounts:
        return amounts[currency]
    return sum(amounts.values())
