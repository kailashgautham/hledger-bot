"""Web import & reconcile workflows — mirrors the Telegram bot's behaviour.

Reuses the bot's parser, categoriser, merchant map, state, writer and git
modules so the website produces identical journal output to the bot.
"""
import logging
import os
import re
import secrets
import tempfile
import threading
import time
from datetime import date
from pathlib import Path

from bot.balances import account_balances, account_total
from bot.categoriser import Categoriser
from bot.config import journal_dir, load_config, merchant_map_path, state_path
from bot.git_ops import GitOps
from bot.merchant_map import MerchantMap
from bot.parser import get_parser
from bot.parser.ai_parser import AIParser
from bot.state import StateManager
from bot.writer import JournalWriter

logger = logging.getLogger(__name__)

config = load_config()

state_mgr = StateManager(state_path(config))
merchant_map = MerchantMap(merchant_map_path(config))
categoriser = Categoriser(config)
writer = JournalWriter(config["hledger"]["journal_path"], config.get("currency", "SGD"))
git_ops = GitOps(str(journal_dir(config)), config["hledger"].get("git_branch", "main"))
journal_path = Path(config["hledger"]["journal_path"])
currency = config.get("currency", "SGD")

_lock = threading.Lock()
_wizards: dict[str, dict] = {}
WIZARD_TTL = 60 * 60  # 1 hour

# ------------------------------------------------------------------
# Import wizard
# ------------------------------------------------------------------


def _prune_wizards() -> None:
    now = time.time()
    for wid in [w for w, s in _wizards.items() if now - s["created"] > WIZARD_TTL]:
        _wizards.pop(wid, None)


def _get(wizard_id: str) -> dict:
    if not wizard_id:
        raise KeyError(wizard_id)
    wizard = _wizards.get(wizard_id)
    if not wizard:
        raise KeyError(wizard_id)
    return wizard


def _normalize_dates(transactions: list) -> None:
    if not config.get("force_current_year", True):
        return
    year = date.today().year
    for tx in transactions:
        d = tx.get("date")
        if isinstance(d, str) and len(d) == 10 and d[4] == "-":
            tx["date"] = f"{year}{d[4:]}"


def parse_statement(filename: str, data: bytes) -> dict:
    """Parse an uploaded image/PDF and stage a new wizard session."""
    is_pdf = filename.lower().endswith(".pdf")
    suffix = ".pdf" if is_pdf else ".jpg"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(data)
        path = tmp.name

    try:
        if is_pdf:
            parser = get_parser(path, config)
            if not parser:
                return {"step": "error", "message": "Could not detect bank/card type in the PDF."}
            transactions = parser.parse(path)
            card_name, offset_account = parser.card_name, parser.offset_account
        else:
            ai_parser = AIParser(config)
            if not ai_parser.vision_available:
                return {"step": "error", "message": "Image parsing needs GOOGLE_API_KEY."}
            transactions = ai_parser.parse_image(path)
            card_name, offset_account = ai_parser.card_name, ai_parser.offset_account
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass

    if not transactions:
        return {"step": "error", "message": "No transactions found in the file."}

    configured_names = [c["name"] for c in config.get("cards", [])]
    card_name = state_mgr.canonical_card_name(card_name, configured_names)

    _normalize_dates(transactions)

    last_date = state_mgr.get_last_date(card_name)
    new_txns = [t for t in transactions if not last_date or t["date"] > last_date]

    if not new_txns:
        return {"step": "none", "message": f"Nothing new since {last_date} for {card_name}."}

    dates = [t["date"] for t in new_txns]
    start_date, end_date = min(dates), max(dates)

    with _lock:
        _prune_wizards()
        wid = secrets.token_hex(8)
        _wizards[wid] = {
            "id": wid,
            "created": time.time(),
            "card_name": card_name,
            "offset_account": offset_account,
            "raw_transactions": new_txns,
            "auto_categorized": [],
            "pending": [],
            "confirmed": [],
            "todo": [],
            "current_idx": 0,
            "start_date": start_date,
            "end_date": end_date,
        }

    return {
        "step": "card",
        "wizard_id": wid,
        "card_name": card_name,
        "offset_account": offset_account,
        "count": len(new_txns),
        "start_date": start_date,
        "end_date": end_date,
    }


