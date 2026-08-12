# Accounts Payable Dashboard — FIX15

Authoritative source: **QuickBooks Bill object → Coefficient → Google Sheets**.

## Live source identifiers
- Spreadsheet ID: `1wU-is7u0YFXbI3ZRYZ2MlEO-mqY8bD4NAXouNxhg73c`
- QuickBooks Bill gid: `1297077839`
- General Ledger gid: `186431676`

No QuickBooks API secret is required by GitHub Actions. Coefficient owns the authenticated QuickBooks connection. The Google Sheet must remain readable by the workflow (current implementation uses the public CSV export URL).

## Important FIX15 corrections
1. Groups Coefficient's split line rows by **Bill Id**, so a Bill is counted once.
2. Uses **Vendor Name (Vendor Reference)** directly; no more `Unknown vendor`.
3. Converts non-USD Bill balances to USD with **Exchange Rate** before summing AP.
4. Uses each Bill's own **line Account Name + Amount** to allocate AP by QuickBooks account.
5. Partially paid Bills allocate remaining balance proportionally across their original line amounts.
6. General Ledger is fallback only; it is not needed when Bill line accounts exist.
7. Account allocations are forced to reconcile to Total AP; workflow fails if variance exceeds $0.05.
8. Bill detail defaults to **Open** so historical paid Bills do not overwhelm the AP view.

## Validation against `Payables 2026.xlsx` supplied 2026-08-12
- Historical Bills: 9,218
- Open Bills: 123
- Total AP in USD after FX: **$234,867.27**
- Overdue < 3 months: **$93,584.69**
- Overdue > 3 months: **$141,282.58**
- Not yet due: **$0.00**
- Due this month: **$0.00**
- Account allocation variance: **$0.00**

The prior $314,050.34 total was incorrect because two SEK Bills were being summed as if their native SEK balances were USD. FIX15 applies their QuickBooks Exchange Rate.
