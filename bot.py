"""Professional Telegram interface for Madina Electronics Manager."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import traceback
from decimal import Decimal
from math import ceil

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, Conflict
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from storage import (
    ManagerError,
    Product,
    SheetStore,
    decimal_value,
    display_number,
    money_value,
    product_name,
    whole_quantity,
)


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("madina-manager")

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"].strip()
SHEET_ID = os.environ["GOOGLE_SHEET_ID"].strip()
GOOGLE_CREDS_JSON = os.environ["GOOGLE_CREDENTIALS_JSON"]
DEFAULT_LOW_STOCK = int(os.getenv("LOW_STOCK_THRESHOLD", "3"))
PAGE_SIZE = max(5, min(12, int(os.getenv("PAGE_SIZE", "8"))))
AUTHORIZED_USER_IDS = {
    int(item.strip())
    for item in os.getenv("AUTHORIZED_USER_IDS", "").split(",")
    if item.strip().isdigit()
}

store = SheetStore(SHEET_ID, GOOGLE_CREDS_JSON, DEFAULT_LOW_STOCK)


def button(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data)


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [button("🧾 New Sale", "act:sale"), button("📦 Inventory", "nav:inventory")],
            [button("👥 Customers", "nav:customers"), button("💸 Record Expense", "act:expense")],
            [button("📊 Reports", "nav:reports"), button("🕘 History", "act:history")],
            [button("🔄 Refresh", "act:refresh"), button("ℹ️ Help", "act:help")],
        ]
    )


def inventory_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [button("📋 View All Stock", "act:view_all"), button("🔍 Search Stock", "act:search")],
            [button("🆕 Add Product", "act:add_product"), button("➕ Add Stock", "act:add_stock")],
            [button("➖ Remove Stock", "act:remove_stock"), button("💰 Set Prices", "act:set_prices")],
            [button("↩️ Customer Return", "act:sale_return"), button("⚠️ Low Stock", "act:low_stock")],
            [button("⬅️ Main Menu", "nav:main")],
        ]
    )


def customers_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [button("📒 Credit Balances", "act:balances")],
            [button("💵 Receive Payment", "act:customer_payment")],
            [button("⬅️ Main Menu", "nav:main")],
        ]
    )


def reports_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [button("📅 Today's Business", "act:today"), button("📦 Inventory Summary", "act:inventory_report")],
            [button("⚠️ Low Stock", "act:low_stock"), button("🕘 Transactions", "act:history")],
            [button("⬅️ Main Menu", "nav:main")],
        ]
    )


def back_menu(target: str = "nav:main") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[button("⬅️ Back", target), button("❌ Cancel", "confirm:no")]])


def confirm_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[button("✅ Confirm", "confirm:yes"), button("❌ Cancel", "confirm:no")]]
    )


def payment_menu(cancel_target: str = "confirm:no", *, allow_credit: bool = True) -> InlineKeyboardMarkup:
    rows = [[button("💵 Cash", "pay:Cash"), button("🏦 Bank", "pay:Bank")]]
    if allow_credit:
        rows.append([button("📝 Credit", "pay:Credit"), button("❌ Cancel", cancel_target)])
    else:
        rows.append([button("❌ Cancel", cancel_target)])
    return InlineKeyboardMarkup(
        rows
    )


def reset(context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()


def set_flow(context, action: str, step: str, **draft):
    context.user_data.clear()
    context.user_data.update(action=action, step=step, draft=draft)


def current_draft(context) -> dict:
    return context.user_data.setdefault("draft", {})


def user_identity(update: Update) -> tuple[int, str]:
    user = update.effective_user
    return user.id, user.username or user.full_name


async def is_authorized(update: Update) -> bool:
    user = update.effective_user
    if not AUTHORIZED_USER_IDS or (user and user.id in AUTHORIZED_USER_IDS):
        return True
    text = (
        "Access denied. This Telegram account is not authorized.\n\n"
        f"Your Telegram ID: {user.id if user else 'Unknown'}"
    )
    if update.callback_query:
        await update.callback_query.answer(text, show_alert=True)
    elif update.effective_message:
        await update.effective_message.reply_text(text)
    return False


async def safe_edit(query, text: str, reply_markup=None):
    try:
        await query.edit_message_text(text, reply_markup=reply_markup)
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


def product_card(product: Product | dict, number: int | None = None) -> str:
    product = product if isinstance(product, Product) else Product.from_dict(product)
    prefix = f"{number}. " if number is not None else ""
    sku = f"\n   SKU: {product.sku}" if product.sku else ""
    color = product.color or "—"
    cost = f"Rs {display_number(product.cost_price)}" if product.cost_price > 0 else "Not set"
    sale = f"Rs {display_number(product.sale_price)}" if product.sale_price > 0 else "Not set"
    return (
        f"{prefix}{product_name(product)}"
        f"{sku}\n   Color: {color} | Qty: {display_number(product.quantity)}"
        f"\n   Cost: {cost} | Sale: {sale} | Low at: {display_number(product.low_stock)}"
    )


def page_keyboard(page: int, pages: int, *, selectable: bool, count: int, parent: str):
    rows = []
    if selectable:
        indexes = list(range(page * PAGE_SIZE, min((page + 1) * PAGE_SIZE, count)))
        for start in range(0, len(indexes), 4):
            rows.append([button(str(index + 1), f"sel:{index}") for index in indexes[start : start + 4]])
    navigation = []
    if page > 0:
        navigation.append(button("◀ Previous", f"pg:{page - 1}"))
    navigation.append(button(f"{page + 1}/{pages}", "noop"))
    if page + 1 < pages:
        navigation.append(button("Next ▶", f"pg:{page + 1}"))
    rows.append(navigation)
    rows.append([button("⬅️ Back", parent), button("❌ Cancel", "confirm:no")])
    return InlineKeyboardMarkup(rows)


async def render_product_page(query, context, page: int = 0):
    raw_products = context.user_data.get("results", [])
    if not raw_products:
        await safe_edit(query, "No products found.", inventory_menu())
        return
    pages = max(1, ceil(len(raw_products) / PAGE_SIZE))
    page = max(0, min(page, pages - 1))
    start = page * PAGE_SIZE
    shown = raw_products[start : start + PAGE_SIZE]
    title = context.user_data.get("list_title", "Products")
    text = f"{title}\n{len(raw_products)} product(s) — Page {page + 1} of {pages}\n\n"
    text += "\n\n".join(product_card(item, start + offset + 1) for offset, item in enumerate(shown))
    context.user_data["page"] = page
    selectable = bool(context.user_data.get("select_action"))
    await safe_edit(
        query,
        text,
        page_keyboard(
            page,
            pages,
            selectable=selectable,
            count=len(raw_products),
            parent=context.user_data.get("list_parent", "nav:inventory"),
        ),
    )


async def render_generic_page(query, context, page: int = 0):
    records = context.user_data.get("generic_results", [])
    if not records:
        await safe_edit(query, "No records found.", reports_menu())
        return
    pages = max(1, ceil(len(records) / PAGE_SIZE))
    page = max(0, min(page, pages - 1))
    start = page * PAGE_SIZE
    text = (
        f"{context.user_data.get('list_title', 'Records')}\n"
        f"{len(records)} record(s) — Page {page + 1} of {pages}\n\n"
        + "\n\n".join(records[start : start + PAGE_SIZE])
    )
    context.user_data["page"] = page
    await safe_edit(
        query,
        text,
        page_keyboard(
            page,
            pages,
            selectable=False,
            count=len(records),
            parent=context.user_data.get("list_parent", "nav:reports"),
        ),
    )


async def show_products(query, context, products, title, *, select_action=None, parent="nav:inventory"):
    context.user_data.update(
        results=[product.to_dict() for product in products],
        list_title=title,
        select_action=select_action,
        list_parent=parent,
        page=0,
        list_type="products",
    )
    await render_product_page(query, context, 0)


async def ask_for_product(query, context, action: str, title: str):
    set_flow(context, action, "product_query")
    await safe_edit(
        query,
        f"{title}\n\nType a brand, model, SKU, variant, or color.\n"
        "You can also browse the complete inventory.",
        InlineKeyboardMarkup(
            [
                [button("📋 Browse All Products", f"browse:{action}")],
                [button("⬅️ Inventory", "nav:inventory"), button("❌ Cancel", "confirm:no")],
            ]
        ),
    )


def selected_product(context) -> Product:
    value = current_draft(context).get("product")
    if not value:
        raise ManagerError("The selected product expired. Please select it again.")
    return Product.from_dict(value)


async def product_selected(query, context, product: Product, action: str):
    draft = current_draft(context)
    draft["product"] = product.to_dict()
    context.user_data.pop("results", None)
    if action == "sale":
        context.user_data["step"] = "sale_quantity"
        text = f"New Sale\n\n{product_card(product)}\n\nEnter the quantity being sold."
    elif action == "sale_return":
        context.user_data["step"] = "return_quantity"
        text = f"Customer Return\n\n{product_card(product)}\n\nEnter the quantity being returned."
    elif action == "add_stock":
        context.user_data["step"] = "stock_in_quantity"
        text = f"Add Stock\n\n{product_card(product)}\n\nEnter the quantity received."
    elif action == "remove_stock":
        context.user_data["step"] = "stock_out_quantity"
        text = f"Remove Stock\n\n{product_card(product)}\n\nEnter the quantity to remove."
    elif action == "set_prices":
        context.user_data["step"] = "set_cost"
        text = f"Set Prices\n\n{product_card(product)}\n\nEnter the cost price per unit. Enter 0 if unknown."
    else:
        raise ManagerError("Unknown product operation.")
    await safe_edit(query, text, back_menu("nav:inventory"))


def sale_price_menu(product: Product):
    rows = []
    if product.sale_price > 0:
        rows.append([button(f"Use saved: Rs {display_number(product.sale_price)}", "use:sale_price")])
    rows.append([button("✏️ Enter another price", "custom:sale_price")])
    rows.append([button("❌ Cancel", "confirm:no")])
    return InlineKeyboardMarkup(rows)


def cost_price_menu(product: Product):
    rows = []
    if product.cost_price > 0:
        rows.append([button(f"Use saved: Rs {display_number(product.cost_price)}", "use:cost_price")])
    rows.extend(
        [
            [button("✏️ Enter purchase cost", "custom:cost_price")],
            [button("Skip cost", "skip:cost_price"), button("❌ Cancel", "confirm:no")],
        ]
    )
    return InlineKeyboardMarkup(rows)


async def request_payment(query, context, heading="Select payment method:"):
    context.user_data["step"] = "payment"
    await safe_edit(query, heading, payment_menu())


async def show_confirmation(query, context):
    action = context.user_data.get("action")
    draft = current_draft(context)
    context.user_data["step"] = "confirm"
    if action in {"sale", "sale_return", "add_stock", "remove_stock"}:
        product = Product.from_dict(draft["product"])
        qty = decimal_value(draft["quantity"])
        if action in {"sale", "sale_return"}:
            price = decimal_value(draft["unit_price"])
            party = draft.get("party") or "—"
            is_return = action == "sale_return"
            text = (
                ("Confirm Customer Return\n\n" if is_return else "Confirm Sale\n\n")
                +
                f"Product: {product_name(product)}\nColor: {product.color or '—'}\n"
                f"Quantity: {display_number(qty)}\nUnit price: Rs {display_number(price)}\n"
                f"{'Refund' if is_return else 'Total'}: Rs {display_number(qty * price)}\n"
                f"{'Refund method' if is_return else 'Payment'}: {draft['payment']}\n"
                f"Customer: {party}\n"
                f"Stock: {display_number(product.quantity)} → "
                f"{display_number(product.quantity + qty if is_return else product.quantity - qty)}"
            )
        elif action == "add_stock":
            cost = decimal_value(draft.get("unit_cost", 0))
            text = (
                "Confirm Stock Receipt\n\n"
                f"Product: {product_name(product)}\nColor: {product.color or '—'}\n"
                f"Quantity received: {display_number(qty)}\nUnit cost: Rs {display_number(cost)}\n"
                f"Supplier/reference: {draft.get('party') or '—'}\n"
                f"Stock: {display_number(product.quantity)} → {display_number(product.quantity + qty)}"
            )
        else:
            text = (
                "Confirm Stock Removal\n\n"
                f"Product: {product_name(product)}\nColor: {product.color or '—'}\n"
                f"Quantity removed: {display_number(qty)}\nReason: {draft['notes']}\n"
                f"Stock: {display_number(product.quantity)} → {display_number(product.quantity - qty)}"
            )
    elif action == "set_prices":
        product = Product.from_dict(draft["product"])
        text = (
            "Confirm Price Settings\n\n"
            f"Product: {product_name(product)}\nColor: {product.color or '—'}\n"
            f"Cost price: Rs {display_number(draft['cost_price'])}\n"
            f"Sale price: Rs {display_number(draft['sale_price'])}\n"
            f"Low-stock alert: {display_number(draft['low_stock'])} units"
        )
    elif action == "add_product":
        text = (
            "Confirm New Product\n\n"
            f"Brand: {draft['brand']}\nCategory: {draft['category']}\nModel: {draft['model']}\n"
            f"Variant: {draft.get('variant') or '—'}\nColor: {draft.get('color') or '—'}\n"
            f"Opening stock: {display_number(draft['quantity'])}\n"
            f"Cost price: Rs {display_number(draft['cost_price'])}\n"
            f"Sale price: Rs {display_number(draft['sale_price'])}\n"
            f"Low-stock alert: {display_number(draft['low_stock'])} units"
        )
    elif action == "expense":
        text = (
            "Confirm Expense\n\n"
            f"Category: {draft['category']}\nAmount: Rs {display_number(draft['amount'])}\n"
            f"Payment: {draft['payment']}\nNotes: {draft.get('notes') or '—'}"
        )
    elif action == "customer_payment":
        text = (
            "Confirm Customer Payment\n\n"
            f"Customer: {draft['customer']}\nAmount: Rs {display_number(draft['amount'])}\n"
            f"Received through: {draft['payment']}\nNotes: {draft.get('notes') or '—'}"
        )
    else:
        raise ManagerError("This operation cannot be confirmed.")
    await safe_edit(query, text, confirm_menu())


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update):
        return
    reset(context)
    await update.effective_message.reply_text(
        "Madina Electronics Manager\n\n"
        "Assalam-o-Alaikum. Choose an option below.\n"
        "Har tabdeeli confirmation ke baad save hogi.",
        reply_markup=main_menu(),
    )


async def show_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(f"Your Telegram user ID is: {update.effective_user.id}")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reset(context)
    await update.effective_message.reply_text("Operation cancelled.", reply_markup=main_menu())


async def refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update):
        return
    products = await asyncio.to_thread(store.load_products, True)
    await update.effective_message.reply_text(
        f"Inventory refreshed successfully.\n{len(products):,} products loaded.", reply_markup=main_menu()
    )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update):
        return
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "noop":
        return
    if data == "nav:main":
        reset(context)
        await safe_edit(query, "Madina Electronics Manager", main_menu())
        return
    if data == "nav:inventory":
        reset(context)
        await safe_edit(query, "Inventory Management", inventory_menu())
        return
    if data == "nav:customers":
        reset(context)
        await safe_edit(query, "Customer Accounts", customers_menu())
        return
    if data == "nav:reports":
        reset(context)
        await safe_edit(query, "Business Reports", reports_menu())
        return
    if data == "confirm:no":
        reset(context)
        await safe_edit(query, "Operation cancelled.", main_menu())
        return

    if data.startswith("pg:"):
        page = int(data.split(":", 1)[1])
        if context.user_data.get("list_type") == "products":
            await render_product_page(query, context, page)
        else:
            await render_generic_page(query, context, page)
        return

    if data.startswith("browse:"):
        action = data.split(":", 1)[1]
        draft = current_draft(context)
        products = await asyncio.to_thread(store.load_products)
        context.user_data["action"] = action
        context.user_data["draft"] = draft
        await show_products(query, context, products, "Select a Product", select_action=action)
        return

    if data.startswith("sel:"):
        index = int(data.split(":", 1)[1])
        results = context.user_data.get("results", [])
        if index < 0 or index >= len(results):
            raise ManagerError("This product list expired. Please search again.")
        product = Product.from_dict(results[index])
        action = context.user_data.get("select_action") or context.user_data.get("action")
        await product_selected(query, context, product, action)
        return

    if data.startswith("act:"):
        action = data.split(":", 1)[1]
        if action == "sale":
            await ask_for_product(query, context, "sale", "New Sale")
        elif action == "sale_return":
            await ask_for_product(query, context, "sale_return", "Customer Return")
        elif action == "search":
            set_flow(context, "search", "search_query")
            await safe_edit(
                query,
                "Search Stock\n\nType any brand, model, SKU, variant, category, or color.",
                back_menu("nav:inventory"),
            )
        elif action == "view_all":
            reset(context)
            products = await asyncio.to_thread(store.load_products)
            await show_products(query, context, products, "Complete Inventory", parent="nav:inventory")
        elif action == "low_stock":
            reset(context)
            products = await asyncio.to_thread(store.load_products, True)
            low = [product for product in products if product.quantity <= product.low_stock]
            low.sort(key=lambda item: (item.quantity, product_name(item).lower()))
            await show_products(query, context, low, "Low-Stock Products", parent="nav:reports")
        elif action in {"add_stock", "remove_stock", "set_prices"}:
            titles = {
                "add_stock": "Add Stock",
                "remove_stock": "Remove Stock",
                "set_prices": "Set Product Prices",
            }
            await ask_for_product(query, context, action, titles[action])
        elif action == "add_product":
            set_flow(context, "add_product", "new_brand")
            await safe_edit(query, "Add New Product\n\nEnter the brand name.", back_menu("nav:inventory"))
        elif action == "expense":
            set_flow(context, "expense", "expense_category")
            await safe_edit(query, "Record Expense\n\nEnter the expense category, such as Transport or Electricity.", back_menu())
        elif action == "customer_payment":
            set_flow(context, "customer_payment", "customer_name")
            await safe_edit(query, "Receive Customer Payment\n\nEnter the customer's name.", back_menu("nav:customers"))
        elif action == "balances":
            reset(context)
            balances = await asyncio.to_thread(store.customer_balances)
            records = [
                f"{index}. {item['customer']}\n   Balance: Rs {display_number(item['balance'])}"
                for index, item in enumerate(balances, start=1)
            ]
            context.user_data.update(
                generic_results=records,
                list_title="Customer Credit Balances",
                list_parent="nav:customers",
                list_type="generic",
            )
            await render_generic_page(query, context)
        elif action == "today":
            reset(context)
            summary = await asyncio.to_thread(store.daily_summary)
            payments = summary["payments"]
            text = (
                f"Daily Business Summary\n{summary['date']:%d %B %Y}\n\n"
                f"Sales: {summary['sales_count']} transaction(s)\n"
                f"Units sold: {display_number(summary['items_sold'])}\n"
                f"Sales value: Rs {display_number(summary['sales'])}\n"
                f"Cost of goods: Rs {display_number(summary['cost_of_goods'])}\n"
                f"Gross profit: Rs {display_number(summary['gross_profit'])}\n"
                f"Expenses: Rs {display_number(summary['expenses'])}\n"
                f"Net after expenses: Rs {display_number(summary['net_after_expenses'])}\n\n"
                f"Returns/refunds: Rs {display_number(summary['returns'])}\n"
                f"Cash: Rs {display_number(payments['Cash'])}\n"
                f"Bank: Rs {display_number(payments['Bank'])}\n"
                f"Credit sales: Rs {display_number(payments['Credit'])}\n\n"
                f"Stock received: {display_number(summary['stock_in'])} units\n"
                f"Stock removed: {display_number(summary['stock_out'])} units"
            )
            await safe_edit(query, text, reports_menu())
        elif action == "inventory_report":
            reset(context)
            summary = await asyncio.to_thread(store.inventory_summary)
            text = (
                "Inventory Summary\n\n"
                f"Products: {summary['products']:,}\nUnits in stock: {display_number(summary['units'])}\n"
                f"Cost value: Rs {display_number(summary['cost_value'])}\n"
                f"Retail value: Rs {display_number(summary['retail_value'])}\n"
                f"Out of stock: {summary['out_of_stock']:,}\nLow stock: {summary['low_stock']:,}\n"
                f"Missing cost prices: {summary['missing_cost']:,}\n"
                f"Missing sale prices: {summary['missing_sale']:,}"
            )
            await safe_edit(query, text, reports_menu())
        elif action == "history":
            reset(context)
            rows = await asyncio.to_thread(store.transaction_records)
            rows.reverse()
            records = []
            for index, item in enumerate(rows, start=1):
                kind = item.get("Type", "Transaction").replace("_", " ").title()
                name = " ".join(
                    value for value in (item.get("Brand", ""), item.get("Model", ""), item.get("Variant", ""), item.get("Color", "")) if value
                ) or item.get("Notes", "") or "General"
                records.append(
                    f"{index}. {kind} — {item.get('Timestamp', '')[:16].replace('T', ' ')}\n"
                    f"   {name}\n   Qty: {item.get('Quantity', '0')} | Total: Rs {display_number(item.get('Total', 0))}\n"
                    f"   ID: {item.get('Transaction ID', '—')}"
                )
            context.user_data.update(
                generic_results=records,
                list_title="Transaction History",
                list_parent="nav:reports",
                list_type="generic",
            )
            await render_generic_page(query, context)
        elif action == "refresh":
            products = await asyncio.to_thread(store.load_products, True)
            reset(context)
            await safe_edit(query, f"Inventory refreshed. {len(products):,} products loaded.", main_menu())
        elif action == "help":
            reset(context)
            await safe_edit(
                query,
                "Madina Manager Help\n\n"
                "• Inventory shows every product with complete pagination.\n"
                "• New Sale reduces stock and records revenue, cost and payment type.\n"
                "• Customer Return restores stock and records the refund.\n"
                "• Add/Remove Stock always records an audit transaction.\n"
                "• Set Prices stores cost, sale price and individual low-stock level.\n"
                "• Add Product creates a complete product record with color and prices.\n"
                "• Customer Credit records named credit sales and received payments.\n"
                "• Reports calculate sales, gross profit, expenses and stock value.\n\n"
                "Commands: /menu, /refresh, /cancel, /id\n"
                "You can type a product name from the main menu for a quick search.",
                main_menu(),
            )
        return

    if data.startswith("use:"):
        choice = data.split(":", 1)[1]
        product = selected_product(context)
        draft = current_draft(context)
        if choice == "sale_price":
            draft["unit_price"] = product.sale_price
            await request_payment(query, context)
        elif choice == "cost_price":
            draft["unit_cost"] = product.cost_price
            context.user_data["step"] = "stock_in_party"
            await safe_edit(
                query,
                "Enter the supplier or invoice reference, or tap Skip.",
                InlineKeyboardMarkup([[button("Skip", "skip:stock_in_party")], [button("❌ Cancel", "confirm:no")]]),
            )
        return

    if data.startswith("custom:"):
        choice = data.split(":", 1)[1]
        if choice == "sale_price":
            context.user_data["step"] = "sale_price"
            await safe_edit(query, "Enter the selling price per unit in PKR.", back_menu())
        elif choice == "cost_price":
            context.user_data["step"] = "stock_in_cost"
            await safe_edit(query, "Enter the purchase cost per unit in PKR.", back_menu("nav:inventory"))
        return

    if data.startswith("skip:"):
        field = data.split(":", 1)[1]
        draft = current_draft(context)
        if field == "cost_price":
            draft["unit_cost"] = Decimal("0")
            context.user_data["step"] = "stock_in_party"
            await safe_edit(
                query,
                "Enter the supplier or invoice reference, or tap Skip.",
                InlineKeyboardMarkup([[button("Skip", "skip:stock_in_party")], [button("❌ Cancel", "confirm:no")]]),
            )
        elif field == "stock_in_party":
            draft["party"] = ""
            await show_confirmation(query, context)
        elif field == "return_customer":
            draft["party"] = ""
            await show_confirmation(query, context)
        elif field == "new_variant":
            draft["variant"] = ""
            context.user_data["step"] = "new_color"
            await safe_edit(query, "Enter the color, or tap Skip.", InlineKeyboardMarkup([[button("Skip", "skip:new_color")], [button("❌ Cancel", "confirm:no")]]))
        elif field == "new_color":
            draft["color"] = ""
            context.user_data["step"] = "new_quantity"
            await safe_edit(query, "Enter the opening quantity. Use 0 if none is currently in stock.", back_menu("nav:inventory"))
        elif field in {"expense_notes", "payment_notes"}:
            draft["notes"] = ""
            await show_confirmation(query, context)
        return

    if data.startswith("pay:"):
        payment = data.split(":", 1)[1]
        draft = current_draft(context)
        draft["payment"] = payment
        action = context.user_data.get("action")
        if action == "sale" and payment == "Credit":
            context.user_data["step"] = "sale_customer"
            await safe_edit(query, "Enter the customer's name for this credit sale.", back_menu())
        elif action == "sale_return":
            context.user_data["step"] = "return_customer"
            await safe_edit(
                query,
                "Enter the customer's name/reference, or tap Skip.",
                InlineKeyboardMarkup(
                    [[button("Skip", "skip:return_customer")], [button("❌ Cancel", "confirm:no")]]
                ),
            )
        elif action == "expense":
            context.user_data["step"] = "expense_notes"
            await safe_edit(query, "Enter an expense description/reference, or tap Skip.", InlineKeyboardMarkup([[button("Skip", "skip:expense_notes")], [button("❌ Cancel", "confirm:no")]]))
        elif action == "customer_payment":
            context.user_data["step"] = "payment_notes"
            await safe_edit(query, "Enter a payment reference/note, or tap Skip.", InlineKeyboardMarkup([[button("Skip", "skip:payment_notes")], [button("❌ Cancel", "confirm:no")]]))
        else:
            draft.setdefault("party", "")
            await show_confirmation(query, context)
        return

    if data == "confirm:yes":
        action = context.user_data.get("action")
        draft = current_draft(context)
        user_id, username = user_identity(update)
        if action == "sale":
            product = Product.from_dict(draft["product"])
            result = await asyncio.to_thread(
                store.change_stock,
                kind="SALE",
                product_key=product.key,
                quantity=draft["quantity"],
                unit_price=draft["unit_price"],
                unit_cost=product.cost_price,
                payment=draft["payment"],
                party=draft.get("party", ""),
                notes="",
                user_id=user_id,
                username=username,
            )
            text = (
                "Sale recorded successfully.\n\n"
                f"Reference: {result['transaction_id']}\nProduct: {product_name(product)}\n"
                f"Quantity: {display_number(result['quantity'])}\nTotal: Rs {display_number(result['total'])}\n"
                f"Stock: {display_number(result['old_quantity'])} → {display_number(result['new_quantity'])}"
            )
        elif action == "sale_return":
            product = Product.from_dict(draft["product"])
            result = await asyncio.to_thread(
                store.change_stock,
                kind="SALE_RETURN",
                product_key=product.key,
                quantity=draft["quantity"],
                unit_price=draft["unit_price"],
                unit_cost=product.cost_price,
                payment=draft["payment"],
                party=draft.get("party", ""),
                notes="Customer return",
                user_id=user_id,
                username=username,
            )
            text = (
                "Customer return recorded successfully.\n\n"
                f"Reference: {result['transaction_id']}\nProduct: {product_name(product)}\n"
                f"Returned: {display_number(result['quantity'])}\nRefund: Rs {display_number(result['total'])}\n"
                f"Stock: {display_number(result['old_quantity'])} → {display_number(result['new_quantity'])}"
            )
        elif action == "add_stock":
            product = Product.from_dict(draft["product"])
            result = await asyncio.to_thread(
                store.change_stock,
                kind="STOCK_IN",
                product_key=product.key,
                quantity=draft["quantity"],
                unit_cost=draft.get("unit_cost", 0),
                party=draft.get("party", ""),
                user_id=user_id,
                username=username,
            )
            text = (
                "Stock added successfully.\n\n"
                f"Reference: {result['transaction_id']}\nProduct: {product_name(product)}\n"
                f"Added: {display_number(result['quantity'])}\n"
                f"Stock: {display_number(result['old_quantity'])} → {display_number(result['new_quantity'])}"
            )
        elif action == "remove_stock":
            product = Product.from_dict(draft["product"])
            result = await asyncio.to_thread(
                store.change_stock,
                kind="STOCK_OUT",
                product_key=product.key,
                quantity=draft["quantity"],
                notes=draft["notes"],
                user_id=user_id,
                username=username,
            )
            text = (
                "Stock removed successfully.\n\n"
                f"Reference: {result['transaction_id']}\nProduct: {product_name(product)}\n"
                f"Removed: {display_number(result['quantity'])}\n"
                f"Stock: {display_number(result['old_quantity'])} → {display_number(result['new_quantity'])}"
            )
        elif action == "set_prices":
            product = Product.from_dict(draft["product"])
            updated = await asyncio.to_thread(
                store.set_prices,
                product.key,
                draft["cost_price"],
                draft["sale_price"],
                draft["low_stock"],
            )
            text = f"Prices saved successfully.\n\n{product_card(updated)}"
        elif action == "add_product":
            product = await asyncio.to_thread(
                store.add_product, draft, user_id=user_id, username=username
            )
            text = f"Product created successfully.\n\n{product_card(product)}"
        elif action == "expense":
            transaction_id = await asyncio.to_thread(
                store.record_expense,
                category=draft["category"],
                amount=draft["amount"],
                payment=draft["payment"],
                notes=draft.get("notes", ""),
                user_id=user_id,
                username=username,
            )
            text = f"Expense recorded successfully.\n\nReference: {transaction_id}\nAmount: Rs {display_number(draft['amount'])}"
        elif action == "customer_payment":
            transaction_id = await asyncio.to_thread(
                store.record_customer_payment,
                customer=draft["customer"],
                amount=draft["amount"],
                payment=draft["payment"],
                notes=draft.get("notes", ""),
                user_id=user_id,
                username=username,
            )
            text = f"Customer payment recorded.\n\nReference: {transaction_id}\nAmount: Rs {display_number(draft['amount'])}"
        else:
            raise ManagerError("This operation expired. Please start again.")
        reset(context)
        await safe_edit(query, text, main_menu())


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update):
        return
    message = update.effective_message
    text = message.text.strip()
    step = context.user_data.get("step")
    action = context.user_data.get("action")
    draft = current_draft(context)

    if not step:
        products = await asyncio.to_thread(store.search, text)
        context.user_data.update(action="search", draft={}, step="browse", select_action=None)
        # Message replies cannot be edited by a CallbackQuery, so create a result message.
        sent = await message.reply_text("Loading results...")
        class MessageQuery:
            async def edit_message_text(self, value, reply_markup=None):
                await sent.edit_text(value, reply_markup=reply_markup)
        await show_products(MessageQuery(), context, products, f'Search Results for "{text}"')
        return

    if step in {"product_query", "search_query"}:
        products = await asyncio.to_thread(store.search, text)
        if not products:
            await message.reply_text(f'No products matched "{text}". Try fewer words.', reply_markup=back_menu("nav:inventory"))
            return
        select_action = action if step == "product_query" else None
        sent = await message.reply_text("Loading results...")
        class MessageQuery:
            async def edit_message_text(self, value, reply_markup=None):
                await sent.edit_text(value, reply_markup=reply_markup)
        await show_products(
            MessageQuery(),
            context,
            products,
            f'{"Select a Product" if select_action else "Stock Search"}: "{text}"',
            select_action=select_action,
            parent="nav:inventory",
        )
        return

    if step in {"sale_quantity", "return_quantity", "stock_in_quantity", "stock_out_quantity"}:
        qty = whole_quantity(text)
        product = selected_product(context)
        if step in {"sale_quantity", "stock_out_quantity"} and qty > product.quantity:
            raise ManagerError(f"Only {display_number(product.quantity)} unit(s) are currently available.")
        draft["quantity"] = qty
        if step in {"sale_quantity", "return_quantity"}:
            context.user_data["step"] = "sale_price_choice"
            prompt = "Choose the original selling price:" if step == "return_quantity" else "Choose the selling price:"
            await message.reply_text(prompt, reply_markup=sale_price_menu(product))
        elif step == "stock_in_quantity":
            context.user_data["step"] = "stock_in_cost_choice"
            await message.reply_text("Choose the purchase cost:", reply_markup=cost_price_menu(product))
        else:
            context.user_data["step"] = "stock_out_reason"
            await message.reply_text("Enter the reason for removing stock, such as damaged, correction, or supplier return.")
        return

    if step == "sale_price":
        draft["unit_price"] = money_value(text, allow_zero=False)
        context.user_data["step"] = "payment"
        await message.reply_text("Select the payment method:", reply_markup=payment_menu())
        return
    if step == "sale_customer":
        if len(text) < 2:
            raise ManagerError("Enter the customer's name.")
        draft["party"] = text[:100]
        sent = await message.reply_text("Preparing confirmation...")
        class MessageQuery:
            async def edit_message_text(self, value, reply_markup=None):
                await sent.edit_text(value, reply_markup=reply_markup)
        await show_confirmation(MessageQuery(), context)
        return
    if step == "return_customer":
        draft["party"] = text[:100]
        sent = await message.reply_text("Preparing confirmation...")
        class MessageQuery:
            async def edit_message_text(self, value, reply_markup=None):
                await sent.edit_text(value, reply_markup=reply_markup)
        await show_confirmation(MessageQuery(), context)
        return
    if step == "stock_in_cost":
        draft["unit_cost"] = money_value(text)
        context.user_data["step"] = "stock_in_party"
        await message.reply_text(
            "Enter the supplier or invoice reference, or tap Skip.",
            reply_markup=InlineKeyboardMarkup([[button("Skip", "skip:stock_in_party")], [button("❌ Cancel", "confirm:no")]]),
        )
        return
    if step == "stock_in_party":
        draft["party"] = text[:150]
    elif step == "stock_out_reason":
        if len(text) < 2:
            raise ManagerError("A short removal reason is required.")
        draft["notes"] = text[:250]
    elif step == "set_cost":
        draft["cost_price"] = money_value(text)
        context.user_data["step"] = "set_sale"
        await message.reply_text("Enter the normal selling price per unit. Enter 0 if not set.")
        return
    elif step == "set_sale":
        draft["sale_price"] = money_value(text)
        context.user_data["step"] = "set_low"
        await message.reply_text("Enter the low-stock alert quantity, for example 3.")
        return
    elif step == "set_low":
        low = decimal_value(text, strict=True)
        if low < 0 or low != low.to_integral_value():
            raise ManagerError("Low-stock level must be a non-negative whole number.")
        draft["low_stock"] = low
    elif step == "new_brand":
        if len(text) < 2:
            raise ManagerError("Brand is required.")
        draft["brand"] = text[:100]
        context.user_data["step"] = "new_category"
        await message.reply_text("Enter the product category, such as Refrigerator or AC.")
        return
    elif step == "new_category":
        if len(text) < 2:
            raise ManagerError("Category is required.")
        draft["category"] = text[:100]
        context.user_data["step"] = "new_model"
        await message.reply_text("Enter the model number/name.")
        return
    elif step == "new_model":
        if len(text) < 1:
            raise ManagerError("Model is required.")
        draft["model"] = text[:120]
        context.user_data["step"] = "new_variant"
        await message.reply_text("Enter the variant/size, or tap Skip.", reply_markup=InlineKeyboardMarkup([[button("Skip", "skip:new_variant")], [button("❌ Cancel", "confirm:no")]]))
        return
    elif step == "new_variant":
        draft["variant"] = text[:120]
        context.user_data["step"] = "new_color"
        await message.reply_text("Enter the color, or tap Skip.", reply_markup=InlineKeyboardMarkup([[button("Skip", "skip:new_color")], [button("❌ Cancel", "confirm:no")]]))
        return
    elif step == "new_color":
        draft["color"] = text[:80]
        context.user_data["step"] = "new_quantity"
        await message.reply_text("Enter the opening quantity. Use 0 if none is in stock.")
        return
    elif step == "new_quantity":
        qty = decimal_value(text, strict=True)
        if qty < 0 or qty != qty.to_integral_value():
            raise ManagerError("Opening quantity must be a non-negative whole number.")
        draft["quantity"] = qty
        context.user_data["step"] = "new_cost"
        await message.reply_text("Enter the cost price per unit. Enter 0 if unknown.")
        return
    elif step == "new_cost":
        draft["cost_price"] = money_value(text)
        context.user_data["step"] = "new_sale"
        await message.reply_text("Enter the normal selling price per unit. Enter 0 if unknown.")
        return
    elif step == "new_sale":
        draft["sale_price"] = money_value(text)
        context.user_data["step"] = "new_low"
        await message.reply_text(f"Enter the low-stock alert quantity. Recommended: {DEFAULT_LOW_STOCK}")
        return
    elif step == "new_low":
        low = decimal_value(text, strict=True)
        if low < 0 or low != low.to_integral_value():
            raise ManagerError("Low-stock level must be a non-negative whole number.")
        draft["low_stock"] = low
    elif step == "expense_category":
        if len(text) < 2:
            raise ManagerError("Expense category is required.")
        draft["category"] = text[:100]
        context.user_data["step"] = "expense_amount"
        await message.reply_text("Enter the expense amount in PKR.")
        return
    elif step == "expense_amount":
        draft["amount"] = money_value(text, allow_zero=False)
        context.user_data["step"] = "payment"
        await message.reply_text("Select how the expense was paid:", reply_markup=payment_menu(allow_credit=False))
        return
    elif step == "expense_notes":
        draft["notes"] = text[:250]
    elif step == "customer_name":
        if len(text) < 2:
            raise ManagerError("Customer name is required.")
        draft["customer"] = text[:100]
        context.user_data["step"] = "customer_amount"
        await message.reply_text("Enter the amount received in PKR.")
        return
    elif step == "customer_amount":
        draft["amount"] = money_value(text, allow_zero=False)
        context.user_data["step"] = "payment"
        await message.reply_text("Select how the payment was received:", reply_markup=payment_menu(allow_credit=False))
        return
    elif step == "payment_notes":
        draft["notes"] = text[:250]
    elif step in {"payment", "confirm", "sale_price_choice", "stock_in_cost_choice", "browse"}:
        await message.reply_text("Please use the buttons shown above, or send /cancel.")
        return
    else:
        raise ManagerError("This operation expired. Please open the menu and try again.")

    sent = await message.reply_text("Preparing confirmation...")
    class MessageQuery:
        async def edit_message_text(self, value, reply_markup=None):
            await sent.edit_text(value, reply_markup=reply_markup)
    await show_confirmation(MessageQuery(), context)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    error = context.error
    if isinstance(error, Conflict):
        log.warning("Another bot instance is polling with the same token")
        return
    log.error("Unhandled bot error: %s\n%s", error, "".join(traceback.format_exception(error)))
    if not isinstance(update, Update) or not update.effective_message:
        return
    if isinstance(error, ManagerError):
        text = str(error)
    elif isinstance(error, PermissionError):
        text = "Google Sheets denied access. Share the sheet with the service account as Editor."
    elif isinstance(error, json.JSONDecodeError):
        text = "GOOGLE_CREDENTIALS_JSON is not valid JSON. Correct it in Railway."
    else:
        text = "The operation could not be completed. No confirmed change should be assumed. Please try again."
    try:
        await update.effective_message.reply_text(text, reply_markup=main_menu())
    except Exception:
        log.exception("Unable to send the user-facing error")


async def post_init(application: Application):
    await application.bot.set_my_commands(
        [
            BotCommand("menu", "Open the manager menu"),
            BotCommand("refresh", "Reload inventory from Google Sheets"),
            BotCommand("cancel", "Cancel the current operation"),
            BotCommand("id", "Show your Telegram user ID"),
        ]
    )


def main():
    if not AUTHORIZED_USER_IDS:
        log.warning("AUTHORIZED_USER_IDS is empty; anyone who finds the bot can use it")
    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("menu", start))
    application.add_handler(CommandHandler("id", show_id))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(CommandHandler("refresh", refresh))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.add_error_handler(on_error)
    log.info("Madina Electronics Manager v2 starting")
    # Discard stale button presses queued during a redeploy; a delayed Confirm
    # must never create an unintended duplicate transaction after a restart.
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
