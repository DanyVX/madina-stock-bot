"""Madina Electronics Telegram Manager.

Manager v1 provides a professional button interface for stock lookup,
stock additions, sales, low-stock alerts, and daily summaries backed by
Google Sheets.
"""

import asyncio
import json
import logging
import os
from datetime import datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

import gspread
from google.oauth2.service_account import Credentials
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("madina-manager")

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"].strip()
SHEET_ID = os.environ["GOOGLE_SHEET_ID"].strip()
GOOGLE_CREDS_JSON = os.environ["GOOGLE_CREDENTIALS_JSON"]
LOW_STOCK_THRESHOLD = int(os.getenv("LOW_STOCK_THRESHOLD", "3"))
CACHE_SECONDS = 60
TZ = ZoneInfo("Asia/Karachi")

AUTHORIZED_USER_IDS = {
    int(value.strip())
    for value in os.getenv("AUTHORIZED_USER_IDS", "").split(",")
    if value.strip().isdigit()
}

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
TRANSACTION_HEADERS = [
    "Timestamp",
    "Type",
    "Brand",
    "Category",
    "Model",
    "Variant",
    "Quantity",
    "Unit Price",
    "Total",
    "Payment",
    "Telegram User ID",
    "Telegram Username",
    "Notes",
]

HEADER_ALIASES = {
    "brand": {"brand", "company", "make"},
    "category": {"category", "type", "product category"},
    "model": {"model", "model no", "model number", "product", "product name", "item"},
    "variant": {"variant", "size", "colour", "color", "description"},
    "qty": {"qty", "quantity", "stock", "stock qty", "available", "units"},
}

_client = None
_cache = {"rows": None, "ts": 0.0}


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🧾 New Sale", callback_data="main:sale"),
                InlineKeyboardButton("🔍 Check Stock", callback_data="main:search"),
            ],
            [
                InlineKeyboardButton("➕ Add Stock", callback_data="main:add"),
                InlineKeyboardButton("⚠️ Low Stock", callback_data="main:low"),
            ],
            [
                InlineKeyboardButton("📊 Today's Summary", callback_data="main:report"),
                InlineKeyboardButton("🔄 Refresh", callback_data="main:refresh"),
            ],
            [InlineKeyboardButton("ℹ️ Help", callback_data="main:help")],
        ]
    )


def back_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ Main Menu", callback_data="back:menu")]]
    )


def confirmation_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Confirm", callback_data="confirm:yes"),
                InlineKeyboardButton("❌ Cancel", callback_data="confirm:no"),
            ]
        ]
    )


def payment_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("💵 Cash", callback_data="payment:Cash"),
                InlineKeyboardButton("🏦 Bank", callback_data="payment:Bank"),
            ],
            [
                InlineKeyboardButton("📝 Customer Credit", callback_data="payment:Credit"),
                InlineKeyboardButton("❌ Cancel", callback_data="confirm:no"),
            ],
        ]
    )


def _get_client():
    global _client
    if _client is None:
        credentials = Credentials.from_service_account_info(
            json.loads(GOOGLE_CREDS_JSON), scopes=SCOPES
        )
        _client = gspread.authorize(credentials)
    return _client


def _normalize(value: str) -> str:
    return " ".join(str(value).strip().lower().replace("_", " ").split())


def _column_map(header):
    normalized = [_normalize(cell) for cell in header]
    found = {}
    for canonical, aliases in HEADER_ALIASES.items():
        for index, value in enumerate(normalized):
            if value in aliases:
                found[canonical] = index
                break
    return found


def _number(value) -> Decimal:
    cleaned = str(value or "0").replace(",", "").replace("Rs.", "").replace("Rs", "").strip()
    try:
        return Decimal(cleaned or "0")
    except InvalidOperation:
        return Decimal("0")


def _display_number(value: Decimal) -> str:
    if value == value.to_integral():
        return f"{int(value):,}"
    return f"{value:,.2f}"


