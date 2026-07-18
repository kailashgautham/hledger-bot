"""Telegram bot entrypoint."""
import logging
import os
import re
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
from .parser.ai_parser import AIParser
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



def _keyboard(idx: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Confirm", callback_data=f"confirm|{idx}"),
            InlineKeyboardButton("✏️ Name", callback_data=f"name|{idx}"),
        ],
        [
            InlineKeyboardButton("📂 Category", callback_data=f"category|{idx}"),
            InlineKeyboardButton("✂️ Split", callback_data=f"split|{idx}"),
        ],
        [
            InlineKeyboardButton("⏭ Skip", callback_data=f"skip|{idx}"),
        ],
    ])


def _account_keyboard(accounts: list[str], idx: int) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(a, callback_data=f"acc|{idx}|{i}")] for i, a in enumerate(accounts)]
    rows.append([
        InlineKeyboardButton("✍️ Custom", callback_data=f"custom|{idx}"),
        InlineKeyboardButton("« Back", callback_data=f"back|{idx}"),
    ])
    return InlineKeyboardMarkup(rows)


def _tx_message(tx: dict, idx: int, total: int) -> str:
    suggestion = tx.get("ai_suggestion")
    confidence = tx.get("ai_confidence", 0.0)
    currency = config.get("currency", "SGD")
    is_income = tx.get("type") == "income"
    direction = "↑ income" if is_income else "↓ expense"
    amount_line = f"{direction}  {currency} {tx['amount']:.2f}  |  {tx['date']}"
    if tx.get("original_amount"):
        amount_line += f"  |  ✂️ split from {currency} {tx['original_amount']:.2f}"
    lines = [
        f"*Transaction {idx + 1}/{total}*",
        f"`{tx['description']}`",
        amount_line,
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
    session["waiting_for_card"] = False
    session["waiting_for_name"] = False
    session["waiting_for_custom"] = False
    session["waiting_for_split"] = False
    session["filtered_accounts"] = []

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
    offset_account = session["offset_account"]
    all_txns = session["auto_categorized"] + session["confirmed"]
    todo_txns = session["todo"]

    skipped = writer.append_transactions(all_txns, offset_account)

    if all_txns:
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
        lines.append(f"  ⏭ {todo_count} skipped (not recorded)")
    if dup_count:
        lines.append(f"  ⚠️ {dup_count} duplicates skipped")
    if account_totals:
        lines.append("")
        for acc, total in sorted(account_totals.items()):
            lines.append(f"  `{acc:<40}` {currency} {total:.2f}")
    if all_txns:
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

    await _process_transactions(
        parser.card_name, parser.offset_account, transactions, status_msg, chat_id, context,
        source="PDF"
    )


async def _process_transactions(
    card_name: str,
    offset_account: str,
    transactions: list,
    status_msg,
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    source: str = "PDF",
) -> None:
    if not transactions:
        await status_msg.edit_text(f"❌ No transactions found in the {source}.")
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

    # Stash raw transactions and ask the user to confirm the card name first
    user_sessions[chat_id] = {
        "card_name": card_name,
        "offset_account": offset_account,
        "raw_transactions": new_txns,
        "auto_categorized": [],
        "pending": [],
        "confirmed": [],
        "todo": [],
        "current_idx": 0,
        "waiting_for_card": True,
        "waiting_for_name": False,
        "waiting_for_custom": False,
        "waiting_for_split": False,
        "filtered_accounts": [],
        "start_date": start_date,
        "end_date": end_date,
    }

    await status_msg.edit_text(
        f"Found {len(new_txns)} transactions ({start_date} → {end_date}).\n\n"
        f"*Card:* {card_name}\n"
        f"*Account:* `{offset_account}`\n\n"
        f"Confirm or enter manually:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(f"✅ Confirm", callback_data="cardconfirm")],
            [InlineKeyboardButton("✍️ Enter manually", callback_data="cardcustom")],
        ]),
    )


