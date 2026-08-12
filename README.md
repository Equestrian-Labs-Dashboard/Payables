# Payables Dashboard — Coefficient / QuickBooks (FIX12)

Source flow: **QuickBooks Online (real company) → Coefficient → Google Sheets → GitHub Actions → GitHub Pages**.

FIX12 handles the actual Coefficient `Vendor Balance Detail` layout where `Amount` and `Open Balance` headers can exist but their transaction cells are blank. The parser reconstructs each transaction's open amount from the change in QuickBooks' populated running `Balance` column, resetting the baseline for each vendor.

Configured source IDs:
- Spreadsheet: `1wU-is7u0YFXbI3ZRYZ2MlEO-mqY8bD4NAXouNxhg73c`
- Vendor Balance Detail GID: `1046490113`
- General Ledger GID: `186431676`

Replace:
- `scripts/transform.py`
- `.github/workflows/update-ap-data.yml` (use the included `update-ap-data.yml` at repository path `.github/workflows/update-ap-data.yml`)

No QuickBooks OAuth secrets are required for this Coefficient-based workflow.