def load_rows(force: bool = False):
    """Load products from every stock worksheet using flexible header names."""
    import time

    now = time.time()
    if not force and _cache["rows"] is not None and now - _cache["ts"] < CACHE_SECONDS:
        return _cache["rows"]

    spreadsheet = _get_client().open_by_key(SHEET_ID)
    rows = []
    for worksheet in spreadsheet.worksheets():
        if _normalize(worksheet.title) in {"summary", "transactions", "sales", "expenses"}:
            continue
        values = worksheet.get_all_values()
        if not values:
            continue

        header_index = None
        columns = {}
        for possible_index, possible_header in enumerate(values[:10]):
            possible_columns = _column_map(possible_header)
            if "qty" in possible_columns and any(
                key in possible_columns for key in ("brand", "model", "category")
            ):
                header_index = possible_index
                columns = possible_columns
                break
        if header_index is None:
            log.warning("Skipped worksheet %s: no recognizable stock header", worksheet.title)
            continue

        def cell(record, name):
            index = columns.get(name)
            return record[index].strip() if index is not None and index < len(record) else ""

        for row_number, record in enumerate(values[header_index + 1 :], start=header_index + 2):
            brand = cell(record, "brand")
            category = cell(record, "category")
            model = cell(record, "model")
            variant = cell(record, "variant")
            if not any((brand, category, model, variant)):
                continue
            if _normalize(brand) == "total units" or _normalize(model) == "total units":
                continue
            rows.append(
                {
                    "worksheet": worksheet,
                    "tab": worksheet.title,
                    "row_number": row_number,
                    "qty_column": columns["qty"] + 1,
                    "brand": brand,
                    "category": category,
                    "model": model,
                    "variant": variant,
                    "qty": _number(cell(record, "qty")),
                }
            )

    _cache.update({"rows": rows, "ts": now})
    log.info("Loaded %d product rows", len(rows))
    return rows


def search_products(query: str, rows):
    words = [_normalize(word) for word in query.split() if word.strip()]
    if not words:
        return []
    matches = []
    for row in rows:
        haystack = _normalize(
            " ".join((row["brand"], row["category"], row["model"], row["variant"]))
        )
        if all(word in haystack for word in words):
            matches.append(row)
    return matches


def product_name(row) -> str:
    return " ".join(
        value for value in (row["brand"], row["category"], row["model"], row["variant"]) if value
    )


def format_product(row) -> str:
    quantity = _display_number(row["qty"])
    status = "In stock / Mojood hai" if row["qty"] > 0 else "Out of stock / Mojood nahi"
    return f"• {product_name(row)}\n  Stock: {quantity} — {status}"


def _transactions_worksheet():
    spreadsheet = _get_client().open_by_key(SHEET_ID)
    try:
        worksheet = spreadsheet.worksheet("Transactions")
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title="Transactions", rows=2000, cols=13)
        worksheet.append_row(TRANSACTION_HEADERS, value_input_option="USER_ENTERED")
        worksheet.freeze(rows=1)
    return worksheet


def _log_transaction(kind, row, quantity, unit_price, payment, user, notes=""):
    total = quantity * unit_price
    worksheet = _transactions_worksheet()
    worksheet.append_row(
        [
            datetime.now(TZ).isoformat(timespec="seconds"),
            kind,
            row["brand"],
            row["category"],
            row["model"],
            row["variant"],
            float(quantity),
            float(unit_price),
            float(total),
            payment,
            user.id,
            user.username or user.full_name,
            notes,
        ],
        value_input_option="USER_ENTERED",
    )


def apply_transaction(kind, row, quantity, unit_price, payment, user):
    current = row["qty"]
    new_quantity = current + quantity if kind == "STOCK IN" else current - quantity
    if new_quantity < 0:
        raise ValueError("Not enough stock available for this sale.")

    row["worksheet"].update_cell(row["row_number"], row["qty_column"], float(new_quantity))
    _log_transaction(kind, row, quantity, unit_price, payment, user)
    row["qty"] = new_quantity
    _cache["rows"] = None
    return current, new_quantity


