"""Build data/ap-data.json from real QuickBooks data imported by Coefficient.

Required source:
  1) Vendor Balance Detail report (authoritative AP/open balance + due dates)
Optional/enrichment source:
  2) General Ledger report (maps each bill/vendor to QuickBooks distribution accounts)

The spreadsheet can be public-read (Anyone with link -> Viewer). Coefficient is
responsible for refreshing QuickBooks -> Google Sheets; this script is read-only.
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

CATEGORIES = [
    "Inventory",
    "Shipping & Fulfillment",
    "Advertising",
    "Sales & Marketing",
    "G&A / OPEX",
    "Unclassified",
]

ACCOUNT_CATEGORY_RULES = [
    # Shipping must be evaluated before generic COGS because QuickBooks accounts such as
    # "Shipping, Freight & Delivery - COGS:Inbound Shipping" are shipping, not inventory.
    ("Shipping & Fulfillment", [
        "shipping", "freight", "fulfillment", "postage", "delivery", "warehouse",
        "warehousing", "packaging", "inbound shipping", "outbound shipping",
    ]),
    ("Inventory", [
        "inventory asset", "inventory", "cost of goods", "cogs", "merchandise",
        "product cost", "purchases for resale", "purchases - resale",
    ]),
    ("Advertising", [
        "advertising", "paid media", "google ads", "meta ads", "facebook ads",
        "ad spend", "ppc", "media buying",
    ]),
    ("Sales & Marketing", [
        "selling & marketing", "marketing", "creative", "content", "seo", "sponsorship",
        "event", "brand", "sales commission", "influencer", "affiliate",
    ]),
    ("G&A / OPEX", [
        "maintenance", "repair", "payroll", "wages", "salary", "staff", "contract labor",
        "contractor", "consulting", "professional fee", "software", "subscription", "rent",
        "office", "insurance", "legal", "accounting", "bank fee", "merchant fee", "utilities",
        "gas and electric", "general & administrative", "g&a", "opex", "tax", "audit",
        "intangible asset", "trademark",
    ]),
]

EXCLUDED_GL_ACCOUNTS = [
    "accounts payable", "a/p", "accounts receivable", "a/r", "bank account",
    "checking", "savings", "undeposited funds", "credit card payable",
]

HEADER_ALIASES = {
    "date": ["date", "transaction date"],
    "transaction_type": ["transaction type", "transactiontype", "type"],
    "num": ["num", "number", "invoice no", "invoice #", "invoice number"],
    "name": ["name", "vendor", "supplier"],
    "due_date": ["due date", "duedate"],
    "amount": ["amount", "original amount"],
    "open_balance": ["open balance", "openbalance", "balance due"],
    "debit": ["debit"],
    "credit": ["credit"],
    "balance": ["balance"],
    "account": ["account", "account/sector", "distribution account"],
}


def norm(value):
    return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())


def text(value):
    return str(value or "").strip()


def money(value):
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace("$", "").replace(",", "")
    if not s or s in {"-", "—"}:
        return 0.0
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    try:
        return float(s)
    except ValueError:
        return 0.0


def parse_date(value):
    s = text(value)
    if not s:
        return ""
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%m-%d-%Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    return ""


def csv_rows(csv_text):
    return [row for row in csv.reader(io.StringIO(csv_text))]


def find_header(rows, required_groups):
    """Return (index, normalized header map) for the first matching row."""
    for i, row in enumerate(rows[:80]):
        norms = [norm(c) for c in row]
        if all(any(norm(alias) in norms for alias in aliases) for aliases in required_groups):
            mapping = {}
            for key, aliases in HEADER_ALIASES.items():
                for alias in aliases:
                    n = norm(alias)
                    if n in norms:
                        mapping[key] = norms.index(n)
                        break
            return i, mapping
    return None, {}


def get_cell(row, mapping, key):
    idx = mapping.get(key)
    return row[idx] if idx is not None and idx < len(row) else ""


def load_vendor_map():
    try:
        with open(VENDOR_MAP_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"rules": [], "default_category": "Unclassified"}


def classify_vendor(vendor, vendor_map):
    v = text(vendor).lower()
    for rule in vendor_map.get("rules", []):
        keyword = text(rule.get("keyword")).lower()
        if keyword and keyword in v:
            cat = rule.get("category", "Unclassified")
            return cat if cat in CATEGORIES else "Unclassified"
    return vendor_map.get("default_category", "Unclassified")


def classify_account(account, vendor, vendor_map):
    a = text(account).lower()
    for category, keywords in ACCOUNT_CATEGORY_RULES:
        if any(k in a for k in keywords):
            return category
    return classify_vendor(vendor, vendor_map)


def aging_bucket(due_date_iso, today=None):
    today = today or date.today()
    if not due_date_iso:
        return "not_yet_due"
    due = datetime.strptime(due_date_iso, "%Y-%m-%d").date()
    days = (due - today).days
    if days >= 0 and due.year == today.year and due.month == today.month:
        return "due_this_month"
    if days >= 0:
        return "not_yet_due"
    return "overdue_lt_3m" if -days <= 90 else "overdue_gt_3m"


def parse_vendor_balance(csv_text):
    rows = csv_rows(csv_text)
    header_i, h = find_header(
        rows,
        [HEADER_ALIASES["date"], HEADER_ALIASES["transaction_type"], HEADER_ALIASES["open_balance"]],
    )
    if header_i is None:
        raise ValueError(
            "Vendor Balance Detail header not found. Expected Date, Transaction type and Open balance columns."
        )

    company = ""
    for row in rows[:header_i]:
        if row and text(row[0]) and "vendor balance" not in text(row[0]).lower() and text(row[0]).lower() != "all dates":
            company = text(row[0])
            break

    current_vendor = ""
    transactions = []
    report_total = None

    for row in rows[header_i + 1:]:
        first = text(row[0]) if row else ""
        dt_raw = get_cell(row, h, "date")
        tx_type = text(get_cell(row, h, "transaction_type"))

        if first.upper() == "TOTAL":
            report_total = money(get_cell(row, h, "open_balance"))
            continue

        # QuickBooks report vendor group row, e.g. "Animal Health International"
        if first and not dt_raw and not tx_type and not first.lower().startswith("total for "):
            current_vendor = first
            continue

        if not dt_raw or not tx_type:
            continue

        vendor = text(get_cell(row, h, "name")) or current_vendor
        open_balance = money(get_cell(row, h, "open_balance"))
        if abs(open_balance) < 0.005:
            continue

        transactions.append({
            "vendor": vendor or "Unknown vendor",
            "transaction_type": tx_type,
            "invoice_number": text(get_cell(row, h, "num")),
            "invoice_date": parse_date(dt_raw),
            "due_date": parse_date(get_cell(row, h, "due_date")),
            "original_amount": money(get_cell(row, h, "amount")),
            "open_balance": round(open_balance, 2),
        })

    if report_total is None:
        report_total = round(sum(t["open_balance"] for t in transactions), 2)

    return company, transactions, round(report_total, 2)


def gl_account_is_distribution(account):
    a = text(account).lower()
    if not a:
        return False
    return not any(token in a for token in EXCLUDED_GL_ACCOUNTS)


def add_weight(bucket, key, account, weight):
    if not key or not account or weight <= 0:
        return
    bucket[key][account] += weight


def parse_general_ledger(csv_text):
    rows = csv_rows(csv_text)
    header_i, h = find_header(
        rows,
        [HEADER_ALIASES["date"], HEADER_ALIASES["transaction_type"], HEADER_ALIASES["name"], HEADER_ALIASES["account"]],
    )
    if header_i is None:
        raise ValueError(
            "General Ledger header not found. Expected Date, Transaction Type, Name and Account columns."
        )

    exact = defaultdict(lambda: defaultdict(float))
    invoice = defaultdict(lambda: defaultdict(float))
    vendor = defaultdict(lambda: defaultdict(float))
    usable_rows = 0

    for row in rows[header_i + 1:]:
        tx_type = text(get_cell(row, h, "transaction_type"))
        vendor_name = text(get_cell(row, h, "name"))
        account = text(get_cell(row, h, "account"))
        num = text(get_cell(row, h, "num"))
        if not tx_type or not vendor_name or not gl_account_is_distribution(account):
            continue
        # The AP dashboard only needs supplier-side postings.
        if tx_type.lower() not in {"bill", "vendor credit", "journal entry", "bill payment (check)", "bill payment"}:
            continue

        debit = abs(money(get_cell(row, h, "debit")))
        credit = abs(money(get_cell(row, h, "credit")))
        amt = abs(money(get_cell(row, h, "amount")))
        weight = max(debit, credit, amt)
        if weight <= 0:
            continue

        vk = norm(vendor_name)
        nk = norm(num)
        tk = norm(tx_type)
        add_weight(exact, (vk, nk, tk), account, weight)
        if nk:
            add_weight(invoice, (vk, nk), account, weight)
        add_weight(vendor, vk, account, weight)
        usable_rows += 1

    return {"exact": exact, "invoice": invoice, "vendor": vendor, "usable_rows": usable_rows}


def choose_account_weights(tx, gl):
    vk = norm(tx["vendor"])
    nk = norm(tx["invoice_number"])
    tk = norm(tx["transaction_type"])
    candidates = [
        (gl.get("exact", {}).get((vk, nk, tk)), "exact invoice + type"),
        (gl.get("invoice", {}).get((vk, nk)), "exact invoice"),
        (gl.get("vendor", {}).get(vk), "vendor history"),
    ]
    for weights, source in candidates:
        if weights:
            return dict(weights), source
    return {}, "unmapped"


def allocate_balance(tx, gl, vendor_map):
    weights, source = choose_account_weights(tx, gl)
    balance = float(tx["open_balance"] or 0)
    if not weights:
        category = classify_vendor(tx["vendor"], vendor_map)
        return [{
            "account": "Unmapped - review in QuickBooks",
            "category": category,
            "open_balance": round(balance, 2),
            "mapping_source": source,
        }]

    total_weight = sum(weights.values()) or 1.0
    items = sorted(weights.items(), key=lambda x: x[1], reverse=True)
    allocations = []
    running = 0.0
    for i, (account, weight) in enumerate(items):
        if i == len(items) - 1:
            amount = round(balance - running, 2)
        else:
            amount = round(balance * weight / total_weight, 2)
            running += amount
        allocations.append({
            "account": account,
            "category": classify_account(account, tx["vendor"], vendor_map),
            "open_balance": amount,
            "mapping_source": source,
        })
    return allocations


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
        with open(OUTPUT_PATH, "r", encoding="utf-8") as f:
            old = json.load(f)
        if old.get("source") != "coefficient_google_sheets_quickbooks":
            return []
        return old.get("monthly_trend", []) or []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def update_trend(previous, total_ap):
    month = date.today().strftime("%Y-%m")
    by_month = {str(r.get("month")): r for r in previous if r.get("month")}
    by_month[month] = {"month": month, "total_ap": round(total_ap, 2)}
    return [by_month[k] for k in sorted(by_month)[-6:]]


def build_dashboard(vendor_csv, gl_csv=""):
    vendor_map = load_vendor_map()
    company, txs, report_total = parse_vendor_balance(vendor_csv)

    if gl_csv.strip():
        try:
            gl = parse_general_ledger(gl_csv)
            gl_note = f"General Ledger loaded ({gl['usable_rows']} usable distribution rows)."
        except ValueError as exc:
            print(f"::warning title=General Ledger parse warning::{exc}")
            gl = {"exact": {}, "invoice": {}, "vendor": {}, "usable_rows": 0}
            gl_note = "General Ledger unavailable; vendor mapping fallback used."
    else:
        gl = {"exact": {}, "invoice": {}, "vendor": {}, "usable_rows": 0}
        gl_note = "General Ledger unavailable; vendor mapping fallback used."

    today = date.today()
    account_rollups = {}
    category_rollups = {c: blank_rollup("name", c) for c in CATEGORIES}
    vendor_totals = defaultdict(float)
    invoices = []

    gross_open_bills = 0.0
    non_bill_adjustments = 0.0
    vendor_credit_balance = 0.0
    aging = defaultdict(float)
    missing_due = 0
    unmapped_count = 0
    unclassified_balance = 0.0

    for tx in txs:
        is_bill = tx["transaction_type"].strip().lower() == "bill"
        bal = float(tx["open_balance"] or 0)
        if is_bill and bal > 0:
            gross_open_bills += bal
            bucket = aging_bucket(tx["due_date"], today)
            aging[bucket] += bal
            if not tx["due_date"]:
                missing_due += 1
        else:
            bucket = None
            non_bill_adjustments += bal
            if "vendor credit" in tx["transaction_type"].lower() and bal < 0:
                vendor_credit_balance += bal

        allocations = allocate_balance(tx, gl, vendor_map)
        if allocations and allocations[0]["mapping_source"] == "unmapped":
            unmapped_count += 1

        for alloc in allocations:
            account = alloc["account"]
            category = alloc["category"] if alloc["category"] in CATEGORIES else "Unclassified"
            amount = float(alloc["open_balance"] or 0)
            if account not in account_rollups:
                account_rollups[account] = blank_rollup("account", account)
                account_rollups[account]["category"] = category
            ar = account_rollups[account]
            ar["total"] += amount
            cr = category_rollups[category]
            cr["total"] += amount
            if is_bill:
                ar["bill_count"] += 1
                cr["bill_count"] += 1
                if bucket:
                    ar[bucket] += amount
                    cr[bucket] += amount
            if category == "Unclassified":
                unclassified_balance += amount

        vendor_totals[tx["vendor"]] += bal

        due = tx["due_date"]
        days_overdue = 0
        if due and is_bill and bal > 0:
            days_overdue = max(0, (today - datetime.strptime(due, "%Y-%m-%d").date()).days)
        if not is_bill:
            status = "Credit" if bal < 0 else "Adjustment"
        elif bal <= 0:
            status = "Paid"
        else:
            b = aging_bucket(due, today)
            status = {
                "not_yet_due": "Not due",
                "due_this_month": "Due this month",
                "overdue_lt_3m": "Overdue",
                "overdue_gt_3m": "Overdue",
            }[b]

        primary = max(allocations, key=lambda x: abs(x["open_balance"])) if allocations else {
            "account": "Unmapped - review in QuickBooks", "category": "Unclassified", "mapping_source": "unmapped"
        }
        original = float(tx["original_amount"] or 0)
        paid = max(0.0, original - bal) if is_bill and bal >= 0 else 0.0
        invoices.append({
            "vendor": tx["vendor"],
            "transaction_type": tx["transaction_type"],
            "invoice_number": tx["invoice_number"],
            "invoice_date": tx["invoice_date"],
            "due_date": tx["due_date"],
            "primary_account": primary["account"],
            "account_labels": [a["account"] for a in allocations],
            "account_mapping_source": primary["mapping_source"],
            "category": primary["category"],
            "original_amount": round(original, 2),
            "amount_paid": round(paid, 2),
            "remaining_balance": round(bal, 2),
            "days_overdue": days_overdue,
            "status": status,
            "duplicate_candidate": False,
        })

    calculated_net = round(sum(t["open_balance"] for t in txs), 2)
    variance = round(calculated_net - report_total, 2)

    accounts_summary = []
    for r in account_rollups.values():
        for k in ["total", "not_yet_due", "due_this_month", "overdue_lt_3m", "overdue_gt_3m"]:
            r[k] = round(r[k], 2)
        accounts_summary.append(r)
    accounts_summary.sort(key=lambda r: abs(r["total"]), reverse=True)

    categories = []
    for name in CATEGORIES:
        r = category_rollups[name]
        for k in ["total", "not_yet_due", "due_this_month", "overdue_lt_3m", "overdue_gt_3m"]:
            r[k] = round(r[k], 2)
        categories.append(r)

    top_vendors = [
        {"vendor": v, "balance": round(b, 2)}
        for v, b in sorted(vendor_totals.items(), key=lambda x: x[1], reverse=True)
        if b > 0.005
    ][:10]

    net_total = calculated_net
    kpis = {
        "total_ap": round(net_total, 2),
        "gross_open_bills": round(gross_open_bills, 2),
        "credits_adjustments": round(non_bill_adjustments, 2),
        "aging_total": round(gross_open_bills, 2),
        "not_yet_due": round(aging["not_yet_due"], 2),
        "due_this_month": round(aging["due_this_month"], 2),
        "overdue_lt_3m": round(aging["overdue_lt_3m"], 2),
        "overdue_gt_3m": round(aging["overdue_gt_3m"], 2),
    }

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "currency": "USD",
        "source": "coefficient_google_sheets_quickbooks",
        "source_note": "Source: QuickBooks Online → Coefficient → Google Sheets",
        "company": company or "Equestrian Labs, Inc. (dba Corro)",
        "environment": "production-data-via-coefficient",
        "kpis": kpis,
        "reconciliation": {
            "calculated_net_ap": calculated_net,
            "vendor_balance_total": report_total,
            "vendor_balance_available": True,
            "variance": variance,
            "reconciled": abs(variance) <= 0.02,
            "gross_open_bills": round(gross_open_bills, 2),
            "credits_adjustments": round(non_bill_adjustments, 2),
            "note": f"{gl_note} Vendor Balance TOTAL is the control total.",
        },
        "accounts_summary": accounts_summary,
        "categories": categories,
        "top_vendors": top_vendors,
        "monthly_trend": update_trend(load_previous_trend(), net_total),
        "invoices": sorted(invoices, key=lambda x: (x["remaining_balance"] > 0, x["days_overdue"], abs(x["remaining_balance"])), reverse=True),
        "data_quality": {
            "open_bill_count": sum(1 for t in txs if t["transaction_type"].lower() == "bill" and t["open_balance"] > 0),
            "missing_account_count": unmapped_count,
            "unclassified_count": sum(1 for x in invoices if x["category"] == "Unclassified"),
            "unclassified_balance": round(unclassified_balance, 2),
            "missing_due_date_count": missing_due,
            "available_vendor_credits": round(abs(vendor_credit_balance), 2),
            "other_adjustments": round(non_bill_adjustments - vendor_credit_balance, 2),
            "gl_usable_rows": int(gl.get("usable_rows", 0)),
        },
    }
    return result


def main():
    sheet_id = os.getenv("GSHEET_ID", "").strip()
    vendor_sheet = os.getenv("GSHEET_VENDOR_BALANCE_SHEET", "AP_VENDOR_BALANCE").strip()
    vendor_gid = os.getenv("GSHEET_VENDOR_BALANCE_GID", "").strip()
    gl_sheet = os.getenv("GSHEET_GENERAL_LEDGER_SHEET", "General Ledger").strip()
    gl_gid = os.getenv("GSHEET_GENERAL_LEDGER_GID", "").strip()

    client = GoogleSheetsClient(sheet_id)
    print(f"Reading Vendor Balance from Google Sheet: {vendor_sheet or vendor_gid}")
    vendor_csv = client.fetch_csv(sheet_name=vendor_sheet, gid=vendor_gid)

    gl_csv = ""
    try:
        print(f"Reading General Ledger from Google Sheet: {gl_sheet or gl_gid}")
        gl_csv = client.fetch_csv(sheet_name=gl_sheet, gid=gl_gid)
    except GoogleSheetsError as exc:
        print(f"::warning title=General Ledger download warning::{exc}")

    data = build_dashboard(vendor_csv, gl_csv)
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print(f"Company: {data['company']}")
    print(f"Net AP: ${data['kpis']['total_ap']:,.2f}")
    print(f"Gross open bills: ${data['kpis']['gross_open_bills']:,.2f}")
    print(f"Credits/adjustments: ${data['kpis']['credits_adjustments']:,.2f}")
    print(f"Vendor Balance control: ${data['reconciliation']['vendor_balance_total']:,.2f}")
    print(f"Variance: ${data['reconciliation']['variance']:,.2f}")
    print(f"Account mappings: {len(data['accounts_summary'])}")
    print(f"Wrote {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