async def _start_categorisation(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Run after card name is confirmed — categorise and begin the per-tx flow."""
    session = user_sessions[chat_id]
    new_txns = session.pop("raw_transactions")
    session["waiting_for_card"] = False

    await context.bot.send_message(chat_id=chat_id, text="Categorising…")

    currency = config.get("currency", "SGD")
    accounts = writer.get_accounts()
    examples = writer.get_recent_examples()

    for tx in new_txns:
        known_account = merchant_map.lookup(tx["description"])
        if known_account:
            session["auto_categorized"].append({**tx, "account": known_account, "status": "auto"})
        else:
            is_income = tx.get("type") == "income"
            if is_income:
                suggestion = ("income:unknown", 0.5)
            else:
                suggestion = categoriser.suggest_category(
                    tx["description"], tx["amount"], currency, accounts, examples
                )
            session["pending"].append({
                **tx,
                "ai_suggestion": suggestion[0] if suggestion else None,
                "ai_confidence": suggestion[1] if suggestion else 0.0,
            })

    if session["pending"]:
        await _send_next(chat_id, context)
    else:
        await _finish_session(chat_id, context)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    if not is_allowed(user_id):
        await update.message.reply_text("Unauthorized.")
        return

    status_msg = await update.message.reply_text("⏳ Downloading and parsing image…")

    photo = update.message.photo[-1]  # highest resolution
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        img_path = tmp.name

    try:
        tg_file = await context.bot.get_file(photo.file_id)
        await tg_file.download_to_drive(img_path)

        ai_parser = AIParser(config)
        if not ai_parser.available:
            await status_msg.edit_text(
                "❌ AI parser not configured. Set GROQ_API_KEY or GOOGLE_API_KEY."
            )
            return

        transactions = ai_parser.parse_image(img_path)
    finally:
        os.unlink(img_path)

    await _process_transactions(
        ai_parser.card_name, ai_parser.offset_account, transactions, status_msg, chat_id, context,
        source="image"
    )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    chat_id = update.effective_chat.id
    session = user_sessions.get(chat_id)
    if not session:
        await query.edit_message_text("Session expired. Send the PDF again.")
        return

    # --- Card confirmation (no |idx suffix) ---
    if query.data == "cardconfirm":
        await query.edit_message_text(
            f"✅ Card: *{session['card_name']}*", parse_mode=ParseMode.MARKDOWN
        )
        await _start_categorisation(chat_id, context)
        return

    if query.data == "cardcustom":
        session["waiting_for_card"] = True
        await query.edit_message_text(
            "Type the card or bank name (e.g. `SC SimplyCash`, `DBS Multiplier`):",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    parts = query.data.split("|")
    action = parts[0]
    idx = int(parts[1])

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

    elif action == "name":
        session["waiting_for_name"] = True
        session["waiting_for_category"] = False
        await query.edit_message_text(
            f"Type a new name for this merchant\n(current: `{tx['description']}`):",
            parse_mode=ParseMode.MARKDOWN,
        )

    elif action == "category":
        accounts = writer.get_accounts()
        session["filtered_accounts"] = accounts
        session["waiting_for_category"] = False
        session["waiting_for_name"] = False
        header = f"📂 *{tx['description']}* — pick a category:"
        await query.edit_message_text(
            header,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_account_keyboard(accounts, idx),
        )

    elif action == "acc":
        acc_idx = int(parts[2])
        account = session["filtered_accounts"][acc_idx]
        merchant_map.save(tx["description"], account)
        session["confirmed"].append({**tx, "account": account, "status": "confirmed"})
        await query.edit_message_text(
            f"✅ `{tx['description']}` → `{account}`", parse_mode=ParseMode.MARKDOWN
        )
        session["current_idx"] += 1
        await _send_next(chat_id, context)

    elif action == "split":
        session["waiting_for_split"] = True
        session["waiting_for_name"] = False
        session["waiting_for_custom"] = False
        currency = config.get("currency", "SGD")
        original = tx.get("original_amount", tx["amount"])
        await query.edit_message_text(
            f"✂️ *Split* — total was {currency} {original:.2f}\n"
            f"How much was *your* share?\n"
            f"Type an amount (e.g. `15.50`) or a percentage (e.g. `50%`):",
            parse_mode=ParseMode.MARKDOWN,
        )

    elif action == "custom":
        session["waiting_for_custom"] = True
        session["waiting_for_name"] = False
        session["waiting_for_split"] = False
        await query.edit_message_text(
            f"Type an account name for `{tx['description']}`\n(e.g. `expenses:food:dining`):",
            parse_mode=ParseMode.MARKDOWN,
        )

    elif action == "back":
        msg = _tx_message(tx, idx, len(session["pending"]))
        await query.edit_message_text(
            msg, parse_mode=ParseMode.MARKDOWN, reply_markup=_keyboard(idx)
        )
        session["waiting_for_custom"] = False
        session["waiting_for_name"] = False

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

    if not session:
        return

    text = update.message.text.strip()

    if session.get("waiting_for_card"):
        if not text:
            await update.message.reply_text("Card name can't be empty.")
            return
        session["card_name"] = text
        session["waiting_for_card"] = False
        # Re-derive offset account from the user's name, keeping credit/debit type
        existing = session["offset_account"]
        slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
        bank_slug = slug.split("-")[0]  # e.g. "uob:one" → "uob-one" → "uob"
        if existing.startswith("assets:bank:"):
            session["offset_account"] = f"assets:bank:{bank_slug}"
        elif existing.startswith("liabilities:creditcard:"):
            session["offset_account"] = f"liabilities:creditcard:{bank_slug}"
        await update.message.reply_text(
            f"✅ Card: *{text}*  |  Account: `{session['offset_account']}`",
            parse_mode=ParseMode.MARKDOWN,
        )
        await _start_categorisation(chat_id, context)
        return

    idx = session["current_idx"]
    tx = session["pending"][idx]

    if session.get("waiting_for_name"):
        if not text:
            await update.message.reply_text("Name can't be empty.")
            return
        tx = session["pending"][idx]
        if "original_description" not in tx:
            session["pending"][idx]["original_description"] = tx["description"]
        session["pending"][idx]["description"] = text
        session["waiting_for_name"] = False
        await update.message.reply_text(f"Name updated to `{text}`.", parse_mode=ParseMode.MARKDOWN)
        await _send_next(chat_id, context)

    elif session.get("waiting_for_split"):
        currency = config.get("currency", "SGD")
        original = tx.get("original_amount", tx["amount"])
        try:
            if text.endswith("%"):
                pct = float(text[:-1])
                if not (0 < pct < 100):
                    raise ValueError
                my_share = round(original * pct / 100, 2)
            else:
                my_share = round(float(text), 2)
                if my_share <= 0 or my_share >= original:
                    raise ValueError
        except ValueError:
            await update.message.reply_text(
                f"Invalid input. Enter an amount less than {currency} {original:.2f}, or a percentage like `50%`.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        session["pending"][idx]["original_amount"] = original
        session["pending"][idx]["amount"] = my_share
        session["waiting_for_split"] = False
        await update.message.reply_text(
            f"✂️ Your share: *{currency} {my_share:.2f}* (others: {currency} {original - my_share:.2f})",
            parse_mode=ParseMode.MARKDOWN,
        )
        await _send_next(chat_id, context)

    elif session.get("waiting_for_custom"):
        if not text:
            await update.message.reply_text("Account name can't be empty.")
            return
        merchant_map.save(tx["description"], text)
        session["confirmed"].append({**tx, "account": text, "status": "changed"})
        session["waiting_for_custom"] = False
        session["current_idx"] += 1
        await update.message.reply_text(
            f"✅ `{tx['description']}` → `{text}`", parse_mode=ParseMode.MARKDOWN
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
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
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
