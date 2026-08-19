# Accounts Payable Dashboard — FIX16 / Weekly Close

This version closes the functional changes reviewed in the meeting while keeping **QuickBooks as the authoritative source** through Coefficient → Google Sheets.

## Meeting changes implemented
- Keeps QuickBooks Bill object as the AP source of truth.
- Shows a non-authoritative **~$373K BILL meeting reference** only for follow-up comparison; it never changes QuickBooks totals.
- Removes **Aging of Current AP** chart (aging remains in KPI cards).
- Removes **6-Month AP Trend**.
- Moves **Top Vendor Concentration** and **AP by Executive Categories** to the primary view.
- Adds **% of Open AP** to the category summary.
- Keeps **AP by QuickBooks Account**.
- Adds a **Priority Invoice Review** block for Yotpo #1, Yotpo #2 and RebatesMe.
- Generates `data/priority-invoice-review.csv` for the follow-up with accounting.
- Keeps a second-control flag for after accountants finish QuickBooks manual updates.

## Current validation from the supplied Payables 2026 workbook
- Current QuickBooks Open AP: **$234,867.27**
- Open Bills: **123**
- Approx. BILL meeting reference: **$373,000**
- Current gap vs reference: **-$138,132.73**
- Account allocation variance: **$0.00**

The $373K reference is intentionally **not** used to force or alter the dashboard total.

## Priority invoice review
1. **Yotpo Inc. — ZINVYUS00445073** — $5,845.98 open — due 2024-09-16 — Software Purchase. General Ledger contains a voided Bill Payment reference.
2. **Yotpo Inc. — ZINVYUS00478594** — $5,845.98 open — due 2024-12-17 — Software Purchase. General Ledger contains a voided Bill Payment reference.
3. **RebatesMe LLC — 2025Q1-0003** — $500.00 open — due 2024-12-19 — Selling & Marketing Expense. No matching payment resolution was found in the imported General Ledger.

The actual invoice PDF/attachment is **not included in the Coefficient imports**, so the dashboard flags these rows for document retrieval from QuickBooks/BILL.

## Executive category logic
QuickBooks account coding has priority. Vendor mapping is now only a fallback.

Current categories from the supplied workbook:
- Inventory — 53.09%
- G&A / OPEX — 18.81%
- Professional Services — 13.46%
- Sales & Marketing — 6.31%
- Shipping & Fulfillment — 5.36%
- Unclassified — 1.90%
- Advertising — 1.09%

## GitHub source configuration
No new sensitive credentials are required.

The workflow uses:
- `GSHEET_ID` (default already configured)
- `GSHEET_BILLS_GID = 1297077839`
- `GSHEET_GENERAL_LEDGER_GID = 186431676`

QuickBooks authentication remains inside Coefficient.

## Replace in GitHub
Replace the whole package, especially:
- `index.html`
- `scripts/transform.py`
- `scripts/google_sheets_client.py`
- `.github/workflows/update-ap-data.yml`

Then run **Actions → Update AP Dashboard Data → Run workflow**.
