"""Build the Accounts Payable dashboard from the real QuickBooks company via Coefficient.

Authoritative source
--------------------
QuickBooks Bill object import (Google Sheets / Coefficient).

The import is expected to include bill-level fields and line-level fields.  One
bill may occupy several rows because Coefficient expands Line as rows.  This
script groups rows by Bill Id, counts/sums each Bill exactly once, converts
foreign-currency balances to the QuickBooks home currency with Exchange Rate,
and allocates each open balance across the Bill's own QuickBooks line accounts.

General Ledger is only a fallback when a Bill line has no account.  Vendor
Balance Detail is not used for totals because the Coefficient QuickBooks report
currently returns its money columns blank for this company.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
from collections import defaultdict
from datetime import date, datetime, timezone

from google_sheets_client import GoogleSheetsClient, GoogleSheetsError

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.abspath(os.path.join(HERE, "..", "data"))
OUTPUT_PATH = os.path.join(DATA_DIR, "ap-data.json")
VENDOR_MAP_PATH = os.path.join(DATA_DIR, "vendor-map.json")

DEFAULT_GSHEET_ID = "1wU-is7u0YFXbI3ZRYZ2MlEO-mqY8bD4NAXouNxhg73c"
DEFAULT_BILLS_SHEET = "QuickBooks Bill"
DEFAULT_BILLS_GID = "1297077839"
DEFAULT_GL_SHEET = "QuickBooks General Ledger Import"
DEFAULT_GL_GID = "186431676"

HOME_CURRENCY = "USD"
CATEGORIES = [
    "Inventory",
    "Shipping & Fulfillment",
    "Advertising",
    "Sales & Marketing",
    "G&A / OPEX",
    "Unclassified",
]

# Account rules are evaluated after explicit vendor rules.  The vendor override
# is intentional for vendors whose accounting account is too broad for the
# executive view (e.g. Meta booked to generic Selling & Marketing; Link
# Logistics booked to Rent & Lease but operationally a fulfillment cost).
ACCOUNT_CATEGORY_RULES = [
    ("Shipping & Fulfillment", [
        "shipping", "freight", "fulfillment", "postage", "delivery",
        "warehouse", "warehousing", "packaging", "dropship",
        "inbound shipping", "outbound shipping",
    ]),
    ("Advertising", [
        "advertising", "advertisement", "paid media", "google ads", "meta ads",
        "facebook ads", "ad spend", "ppc", "media buying", "affiliate support",
    ]),
    ("Sales & Marketing", [
        "selling & marketing", "marketing expense", "creative", "content",
        "seo", "sponsorship", "event", "brand", "sales commission",
        "influencer", "affiliate",
    ]),
    ("Inventory", [
        "inventory asset", "inventory assets", "inventory", "cost of goods",
        "cogs", "merchandise", "product cost", "purchases for resale",
        "purchases - resale",
    ]),
    ("G&A / OPEX", [
        "employee reimbursement", "maintenance", "repair", "payroll", "wages",
        "salary", "staff", "contract labor", "contractor", "consulting",
        "professional service", "professional fee", "software", "subscription",
        "rent", "lease", "office", "insurance", "legal", "accounting",
        "bank fee", "merchant fee", "utilities", "gas and electric",
        "general & administrative", "g&a", "opex", "tax", "audit",
        "intangible asset", "trademark",
    ]),
]

EXCLUDED_GL_ACCOUNTS = [
    "accounts payable", "a/p", "accounts receivable", "a/r", "bank account",
    "checking", "savings", "undeposited funds", "credit card payable",
]

ALIASES = {
    "id": ["id", "bill id"],
    "vendor": ["vendor name (vendor reference)", "vendor name", "vendor", "name"],
    "vendor_id": ["vendor id (vendor reference)", "vendor id"],
    "txn_date": ["transaction date", "txn date", "txndate", "date"],
    "due_date": ["due date", "duedate"],
    "total_amt": ["total amount", "totalamt", "total amt"],
    "currency": ["currency (currency reference)", "currency"],
    "doc_number": ["bill number", "docnumber", "doc number", "invoice number", "num"],
    "balance": ["balance", "open balance"],
    "exchange_rate": ["exchange rate", "exchangerate"],
    "ap_account": ["account name (ap account reference)", "ap account"],
    "line_account": ["account name (line > account based expense line detail > account reference)"],
    "line_account_id": ["account id (line > account based expense line detail > account reference)"],
    "line_item": ["item name (line > item based expense line detail > item reference)"],
    "line_item_id": ["item id (line > item based expense line detail > item reference)"],
    "line_number": ["line number (line)", "line number"],
    "line_description": ["description (line)", "description"],
    "line_amount": ["amount (line)", "line amount"],
    # General Ledger fields
    "transaction_type": ["transaction type"],
    "account": ["account", "distribution account"],
    "debit": ["debit"],
    "credit": ["credit"],
    "amount": ["amount"],
    "memo": ["memo/description", "memo", "private note"],
}


def text(v):
    return "" if v is None else str(v).strip()


def norm(v):
    return re.sub(r"[^a-z0-9]+", "", text(v).lower())


def money(v):
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = text(v).replace("$", "").replace(",", "")
    if not s or s in {"-", "—"}:
        return 0.0
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    try:
        return float(s)
    except ValueError:
        return 0.0


def parse_date(v):
    s = text(v)
    if not s:
        return ""
    if "T" in s:
        s = s.split("T", 1)[0]
    if " " in s and re.match(r"^\d{4}-\d{2}-\d{2} ", s):
        s = s.split(" ", 1)[0]
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    return ""


def rows_from_csv(s):
    return list(csv.reader(io.StringIO(s)))


def header_map(row):
    normalized = [norm(x) for x in row]
    out = {}
    for key, aliases in ALIASES.items():
        wanted = {norm(a) for a in aliases}
        out[key] = [i for i, cell in enumerate(normalized) if cell in wanted]
    return out


def first_cell(row, h, key):
    for i in h.get(key, []):
        if i < len(row) and text(row[i]) != "":
            return row[i]
    return ""


def load_vendor_map():
    try:
        with open(VENDOR_MAP_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"rules": [], "default_category": "Unclassified"}


def vendor_override(vendor, vm):
    v = text(vendor).lower()
    for rule in vm.get("rules", []):
        kw = text(rule.get("keyword")).lower()
        cat = text(rule.get("category"))
        if kw and kw in v and cat in CATEGORIES:
            return cat
    return ""


def classify_account(account, vendor, vm):
    # Explicit executive vendor mapping wins for known vendors.
    override = vendor_override(vendor, vm)
    if override:
        return override
    a = text(account).lower()
    for cat, kws in ACCOUNT_CATEGORY_RULES:
        if any(k in a for k in kws):
            return cat
    return "Unclassified"


def aging_bucket(due_iso, today):
    if not due_iso:
        return "not_yet_due"
    due = datetime.strptime(due_iso, "%Y-%m-%d").date()
    days = (due - today).days
    if days >= 0 and due.year == today.year and due.month == today.month:
        return "due_this_month"
    if days >= 0:
        return "not_yet_due"
    return "overdue_lt_3m" if -days <= 90 else "overdue_gt_3m"


def find_bills_header(rows):
    for i, row in enumerate(rows[:40]):
        h = header_map(row)
        if h.get("id") and h.get("vendor") and h.get("txn_date") and h.get("balance"):
            return i, h
    return None, None


def home_fx(currency, exchange_rate):
    cur = text(currency).upper() or HOME_CURRENCY
    if cur == HOME_CURRENCY:
        return 1.0
    fx = money(exchange_rate)
    return fx if fx > 0 else 1.0


def parse_bills_object(csv_text):
    """Parse Bill + Line rows into one Bill object per QuickBooks Bill Id."""
    rows = rows_from_csv(csv_text)
    hi, h = find_bills_header(rows)
    if hi is None:
        raise ValueError(
            "QuickBooks Bill header not found. Expected Id, Vendor Name (Vendor Reference), "
            "Transaction Date, Total Amount, Balance and line fields."
        )

    headers = rows[hi]
    print("Bills object headers:", " | ".join(text(x) for x in headers if text(x)))
    print(
        "Detected -> "
        f"Id:{h['id']} Vendor:{h['vendor']} Date:{h['txn_date']} Due:{h['due_date']} "
        f"Total:{h['total_amt']} Balance:{h['balance']} FX:{h['exchange_rate']} "
        f"LineAccount:{h['line_account']} LineAmount:{h['line_amount']}"
    )

    bills = {}
    current_id = ""

    for row in rows[hi + 1 :]:
        row_id = text(first_cell(row, h, "id"))
        if row_id:
            current_id = row_id
            vendor = text(first_cell(row, h, "vendor")) or "Unknown vendor"
            txn_date = parse_date(first_cell(row, h, "txn_date"))
            if not txn_date:
                current_id = ""
                continue
            currency = text(first_cell(row, h, "currency")) or HOME_CURRENCY
            fx = home_fx(currency, first_cell(row, h, "exchange_rate"))
            total_native = money(first_cell(row, h, "total_amt"))
            balance_native = money(first_cell(row, h, "balance"))
            bills[current_id] = {
                "id": current_id,
                "vendor": vendor,
                "vendor_id": text(first_cell(row, h, "vendor_id")),
                "invoice_number": text(first_cell(row, h, "doc_number")),
                "invoice_date": txn_date,
                "due_date": parse_date(first_cell(row, h, "due_date")),
                "currency": currency,
                "exchange_rate": fx,
                "original_amount_native": total_native,
                "open_balance_native": balance_native,
                "original_amount": round(total_native * fx, 2),
                "open_balance": round(balance_native * fx, 2),
                "ap_account": text(first_cell(row, h, "ap_account")),
                "lines": [],
            }
        elif not current_id:
            continue

        bill = bills.get(current_id)
        if not bill:
            continue

        line_amount_native = money(first_cell(row, h, "line_amount"))
        line_account = text(first_cell(row, h, "line_account"))
        item_name = text(first_cell(row, h, "line_item"))
        description = text(first_cell(row, h, "line_description"))
        # Keep zero-amount lines only if they contain useful coding information.
        if line_account or item_name or description or abs(line_amount_native) > 0.0001:
            bill["lines"].append({
                "line_number": text(first_cell(row, h, "line_number")),
                "account": line_account,
                "account_id": text(first_cell(row, h, "line_account_id")),
                "item": item_name,
                "item_id": text(first_cell(row, h, "line_item_id")),
                "description": description,
                "amount_native": line_amount_native,
                "amount": round(line_amount_native * bill["exchange_rate"], 2),
            })

    parsed = list(bills.values())
    if not parsed:
        raise ValueError("QuickBooks Bill import contained no usable Bills.")

    open_bills = [b for b in parsed if b["open_balance"] > 0.005]
    foreign = [b for b in open_bills if text(b["currency"]).upper() != HOME_CURRENCY]
    print(f"Unique Bills parsed: {len(parsed)}; open bills: {len(open_bills)}")
    print(f"Open AP after FX conversion: ${sum(b['open_balance'] for b in open_bills):,.2f}")
    if foreign:
        print("Foreign-currency open bills converted to USD:")
        for b in foreign:
            print(
                f"  {b['vendor']} #{b['invoice_number']}: {b['open_balance_native']:,.2f} "
                f"{b['currency']} × {b['exchange_rate']:.6f} = ${b['open_balance']:,.2f}"
            )
    return parsed


def find_gl_header(rows):
    for i, row in enumerate(rows[:100]):
        h = header_map(row)
        if h.get("txn_date") and h.get("transaction_type") and h.get("vendor") and h.get("account"):
            return i, h
    return None, None


def gl_account_is_distribution(account):
    s = text(account).lower()
    return bool(s) and not any(x in s for x in EXCLUDED_GL_ACCOUNTS)


def parse_general_ledger(csv_text):
    """Build lightweight fallback mappings for Bills missing line-account coding."""
    if not csv_text.strip():
        return {"invoice": {}, "vendor": {}, "usable_rows": 0}
    rows = rows_from_csv(csv_text)
    hi, h = find_gl_header(rows)
    if hi is None:
        print("::warning title=General Ledger::Header not recognized; Bill line accounts will still be used.")
        return {"invoice": {}, "vendor": {}, "usable_rows": 0}

    invoice = defaultdict(lambda: defaultdict(float))
    vendor = defaultdict(lambda: defaultdict(float))
    usable = 0
    for row in rows[hi + 1 :]:
        t = text(first_cell(row, h, "transaction_type"))
        v = text(first_cell(row, h, "vendor"))
        a = text(first_cell(row, h, "account"))
        n = text(first_cell(row, h, "doc_number"))
        if t.lower() != "bill" or not v or not gl_account_is_distribution(a):
            continue
        weights = []
        for k in ("debit", "credit", "amount"):
            raw = first_cell(row, h, k)
            if text(raw):
                weights.append(abs(money(raw)))
        w = max(weights or [0])
        if w <= 0:
            continue
        vk, nk = norm(v), norm(n)
        if nk:
            invoice[(vk, nk)][a] += w
        vendor[vk][a] += w
        usable += 1
    print(f"General Ledger fallback distribution rows: {usable}")
    return {"invoice": invoice, "vendor": vendor, "usable_rows": usable}


def fallback_gl_weights(bill, gl):
    vk, nk = norm(bill["vendor"]), norm(bill["invoice_number"])
    if nk and gl.get("invoice", {}).get((vk, nk)):
        return dict(gl["invoice"][(vk, nk)]), "general ledger exact invoice"
    if gl.get("vendor", {}).get(vk):
        return dict(gl["vendor"][vk]), "general ledger vendor history"
    return {}, "unmapped"


def allocate_open_balance(bill, gl, vm):
    """Allocate USD open balance using this Bill's own line amounts/accounts.

    If a Bill is partially paid, each line receives the same open percentage:
      line allocation = open balance × line amount / total bill line amount.
    This keeps account allocations exactly reconciled to Bill.Balance.
    """
    open_balance = max(0.0, float(bill["open_balance"] or 0))
    if open_balance <= 0.005:
        return []

    usable_lines = [
        line for line in bill.get("lines", [])
        if line.get("account") and abs(float(line.get("amount", 0) or 0)) > 0.0001
    ]
    line_total = sum(max(0.0, float(x.get("amount", 0) or 0)) for x in usable_lines)

    if usable_lines and line_total > 0:
        allocs = []
        running = 0.0
        for i, line in enumerate(usable_lines):
            if i == len(usable_lines) - 1:
                amt = round(open_balance - running, 2)
            else:
                amt = round(open_balance * max(0.0, line["amount"]) / line_total, 2)
                running += amt
            account = line["account"]
            allocs.append({
                "account": account,
                "category": classify_account(account, bill["vendor"], vm),
                "open_balance": amt,
                "mapping_source": "QuickBooks Bill line account",
            })
        return allocs

    # Fallback to GL only when Bill lines do not provide an account.
    weights, source = fallback_gl_weights(bill, gl)
    if weights:
        total_w = sum(weights.values()) or 1.0
        allocs, running = [], 0.0
        items = sorted(weights.items(), key=lambda x: x[1], reverse=True)
        for i, (account, weight) in enumerate(items):
            if i == len(items) - 1:
                amt = round(open_balance - running, 2)
            else:
                amt = round(open_balance * weight / total_w, 2)
                running += amt
            allocs.append({
                "account": account,
                "category": classify_account(account, bill["vendor"], vm),
                "open_balance": amt,
                "mapping_source": source,
            })
        return allocs

    category = vendor_override(bill["vendor"], vm) or "Unclassified"
    return [{
        "account": "Unmapped - review in QuickBooks",
        "category": category,
        "open_balance": round(open_balance, 2),
        "mapping_source": "unmapped",
    }]


def blank_rollup(key, name):
    return {
        key: name,
        "total": 0.0,
        "not_yet_due": 0.0,
        "due_this_month": 0.0,
        "overdue_lt_3m": 0.0,
        "overdue_gt_3m": 0.0,
        "bill_count": 0,
    }


def load_previous_trend():
    try:
        with open(OUTPUT_PATH, encoding="utf-8") as f:
            return json.load(f).get("monthly_trend", []) or []
    except Exception:
        return []


def update_trend(prev, total):
    month = date.today().strftime("%Y-%m")
    by_month = {str(x.get("month")): x for x in prev if x.get("month")}
    by_month[month] = {"month": month, "total_ap": round(total, 2)}
    return [by_month[k] for k in sorted(by_month)[-6:]]


def build_dashboard(bills_csv, gl_csv=""):
    vm = load_vendor_map()
    bills = parse_bills_object(bills_csv)
    gl = parse_general_ledger(gl_csv)
    today = date.today()

    accounts = {}
    categories = {c: blank_rollup("name", c) for c in CATEGORIES}
    vendors = defaultdict(float)
    invoices = []
    aging = defaultdict(float)

    total_ap = 0.0
    allocation_total = 0.0
    missing_due = 0
    missing_account = 0
    unclassified_balance = 0.0
    unclassified_count = 0

    for bill in bills:
        bal = max(0.0, float(bill["open_balance"] or 0))
        original = max(0.0, float(bill["original_amount"] or 0))
        paid = max(0.0, original - bal)
        bucket = aging_bucket(bill["due_date"], today) if bal > 0.005 else ""

        allocs = allocate_open_balance(bill, gl, vm) if bal > 0.005 else []
        labels = [a["account"] for a in allocs]
        cat_amounts = defaultdict(float)

        if bal > 0.005:
            total_ap += bal
            aging[bucket] += bal
            vendors[bill["vendor"]] += bal
            if not bill["due_date"]:
                missing_due += 1
            if not labels or labels == ["Unmapped - review in QuickBooks"]:
                missing_account += 1

            for a in allocs:
                allocation_total += a["open_balance"]
                cat_amounts[a["category"]] += a["open_balance"]
                r = accounts.setdefault(a["account"], blank_rollup("account", a["account"]))
                r["category"] = a["category"]
                r["total"] += a["open_balance"]
                r[bucket] += a["open_balance"]

            for account in set(labels):
                accounts[account]["bill_count"] += 1

        primary_category = (
            max(cat_amounts, key=cat_amounts.get)
            if cat_amounts else (vendor_override(bill["vendor"], vm) or "Unclassified")
        )

        if bal > 0.005:
            # Category roll-up uses the actual line allocations, not only the
            # primary category, so multi-account Bills stay mathematically exact.
            for cat, amount in cat_amounts.items():
                categories[cat]["total"] += amount
                categories[cat][bucket] += amount
            # Count a Bill once in every category it actually touches.
            for cat in cat_amounts:
                categories[cat]["bill_count"] += 1
            if not cat_amounts:
                categories[primary_category]["total"] += bal
                categories[primary_category][bucket] += bal
                categories[primary_category]["bill_count"] += 1
            if primary_category == "Unclassified":
                unclassified_count += 1
                unclassified_balance += bal

        days_overdue = 0
        if bill["due_date"]:
            due = datetime.strptime(bill["due_date"], "%Y-%m-%d").date()
            days_overdue = max(0, (today - due).days)

        status = (
            "Paid" if bal <= 0.005
            else "Overdue" if days_overdue > 0
            else "Due this month" if bucket == "due_this_month"
            else "Not due"
        )

        invoices.append({
            "id": bill["id"],
            "vendor": bill["vendor"],
            "invoice_number": bill["invoice_number"],
            "invoice_date": bill["invoice_date"],
            "due_date": bill["due_date"],
            "currency": bill["currency"],
            "exchange_rate": round(float(bill["exchange_rate"] or 1), 6),
            "original_amount": round(original, 2),
            "amount_paid": round(paid, 2),
            "remaining_balance": round(bal, 2),
            "days_overdue": days_overdue,
            "status": status,
            "primary_account": labels[0] if labels else "No open balance",
            "account_labels": labels,
            "category": primary_category,
            "mapping_source": allocs[0]["mapping_source"] if allocs else "paid",
        })

    total_ap = round(total_ap, 2)
    allocation_total = round(allocation_total, 2)
    allocation_variance = round(total_ap - allocation_total, 2)

    # Hard reconciliation: all account allocations must equal Bill open balances.
    if abs(allocation_variance) > 0.05:
        raise ValueError(
            f"Account allocation does not reconcile to Bill balances: "
            f"AP ${total_ap:,.2f} vs allocated ${allocation_total:,.2f} "
            f"(variance ${allocation_variance:,.2f})."
        )

    for d in list(accounts.values()) + list(categories.values()):
        for key in ("total", "not_yet_due", "due_this_month", "overdue_lt_3m", "overdue_gt_3m"):
            d[key] = round(d[key], 2)

    account_summary = sorted(
        [r for r in accounts.values() if r["total"] > 0.005],
        key=lambda x: x["total"], reverse=True,
    )
    category_summary = sorted(categories.values(), key=lambda x: x["total"], reverse=True)
    top_vendors = [
        {"vendor": vendor, "balance": round(balance, 2)}
        for vendor, balance in sorted(vendors.items(), key=lambda x: x[1], reverse=True)[:12]
    ]

    open_bill_count = sum(1 for x in invoices if x["remaining_balance"] > 0.005)
    paid_bill_count = len(invoices) - open_bill_count

    data_quality = {
        "open_bill_count": open_bill_count,
        "historical_bill_count": len(invoices),
        "paid_bill_count": paid_bill_count,
        "missing_account_count": missing_account,
        "unclassified_count": unclassified_count,
        "unclassified_balance": round(unclassified_balance, 2),
        "missing_due_date_count": missing_due,
        "available_vendor_credits": 0.0,
        "general_ledger_usable_rows": gl.get("usable_rows", 0),
    }

    return {
        "currency": HOME_CURRENCY,
        "source": "coefficient_google_sheets_quickbooks_bill_lines",
        "source_note": (
            "Source: QuickBooks Online (Equestrian Labs) → Coefficient QuickBooks Bill object. "
            "Balances are converted to USD using each Bill's Exchange Rate; QuickBooks Bill line "
            "accounts drive the account/category allocation."
        ),
        "company": "Equestrian Labs, Inc. (dba Corro)",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "kpis": {
            "total_ap": total_ap,
            "gross_open_bills": total_ap,
            "credits_adjustments": 0.0,
            "aging_total": total_ap,
            "not_yet_due": round(aging["not_yet_due"], 2),
            "due_this_month": round(aging["due_this_month"], 2),
            "overdue_lt_3m": round(aging["overdue_lt_3m"], 2),
            "overdue_gt_3m": round(aging["overdue_gt_3m"], 2),
        },
        "reconciliation": {
            "calculated_net_ap": total_ap,
            "vendor_balance_available": True,
            "vendor_balance_total": allocation_total,
            "variance": allocation_variance,
            "reconciled": abs(allocation_variance) <= 0.05,
            "control_label": "Allocated QuickBooks line balance",
            "note": (
                "Control compares USD open Bill balances with the sum allocated across each Bill's "
                "QuickBooks line accounts. Foreign-currency Bills are converted using Exchange Rate."
            ),
        },
        "accounts_summary": account_summary,
        "categories": category_summary,
        "top_vendors": top_vendors,
        "monthly_trend": update_trend(load_previous_trend(), total_ap),
        "invoices": sorted(
            invoices,
            key=lambda x: (
                x["remaining_balance"] <= 0.005,
                -x["days_overdue"],
                -x["remaining_balance"],
            ),
        ),
        "data_quality": data_quality,
        # Backward compatibility with prior workflow validation.
        "quality": {
            "open_bills": open_bill_count,
            "missing_due_date": missing_due,
            "unmapped_bills": missing_account,
            "unclassified_balance": round(unclassified_balance, 2),
            "general_ledger_usable_rows": gl.get("usable_rows", 0),
        },
    }


def main():
    sid = os.getenv("GSHEET_ID", DEFAULT_GSHEET_ID).strip() or DEFAULT_GSHEET_ID
    bills_sheet = os.getenv("GSHEET_BILLS_SHEET", DEFAULT_BILLS_SHEET).strip() or DEFAULT_BILLS_SHEET
    bills_gid = os.getenv("GSHEET_BILLS_GID", DEFAULT_BILLS_GID).strip() or DEFAULT_BILLS_GID
    gl_sheet = os.getenv("GSHEET_GENERAL_LEDGER_SHEET", DEFAULT_GL_SHEET).strip() or DEFAULT_GL_SHEET
    gl_gid = os.getenv("GSHEET_GENERAL_LEDGER_GID", DEFAULT_GL_GID).strip() or DEFAULT_GL_GID

    client = GoogleSheetsClient(sid)
    print(f"Reading QuickBooks Bills: {bills_sheet} (gid={bills_gid})")
    try:
        bills_csv = client.fetch_csv(sheet_name=bills_sheet, gid=bills_gid)
    except GoogleSheetsError as e:
        raise SystemExit(f"BILLS SOURCE ERROR: {e}")

    print(f"Reading General Ledger fallback: {gl_sheet} (gid={gl_gid})")
    try:
        gl_csv = client.fetch_csv(sheet_name=gl_sheet, gid=gl_gid)
    except Exception as e:
        print(f"::warning title=General Ledger fallback unavailable::{e}")
        gl_csv = ""

    data = build_dashboard(bills_csv, gl_csv)
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print(f"Wrote {OUTPUT_PATH}")
    print(f"Total AP (USD): ${data['kpis']['total_ap']:,.2f}")
    print(f"Open bills: {data['data_quality']['open_bill_count']}")
    print(f"Account allocation variance: ${data['reconciliation']['variance']:,.2f}")
    print(f"Accounts with open AP: {len(data['accounts_summary'])}")
    print(f"Unclassified open balance: ${data['data_quality']['unclassified_balance']:,.2f}")


if __name__ == "__main__":
    main()
