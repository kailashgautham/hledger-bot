"""Telegram bot entrypoint."""
import logging
import os
import tempfile
from pathlib import Path

from telegram import (
    Document,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .categoriser import Categoriser
from .config import journal_dir, load_config, merchant_map_path, state_path
from .git_ops import GitOps
from .merchant_map import MerchantMap
from .parser import get_parser
from .state import StateManager
from .writer import JournalWriter

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Global singletons (initialised in main())
# ------------------------------------------------------------------
config: dict = {}
state_mgr: StateManager
merchant_map: MerchantMap
categoriser: Categoriser
writer: JournalWriter
git_ops: GitOps

# Per-user session: user_id → session dict
user_sessions: dict[int, dict] = {}


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def is_allowed(user_id: int) -> bool:
    allowed = config.get("telegram", {}).get("allowed_user_ids", [])
    return not allowed or user_id in allowed


def card_config_for(card_name: str) -> dict:
    needle = card_name.lower()
    for card in config.get("cards", []):
        name = card["name"].lower()
        if name == needle or name in needle or needle in name:
            return card
    return {"name": card_name, "liability_account": "liabilities:creditcard:unknown"}


def _keyboard(idx: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Confirm", callback_data=f"confirm|{idx}"),
            InlineKeyboardButton("✏️ Change", callback_data=f"change|{idx}"),
            InlineKeyboardButton("⏭ Skip", callback_data=f"skip|{idx}"),
        ]
    ])


def _tx_message(tx: dict, idx: int, total: int) -> str:
    suggestion = tx.get("ai_suggestion")
    confidence = tx.get("ai_confidence", 0.0)
    lines = [
        f"*Transaction {idx + 1}/{total}*",
        f"`{tx['description']}`",
        f"Amount: {config.get('currency', 'SGD')} {tx['amount']:.2f}  |  Date: {tx['date']}",
    ]
    if suggestion:
        conf_pct = int(confidence * 100)
        lines.append(f"\nBest guess: `{suggestion}` ({conf_pct}% confident)")
    else:
        lines.append("\nNo AI suggestion — please choose an account.")
    return "\n".join(lines)


# ------------------------------------------------------------------
# Session management
# ------------------------------------------------------------------

