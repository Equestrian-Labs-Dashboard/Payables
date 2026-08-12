"""Build data/ap-data.json from QuickBooks Online for the AP Executive Dashboard.

The dashboard is account-first: open AP is allocated proportionally to the
QuickBooks expense/item accounts carried by each Bill line. Executive categories
are a secondary roll-up, not the primary accounting view.
"""

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


def bill_account_allocations(bill, accounts, items, vendor_name, vendor_map, remaining_balance):
    """Allocate an open bill balance across its QBO line accounts.

    Partial payments are allocated proportionally to line amounts so account totals
    reconcile exactly to the bill's current Balance.
    """
    raw = []
    for line in bill.get("Line", []) or []:
        amount = float(line.get("Amount", 0) or 0)
        if amount <= 0:
            continue
        label = line_account_label(line, accounts, items) or "No account assigned"
        category = classify_text(label, ACCOUNT_CATEGORY_RULES) or classify_vendor(vendor_name, vendor_map)
        if category not in CATEGORIES:
            category = "Unclassified"
        raw.append((label, category, amount))

    if not raw:
        fallback_category = classify_vendor(vendor_name, vendor_map)
        if fallback_category not in CATEGORIES:
            fallback_category = "Unclassified"
        return [{
            "account": "No account assigned",
            "category": fallback_category,
            "line_amount": float(bill.get("TotalAmt", 0) or 0),
            "open_balance": round(max(0.0, remaining_balance), 2),
        }]

    line_total = sum(x[2] for x in raw)
    grouped = defaultdict(lambda: {"line_amount": 0.0, "category": "Unclassified"})
    for label, category, amount in raw:
        grouped[label]["line_amount"] += amount
        grouped[label]["category"] = category

    allocations = []
    running = 0.0
    rows = list(grouped.items())
    for idx, (label, info) in enumerate(rows):
        if idx == len(rows) - 1:
            open_amt = round(max(0.0, remaining_balance) - running, 2)
        else:
            open_amt = round(max(0.0, remaining_balance) * info["line_amount"] / line_total, 2) if line_total else 0.0
            running += open_amt
        allocations.append({
            "account": label,
            "category": info["category"],
            "line_amount": round(info["line_amount"], 2),
            "open_balance": open_amt,
        })
    return allocations


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


def blank_rollup(name_key, name):
    return {
        name_key: name,
        "total": 0.0,
        "not_yet_due": 0.0,
        "due_this_month": 0.0,
        "overdue_lt_3m": 0.0,
        "overdue_gt_3m": 0.0,
        "bill_count": 0,
    }


def build_dataset(raw_bills, vendor_credits, accounts, items, vendor_map, company_info=None, previous_trend=None):
    kpis = {k: 0.0 for k in ["total_ap", "not_yet_due", "due_this_month", "overdue_lt_3m", "overdue_gt_3m"]}
    category_rollup = {cat: blank_rollup("name", cat) for cat in CATEGORIES}
    account_rollup = {}
    invoices = []

    for bill in raw_bills:
        vendor_ref = bill.get("VendorRef") or {}
        vendor_name = vendor_ref.get("name") or "Unknown vendor"
        total_amount = float(bill.get("TotalAmt", 0) or 0)
        remaining = max(0.0, float(bill.get("Balance", 0) or 0))
        due_date = bill.get("DueDate") or ""
        bucket = aging_bucket(due_date) if remaining > 0 else None
        allocations = bill_account_allocations(bill, accounts, items, vendor_name, vendor_map, remaining)
        allocations_sorted = sorted(allocations, key=lambda x: x["open_balance"], reverse=True)
        primary_account = allocations_sorted[0]["account"] if allocations_sorted else "No account assigned"
        primary_category = allocations_sorted[0]["category"] if allocations_sorted else "Unclassified"

        if remaining > 0:
            kpis["total_ap"] += remaining
            kpis[bucket] += remaining

            touched_categories = set()
            touched_accounts = set()
            for allocation in allocations:
                account = allocation["account"]
                category = allocation["category"]
                open_amt = allocation["open_balance"]

                if account not in account_rollup:
                    account_rollup[account] = blank_rollup("account", account)
                    account_rollup[account]["category"] = category
                account_rollup[account]["total"] += open_amt
                account_rollup[account][bucket] += open_amt
                touched_accounts.add(account)

                category_rollup[category]["total"] += open_amt
                category_rollup[category][bucket] += open_amt
                touched_categories.add(category)

            for account in touched_accounts:
                account_rollup[account]["bill_count"] += 1
            for category in touched_categories:
                category_rollup[category]["bill_count"] += 1

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

        combined = " ".join([a["account"] for a in allocations] + [vendor_name])
        subcategory = classify_text(combined, SUBCATEGORY_RULES) if primary_category == "G&A / OPEX" else ""

        invoices.append({
            "id": str(bill.get("Id", "")),
            "vendor": vendor_name,
            "invoice_number": bill.get("DocNumber", ""),
            "invoice_date": bill.get("TxnDate", ""),
            "due_date": due_date,
            "primary_account": primary_account,
            "account_labels": [a["account"] for a in allocations],
            "account_breakdown": allocations,
            "category": primary_category,
            "subcategory": subcategory or "",
            "original_amount": round(total_amount, 2),
            "amount_paid": round(max(0.0, total_amount - remaining), 2),
            "remaining_balance": round(remaining, 2),
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
    no_account = [x for x in open_invoices if "No account assigned" in x.get("account_labels", [])]

    data_quality = {
        "open_bill_count": len(open_invoices),
        "paid_bill_count": len(invoices) - len(open_invoices),
        "unclassified_count": len(unclassified),
        "unclassified_balance": round(sum(x["remaining_balance"] for x in unclassified), 2),
        "missing_account_count": len(no_account),
        "missing_due_date_count": len(missing_due),
        "duplicate_candidate_count": len(duplicates),
        "available_vendor_credits": round(available_vendor_credits, 2),
    }

    def cleaned_rows(rows):
        out = []
        for row in rows:
            out.append({k: round(v, 2) if isinstance(v, float) else v for k, v in row.items()})
        return out

    accounts_summary = cleaned_rows(sorted(account_rollup.values(), key=lambda x: x["total"], reverse=True))
    categories_summary = cleaned_rows(category_rollup.values())

    return {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "currency": (company_info or {}).get("Currency") or "USD",
        "source": "quickbooks_online",
        "source_note": "QuickBooks Online; BILL-originated transactions are included when synchronized into QBO.",
        "company": (company_info or {}).get("CompanyName", ""),
        "kpis": {k: round(v, 2) for k, v in kpis.items()},
        "accounts_summary": accounts_summary,
        "categories": categories_summary,
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

    print(f"Wrote {OUTPUT_PATH}")
    print(f"Open AP: {dataset['kpis']['total_ap']}")
    print(f"Accounts with open AP: {len(dataset['accounts_summary'])}")


if __name__ == "__main__":
    main()
