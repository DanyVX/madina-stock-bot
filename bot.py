"""
Madina Electronics — Stock Lookup Telegram Bot
------------------------------------------------
Lets anyone message the bot with a product name/brand/model and get
back the matching rows from the "Madina Electronics Stock Quantities"
Google Sheet, with quantity, in English + Roman Urdu.

Setup instructions are in README.md.
"""

import os
import logging
from functools import lru_cache

import gspread
from google.oauth2.service_account import Credentials
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("madina-bot")

# ---- config from environment variables (set these on your host) ----
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
SHEET_ID = os.environ["GOOGLE_SHEET_ID"]  # the long id in the sheet's URL
GOOGLE_CREDS_JSON = os.environ["GOOGLE_CREDENTIALS_JSON"]  # full service-account JSON, as one line

CACHE_SECONDS = 120  # how long to keep sheet data before re-fetching

# ---- Google Sheets ----
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]


def _get_client():
    import json
    creds_dict = json.loads(GOOGLE_CREDS_JSON)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return gspread.authorize(creds)


_client = None
_cache = {"rows": None, "ts": 0}


def load_rows(force: bool = False):
    """Pulls every row from every worksheet tab except 'Summary'."""
    import time
    global _client
    now = time.time()
    if not force and _cache["rows"] is not None and now - _cache["ts"] < CACHE_SECONDS:
        return _cache["rows"]

    if _client is None:
        _client = _get_client()

    sh = _client.open_by_key(SHEET_ID)
    rows = []
    for ws in sh.worksheets():
        if ws.title.strip().lower() == "summary":
            continue
        values = ws.get_all_values()
        if not values:
            continue
        header = [h.strip().lower() for h in values[0]]
        try:
            i_brand = header.index("brand")
            i_cat = header.index("category")
            i_model = header.index("model")
            i_var = header.index("variant")
            i_qty = header.index("qty")
        except ValueError:
            continue  # tab doesn't look like a stock tab, skip it
        for r in values[1:]:
            if len(r) <= i_qty:
                continue
            brand, cat, model, variant, qty = (r[i_brand], r[i_cat], r[i_model], r[i_var], r[i_qty])
            if not (brand or cat or model or variant):
                continue
            if brand.strip().lower() == "total units":
                continue
            rows.append({
                "tab": ws.title,
                "brand": brand.strip(),
                "category": cat.strip(),
                "model": model.strip(),
                "variant": variant.strip(),
                "qty": qty.strip(),
            })
    _cache["rows"] = rows
    _cache["ts"] = now
    log.info("Loaded %d rows from sheet", len(rows))
    return rows


def search(query: str, rows):
    q_words = [w for w in query.lower().split() if w]
    if not q_words:
        return []
    results = []
    for row in rows:
        hay = " ".join([row["brand"], row["category"], row["model"], row["variant"]]).lower()
        if all(w in hay for w in q_words):
            results.append(row)
    return results


def fmt_row(row) -> str:
    name = " ".join(x for x in [row["brand"], row["category"], row["model"], row["variant"]] if x)
    qty = row["qty"] or "0"
    try:
        qn = int(float(qty))
        in_stock = qn > 0
    except ValueError:
        in_stock = bool(qty) and qty != "0"
        qn = qty
    status_en = "In stock" if in_stock else "Out of stock"
    status_ur = "Mojood hai" if in_stock else "Mojood nahi"
    return f"• {name}\n   Qty: {qn} — {status_en} / {status_ur}"


# ---- Telegram handlers ----

WELCOME = (
    "Assalam-o-Alaikum! Madina Electronics stock bot mein khush aamdeed.\n\n"
    "Kisi bhi product ka naam, brand ya model type karein, jaise:\n"
    "  • PEL AC\n"
    "  • Metro geyser\n"
    "  • Dawlance 9160\n\n"
    "Mujhe woh product aur uski quantity mil jayegi.\n\n"
    "----\n\n"
    "Welcome to the Madina Electronics stock bot!\n\n"
    "Type any product name, brand, or model, e.g.:\n"
    "  • PEL AC\n"
    "  • Metro geyser\n"
    "  • Dawlance 9160\n\n"
    "I'll find it and tell you the quantity in stock."
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME)


async def refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = load_rows(force=True)
    await update.message.reply_text(
        f"Sheet refresh ho gayi hai — {len(rows)} items load hue.\n"
        f"Sheet refreshed — {len(rows)} items loaded."
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.message.text.strip()
    if not query:
        return
    rows = load_rows()
    matches = search(query, rows)

    if not matches:
        await update.message.reply_text(
            f'"{query}" ke liye kuch nahi mila. Mukhtasir naam try karein, jaise sirf brand ya model.\n\n'
            f'Nothing found for "{query}". Try a shorter search, like just the brand or model.'
        )
        return

    if len(matches) > 25:
        await update.message.reply_text(
            f"{len(matches)} items mile — zyada specific likhein (jaise brand + model).\n"
            f"{len(matches)} items matched — please be more specific (brand + model)."
        )
        return

    lines = [fmt_row(r) for r in matches]
    header = f"{len(matches)} item(s) mile / found:\n\n"
    text = header + "\n\n".join(lines)
    # Telegram messages cap around 4096 chars; split if needed
    for i in range(0, len(text), 3500):
        await update.message.reply_text(text[i:i + 3500])


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("refresh", refresh))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    log.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