async def _send_next(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    session = user_sessions.get(chat_id)
    if not session:
        return
    pending = session["pending"]
    idx = session["current_idx"]

    if idx >= len(pending):
        await _finish_session(chat_id, context)
        return

    tx = pending[idx]
    total = len(pending)
    msg = _tx_message(tx, idx, total)
    await context.bot.send_message(
        chat_id=chat_id,
        text=msg,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_keyboard(idx),
    )


async def _finish_session(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    session = user_sessions.pop(chat_id, None)
    if not session:
        return

    card_name = session["card_name"]
    card_cfg = card_config_for(card_name)
    all_txns = session["auto_categorized"] + session["confirmed"]
    todo_txns = session["todo"]

    skipped = writer.append_transactions(all_txns + todo_txns, card_cfg)

    if all_txns or todo_txns:
        end_date = session["end_date"]
        start_date = session["start_date"]
        state_mgr.set_last_date(end_date, card_name)

        jpath = config["hledger"]["journal_path"]
        jdir = journal_dir(config)
        files = [
            str(Path(jpath).relative_to(jdir)),
            "merchant_map.json",
            "state.json",
        ]
        commit_msg = (
            f"Add transactions {start_date} to {end_date} [{card_name}]"
        )
        success, err = git_ops.commit_and_push(commit_msg, files)

    # Build summary
    currency = config.get("currency", "SGD")
    auto_count = len(session["auto_categorized"])
    confirmed_count = len(session["confirmed"])
    todo_count = len(todo_txns)
    dup_count = len(skipped)

    account_totals: dict[str, float] = {}
    for tx in all_txns:
        acc = tx.get("account", "expenses:unknown")
        account_totals[acc] = account_totals.get(acc, 0.0) + tx["amount"]

    lines = ["*Done!* Here's a summary:"]
    if auto_count:
        lines.append(f"  ✅ {auto_count} auto-categorised")
    if confirmed_count:
        lines.append(f"  ✅ {confirmed_count} confirmed by you")
    if todo_count:
        lines.append(f"  ⏭ {todo_count} skipped (; TODO in journal)")
    if dup_count:
        lines.append(f"  ⚠️ {dup_count} duplicates skipped")
    if account_totals:
        lines.append("")
        for acc, total in sorted(account_totals.items()):
            lines.append(f"  `{acc:<40}` {currency} {total:.2f}")
    if all_txns or todo_txns:
        lines.append(f"\nAppended to `journal.hledger`")
        if success:
            lines.append(f"Committed & pushed: _{commit_msg}_")
        else:
            lines.append(f"⚠️ Commit local only — push failed: {err}")

    await context.bot.send_message(
        chat_id=chat_id,
        text="\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
    )


# ------------------------------------------------------------------
# Handlers
# ------------------------------------------------------------------

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    if not is_allowed(user_id):
        await update.message.reply_text("Unauthorized.")
        return

    doc: Document = update.message.document
    if doc.mime_type != "application/pdf":
        await update.message.reply_text("Please send a PDF file.")
        return

    status_msg = await update.message.reply_text("⏳ Downloading and parsing PDF…")

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        pdf_path = tmp.name

    try:
        tg_file = await context.bot.get_file(doc.file_id)
        await tg_file.download_to_drive(pdf_path)

        parser = get_parser(pdf_path, config)
        if not parser:
            await status_msg.edit_text(
                "❌ Could not detect bank/card type. "
                "Check the PDF or add a parser for your bank."
            )
            return

        transactions = parser.parse(pdf_path)
    finally:
        os.unlink(pdf_path)

    card_name = parser.card_name
    if not transactions:
        await status_msg.edit_text("❌ No transactions found in the PDF.")
        return

    last_date = state_mgr.get_last_date(card_name)
    new_txns = [
        t for t in transactions if not last_date or t["date"] > last_date
    ]

    if not new_txns:
        await status_msg.edit_text(
            f"Nothing new since {last_date} for {card_name}."
        )
        return

    dates = [t["date"] for t in new_txns]
    start_date, end_date = min(dates), max(dates)

    await status_msg.edit_text(
        f"Found {len(new_txns)} transactions ({start_date} → {end_date}). "
        f"Categorising…"
    )

    currency = config.get("currency", "SGD")
    accounts = writer.get_accounts()
    examples = writer.get_recent_examples()

    auto_categorized: list[dict] = []
    pending: list[dict] = []

    for tx in new_txns:
        known_account = merchant_map.lookup(tx["description"])
        if known_account:
            auto_categorized.append({**tx, "account": known_account, "status": "auto"})
        else:
            suggestion = categoriser.suggest_category(
                tx["description"], tx["amount"], currency, accounts, examples
            )
            pending.append({
                **tx,
                "ai_suggestion": suggestion[0] if suggestion else None,
                "ai_confidence": suggestion[1] if suggestion else 0.0,
            })

    user_sessions[chat_id] = {
        "card_name": card_name,
        "auto_categorized": auto_categorized,
        "pending": pending,
        "confirmed": [],
        "todo": [],
        "current_idx": 0,
        "waiting_for_custom": False,
        "start_date": start_date,
        "end_date": end_date,
    }

    if pending:
        await _send_next(chat_id, context)
    else:
        await _finish_session(chat_id, context)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    chat_id = update.effective_chat.id
    session = user_sessions.get(chat_id)
    if not session:
        await query.edit_message_text("Session expired. Send the PDF again.")
        return

    action, idx_str = query.data.split("|", 1)
    idx = int(idx_str)

    if idx != session["current_idx"]:
        return  # stale button

    tx = session["pending"][idx]

    if action == "confirm":
        account = tx["ai_suggestion"] or "expenses:unknown"
        merchant_map.save(tx["description"], account)
        session["confirmed"].append({**tx, "account": account, "status": "confirmed"})
        await query.edit_message_text(
            f"✅ `{tx['description']}` → `{account}`", parse_mode=ParseMode.MARKDOWN
        )
        session["current_idx"] += 1
        await _send_next(chat_id, context)

    elif action == "change":
        session["waiting_for_custom"] = True
        await query.edit_message_text(
            f"Type the account for `{tx['description']}` (e.g. `expenses:food:dining`):",
            parse_mode=ParseMode.MARKDOWN,
        )

    elif action == "skip":
        session["todo"].append({**tx, "account": None, "status": "todo"})
        await query.edit_message_text(
            f"⏭ `{tx['description']}` → marked ; TODO", parse_mode=ParseMode.MARKDOWN
        )
        session["current_idx"] += 1
        await _send_next(chat_id, context)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    session = user_sessions.get(chat_id)

    if not session or not session.get("waiting_for_custom"):
        return  # not in a session — ignore plain text

    account = update.message.text.strip()
    if not account:
        await update.message.reply_text("Please enter a valid account name.")
        return

    idx = session["current_idx"]
    tx = session["pending"][idx]
    merchant_map.save(tx["description"], account)
    session["confirmed"].append({**tx, "account": account, "status": "changed"})
    session["waiting_for_custom"] = False
    session["current_idx"] += 1

    await update.message.reply_text(
        f"✅ `{tx['description']}` → `{account}`", parse_mode=ParseMode.MARKDOWN
    )
    await _send_next(chat_id, context)


# ------------------------------------------------------------------
# Commands
# ------------------------------------------------------------------

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return
    state = state_mgr.to_dict()
    todo_lines = git_ops.todo_entries(config["hledger"]["journal_path"])
    lines = ["*Status*"]
    card_dates = state.get("card_last_dates", {})
    if card_dates:
        for card, date in card_dates.items():
            lines.append(f"  {card}: last processed `{date}`")
    else:
        lines.append("  No cards processed yet.")
    if todo_lines:
        lines.append(f"\n⚠️ {len(todo_lines)} ; TODO entries in journal:")
        for l in todo_lines[:5]:
            lines.append(f"  `{l}`")
        if len(todo_lines) > 5:
            lines.append(f"  … and {len(todo_lines) - 5} more")
    else:
        lines.append("\n✅ No ; TODO entries.")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return
    accounts = writer.get_accounts()
    if not accounts:
        await update.message.reply_text("No accounts found in journal yet.")
        return
    text = "*Accounts in journal:*\n" + "\n".join(f"  `{a}`" for a in accounts)
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_undo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return
    await update.message.reply_text("Reverting last commit…")
    success, err = git_ops.revert_last_commit()
    if success:
        msg = "✅ Last commit reverted and pushed."
        if err:
            msg = f"✅ Reverted locally.\n⚠️ {err}"
    else:
        msg = f"❌ Revert failed: {err}"
    await update.message.reply_text(msg)


async def cmd_merchants(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return
    data = merchant_map.to_dict()
    if not data:
        await update.message.reply_text("Merchant map is empty.")
        return
    lines = ["*Merchant map:*"]
    for merchant, account in sorted(data.items()):
        lines.append(f"  `{merchant}` → `{account}`")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

def main() -> None:
    global config, state_mgr, merchant_map, categoriser, writer, git_ops

    config = load_config()

    jdir = journal_dir(config)
    state_mgr = StateManager(state_path(config))
    merchant_map = MerchantMap(merchant_map_path(config))
    categoriser = Categoriser(config)
    writer = JournalWriter(config["hledger"]["journal_path"], config.get("currency", "SGD"))
    git_ops = GitOps(str(jdir), config["hledger"].get("git_branch", "main"))

    token = os.environ["TELEGRAM_BOT_TOKEN"]
    app = Application.builder().token(token).build()

    app.add_handler(MessageHandler(filters.Document.PDF, handle_document))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("accounts", cmd_accounts))
    app.add_handler(CommandHandler("undo", cmd_undo))
    app.add_handler(CommandHandler("merchants", cmd_merchants))

    logger.info("Bot started. Waiting for messages…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
