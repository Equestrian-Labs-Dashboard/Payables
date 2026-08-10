"""QuickBooks Online API client for the AP dashboard.

Required environment variables:
    QBO_CLIENT_ID
    QBO_CLIENT_SECRET
    QBO_REALM_ID
    QBO_REFRESH_TOKEN

Optional:
    QBO_ENVIRONMENT=production|sandbox   (default: sandbox)
    QBO_MINOR_VERSION                    (optional API minor version)

Development credentials can only access QuickBooks sandbox companies.
Production credentials are required for a real QuickBooks Online company.
"""

import os
import sys
from typing import Dict, List, Optional

import requests

TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
API_BASES = {
    "production": "https://quickbooks.api.intuit.com",
    "sandbox": "https://sandbox-quickbooks.api.intuit.com",
}


class QuickBooksClient:
    def __init__(self):
        self.client_id = os.environ.get("QBO_CLIENT_ID")
        self.client_secret = os.environ.get("QBO_CLIENT_SECRET")
        self.realm_id = os.environ.get("QBO_REALM_ID")
        self.refresh_token = os.environ.get("QBO_REFRESH_TOKEN")
        self.environment = os.environ.get("QBO_ENVIRONMENT", "sandbox").strip().lower()
        self.minor_version = os.environ.get("QBO_MINOR_VERSION", "").strip()
        self.access_token: Optional[str] = None
        self.latest_refresh_token: Optional[str] = None

        if self.environment not in API_BASES:
            raise ValueError("QBO_ENVIRONMENT must be 'sandbox' or 'production'")

        missing = [
            name
            for name, value in [
                ("QBO_CLIENT_ID", self.client_id),
                ("QBO_CLIENT_SECRET", self.client_secret),
                ("QBO_REALM_ID", self.realm_id),
                ("QBO_REFRESH_TOKEN", self.refresh_token),
            ]
            if not value
        ]
        if missing:
            raise RuntimeError(
                "Missing QuickBooks environment variables: " + ", ".join(missing)
            )

    @property
    def api_base(self) -> str:
        return API_BASES[self.environment]

    def refresh_access_token(self) -> str:
        """Exchange the refresh token for a fresh access token."""
        response = requests.post(
            TOKEN_URL,
            auth=(self.client_id, self.client_secret),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
            },
            timeout=30,
        )
        if not response.ok:
            print(f"[quickbooks] OAuth refresh failed: {response.text}", file=sys.stderr)
        response.raise_for_status()
        payload = response.json()
        self.access_token = payload["access_token"]
        self.latest_refresh_token = payload.get("refresh_token", self.refresh_token)
        return self.access_token

    def _headers(self) -> Dict[str, str]:
        if not self.access_token:
            self.refresh_access_token()
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        params = dict(params or {})
        if self.minor_version:
            params["minorversion"] = self.minor_version
        url = f"{self.api_base}{path}"
        response = requests.get(url, headers=self._headers(), params=params, timeout=60)
        if response.status_code == 401:
            # One retry with a newly refreshed access token.
            self.access_token = None
            response = requests.get(url, headers=self._headers(), params=params, timeout=60)
        if not response.ok:
            print(f"[quickbooks] GET {response.url} failed: {response.text}", file=sys.stderr)
        response.raise_for_status()
        return response.json()

    def query(self, query: str) -> dict:
        return self._get(
            f"/v3/company/{self.realm_id}/query",
            params={"query": query},
        ).get("QueryResponse", {})

    def _query_all(self, entity: str, where: str = "", page_size: int = 1000) -> List[dict]:
        rows: List[dict] = []
        start = 1
        where_clause = f" WHERE {where}" if where else ""

        while True:
            q = (
                f"SELECT * FROM {entity}{where_clause} "
                f"STARTPOSITION {start} MAXRESULTS {page_size}"
            )
            result = self.query(q)
            page = result.get(entity, [])
            rows.extend(page)
            if len(page) < page_size:
                break
            start += page_size
        return rows

    def get_open_bills(self) -> List[dict]:
        """Return QBO Bills that still have an outstanding balance."""
        # Balance filtering is kept in Python for compatibility across QBO query behavior.
        return [
            bill
            for bill in self._query_all("Bill")
            if float(bill.get("Balance", 0) or 0) > 0
        ]

    def get_accounts(self) -> Dict[str, dict]:
        return {str(x.get("Id")): x for x in self._query_all("Account") if x.get("Id")}

    def get_items(self) -> Dict[str, dict]:
        return {str(x.get("Id")): x for x in self._query_all("Item") if x.get("Id")}

    def get_company_info(self) -> dict:
        payload = self._get(f"/v3/company/{self.realm_id}/companyinfo/{self.realm_id}")
        return payload.get("CompanyInfo", {})


if __name__ == "__main__":
    client = QuickBooksClient()
    bills = client.get_open_bills()
    company = client.get_company_info()
    print(f"Environment: {client.environment}")
    print(f"Company: {company.get('CompanyName', client.realm_id)}")
    print(f"Open bills: {len(bills)}")
