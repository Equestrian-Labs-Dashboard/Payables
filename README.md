# Payables Dashboard — Coefficient Bills Object

## Required Coefficient import
Create a new QuickBooks import in the existing **Payables 2026** spreadsheet:

1. Coefficient → Import → QuickBooks → **From Objects & Fields**.
2. Object: **Bill**.
3. Include at least these fields:
   - Id
   - DocNumber
   - TxnDate
   - DueDate
   - TotalAmt
   - Balance
   - VendorRef (or Vendor / Vendor Name)
4. No date filter for the first run (or include enough history for all currently open bills).
5. Import name/tab: **QuickBooks Bills Import**.
6. Schedule it to refresh daily before the GitHub Action.

The existing tabs remain:
- QuickBooks General Ledger Import (account/category enrichment)
- QuickBooks Vendor Balance Detail Import (optional reconciliation only)

The Vendor Balance Detail report is deliberately NOT the main AP source because the current Coefficient/QuickBooks report response exposes Amount/Open Balance/Balance headers but returns blank values.
