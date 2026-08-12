# Accounts Payable Dashboard — QuickBooks via Coefficient

## Data flow

QuickBooks Online (real Equestrian Labs company) → Coefficient → Google Sheets → GitHub Actions → `data/ap-data.json` → GitHub Pages.

This version does **not** require Intuit Developer / QBO OAuth secrets in GitHub.

## Required Coefficient imports in the same Google spreadsheet

### 1. `AP_VENDOR_BALANCE` (required)
Create/import: QuickBooks → **Vendor Balance Detail** → **All Dates** → **Accrual**.
This report is the authoritative source for vendor, bill number, bill date, due date, original amount and open balance.

### 2. `General Ledger` (recommended)
Reuse the existing General Ledger Coefficient import. The dashboard uses Bill rows to infer the QuickBooks distribution account for each open transaction. It excludes A/P and bank control accounts.

If the General Ledger import is unavailable, the dashboard still runs and falls back to `data/vendor-map.json` for executive categories.

## Google Sheet sharing
Set the spreadsheet to **Anyone with the link → Viewer** (not Editor). This lets GitHub Actions read the CSV without storing Google credentials.

## GitHub Variables
Repository → Settings → Secrets and variables → Actions → **Variables**

- `GSHEET_ID` = `1wU-is7u0YFXbI3ZRYZ2MlEO-mqY8bD4NAXouNxhg73c`
- `GSHEET_VENDOR_BALANCE_SHEET` = `AP_VENDOR_BALANCE`
- `GSHEET_GENERAL_LEDGER_SHEET` = `General Ledger`
- `GSHEET_GENERAL_LEDGER_GID` = `186431676` (optional; use if this is the current General Ledger tab)
- `GSHEET_VENDOR_BALANCE_GID` = leave unset unless you prefer using the tab gid.

No QBO secrets are required by this workflow.

## Refresh
1. Coefficient refreshes QuickBooks data in Google Sheets (manual or scheduled in Coefficient).
2. GitHub Actions runs daily at 09:00 La Paz and can also be run manually.
3. `data/ap-data.json` is rebuilt and GitHub Pages redeploys.

## AP logic
- `Vendor Balance Detail` TOTAL = independent control total.
- `Net AP` = sum of all non-zero open balances in Vendor Balance Detail.
- `Gross open bills` = positive open balances where transaction type = Bill.
- Vendor credits and other adjustments are shown separately.
- Aging is calculated from positive open Bills only.
- Account allocation tries, in order: exact Vendor + Invoice + Type, exact Vendor + Invoice, Vendor history, then vendor-map fallback.
- `Accounts Payable (A/P)` and bank control accounts are excluded from distribution-account classification to avoid double counting.