def daily_summary():
    try:
        records = _transactions_worksheet().get_all_records()
    except gspread.WorksheetNotFound:
        records = []

    today = datetime.now(TZ).date()
    sales = Decimal("0")
    stock_in = Decimal("0")
    items_sold = Decimal("0")
    sale_count = 0
    payment_totals = {"Cash": Decimal("0"), "Bank": Decimal("0"), "Credit": Decimal("0")}

    for record in records:
        try:
            timestamp = datetime.fromisoformat(str(record.get("Timestamp", "")))
        except ValueError:
            continue
        if timestamp.date() != today:
            continue
        kind = str(record.get("Type", "")).upper()
        quantity = _number(record.get("Quantity", 0))
        total = _number(record.get("Total", 0))
        if kind == "SALE":
            sale_count += 1
            items_sold += quantity
            sales += total
            payment = str(record.get("Payment", ""))
            if payment in payment_totals:
                payment_totals[payment] += total
        elif kind == "STOCK IN":
            stock_in += quantity

    return {
        "date": today.strftime("%d %B %Y"),
        "sale_count": sale_count,
        "items_sold": items_sold,
        "sales": sales,
        "stock_in": stock_in,
        "payments": payment_totals,
    }


def reset_flow(context):
    for key in ("mode", "action", "matches", "selected", "draft"):
        context.user_data.pop(key, None)


async def authorized(update: Update) -> bool:
    user = update.effective_user
    if not AUTHORIZED_USER_IDS or (user and user.id in AUTHORIZED_USER_IDS):
        return True
    message = (
        "Access denied. This account is not authorized to use Madina Manager.\n\n"
        f"Your Telegram ID: {user.id if user else 'Unknown'}"
    )
    if update.callback_query:
        await update.callback_query.answer("Access denied", show_alert=True)
    elif update.effective_message:
        await update.effective_message.reply_text(message)
    return False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await authorized(update):
        return
    reset_flow(context)
    await update.effective_message.reply_text(
        "Madina Electronics Manager\n\n"
        "Assalam-o-Alaikum. Select an option below.\n"
        "Neeche se apna kaam select karein.",
        reply_markup=main_menu(),
    )


async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(f"Your Telegram user ID is: {update.effective_user.id}")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reset_flow(context)
    await update.effective_message.reply_text("Operation cancelled.", reply_markup=main_menu())


async def refresh_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await authorized(update):
        return
    rows = await asyncio.to_thread(load_rows, True)
    await update.effective_message.reply_text(
        f"Inventory refreshed successfully.\n{len(rows):,} product rows loaded.",
        reply_markup=main_menu(),
    )


