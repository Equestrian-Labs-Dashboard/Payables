"""
transform.py
--------------
Toma las facturas crudas de Bill.com (via bill_client.py) + vendor-map.json
y genera data/ap-data.json en el formato que consume el dashboard.

Uso local (con variables de entorno ya seteadas):
    python transform.py

En GitHub Actions esto se corre automaticamente (ver
.github/workflows/update-ap-data.yml).
"""

import json
import os
from datetime import datetime, date

from bill_client import BillClient

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


def load_vendor_map():
    with open(VENDOR_MAP_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def classify_vendor(vendor_name, vendor_map):
    name_lower = (vendor_name or "").lower()
    for rule in vendor_map["rules"]:
        if rule["keyword"].lower() in name_lower:
            return rule["category"]
    return vendor_map.get("default_category", "Unclassified")


def aging_bucket(due_date_str, today=None):
    """Devuelve uno de: not_yet_due, due_this_month, overdue_lt_3m, overdue_gt_3m"""
    today = today or date.today()
    due = datetime.strptime(due_date_str, "%Y-%m-%d").date()
    days = (due - today).days

    if days > 0 and due.month == today.month and due.year == today.year:
        return "due_this_month"
    if days > 0:
        return "not_yet_due"

    overdue_days = -days
    if overdue_days <= 90:
        return "overdue_lt_3m"
    return "overdue_gt_3m"


def build_dataset(raw_bills, vendor_map):
    kpis = {
        "total_ap": 0.0,
        "not_yet_due": 0.0,
        "due_this_month": 0.0,
        "overdue_lt_3m": 0.0,
        "overdue_gt_3m": 0.0,
    }
    categories = {
        cat: {"name": cat, "total": 0.0, "not_yet_due": 0.0, "due_this_month": 0.0,
              "overdue_lt_3m": 0.0, "overdue_gt_3m": 0.0}
        for cat in CATEGORIES
    }
    invoices = []

    for bill in raw_bills:
        # NOTA: estos nombres de campo son un punto de partida y deben
        # confirmarse contra la respuesta real de la API de Bill.com.
        vendor_name = bill.get("vendorName", "Unknown vendor")
        remaining = float(bill.get("amountDue", 0) or 0)
        if remaining <= 0:
            continue  # ya pagada, no cuenta como AP pendiente

        due_date = bill.get("dueDate")
        category = classify_vendor(vendor_name, vendor_map)
        bucket = aging_bucket(due_date)

        kpis["total_ap"] += remaining
        kpis[bucket] += remaining
        categories[category]["total"] += remaining
        categories[category][bucket] += remaining

        invoices.append({
            "vendor": vendor_name,
            "invoice_number": bill.get("invoiceNumber", ""),
            "invoice_date": bill.get("invoiceDate", ""),
            "due_date": due_date,
            "category": category,
            "original_amount": float(bill.get("amount", 0) or 0),
            "amount_paid": float(bill.get("amount", 0) or 0) - remaining,
            "remaining_balance": remaining,
            "days_overdue": max(0, (date.today() - datetime.strptime(due_date, "%Y-%m-%d").date()).days) if due_date else None,
            "status": "Overdue" if bucket.startswith("overdue") else ("Due this month" if bucket == "due_this_month" else "Not due"),
        })

    return {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "currency": "USD",
        "source": "bill.com",
        "kpis": {k: round(v, 2) for k, v in kpis.items()},
        "categories": [
            {k: (round(v, 2) if isinstance(v, float) else v) for k, v in cat.items()}
            for cat in categories.values()
        ],
        "monthly_trend": [],  # TODO: requiere historico; se puede acumular corrida a corrida
        "invoices": invoices,
    }


def main():
    vendor_map = load_vendor_map()
    client = BillClient()
    client.login()
    raw_bills = client.get_open_bills()

    dataset = build_dataset(raw_bills, vendor_map)

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(dataset, f, indent=2, ensure_ascii=False)

    print(f"ap-data.json actualizado con {len(dataset['invoices'])} facturas abiertas.")


if __name__ == "__main__":
    main()
