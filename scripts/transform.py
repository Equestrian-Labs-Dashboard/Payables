"""Build data/ap-data.json from QuickBooks Online for the AP Executive Dashboard."""

import json
import os
from collections import Counter, defaultdict
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

ACCOUNT_CATEGORY_RULES = [
    ("Inventory", ["inventory", "cost of goods", "cogs", "merchandise", "product cost", "purchases - resale"]),
    ("Shipping & Fulfillment", ["shipping", "freight", "fulfillment", "postage", "delivery", "warehouse", "packaging"]),
    ("Advertising", ["advertising", "paid media", "google ads", "meta ads", "facebook ads", "ad spend", "ppc"]),
    ("Sales & Marketing", ["marketing", "sponsorship", "event", "brand", "sales commission", "influencer", "affiliate"]),
    ("G&A / OPEX", [
        "payroll", "wages", "salary", "staff", "contractor", "consulting", "professional fee",
        "software", "subscription", "rent", "office", "insurance", "legal", "accounting",
        "bank fee", "merchant fee", "utilities", "general & administrative", "g&a", "opex",
    ]),
]

SUBCATEGORY_RULES = [
    ("Payroll / Staff", ["payroll", "wages", "salary", "staff"]),
    ("Consulting", ["consulting", "consultant", "contractor"]),
    ("Software / Apps", ["software", "subscription", "saas", "app"]),
    ("Professional Services", ["legal", "accounting", "professional fee", "tax", "audit"]),
    ("Rent / Office", ["rent", "office", "utilities"]),
    ("Insurance / Fees", ["insurance", "bank fee", "merchant fee"]),
]


def load_previous_qbo_trend():
    try:
        with open(OUTPUT_PATH, "r", encoding="utf-8") as handle:
            previous = json.load(handle)
        if previous.get("source") != "quickbooks_online":
            return []
        return [
            {"month": str(r.get("month", "")), "total_ap": round(float(r.get("total_ap", 0) or 0), 2)}
            for r in (previous.get("monthly_trend") or []) if r.get("month")
        ]
    except (FileNotFoundError, json.JSONDecodeError, TypeError, ValueError):
        return []


def update_monthly_trend(previous_trend, total_ap):
    current_month = date.today().strftime("%Y-%m")
    by_month = {row["month"]: row for row in previous_trend if row.get("month")}
    by_month[current_month] = {"month": current_month, "total_ap": round(float(total_ap or 0), 2)}
    return [by_month[k] for k in sorted(by_month.keys())[-6:]]


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


def classify_text(value, rules):
    value = (value or "").lower()
    for category, keywords in rules:
        if any(keyword in value for keyword in keywords):
            return category
    return None


def aging_bucket(due_date_str, today=None):
    today = today or date.today()
    if not due_date_str:
        # Keep the bill inside Total AP and Not Yet Due, but data_quality tracks it separately.
        return "not_yet_due"
    due = datetime.strptime(due_date_str, "%Y-%m-%d").date()
    days = (due - today).days
    if days >= 0 and due.month == today.month and due.year == today.year:
        return "due_this_month"
    if days >= 0:
        return "not_yet_due"
    return "overdue_lt_3m" if -days <= 90 else "overdue_gt_3m"


def line_account_label(line, accounts, items):
    account_detail = line.get("AccountBasedExpenseLineDetail") or {}
    account_ref = account_detail.get("AccountRef") or {}
    if account_ref:
        account = accounts.get(str(account_ref.get("value", "")), {})
        return account_ref.get("name") or account.get("FullyQualifiedName") or account.get("Name") or ""

    item_detail = line.get("ItemBasedExpenseLineDetail") or {}
    item_ref = item_detail.get("ItemRef") or {}
    if item_ref:
        item = items.get(str(item_ref.get("value", "")), {})
        expense_ref = item.get("ExpenseAccountRef") or {}
        expense_account = accounts.get(str(expense_ref.get("value", "")), {})
        return (
            expense_ref.get("name") or expense_account.get("FullyQualifiedName") or expense_account.get("Name")
            or item_ref.get("name") or item.get("FullyQualifiedName") or item.get("Name") or ""
        )
    return ""


def bill_classification(bill, accounts, items, vendor_name, vendor_map):
    weighted = Counter()
    labels = []
    for line in bill.get("Line", []) or []:
        label = line_account_label(line, accounts, items)
        if not label:
            continue
        labels.append(label)
        category = classify_text(label, ACCOUNT_CATEGORY_RULES)
        if category:
            weighted[category] += float(line.get("Amount", 0) or 0)

    category = weighted.most_common(1)[0][0] if weighted else classify_vendor(vendor_name, vendor_map)
    combined = " ".join(labels + [vendor_name])
    subcategory = classify_text(combined, SUBCATEGORY_RULES) if category == "G&A / OPEX" else None
    return category, subcategory or "", sorted(set(labels))


def duplicate_keys(invoice):
    vendor = (invoice.get("vendor") or "").strip().lower()
    number = (invoice.get("invoice_number") or "").strip().lower()
    date_key = invoice.get("invoice_date") or ""
    amount = round(float(invoice.get("original_amount", 0) or 0), 2)
    keys = []
    if vendor and number:
        keys.append(("number", vendor, number))
    if vendor and date_key and amount:
        keys.append(("fallback", vendor, date_key, amount))
    return keys


