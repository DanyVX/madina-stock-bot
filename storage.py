"""Google Sheets persistence and business rules for Madina Manager.

All Telegram handlers call this module through ``asyncio.to_thread``. Writes
are serialized and use compensating rollback when audit logging fails.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

import gspread
from google.oauth2.service_account import Credentials


log = logging.getLogger("madina-manager.storage")
TZ = ZoneInfo("Asia/Karachi")
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
CACHE_SECONDS = 45
INVENTORY_SHEET = "Manager_Inventory"
CATALOG_SHEET = "Manager_Catalog"
TRANSACTIONS_SHEET = "Manager_Transactions"
RESERVED_SHEETS = {
    "summary",
    "transactions",
    "product catalog",
    "manager catalog",
    "manager transactions",
    "sales",
    "expenses",
    "customers",
    "suppliers",
}

INVENTORY_HEADERS = [
    "SKU",
    "Brand",
    "Category",
    "Model",
    "Variant",
    "Color",
    "Qty",
    "Cost Price",
    "Sale Price",
    "Low Stock Level",
]
CATALOG_HEADERS = [
    "Product Key",
    "SKU",
    "Brand",
    "Category",
    "Model",
    "Variant",
    "Color",
    "Cost Price",
    "Sale Price",
    "Low Stock Level",
    "Updated At",
]
TRANSACTION_HEADERS = [
    "Transaction ID",
    "Timestamp",
    "Type",
    "Product Key",
    "SKU",
    "Brand",
    "Category",
    "Model",
    "Variant",
    "Color",
    "Quantity",
    "Unit Cost",
    "Unit Price",
    "Total",
    "Payment",
    "Party",
    "Notes",
    "Telegram User ID",
    "Telegram Username",
    "Reversed",
]

HEADER_ALIASES = {
    "sku": {"sku", "item code", "product code", "code"},
    "brand": {"brand", "company", "make"},
    "category": {"category", "type", "product category"},
    "model": {"model", "model no", "model number", "product", "product name", "item"},
    "variant": {"variant", "size", "description", "specification", "specifications"},
    "color": {"color", "colour"},
    "qty": {"qty", "quantity", "stock", "stock qty", "available", "units"},
    "cost_price": {"cost price", "cost", "purchase price", "buying price", "rate"},
    "sale_price": {"sale price", "selling price", "retail price", "price"},
    "low_stock": {"low stock", "low stock level", "reorder level", "minimum stock", "min stock"},
}


class ManagerError(Exception):
    """Safe business error that may be shown to a Telegram user."""


@dataclass
class Product:
    key: str
    sku: str
    sheet: str
    row: int
    qty_column: int
    brand: str
    category: str
    model: str
    variant: str
    color: str
    quantity: Decimal
    cost_price: Decimal
    sale_price: Decimal
    low_stock: Decimal

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Product":
        converted = dict(value)
        for field in ("quantity", "cost_price", "sale_price", "low_stock"):
            converted[field] = decimal_value(converted.get(field, 0))
        return cls(**converted)


def normalize(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().replace("_", " ").split())


def decimal_value(value: Any, *, strict: bool = False) -> Decimal:
    cleaned = (
        str(value if value is not None else "")
        .replace(",", "")
        .replace("PKR", "")
        .replace("Rs.", "")
        .replace("Rs", "")
        .strip()
    )
    if not cleaned:
        return Decimal("0")
    try:
        result = Decimal(cleaned)
    except InvalidOperation as exc:
        if strict:
            raise ManagerError(f'"{value}" is not a valid number.') from exc
        return Decimal("0")
    if not result.is_finite():
        if strict:
            raise ManagerError("The number must be finite.")
        return Decimal("0")
    return result


def whole_quantity(value: Any) -> Decimal:
    quantity = decimal_value(value, strict=True)
    if quantity <= 0 or quantity != quantity.to_integral_value():
        raise ManagerError("Quantity must be a positive whole number, for example 1 or 5.")
    return quantity


def money_value(value: Any, *, allow_zero: bool = True) -> Decimal:
    amount = decimal_value(value, strict=True)
    if amount < 0 or (not allow_zero and amount == 0):
        raise ManagerError("Enter a valid positive amount.")
    return amount.quantize(Decimal("0.01"))


def display_number(value: Any) -> str:
    number = decimal_value(value)
    if number == number.to_integral_value():
        return f"{int(number):,}"
    return f"{number:,.2f}"


def product_name(product: Product | dict[str, Any]) -> str:
    getter = (lambda key: getattr(product, key)) if isinstance(product, Product) else product.get
    parts = [getter("brand"), getter("category"), getter("model"), getter("variant"), getter("color")]
    return " ".join(str(part).strip() for part in parts if str(part or "").strip()) or "Unnamed product"


def _product_key(sheet: str, sku: str, fields: list[str]) -> str:
    if normalize(sku):
        return f"sku:{normalize(sku)}"
    identity = "|".join([normalize(sheet), *[normalize(field) for field in fields]])
    return "p:" + hashlib.sha1(identity.encode("utf-8")).hexdigest()[:16]


def _column_map(header: list[Any]) -> dict[str, int]:
    normalized = [normalize(cell) for cell in header]
    found: dict[str, int] = {}
    for canonical, aliases in HEADER_ALIASES.items():
        for index, value in enumerate(normalized):
            if value in aliases:
                found[canonical] = index
                break
    return found


def _cell(record: list[Any], columns: dict[str, int], name: str) -> str:
    index = columns.get(name)
    if index is None or index >= len(record):
        return ""
    return str(record[index]).strip()


class SheetStore:
    def __init__(self, sheet_id: str, credentials_json: str, default_low_stock: int = 3):
        self.sheet_id = sheet_id.strip()
        self.credentials_json = credentials_json
        self.default_low_stock = Decimal(max(0, int(default_low_stock)))
        self._client = None
        self._spreadsheet = None
        self._cache: list[Product] | None = None
        self._cache_time = 0.0
        self._lock = threading.RLock()

    def _book(self):
        if self._spreadsheet is None:
            credentials = Credentials.from_service_account_info(
                json.loads(self.credentials_json), scopes=SCOPES
            )
            self._client = gspread.authorize(credentials)
            self._spreadsheet = self._client.open_by_key(self.sheet_id)
        return self._spreadsheet

    def _worksheet(self, title: str, headers: list[str], rows: int = 3000):
        book = self._book()
        try:
            worksheet = book.worksheet(title)
        except gspread.WorksheetNotFound:
            worksheet = book.add_worksheet(title=title, rows=rows, cols=len(headers))
            worksheet.append_row(headers, value_input_option="RAW")
            worksheet.freeze(rows=1)
        values = worksheet.row_values(1)
        if not values:
            worksheet.append_row(headers, value_input_option="RAW")
        elif [normalize(value) for value in values[: len(headers)]] != [normalize(value) for value in headers]:
            raise ManagerError(
                f'The "{title}" sheet has an unexpected header layout. Rename that sheet and try again; '
                "the bot will create a clean manager sheet without overwriting it."
            )
        return worksheet

    def _catalog_map(self) -> tuple[Any, dict[str, tuple[int, dict[str, Any]]]]:
        worksheet = self._worksheet(CATALOG_SHEET, CATALOG_HEADERS)
        values = worksheet.get_all_values()
        catalog: dict[str, tuple[int, dict[str, Any]]] = {}
        for row_number, record in enumerate(values[1:], start=2):
            if not record or not str(record[0]).strip():
                continue
            padded = record + [""] * (len(CATALOG_HEADERS) - len(record))
            data = dict(zip(CATALOG_HEADERS, padded))
            catalog[str(data["Product Key"]).strip()] = (row_number, data)
        return worksheet, catalog

    def load_products(self, force: bool = False) -> list[Product]:
        with self._lock:
            now = time.monotonic()
            if not force and self._cache is not None and now - self._cache_time < CACHE_SECONDS:
                return list(self._cache)

            _, catalog = self._catalog_map()
            products: list[Product] = []
            used_keys: set[str] = set()
            for worksheet in self._book().worksheets():
                if normalize(worksheet.title) in RESERVED_SHEETS:
                    continue
                values = worksheet.get_all_values()
                if not values:
                    continue
                header_index = None
                columns: dict[str, int] = {}
                for possible_index, possible_header in enumerate(values[:15]):
                    candidate = _column_map(possible_header)
                    if "qty" in candidate and any(key in candidate for key in ("brand", "model", "category")):
                        header_index = possible_index
                        columns = candidate
                        break
                if header_index is None:
                    log.warning("Skipped %s: recognizable product and quantity columns were not found", worksheet.title)
                    continue

                for row_number, record in enumerate(values[header_index + 1 :], start=header_index + 2):
                    sku = _cell(record, columns, "sku")
                    brand = _cell(record, columns, "brand")
                    category = _cell(record, columns, "category")
                    model = _cell(record, columns, "model")
                    variant = _cell(record, columns, "variant")
                    color = _cell(record, columns, "color")
                    if not any((sku, brand, category, model, variant, color)):
                        continue
                    if "total" in {normalize(brand), normalize(model)} or normalize(brand) == "total units":
                        continue
                    key = _product_key(worksheet.title, sku, [brand, category, model, variant, color])
                    if key in used_keys:
                        key = f"{key}:{row_number}"
                    used_keys.add(key)
                    overlay = catalog.get(key, (0, {}))[1]
                    cost = decimal_value(_cell(record, columns, "cost_price"))
                    sale = decimal_value(_cell(record, columns, "sale_price"))
                    raw_low = _cell(record, columns, "low_stock")
                    low = decimal_value(raw_low)
                    if cost == 0:
                        cost = decimal_value(overlay.get("Cost Price", 0))
                    if sale == 0:
                        sale = decimal_value(overlay.get("Sale Price", 0))
                    if not raw_low:
                        overlay_low = str(overlay.get("Low Stock Level", "")).strip()
                        low = decimal_value(overlay_low) if overlay_low else self.default_low_stock
                    products.append(
                        Product(
                            key=key,
                            sku=sku,
                            sheet=worksheet.title,
                            row=row_number,
                            qty_column=columns["qty"] + 1,
                            brand=brand,
                            category=category,
                            model=model,
                            variant=variant,
                            color=color,
                            quantity=decimal_value(_cell(record, columns, "qty")),
                            cost_price=cost,
                            sale_price=sale,
                            low_stock=low,
                        )
                    )

            products.sort(key=lambda product: normalize(product_name(product)))
            self._cache = products
            self._cache_time = now
            log.info("Loaded %d products from %d worksheets", len(products), len(self._book().worksheets()))
            return list(products)

    def search(self, query: str, force: bool = False) -> list[Product]:
        words = [normalize(word) for word in str(query).split() if normalize(word)]
        if not words:
            return []
        matches = []
        for product in self.load_products(force=force):
            haystack = normalize(
                " ".join(
                    [product.sku, product.brand, product.category, product.model, product.variant, product.color]
                )
            )
            if all(word in haystack for word in words):
                matches.append(product)
        return matches

    def get_product(self, key: str, force: bool = False) -> Product:
        for product in self.load_products(force=force):
            if product.key == key:
                return product
        raise ManagerError("This product no longer exists or was moved. Refresh and select it again.")

    def add_product(
        self, data: dict[str, Any], *, user_id: int = 0, username: str = ""
    ) -> Product:
        with self._lock:
            required = [str(data.get("brand", "")).strip(), str(data.get("model", "")).strip()]
            if not all(required):
                raise ManagerError("Brand and model are required.")
            fields = [
                str(data.get(name, "")).strip()
                for name in ("brand", "category", "model", "variant", "color")
            ]
            candidate_identity = [normalize(value) for value in fields]
            for product in self.load_products(force=True):
                current_identity = [
                    normalize(value)
                    for value in (product.brand, product.category, product.model, product.variant, product.color)
                ]
                if current_identity == candidate_identity:
                    raise ManagerError(f"This product already exists: {product_name(product)}")

            sku = str(data.get("sku", "")).strip() or f"ME-{datetime.now(TZ):%y%m%d}-{uuid.uuid4().hex[:5].upper()}"
            quantity = decimal_value(data.get("quantity", 0), strict=True)
            if quantity < 0 or quantity != quantity.to_integral_value():
                raise ManagerError("Opening quantity must be a non-negative whole number.")
            cost = money_value(data.get("cost_price", 0))
            sale = money_value(data.get("sale_price", 0))
            low = decimal_value(data.get("low_stock", self.default_low_stock), strict=True)
            if low < 0 or low != low.to_integral_value():
                raise ManagerError("Low-stock level must be a non-negative whole number.")

            worksheet = self._worksheet(INVENTORY_SHEET, INVENTORY_HEADERS)
            # Create at zero and post opening stock through the same audited,
            # rollback-protected path used for later stock receipts.
            worksheet.append_row(
                [sku, *fields, 0, str(cost), str(sale), int(low)],
                value_input_option="RAW",
            )
            self.invalidate()
            matches = [product for product in self.load_products(force=True) if normalize(product.sku) == normalize(sku)]
            if not matches:
                raise ManagerError("The product row was added, but it could not be reloaded. Press Refresh.")
            product = matches[0]
            if quantity > 0:
                self.change_stock(
                    kind="STOCK_IN",
                    product_key=product.key,
                    quantity=quantity,
                    unit_cost=cost,
                    party="Opening stock",
                    notes="Opening stock for new product",
                    user_id=user_id,
                    username=username,
                )
            return self.get_product(product.key, force=True)

    def set_prices(self, product_key: str, cost_price: Any, sale_price: Any, low_stock: Any) -> Product:
        with self._lock:
            product = self.get_product(product_key, force=True)
            cost = money_value(cost_price)
            sale = money_value(sale_price)
            low = decimal_value(low_stock, strict=True)
            if low < 0 or low != low.to_integral_value():
                raise ManagerError("Low-stock level must be a non-negative whole number.")
            worksheet, catalog = self._catalog_map()
            row = [
                product.key,
                product.sku,
                product.brand,
                product.category,
                product.model,
                product.variant,
                product.color,
                str(cost),
                str(sale),
                int(low),
                datetime.now(TZ).isoformat(timespec="seconds"),
            ]
            existing = catalog.get(product.key)
            if existing:
                worksheet.update([row], f"A{existing[0]}:K{existing[0]}", value_input_option="RAW")
            else:
                worksheet.append_row(row, value_input_option="RAW")
            self.invalidate()
            return self.get_product(product.key, force=True)

    def _transaction_sheet(self):
        return self._worksheet(TRANSACTIONS_SHEET, TRANSACTION_HEADERS, rows=10000)

    def _append_transaction(
        self,
        *,
        kind: str,
        product: Product | None,
        quantity: Decimal,
        unit_cost: Decimal,
        unit_price: Decimal,
        payment: str,
        party: str,
        notes: str,
        user_id: int,
        username: str,
    ) -> str:
        transaction_id = f"ME-{datetime.now(TZ):%Y%m%d%H%M%S}-{uuid.uuid4().hex[:5].upper()}"
        # A stock receipt is valued at its purchase cost; sales and returns are
        # valued at the customer price. This keeps history and reports truthful.
        total = quantity * (unit_cost if kind == "STOCK_IN" else unit_price)
        values = [
            transaction_id,
            datetime.now(TZ).isoformat(timespec="seconds"),
            kind,
            product.key if product else "",
            product.sku if product else "",
            product.brand if product else "",
            product.category if product else "",
            product.model if product else "",
            product.variant if product else "",
            product.color if product else "",
            str(quantity),
            str(unit_cost),
            str(unit_price),
            str(total),
            payment,
            party,
            notes,
            user_id,
            username,
            "No",
        ]
        self._transaction_sheet().append_row(values, value_input_option="RAW")
        return transaction_id

    def change_stock(
        self,
        *,
        kind: str,
        product_key: str,
        quantity: Any,
        unit_price: Any = 0,
        unit_cost: Any = 0,
        payment: str = "",
        party: str = "",
        notes: str = "",
        user_id: int,
        username: str,
    ) -> dict[str, Any]:
        kind = normalize(kind).upper().replace(" ", "_")
        if kind not in {"SALE", "STOCK_IN", "STOCK_OUT", "SALE_RETURN"}:
            raise ManagerError("Unsupported stock transaction type.")
        qty = whole_quantity(quantity)
        price = money_value(unit_price)
        cost = money_value(unit_cost)
        if kind in {"SALE", "SALE_RETURN"} and price <= 0:
            raise ManagerError("A sale or return price greater than zero is required.")
        if kind == "STOCK_OUT" and not str(notes).strip():
            raise ManagerError("A reason is required when removing stock.")

        with self._lock:
            product = self.get_product(product_key, force=True)
            worksheet = self._book().worksheet(product.sheet)
            raw_current = worksheet.cell(product.row, product.qty_column).value
            current = decimal_value(raw_current, strict=True)
            delta = qty if kind in {"STOCK_IN", "SALE_RETURN"} else -qty
            new_quantity = current + delta
            if new_quantity < 0:
                raise ManagerError(
                    f"Only {display_number(current)} unit(s) are available; {display_number(qty)} cannot be removed."
                )

            worksheet.update_cell(product.row, product.qty_column, str(new_quantity))
            try:
                transaction_id = self._append_transaction(
                    kind=kind,
                    product=product,
                    quantity=qty,
                    unit_cost=cost if cost else product.cost_price,
                    unit_price=price,
                    payment=str(payment).strip(),
                    party=str(party).strip(),
                    notes=str(notes).strip(),
                    user_id=user_id,
                    username=username,
                )
            except Exception:
                try:
                    worksheet.update_cell(product.row, product.qty_column, str(current))
                except Exception:
                    log.critical("Stock rollback failed for %s", product.key, exc_info=True)
                raise

            if kind == "STOCK_IN" and cost > 0:
                try:
                    self.set_prices(product.key, cost, product.sale_price, product.low_stock)
                except Exception:
                    log.exception("Stock was recorded but automatic cost-price update failed")
            self.invalidate()
            return {
                "transaction_id": transaction_id,
                "product": product.to_dict(),
                "old_quantity": current,
                "new_quantity": new_quantity,
                "quantity": qty,
                "unit_price": price,
                "unit_cost": cost if cost else product.cost_price,
                "total": qty * (cost if kind == "STOCK_IN" else price),
                "kind": kind,
            }

    def record_expense(
        self,
        *,
        category: str,
        amount: Any,
        payment: str,
        notes: str,
        user_id: int,
        username: str,
    ) -> str:
        value = money_value(amount, allow_zero=False)
        if not str(category).strip():
            raise ManagerError("Expense category is required.")
        with self._lock:
            return self._append_transaction(
                kind="EXPENSE",
                product=None,
                quantity=Decimal("1"),
                unit_cost=Decimal("0"),
                unit_price=value,
                payment=payment,
                party="",
                notes=f"{category.strip()}: {notes.strip()}".strip(": "),
                user_id=user_id,
                username=username,
            )

    def record_customer_payment(
        self,
        *,
        customer: str,
        amount: Any,
        payment: str,
        notes: str,
        user_id: int,
        username: str,
    ) -> str:
        value = money_value(amount, allow_zero=False)
        if not str(customer).strip():
            raise ManagerError("Customer name is required.")
        with self._lock:
            return self._append_transaction(
                kind="CUSTOMER_PAYMENT",
                product=None,
                quantity=Decimal("1"),
                unit_cost=Decimal("0"),
                unit_price=value,
                payment=payment,
                party=customer,
                notes=notes,
                user_id=user_id,
                username=username,
            )

    def transaction_records(self) -> list[dict[str, str]]:
        values = self._transaction_sheet().get_all_values()
        if not values:
            return []
        header = values[0]
        records = []
        for row in values[1:]:
            if not any(str(cell).strip() for cell in row):
                continue
            padded = row + [""] * (len(header) - len(row))
            records.append(dict(zip(header, padded)))
        return records

    def daily_summary(self, date=None) -> dict[str, Any]:
        target = date or datetime.now(TZ).date()
        summary = {
            "date": target,
            "sales_count": 0,
            "items_sold": Decimal("0"),
            "sales": Decimal("0"),
            "cost_of_goods": Decimal("0"),
            "expenses": Decimal("0"),
            "stock_in": Decimal("0"),
            "stock_out": Decimal("0"),
            "returns": Decimal("0"),
            "payments": {"Cash": Decimal("0"), "Bank": Decimal("0"), "Credit": Decimal("0")},
        }
        for record in self.transaction_records():
            try:
                timestamp = datetime.fromisoformat(str(record.get("Timestamp", "")))
            except ValueError:
                continue
            if timestamp.date() != target or normalize(record.get("Reversed")) == "yes":
                continue
            kind = str(record.get("Type", "")).upper()
            qty = decimal_value(record.get("Quantity", 0))
            price = decimal_value(record.get("Unit Price", 0))
            cost = decimal_value(record.get("Unit Cost", 0))
            total = decimal_value(record.get("Total", qty * price))
            if kind == "SALE":
                summary["sales_count"] += 1
                summary["items_sold"] += qty
                summary["sales"] += total
                summary["cost_of_goods"] += qty * cost
                payment = str(record.get("Payment", "")).title()
                if payment in summary["payments"]:
                    summary["payments"][payment] += total
            elif kind == "EXPENSE":
                summary["expenses"] += total
            elif kind == "STOCK_IN":
                summary["stock_in"] += qty
            elif kind == "STOCK_OUT":
                summary["stock_out"] += qty
            elif kind == "SALE_RETURN":
                summary["returns"] += total
                summary["sales"] -= total
                summary["cost_of_goods"] -= qty * cost
                payment = str(record.get("Payment", "")).title()
                if payment in summary["payments"]:
                    summary["payments"][payment] -= total
        summary["gross_profit"] = summary["sales"] - summary["cost_of_goods"]
        summary["net_after_expenses"] = summary["gross_profit"] - summary["expenses"]
        return summary

    def inventory_summary(self) -> dict[str, Any]:
        products = self.load_products(force=True)
        return {
            "products": len(products),
            "units": sum((product.quantity for product in products), Decimal("0")),
            "cost_value": sum((product.quantity * product.cost_price for product in products), Decimal("0")),
            "retail_value": sum((product.quantity * product.sale_price for product in products), Decimal("0")),
            "out_of_stock": sum(1 for product in products if product.quantity <= 0),
            "low_stock": sum(1 for product in products if product.quantity <= product.low_stock),
            "missing_cost": sum(1 for product in products if product.cost_price <= 0),
            "missing_sale": sum(1 for product in products if product.sale_price <= 0),
        }

    def customer_balances(self) -> list[dict[str, Any]]:
        balances: dict[str, Decimal] = {}
        labels: dict[str, str] = {}
        for record in self.transaction_records():
            if normalize(record.get("Reversed")) == "yes":
                continue
            party = str(record.get("Party", "")).strip()
            if not party:
                continue
            key = normalize(party)
            labels[key] = party
            kind = str(record.get("Type", "")).upper()
            total = decimal_value(record.get("Total", 0))
            if kind == "SALE" and str(record.get("Payment", "")).lower() == "credit":
                balances[key] = balances.get(key, Decimal("0")) + total
            elif kind == "CUSTOMER_PAYMENT":
                balances[key] = balances.get(key, Decimal("0")) - total
            elif kind == "SALE_RETURN" and str(record.get("Payment", "")).lower() == "credit":
                balances[key] = balances.get(key, Decimal("0")) - total
        return [
            {"customer": labels[key], "balance": balance}
            for key, balance in sorted(balances.items(), key=lambda item: item[1], reverse=True)
            if balance != 0
        ]

    def invalidate(self):
        self._cache = None
        self._cache_time = 0.0
