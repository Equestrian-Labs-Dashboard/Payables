"""Build data/ap-data.json from QuickBooks Online Accounts Payable data.

QuickBooks is the source of truth for this dashboard. If BILL is already synced
with QuickBooks, no BILL API credentials are required by this repository.
"""

import json
import os
from collections import Counter
from datetime import date, datetime, timezone

from quickbooks_client import QuickBooksClient

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "..", "data")
VENDOR_MAP_PATH = os.path.join(DATA_DIR, "vendor-map.json")
OUTPUT_PATH = os.path.join(DATA_DIR, "ap-data.json")

CATEGORIES = [
    "Inventory",
    "Shipping & Fulfillment",
    "Advertising",
    "Sales & Marketing",
    "G&A / OPEX",
    "Unclassified",
]

# Accounting-name rules have priority over vendor-name fallback rules.
ACCOUNT_CATEGORY_RULES = [
    ("Inventory", ["inventory", "cost of goods", "cogs", "merchandise", "product cost"]),
    ("Shipping & Fulfillment", ["shipping", "freight", "fulfillment", "postage", "delivery", "warehouse"]),
    ("Advertising", ["advertising", "paid media", "google ads", "meta ads", "facebook ads", "ad spend"]),
    ("Sales & Marketing", ["marketing", "sponsorship", "event", "brand", "sales commission", "influencer"]),
    ("G&A / OPEX", [
        "payroll", "wages", "salary", "staff", "contractor", "consulting", "professional fee",
        "software", "subscription", "rent", "office", "insurance", "legal", "accounting",
        "bank fee", "merchant fee", "utilities", "general & administrative", "g&a", "opex",
    ]),
]


def load_vendor_map():
    try:
        with open(VENDOR_MAP_PATH, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"rules": [], "default_category": "Unclassified"}


def classify_vendor(vendor_name, vendor_map):
    name_lower = (vendor_name or "").lower()
    for rule in vendor_map.get("rules", []):
        if rule.get("keyword", "").lower() in name_lower:
            return rule.get("category", "Unclassified")
    return vendor_map.get("default_category", "Unclassified")


def classify_account_name(name):
    value = (name or "").lower()
    for category, keywords in ACCOUNT_CATEGORY_RULES:
        if any(keyword in value for keyword in keywords):
            return category
    return None


def aging_bucket(due_date_str, today=None):
    today = today or date.today()
    if not due_date_str:
        return "not_yet_due"
    due = datetime.strptime(due_date_str, "%Y-%m-%d").date()
    days = (due - today).days
    if days > 0 and due.month == today.month and due.year == today.year:
        return "due_this_month"
    if days > 0:
        return "not_yet_due"
    overdue_days = -days
    return "overdue_lt_3m" if overdue_days <= 90 else "overdue_gt_3m"


def line_account_label(line, accounts, items):
    """Resolve the best accounting label available on a QBO Bill line."""
    account_detail = line.get("AccountBasedExpenseLineDetail") or {}
    account_ref = account_detail.get("AccountRef") or {}
    if account_ref:
        account_id = str(account_ref.get("value", ""))
        account = accounts.get(account_id, {})
        return (
            account_ref.get("name")
            or account.get("FullyQualifiedName")
            or account.get("Name")
            or ""
        )

    item_detail = line.get("ItemBasedExpenseLineDetail") or {}
    item_ref = item_detail.get("ItemRef") or {}
    if item_ref:
        item_id = str(item_ref.get("value", ""))
        item = items.get(item_id, {})
        # Prefer the item's expense account when available; it is more meaningful
        # for AP classification than the item display name alone.
        expense_ref = item.get("ExpenseAccountRef") or {}
        expense_id = str(expense_ref.get("value", ""))
        expense_account = accounts.get(expense_id, {})
        return (
            expense_ref.get("name")
            or expense_account.get("FullyQualifiedName")
            or expense_account.get("Name")
            or item_ref.get("name")
            or item.get("FullyQualifiedName")
            or item.get("Name")
            or ""
        )
    return ""