def build_dataset(raw_bills, vendor_credits, accounts, items, vendor_map, company_info=None, previous_trend=None):
    kpis = {k: 0.0 for k in ["total_ap", "not_yet_due", "due_this_month", "overdue_lt_3m", "overdue_gt_3m"]}
    categories = {
        cat: {"name": cat, "total": 0.0, "not_yet_due": 0.0, "due_this_month": 0.0, "overdue_lt_3m": 0.0, "overdue_gt_3m": 0.0}
        for cat in CATEGORIES
    }
    invoices = []

    for bill in raw_bills:
        vendor_ref = bill.get("VendorRef") or {}
        vendor_name = vendor_ref.get("name") or "Unknown vendor"
        total_amount = float(bill.get("TotalAmt", 0) or 0)
        remaining = float(bill.get("Balance", 0) or 0)
        due_date = bill.get("DueDate") or ""
        category, subcategory, account_labels = bill_classification(bill, accounts, items, vendor_name, vendor_map)
        if category not in categories:
            category = "Unclassified"

        bucket = aging_bucket(due_date) if remaining > 0 else None
        if remaining > 0:
            kpis["total_ap"] += remaining
            kpis[bucket] += remaining
            categories[category]["total"] += remaining
            categories[category][bucket] += remaining

        days_overdue = 0
        if due_date:
            due_obj = datetime.strptime(due_date, "%Y-%m-%d").date()
            days_overdue = max(0, (date.today() - due_obj).days)

        if remaining <= 0:
            status = "Paid"
        elif bucket and bucket.startswith("overdue"):
            status = "Overdue"
        elif bucket == "due_this_month":
            status = "Due this month"
        else:
            status = "Not due"

        invoices.append({
            "id": str(bill.get("Id", "")),
            "vendor": vendor_name,
            "invoice_number": bill.get("DocNumber", ""),
            "invoice_date": bill.get("TxnDate", ""),
            "due_date": due_date,
            "category": category,
            "subcategory": subcategory,
            "account_labels": account_labels,
            "original_amount": round(total_amount, 2),
            "amount_paid": round(max(0.0, total_amount - remaining), 2),
            "remaining_balance": round(max(0.0, remaining), 2),
            "days_overdue": days_overdue,
            "status": status,
            "duplicate_candidate": False,
            "missing_due_date": not bool(due_date),
        })

    key_to_indexes = defaultdict(list)
    for i, invoice in enumerate(invoices):
        for key in duplicate_keys(invoice):
            key_to_indexes[key].append(i)
    duplicate_indexes = set()
    for indexes in key_to_indexes.values():
        if len(indexes) > 1:
            duplicate_indexes.update(indexes)
    for i in duplicate_indexes:
        invoices[i]["duplicate_candidate"] = True

    invoices.sort(key=lambda x: (0 if x["remaining_balance"] > 0 else 1, x.get("due_date") or "9999-12-31", -x["remaining_balance"]))

    open_invoices = [x for x in invoices if x["remaining_balance"] > 0]
    vendor_concentration = Counter()
    for x in open_invoices:
        vendor_concentration[x["vendor"]] += x["remaining_balance"]

    available_vendor_credits = sum(max(0.0, float(vc.get("Balance", 0) or 0)) for vc in vendor_credits)
    unclassified = [x for x in open_invoices if x["category"] == "Unclassified"]
    missing_due = [x for x in open_invoices if x["missing_due_date"]]
    duplicates = [x for x in invoices if x["duplicate_candidate"]]

    data_quality = {
        "open_bill_count": len(open_invoices),
        "paid_bill_count": len(invoices) - len(open_invoices),
        "unclassified_count": len(unclassified),
        "unclassified_balance": round(sum(x["remaining_balance"] for x in unclassified), 2),
        "missing_due_date_count": len(missing_due),
        "duplicate_candidate_count": len(duplicates),
        "available_vendor_credits": round(available_vendor_credits, 2),
    }

    return {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "currency": (company_info or {}).get("Currency") or "USD",
        "source": "quickbooks_online",
        "source_note": "QuickBooks Online; BILL-originated transactions are included when synchronized into QBO.",
        "company": (company_info or {}).get("CompanyName", ""),
        "kpis": {k: round(v, 2) for k, v in kpis.items()},
        "categories": [
            {k: (round(v, 2) if isinstance(v, float) else v) for k, v in cat.items()}
            for cat in categories.values()
        ],
        "monthly_trend": update_monthly_trend(previous_trend or [], kpis["total_ap"]),
        "data_quality": data_quality,
        "top_vendors": [
            {"vendor": vendor, "balance": round(balance, 2)}
            for vendor, balance in vendor_concentration.most_common(10)
        ],
        "invoices": invoices,
    }


def main():
    vendor_map = load_vendor_map()
    previous_trend = load_previous_qbo_trend()
    client = QuickBooksClient()
    company_info = client.get_company_info()
    raw_bills = client.get_bills()
    vendor_credits = client.get_vendor_credits()
    accounts = client.get_accounts()
    items = client.get_items()

    dataset = build_dataset(raw_bills, vendor_credits, accounts, items, vendor_map, company_info, previous_trend)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as handle:
        json.dump(dataset, handle, indent=2, ensure_ascii=False)

    print(f"QuickBooks environment: {client.environment}")
    print(f"Company: {dataset.get('company') or client.realm_id}")
    print(f"Bills loaded: {len(dataset['invoices'])}; open: {dataset['data_quality']['open_bill_count']}")
    print(f"Total AP: {dataset['kpis']['total_ap']:.2f}")
    if client.latest_refresh_token and client.latest_refresh_token != client.refresh_token:
        print("NOTICE: Intuit returned a newer refresh token. Update QBO_REFRESH_TOKEN securely if needed.")


if __name__ == "__main__":
    main()
