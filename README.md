# Accounts Payable Dashboard — QuickBooks Online

The dashboard design remains unchanged. The data source is **QuickBooks Online**.
If BILL is already synchronized with QuickBooks, this repository does **not** need the BILL API.

## Data flow

`BILL -> QuickBooks Online -> QBO Accounting API -> GitHub Actions -> data/ap-data.json -> GitHub Pages`

## GitHub secrets

Go to:

`Repository -> Settings -> Secrets and variables -> Actions -> Secrets -> New repository secret`

Create exactly these four repository secrets:

- `QBO_CLIENT_ID`
- `QBO_CLIENT_SECRET`
- `QBO_REALM_ID`
- `QBO_REFRESH_TOKEN`

Do **not** put quotes around their values.

## GitHub variable

Go to:

`Repository -> Settings -> Secrets and variables -> Actions -> Variables -> New repository variable`

Create:

- Name: `QBO_ENVIRONMENT`
- Value: `sandbox`

Use `sandbox` while the Intuit app uses Development credentials.
Change it to `production` only after Production credentials are available and the real QuickBooks company has authorized the app.

## GitHub Pages setting

Go to:

`Repository -> Settings -> Pages`

Set:

- **Source:** `GitHub Actions`

The workflow declares the required `github-pages` deployment environment automatically.

## Files used by the integration

- `.github/workflows/update-ap-data.yml` — scheduled/manual data refresh + Pages deployment.
- `scripts/quickbooks_client.py` — OAuth refresh and QuickBooks API queries.
- `scripts/transform.py` — converts open QBO Bills into dashboard JSON.
- `scripts/requirements.txt` — Python dependency list.
- `data/vendor-map.json` — fallback classification only when QBO accounting detail is insufficient.
- `data/ap-data.json` — generated dashboard data.

## QuickBooks data used

The pipeline reads open QuickBooks `Bill` transactions (`Balance > 0`) and resolves:

- Vendor (`VendorRef`)
- Bill/invoice number (`DocNumber`)
- Transaction date (`TxnDate`)
- Due date (`DueDate`)
- Original amount (`TotalAmt`)
- Outstanding balance (`Balance`)
- Expense/item accounting detail from each bill line

Account names are used first to classify AP. Vendor mapping is only a fallback.

## Run a test

1. Add all four QBO secrets.
2. Add `QBO_ENVIRONMENT=sandbox` under **Variables**.
3. Ensure the `QBO_REALM_ID` and `QBO_REFRESH_TOKEN` belong to the same sandbox company authorized with the Development app.
4. Go to **Actions -> Update AP Dashboard Data -> Run workflow**.
5. Confirm both jobs are green:
   - `Update data from QuickBooks`
   - `Deploy GitHub Pages`

If OAuth fails, check that the Client ID, Client Secret, Realm ID and Refresh Token all belong to the same Intuit Development app/sandbox authorization.
