"""Small read-only Google Sheets CSV client.

Designed for a sheet shared as "Anyone with the link -> Viewer". No Google API
credentials are required. Coefficient remains responsible for refreshing the
QuickBooks imports inside the spreadsheet; GitHub Actions only reads the sheet.
"""

from __future__ import annotations

import os
from urllib.parse import quote

import requests


class GoogleSheetsError(RuntimeError):
    pass


class GoogleSheetsClient:
    def __init__(self, spreadsheet_id: str | None = None, timeout: int = 45):
        self.spreadsheet_id = (spreadsheet_id or os.getenv("GSHEET_ID", "")).strip()
        self.timeout = timeout
        if not self.spreadsheet_id:
            raise GoogleSheetsError("GSHEET_ID is missing")

    def _get(self, url: str, params: dict | None = None) -> str:
        response = requests.get(
            url,
            params=params,
            timeout=self.timeout,
            headers={"User-Agent": "Corro-Payables-Dashboard/1.0"},
        )
        if response.status_code != 200:
            raise GoogleSheetsError(
                f"Google Sheets returned HTTP {response.status_code} for {response.url}"
            )
        text = response.text
        ctype = (response.headers.get("content-type") or "").lower()
        sample = text[:1000].lower()
        if "text/html" in ctype or "accounts.google.com" in sample or "sign in" in sample:
            raise GoogleSheetsError(
                "Google returned an HTML/login page instead of CSV. Share the spreadsheet as "
                "'Anyone with the link -> Viewer', or provide a published/readable sheet."
            )
        if not text.strip():
            raise GoogleSheetsError("Google Sheets returned an empty CSV")
        return text

    def fetch_csv(self, *, sheet_name: str | None = None, gid: str | None = None) -> str:
        gid = (gid or "").strip()
        sheet_name = (sheet_name or "").strip()
        if gid:
            url = (
                f"https://docs.google.com/spreadsheets/d/{quote(self.spreadsheet_id)}/export"
            )
            return self._get(url, {"format": "csv", "gid": gid})
        if not sheet_name:
            raise GoogleSheetsError("A sheet_name or gid is required")
        url = f"https://docs.google.com/spreadsheets/d/{quote(self.spreadsheet_id)}/gviz/tq"
        return self._get(url, {"tqx": "out:csv", "sheet": sheet_name})