async def show_main_from_query(query, context, text="Select an option:"):
    reset_flow(context)
    await query.edit_message_text(text, reply_markup=main_menu())


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await authorized(update):
        return
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "back:menu":
        await show_main_from_query(query, context, "Madina Electronics Manager")
        return

    if data.startswith("main:"):
        reset_flow(context)
        action = data.split(":", 1)[1]
        if action == "search":
            context.user_data.update(mode="product", action="search")
            await query.edit_message_text(
                "Type the brand, model, or product name to search.\n"
                "Brand ya model likhein.",
                reply_markup=back_menu(),
            )
        elif action in {"sale", "add"}:
            context.user_data.update(mode="product", action=action)
            title = "New Sale" if action == "sale" else "Add Stock"
            await query.edit_message_text(
                f"{title}\n\nType the product brand or model.", reply_markup=back_menu()
            )
        elif action == "low":
            rows = await asyncio.to_thread(load_rows)
            low = sorted(
                (row for row in rows if row["qty"] <= LOW_STOCK_THRESHOLD),
                key=lambda row: row["qty"],
            )
            if not low:
                text = f"No products are at or below {LOW_STOCK_THRESHOLD} units."
            else:
                shown = low[:25]
                text = (
                    f"Low Stock — {len(low)} product(s)\n"
                    f"Threshold: {LOW_STOCK_THRESHOLD}\n\n"
                    + "\n\n".join(format_product(row) for row in shown)
                )
                if len(low) > len(shown):
                    text += f"\n\nShowing the first {len(shown)} products."
            await query.edit_message_text(text, reply_markup=back_menu())
        elif action == "report":
            summary = await asyncio.to_thread(daily_summary)
            payments = summary["payments"]
            text = (
                "Madina Electronics — Daily Summary\n"
                f"{summary['date']}\n\n"
                f"Sales recorded: {summary['sale_count']}\n"
                f"Items sold: {_display_number(summary['items_sold'])}\n"
                f"Sales value: Rs {_display_number(summary['sales'])}\n"
                f"Cash: Rs {_display_number(payments['Cash'])}\n"
                f"Bank: Rs {_display_number(payments['Bank'])}\n"
                f"Credit: Rs {_display_number(payments['Credit'])}\n"
                f"Stock received: {_display_number(summary['stock_in'])} units"
            )
            await query.edit_message_text(text, reply_markup=back_menu())
        elif action == "refresh":
            rows = await asyncio.to_thread(load_rows, True)
            await query.edit_message_text(
                f"Inventory refreshed. {len(rows):,} product rows loaded.",
                reply_markup=main_menu(),
            )
        elif action == "help":
            await query.edit_message_text(
                "Help\n\n"
                "• New Sale records a sale and reduces stock.\n"
                "• Check Stock finds products by brand or model.\n"
                "• Add Stock increases an existing product's quantity.\n"
                "• Low Stock shows products needing attention.\n"
                "• Today's Summary totals transactions recorded today.\n\n"
                "You can also type a product name directly at any time.\n"
                "Use /cancel to stop an operation and /id to see your Telegram ID.",
                reply_markup=back_menu(),
            )
        return

    if data.startswith("pick:"):
        try:
            selected = context.user_data["matches"][int(data.split(":", 1)[1])]
        except (KeyError, IndexError, ValueError):
            await query.edit_message_text("This selection expired. Please start again.", reply_markup=main_menu())
            reset_flow(context)
            return
        context.user_data["selected"] = selected
        action = context.user_data.get("action")
        if action == "search":
            reset_flow(context)
            await query.edit_message_text(format_product(selected), reply_markup=main_menu())
        else:
            context.user_data["mode"] = "quantity"
            verb = "sell" if action == "sale" else "add"
            await query.edit_message_text(
                f"Selected: {product_name(selected)}\n"
                f"Current stock: {_display_number(selected['qty'])}\n\n"
                f"How many units do you want to {verb}?",
                reply_markup=back_menu(),
            )
        return

    if data.startswith("payment:"):
        draft = context.user_data.get("draft")
        selected = context.user_data.get("selected")
        if not draft or not selected:
            await query.edit_message_text("This sale expired. Please start again.", reply_markup=main_menu())
            reset_flow(context)
            return
        draft["payment"] = data.split(":", 1)[1]
        total = draft["quantity"] * draft["unit_price"]
        context.user_data["mode"] = "confirm"
        await query.edit_message_text(
            "Confirm Sale\n\n"
            f"Product: {product_name(selected)}\n"
            f"Quantity: {_display_number(draft['quantity'])}\n"
            f"Price each: Rs {_display_number(draft['unit_price'])}\n"
            f"Total: Rs {_display_number(total)}\n"
            f"Payment: {draft['payment']}\n"
            f"Stock: {_display_number(selected['qty'])} → "
            f"{_display_number(selected['qty'] - draft['quantity'])}",
            reply_markup=confirmation_menu(),
        )
        return

    if data == "confirm:no":
        await show_main_from_query(query, context, "Operation cancelled.")
        return

    if data == "confirm:yes":
        selected = context.user_data.get("selected")
        draft = context.user_data.get("draft")
        if not selected or not draft:
            await query.edit_message_text("This operation expired. Please start again.", reply_markup=main_menu())
            reset_flow(context)
            return
        kind = "SALE" if draft["action"] == "sale" else "STOCK IN"
        old, new = await asyncio.to_thread(
            apply_transaction,
            kind,
            selected,
            draft["quantity"],
            draft.get("unit_price", Decimal("0")),
            draft.get("payment", ""),
            update.effective_user,
        )
        total = draft["quantity"] * draft.get("unit_price", Decimal("0"))
        text = (
            ("Sale recorded successfully." if kind == "SALE" else "Stock added successfully.")
            + "\n\n"
            + f"Product: {product_name(selected)}\n"
            + f"Quantity: {_display_number(draft['quantity'])}\n"
            + f"Stock: {_display_number(old)} → {_display_number(new)}"
        )
        if kind == "SALE":
            text += f"\nTotal: Rs {_display_number(total)}\nPayment: {draft['payment']}"
        reset_flow(context)
        await query.edit_message_text(text, reply_markup=main_menu())


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await authorized(update):
        return
    message = update.effective_message
    text = message.text.strip()
    mode = context.user_data.get("mode")

    if mode in {None, "product"}:
        action = context.user_data.get("action", "search")
        rows = await asyncio.to_thread(load_rows)
        matches = search_products(text, rows)
        if not matches:
            await message.reply_text(
                f'No product found for "{text}". Try a shorter brand or model name.',
                reply_markup=back_menu() if mode else main_menu(),
            )
            return
        if len(matches) == 1:
            selected = matches[0]
            if action == "search":
                reset_flow(context)
                await message.reply_text(format_product(selected), reply_markup=main_menu())
            else:
                context.user_data.update(selected=selected, mode="quantity")
                verb = "sell" if action == "sale" else "add"
                await message.reply_text(
                    f"Selected: {product_name(selected)}\n"
                    f"Current stock: {_display_number(selected['qty'])}\n\n"
                    f"How many units do you want to {verb}?",
                    reply_markup=back_menu(),
                )
            return

        shown = matches[:10]
        context.user_data.update(matches=shown, mode="pick")
        keyboard = [
            [
                InlineKeyboardButton(
                    f"{product_name(row)[:45]} — {row['qty']}", callback_data=f"pick:{index}"
                )
            ]
            for index, row in enumerate(shown)
        ]
        keyboard.append([InlineKeyboardButton("⬅️ Main Menu", callback_data="back:menu")])
        await message.reply_text(
            f"{len(matches)} products matched. Select one:"
            + ("\nShowing the first 10." if len(matches) > 10 else ""),
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    if mode == "pick":
        await message.reply_text("Please select a product using the buttons above.")
        return

    if mode == "quantity":
        selected = context.user_data["selected"]
        quantity = _number(text)
        if quantity <= 0 or quantity != quantity.to_integral():
            await message.reply_text("Enter a valid whole-number quantity, for example: 2")
            return
        action = context.user_data["action"]
        if action == "sale" and quantity > selected["qty"]:
            await message.reply_text(
                f"Only {_display_number(selected['qty'])} unit(s) are available. Enter a smaller quantity."
            )
            return
        context.user_data["draft"] = {"action": action, "quantity": quantity}
        if action == "sale":
            context.user_data["mode"] = "price"
            await message.reply_text("Enter the selling price per unit in PKR, for example: 145000")
        else:
            context.user_data["mode"] = "confirm"
            await message.reply_text(
                "Confirm Stock Addition\n\n"
                f"Product: {product_name(selected)}\n"
                f"Add: {_display_number(quantity)} units\n"
                f"Stock: {_display_number(selected['qty'])} → "
                f"{_display_number(selected['qty'] + quantity)}",
                reply_markup=confirmation_menu(),
            )
        return

    if mode == "price":
        price = _number(text)
        if price <= 0:
            await message.reply_text("Enter a valid selling price, for example: 145000")
            return
        context.user_data["draft"]["unit_price"] = price
        context.user_data["mode"] = "payment"
        await message.reply_text("Select the payment method:", reply_markup=payment_menu())
        return

    if mode in {"payment", "confirm"}:
        await message.reply_text("Please use the buttons shown above, or send /cancel.")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("Unhandled bot error", exc_info=context.error)
    if not isinstance(update, Update) or not update.effective_message:
        return
    if isinstance(context.error, PermissionError):
        text = (
            "Google Sheets access was denied. Share the spreadsheet with the service-account "
            "email as Editor, then try again."
        )
    elif isinstance(context.error, json.JSONDecodeError):
        text = "Google credentials are not valid JSON. Check GOOGLE_CREDENTIALS_JSON in Railway."
    elif isinstance(context.error, ValueError):
        text = str(context.error)
    else:
        text = "Something went wrong. No changes were intentionally made. Please try again."
    try:
        await update.effective_message.reply_text(text, reply_markup=main_menu())
    except Exception:
        log.exception("Could not send error message to Telegram")


def main():
    if not AUTHORIZED_USER_IDS:
        log.warning("AUTHORIZED_USER_IDS is empty; anyone who finds the bot can use it")
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("menu", start))
    application.add_handler(CommandHandler("id", my_id))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(CommandHandler("refresh", refresh_command))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.add_error_handler(on_error)
    log.info("Madina Electronics Manager starting")
    application.run_polling()


if __name__ == "__main__":
    main()