def bill_category(bill, accounts, items, vendor_name, vendor_map):
    weighted = Counter()
    labels = []
    for line in bill.get("Line", []) or []:
        label = line_account_label(line, accounts, items)
        if not label:
            continue
        labels.append(label)
        category = classify_account_name(label)
        if category:
            weighted[category] += float(line.get("Amount", 0) or 0)

    if weighted:
        return weighted.most_common(1)[0][0], labels
    return classify_vendor(vendor_name, vendor_map), labels


def build_dataset(raw_bills, accounts, items, vendor_map, company_info=None):
    kpis = {key: 0.0 for key in ["total_ap", "not_yet_due", "due_this_month", "overdue_lt_3m", "overdue_gt_3m"]}
    categories = {
        cat: {"name": cat, "total": 0.0, "not_yet_due": 0.0, "due_this_month": 0.0, "overdue_lt_3m": 0.0, "overdue_gt_3m": 0.0}
        for cat in CATEGORIES
    }
    invoices = []

    for bill in raw_bills:
        vendor_ref = bill.get("VendorRef") or {}
        vendor_name = vendor_ref.get("name") or "Unknown vendor"
        remaining = float(bill.get("Balance", 0) or 0)
        if remaining <= 0:
            continue

        due_date = bill.get("DueDate") or bill.get("TxnDate")
        category, account_labels = bill_category(bill, accounts, items, vendor_name, vendor_map)
        if category not in categories:
            category = "Unclassified"
        bucket = aging_bucket(due_date)
        total_amount = float(bill.get("TotalAmt", 0) or 0)

        kpis["total_ap"] += remaining
        kpis[bucket] += remaining
        categories[category]["total"] += remaining
        categories[category][bucket] += remaining

        days_overdue = None
        if due_date:
            due_obj = datetime.strptime(due_date, "%Y-%m-%d").date()
            days_overdue = max(0, (date.today() - due_obj).days)

        invoices.append({
            "id": str(bill.get("Id", "")),
            "vendor": vendor_name,
            "invoice_number": bill.get("DocNumber", ""),
            "invoice_date": bill.get("TxnDate", ""),
            "due_date": due_date or "",
            "category": category,
            "account_labels": sorted(set(account_labels)),
            "original_amount": round(total_amount, 2),
            "amount_paid": round(max(0.0, total_amount - remaining), 2),
            "remaining_balance": round(remaining, 2),
            "days_overdue": days_overdue,
            "status": "Overdue" if bucket.startswith("overdue") else ("Due this month" if bucket == "due_this_month" else "Not due"),
        })

    invoices.sort(key=lambda x: (x.get("due_date") or "9999-12-31", -x["remaining_balance"]))
    currency = ((company_info or {}).get("Country") and "USD") or "USD"

    return {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "currency": currency,
        "source": "quickbooks_online",
        "company": (company_info or {}).get("CompanyName", ""),
        "kpis": {k: round(v, 2) for k, v in kpis.items()},
        "categories": [
            {k: (round(v, 2) if isinstance(v, float) else v) for k, v in cat.items()}
            for cat in categories.values()
        ],
        "monthly_trend": [],
        "invoices": invoices,
    }


def main():
    vendor_map = load_vendor_map()
    client = QuickBooksClient()
    company_info = client.get_company_info()
    raw_bills = client.get_open_bills()
    accounts = client.get_accounts()
    items = client.get_items()

    dataset = build_dataset(raw_bills, accounts, items, vendor_map, company_info)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as handle:
        json.dump(dataset, handle, indent=2, ensure_ascii=False)

    print(f"QuickBooks environment: {client.environment}")
    print(f"Company: {dataset.get('company') or client.realm_id}")
    print(f"ap-data.json updated with {len(dataset['invoices'])} open bills.")
    if client.latest_refresh_token and client.latest_refresh_token != client.refresh_token:
        print("NOTICE: Intuit returned a newer refresh token. Store the latest token securely for long-running automation.")


if __name__ == "__main__":
    main()