def _review_view(wizard: dict) -> dict:
    idx = wizard["current_idx"]
    pending = wizard["pending"]
    if idx >= len(pending):
        return _finish_locked(wizard)
    tx = pending[idx]
    return {
        "step": "review",
        "wizard_id": wizard["id"],
        "total": len(pending),
        "current_idx": idx,
        "card_name": wizard["card_name"],
        "tx": {
            "description": tx["description"],
            "original_description": tx.get("original_description"),
            "amount": tx["amount"],
            "original_amount": tx.get("original_amount"),
            "date": tx["date"],
            "type": tx.get("type", "expense"),
            "ai_suggestion": tx.get("ai_suggestion"),
            "ai_confidence": tx.get("ai_confidence", 0.0),
        },
    }


def start_categorisation(wizard_id: str, card_name: str | None = None) -> dict:
    with _lock:
        wizard = _get(wizard_id)
        if card_name and card_name.strip() and card_name.strip() != wizard["card_name"]:
            wizard["card_name"] = card_name.strip()
            slug = re.sub(r"[^a-z0-9]+", "-", wizard["card_name"].lower()).strip("-")
            bank_slug = slug.split("-")[0]
            if wizard["offset_account"].startswith("assets:bank:"):
                wizard["offset_account"] = f"assets:bank:{bank_slug}"
            elif wizard["offset_account"].startswith("liabilities:creditcard:"):
                wizard["offset_account"] = f"liabilities:creditcard:{bank_slug}"

        new_txns = wizard.pop("raw_transactions")
        accounts = writer.get_accounts()
        examples = writer.get_recent_examples()

        for tx in new_txns:
            known_account = merchant_map.lookup(tx["description"])
            if known_account:
                wizard["auto_categorized"].append(
                    {**tx, "account": known_account, "status": "auto"}
                )
            else:
                is_income = tx.get("type") == "income"
                if is_income:
                    suggestion = ("income:unknown", 0.5)
                else:
                    suggestion = categoriser.suggest_category(
                        tx["description"], tx["amount"], currency, accounts, examples
                    )
                wizard["pending"].append({
                    **tx,
                    "ai_suggestion": suggestion[0] if suggestion else None,
                    "ai_confidence": suggestion[1] if suggestion else 0.0,
                })

        return _review_view(wizard)


def tx_action(wizard_id: str, action: str, payload: dict) -> dict:
    with _lock:
        wizard = _get(wizard_id)
        idx = wizard["current_idx"]
        pending = wizard["pending"]
        if not pending or idx >= len(pending):
            return _finish_locked(wizard)

        tx = pending[idx]

        if action == "confirm":
            account = tx.get("ai_suggestion") or "expenses:unknown"
            merchant_map.save(tx["description"], account)
            wizard["confirmed"].append({**tx, "account": account, "status": "confirmed"})
            wizard["current_idx"] += 1

        elif action == "category":
            account = (payload.get("account") or "").strip()
            if not account:
                return {"step": "error", "message": "Account can't be empty."}
            merchant_map.save(tx["description"], account)
            wizard["confirmed"].append({**tx, "account": account, "status": "changed"})
            wizard["current_idx"] += 1

        elif action == "name":
            new_name = (payload.get("description") or "").strip()
            if not new_name:
                return {"step": "error", "message": "Name can't be empty."}
            if "original_description" not in tx:
                tx["original_description"] = tx["description"]
            tx["description"] = new_name

        elif action == "split":
            value = payload.get("value")
            original = tx.get("original_amount", tx["amount"])
            try:
                if isinstance(value, str) and value.strip().endswith("%"):
                    pct = float(value.strip()[:-1])
                    if not (0 < pct < 100):
                        raise ValueError
                    my_share = round(original * pct / 100, 2)
                else:
                    my_share = round(float(value), 2)
                    if my_share <= 0 or my_share >= original:
                        raise ValueError
            except (TypeError, ValueError):
                return {
                    "step": "error",
                    "message": f"Enter an amount less than {currency} {original:.2f}, or a percentage like 50%.",
                }
            tx["original_amount"] = original
            tx["amount"] = my_share

        elif action == "skip":
            wizard["todo"].append({**tx, "account": None, "status": "todo"})
            wizard["current_idx"] += 1

        else:
            return {"step": "error", "message": "Unknown action."}

        return _review_view(wizard)


