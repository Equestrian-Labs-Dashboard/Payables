"""
bill_client.py
----------------
Cliente para la API de Bill.com (BDC API v3). Obtiene facturas por pagar (AP bills)
y las normaliza a un formato simple que luego usa transform.py.

Credenciales esperadas como variables de entorno (configuradas como
GitHub Actions Secrets, ver README.md):

    BILL_API_KEY        -> Dev/App key de Bill.com
    BILL_USERNAME        -> Usuario de la organizacion
    BILL_PASSWORD        -> Password del usuario
    BILL_ORG_ID           -> orgId de la organizacion en Bill.com

Nota: Bill.com tiene dos generaciones de API (v2 "Classic" y v3 "BDC").
Este scaffold usa v3 (REST + JSON). Si la cuenta de Corro/Cavali todavia
usa v2, avisame y adapto el cliente (la logica de transform.py no cambia).

Este archivo es un punto de partida funcional pero requiere que confirmes
contra la documentacion real de la cuenta (https://developer.bill.com)
los nombres exactos de campos antes de correrlo en produccion.
"""

import os
import sys
import requests

BILL_API_BASE = "https://api.bill.com/api/v3"


class BillClient:
    def __init__(self):
        self.api_key = os.environ.get("BILL_API_KEY")
        self.username = os.environ.get("BILL_USERNAME")
        self.password = os.environ.get("BILL_PASSWORD")
        self.org_id = os.environ.get("BILL_ORG_ID")
        self.session_id = None

        missing = [
            name for name, val in [
                ("BILL_API_KEY", self.api_key),
                ("BILL_USERNAME", self.username),
                ("BILL_PASSWORD", self.password),
                ("BILL_ORG_ID", self.org_id),
            ] if not val
        ]
        if missing:
            print(f"[bill_client] Faltan variables de entorno: {', '.join(missing)}", file=sys.stderr)

    def login(self):
        """Autentica contra Bill.com y guarda el sessionId."""
        resp = requests.post(
            f"{BILL_API_BASE}/login",
            json={
                "username": self.username,
                "password": self.password,
                "orgId": self.org_id,
                "devKey": self.api_key,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        self.session_id = data.get("sessionId")
        return self.session_id

    def _headers(self):
        return {
            "Content-Type": "application/json",
            "sessionId": self.session_id,
        }

    def get_open_bills(self):
        """
        Trae todas las facturas (bills) abiertas o parcialmente pagadas.
        Devuelve una lista de dicts crudos tal cual los entrega Bill.com.
        """
        if not self.session_id:
            self.login()

        bills = []
        start = 0
        page_size = 100

        while True:
            resp = requests.post(
                f"{BILL_API_BASE}/bill/list",
                headers=self._headers(),
                json={
                    "start": start,
                    "max": page_size,
                    "filters": [
                        {"field": "isActive", "op": "=", "value": "1"}
                    ],
                },
                timeout=30,
            )
            resp.raise_for_status()
            page = resp.json().get("results", [])
            bills.extend(page)
            if len(page) < page_size:
                break
            start += page_size

        return bills

    def get_vendors(self):
        """Trae el catalogo de vendors para poder resolver nombre a partir de vendorId."""
        if not self.session_id:
            self.login()

        resp = requests.post(
            f"{BILL_API_BASE}/vendor/list",
            headers=self._headers(),
            json={"start": 0, "max": 999},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json().get("results", [])


if __name__ == "__main__":
    client = BillClient()
    client.login()
    bills = client.get_open_bills()
    print(f"Facturas abiertas obtenidas: {len(bills)}")
