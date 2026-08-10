# Accounts Payable Dashboard — QuickBooks Online

The dashboard UI is unchanged. The data source is now **QuickBooks Online**.
If BILL is already synchronized with QuickBooks, this repository does **not** connect to the BILL API.

## Data flow

`BILL -> QuickBooks Online -> QBO Accounting API -> GitHub Actions -> data/ap-data.json -> GitHub Pages`

## Repository secrets

Create these under **Settings -> Secrets and variables -> Actions -> Repository secrets**:

- `QBO_CLIENT_ID`
- `QBO_CLIENT_SECRET`
- `QBO_REALM_ID`
- `QBO_REFRESH_TOKEN`

Under **Variables**, create:

- `QBO_ENVIRONMENT` = `sandbox` while using Development credentials.
- Change it to `production` only when Intuit has issued Production credentials and the real QBO company has authorized the app.

> Development credentials only connect to Intuit sandbox companies. They cannot read the real company file.

## Source and classification logic

The pipeline reads open QuickBooks `Bill` transactions (`Balance > 0`) and resolves:

- Vendor (`VendorRef`)
- Bill/invoice number (`DocNumber`)
- Transaction date (`TxnDate`)
- Due date (`DueDate`)
- Original amount (`TotalAmt`)
- Outstanding balance (`Balance`)
- Accounting detail from `AccountBasedExpenseLineDetail` or the expense account associated with item lines

Accounting account names are used first to classify AP into:

- Inventory
- Shipping & Fulfillment
- Advertising
- Sales & Marketing
- G&A / OPEX
- Unclassified

`data/vendor-map.json` remains only as a fallback when a bill does not expose enough accounting detail.

## First test

1. Add the four QBO secrets.
2. Set repository variable `QBO_ENVIRONMENT=sandbox`.
3. Authorize a QuickBooks sandbox company and obtain its `realmId` + refresh token.
4. Run **Actions -> Update AP Dashboard Data -> Run workflow**.

For the real Corro/Cavali company, repeat the OAuth authorization with **Production** credentials and set `QBO_ENVIRONMENT=production`.

## OAuth token note

QuickBooks can return a new refresh token during refresh. For a durable unattended production integration, the latest refresh token should be persisted securely. The current workflow is ready for initial testing with a GitHub Secret; production token persistence should be finalized when Production access is available.