def _finish_locked(wizard: dict) -> dict:
    card_name = wizard["card_name"]
    offset_account = wizard["offset_account"]
    all_txns = wizard["auto_categorized"] + wizard["confirmed"]
    todo_txns = wizard["todo"]

    skipped = writer.append_transactions(all_txns, offset_account)

    success, err, commit_msg = True, "", None
    if all_txns:
        start_date = wizard["start_date"]
        end_date = wizard["end_date"]
        state_mgr.set_last_date(end_date, card_name)
        jdir = journal_dir(config)
        files = [
            str(journal_path.relative_to(jdir)),
            "merchant_map.json",
            "state.json",
        ]
        commit_msg = f"Add transactions {start_date} to {end_date} [{card_name}]"
        success, err = git_ops.commit_and_push(commit_msg, files)

    account_totals: dict[str, float] = {}
    for tx in all_txns:
        acc = tx.get("account", "expenses:unknown")
        account_totals[acc] = account_totals.get(acc, 0.0) + tx["amount"]

    summary = {
        "auto_count": len(wizard["auto_categorized"]),
        "confirmed_count": len(wizard["confirmed"]),
        "todo_count": len(todo_txns),
        "dup_count": len(skipped),
        "account_totals": account_totals,
        "commit": commit_msg,
        "success": success,
        "error": err or None,
    }
    _wizards.pop(wizard["id"], None)
    return {"step": "finish", "summary": summary}


# ------------------------------------------------------------------
# Account balance reconciliation (bot /reconcile parity)
# ------------------------------------------------------------------


def _balances() -> dict:
    text = journal_path.read_text() if journal_path.exists() else ""
    return account_balances(text)


def reconcile_accounts() -> dict:
    balances = _balances()
    accounts = [
        a for a in sorted(balances) if a.startswith(("assets:", "liabilities:"))
    ]
    reconciled = state_mgr.get_reconciled()
    items = []
    for acc in accounts:
        total = account_total(balances, acc, currency)
        rec = reconciled.get(acc)
        items.append({
            "account": acc,
            "total": round(total, 2),
            "reconciled": rec is not None,
            "matches": bool(rec) and abs(total - rec["balance"]) < 0.005,
            "last_date": (rec or {}).get("date"),
            "last_balance": (rec or {}).get("balance"),
        })
    return {"currency": currency, "accounts": items}


def reconcile_submit(account: str, actual: float) -> dict:
    computed = account_total(_balances(), account, currency)
    return {
        "account": account,
        "computed": round(computed, 2),
        "actual": round(actual, 2),
        "diff": round(actual - computed, 2),
    }


def reconcile_settle(account: str, actual: float, diff: float) -> dict:
    today = date.today().isoformat()
    settled = abs(diff) >= 0.005
    if settled:
        writer.append_reconciliation_entry(today, account, diff, actual)
    state_mgr.set_reconciled(account, actual, today)
    jdir = journal_dir(config)
    files = [str(journal_path.relative_to(jdir)), "state.json"]
    success, err = git_ops.commit_and_push(f"Reconcile {account} ({today})", files)
    return {
        "account": account,
        "actual": round(actual, 2),
        "diff": round(diff, 2),
        "settled": settled,
        "date": today,
        "success": success,
        "error": err or None,
    }


def accounts_list() -> list[str]:
    return writer.get_accounts()
