"""Build data/ap-data.json from the REAL QuickBooks company via Coefficient.

Primary source (required): QuickBooks Bill OBJECT import in Google Sheets.
  - This avoids the QuickBooks Reports API issue where Vendor Balance Detail can
    expose Amount/Open Balance/Balance headers but return blank values.
  - Required fields: Id, DocNumber, TxnDate, DueDate, TotalAmt, Balance,
    VendorRef (or Vendor/Vendor Name).

Enrichment source (recommended): General Ledger report import.
  - Used only to map bills to distribution accounts/categories.
  - A/P and bank control accounts are excluded.

Optional control source: Vendor Balance Detail report import.
  - If Coefficient actually returns numeric Open Balance/Balance values, it is
    used as an independent reconciliation control. If its money fields are
    blank, the dashboard still builds correctly from the Bill object import.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
from collections import defaultdict
from datetime import date, datetime, timezone

from google_sheets_client import GoogleSheetsClient, GoogleSheetsError

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.abspath(os.path.join(HERE, "..", "data"))
OUTPUT_PATH = os.path.join(DATA_DIR, "ap-data.json")
VENDOR_MAP_PATH = os.path.join(DATA_DIR, "vendor-map.json")

DEFAULT_GSHEET_ID = "1wU-is7u0YFXbI3ZRYZ2MlEO-mqY8bD4NAXouNxhg73c"
DEFAULT_BILLS_SHEET = "QuickBooks Bills Import"
DEFAULT_GL_SHEET = "QuickBooks General Ledger Import"
DEFAULT_GL_GID = "186431676"
DEFAULT_VENDOR_BALANCE_SHEET = "QuickBooks Vendor Balance Detail Import"
DEFAULT_VENDOR_BALANCE_GID = "1046490113"

CATEGORIES = [
    "Inventory", "Shipping & Fulfillment", "Advertising",
    "Sales & Marketing", "G&A / OPEX", "Unclassified",
]

ACCOUNT_CATEGORY_RULES = [
    ("Shipping & Fulfillment", ["shipping", "freight", "fulfillment", "postage", "delivery", "warehouse", "warehousing", "packaging", "inbound shipping", "outbound shipping"]),
    ("Inventory", ["inventory asset", "inventory", "cost of goods", "cogs", "merchandise", "product cost", "purchases for resale", "purchases - resale"]),
    ("Advertising", ["advertising", "paid media", "google ads", "meta ads", "facebook ads", "ad spend", "ppc", "media buying"]),
    ("Sales & Marketing", ["selling & marketing", "marketing", "creative", "content", "seo", "sponsorship", "event", "brand", "sales commission", "influencer", "affiliate"]),
    ("G&A / OPEX", ["maintenance", "repair", "payroll", "wages", "salary", "staff", "contract labor", "contractor", "consulting", "professional fee", "software", "subscription", "rent", "office", "insurance", "legal", "accounting", "bank fee", "merchant fee", "utilities", "gas and electric", "general & administrative", "g&a", "opex", "tax", "audit", "intangible asset", "trademark"]),
]

EXCLUDED_GL_ACCOUNTS = ["accounts payable", "a/p", "accounts receivable", "a/r", "bank account", "checking", "savings", "undeposited funds", "credit card payable"]

ALIASES = {
    "id": ["id", "bill id", "transaction id"],
    "doc_number": ["docnumber", "doc number", "num", "number", "invoice number", "invoice #", "bill number"],
    "txn_date": ["txndate", "txn date", "transaction date", "date", "bill date"],
    "due_date": ["duedate", "due date"],
    "total_amt": ["totalamt", "total amt", "total amount", "amount", "original amount"],
    "balance": ["balance", "open balance", "openbalance", "balance due", "remaining balance"],
    "vendor": ["vendor", "vendor name", "supplier", "name", "vendorref.name", "vendor ref name", "vendorref"],
    "ap_account": ["apaccountref.name", "ap account", "accounts payable account", "apaccountref"],
    "currency": ["currencyref", "currency", "currencyref.value"],
    "exchange_rate": ["exchangerate", "exchange rate"],
    "memo": ["privatenote", "private note", "memo", "memo/description", "description"],
    "transaction_type": ["transaction type", "transactiontype", "type"],
    "debit": ["debit"], "credit": ["credit"], "account": ["account", "account/sector", "distribution account"],
}


def norm(v): return re.sub(r"[^a-z0-9]+", "", str(v or "").strip().lower())
def text(v): return str(v or "").strip()

def money(v):
    if v is None: return 0.0
    if isinstance(v, (int, float)): return float(v)
    s = str(v).strip().replace("$", "").replace(",", "")
    if not s or s in {"-", "—"}: return 0.0
    if s.startswith("(") and s.endswith(")"): s = "-" + s[1:-1]
    try: return float(s)
    except ValueError: return 0.0

def parse_date(v):
    s = text(v)
    if not s: return ""
    # ISO timestamps from object imports are accepted too.
    if "T" in s:
        s = s.split("T", 1)[0]
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y"):
        try: return datetime.strptime(s, fmt).date().isoformat()
        except ValueError: pass
    return ""

def parse_ref_name(v):
    """Extract a human name from QuickBooks Ref fields exported as JSON/text."""
    s = text(v)
    if not s: return ""
    if s.startswith("{"):
        try:
            obj = json.loads(s)
            for k in ("name", "Name", "value", "Value"):
                if text(obj.get(k)): return text(obj.get(k))
        except Exception:
            pass
    # Handles strings such as "123 - Vendor Name" / "Vendor Name (123)".
    if " - " in s and s.split(" - ", 1)[0].strip().isdigit():
        return s.split(" - ", 1)[1].strip()
    return s

def rows_from_csv(s): return list(csv.reader(io.StringIO(s)))

def header_map(row):
    n = [norm(x) for x in row]
    out = {}
    for key, aliases in ALIASES.items():
        wanted = {norm(a) for a in aliases}
        out[key] = [i for i, x in enumerate(n) if x in wanted]
    return out

def first_cell(row, h, key):
    for i in h.get(key, []):
        if i < len(row) and text(row[i]) != "": return row[i]
    return ""

def find_object_header(rows):
    # Bill object import must have a date, a balance and vendor/ref.
    for i, row in enumerate(rows[:80]):
        h = header_map(row)
        if h["balance"] and h["txn_date"] and (h["vendor"] or h["doc_number"]):
            return i, h
    return None, None

def load_vendor_map():
    try:
        with open(VENDOR_MAP_PATH, "r", encoding="utf-8") as f: return json.load(f)
    except Exception: return {"rules": [], "default_category": "Unclassified"}

def classify_vendor(vendor, vm):
    v = text(vendor).lower()
    for r in vm.get("rules", []):
        kw = text(r.get("keyword")).lower()
        if kw and kw in v:
            c = r.get("category", "Unclassified")
            return c if c in CATEGORIES else "Unclassified"
    return vm.get("default_category", "Unclassified")

def classify_account(account, vendor, vm):
    a = text(account).lower()
    for cat, kws in ACCOUNT_CATEGORY_RULES:
        if any(k in a for k in kws): return cat
    return classify_vendor(vendor, vm)

def aging_bucket(due_iso, today):
    if not due_iso: return "not_yet_due"
    due = datetime.strptime(due_iso, "%Y-%m-%d").date()
    days = (due - today).days
    if days >= 0 and due.year == today.year and due.month == today.month: return "due_this_month"
    if days >= 0: return "not_yet_due"
    return "overdue_lt_3m" if -days <= 90 else "overdue_gt_3m"

def parse_bills_object(csv_text):
    rows = rows_from_csv(csv_text)
    hi, h = find_object_header(rows)
    if hi is None:
        raise ValueError("QuickBooks Bills object import header not found. Required fields include TxnDate/Date, Balance, and Vendor/VendorRef.")
    print("Bills object headers:", " | ".join(text(x) for x in rows[hi] if text(x)))
    bills = []
    seen = set()
    for row in rows[hi+1:]:
        txn = parse_date(first_cell(row, h, "txn_date"))
        if not txn: continue
        bal_raw = first_cell(row, h, "balance")
        # Balance=0 is valid (paid bill), but blank means unusable row.
        if text(bal_raw) == "": continue
        bal = money(bal_raw)
        total = money(first_cell(row, h, "total_amt"))
        vendor = parse_ref_name(first_cell(row, h, "vendor")) or "Unknown vendor"
        doc = text(first_cell(row, h, "doc_number"))
        bill_id = text(first_cell(row, h, "id"))
        key = bill_id or (norm(vendor), norm(doc), txn, round(total, 2), round(bal, 2))
        if key in seen: continue
        seen.add(key)
        bills.append({
            "id": bill_id, "vendor": vendor, "transaction_type": "Bill",
            "invoice_number": doc, "invoice_date": txn,
            "due_date": parse_date(first_cell(row, h, "due_date")),
            "original_amount": round(total if total else bal, 2),
            "open_balance": round(bal, 2),
            "memo": text(first_cell(row, h, "memo")),
            "ap_account": parse_ref_name(first_cell(row, h, "ap_account")),
        })
    if not bills:
        raise ValueError("QuickBooks Bills object import returned no usable Bill rows with a Balance field.")
    open_count = sum(1 for b in bills if b["open_balance"] > 0.005)
    print(f"Bills object rows parsed: {len(bills)}; open bills: {open_count}; open AP: ${sum(max(0,b['open_balance']) for b in bills):,.2f}")
    return bills

def find_report_header(rows, required):
    for i,row in enumerate(rows[:100]):
        h=header_map(row)
        if all(h[k] for k in required): return i,h
    return None,None

def parse_vendor_balance_optional(csv_text):
    """Optional independent control. Never blocks dashboard when report money is blank."""
    if not csv_text.strip(): return {"available": False, "total": None, "note": "Vendor Balance Detail not loaded."}
    rows=rows_from_csv(csv_text); hi,h=find_report_header(rows,["txn_date","transaction_type"])
    if hi is None: return {"available": False, "total": None, "note": "Vendor Balance Detail header not recognized."}
    total=None; numeric=[]
    for row in rows[hi+1:]:
        populated=[text(c) for c in row if text(c)]
        if populated and populated[0].upper()=="TOTAL":
            # Prefer Open Balance; then Balance/Amount.
            for key in ("balance","total_amt"):
                raw=first_cell(row,h,key)
                if text(raw): total=money(raw); break
            continue
        if not parse_date(first_cell(row,h,"txn_date")): continue
        # Some Coefficient report imports expose blank numeric fields; detect but don't fail.
        for key in ("balance","total_amt"):
            raw=first_cell(row,h,key)
            if text(raw): numeric.append(money(raw)); break
    if total is None and numeric: total=round(sum(numeric),2)
    if total is None or (abs(total)<0.005 and not any(abs(x)>0.005 for x in numeric)):
        return {"available":False,"total":None,"note":"Vendor Balance Detail money fields are blank in the Coefficient/QuickBooks report API; Bills object is used as the authoritative AP source."}
    return {"available":True,"total":round(total,2),"note":"Vendor Balance Detail numeric control loaded."}

def gl_account_is_distribution(a):
    s=text(a).lower(); return bool(s) and not any(x in s for x in EXCLUDED_GL_ACCOUNTS)

def addw(bucket,key,account,w):
    if key and account and w>0: bucket[key][account]+=w

def parse_general_ledger(csv_text):
    rows=rows_from_csv(csv_text); hi,h=find_report_header(rows,["txn_date","transaction_type","vendor","account"])
    if hi is None: raise ValueError("General Ledger header not found. Expected Date, Transaction Type, Name/Vendor and Account.")
    exact=defaultdict(lambda:defaultdict(float)); invoice=defaultdict(lambda:defaultdict(float)); vendor=defaultdict(lambda:defaultdict(float)); usable=0
    for row in rows[hi+1:]:
        t=text(first_cell(row,h,"transaction_type")); v=text(first_cell(row,h,"vendor")); a=text(first_cell(row,h,"account")); n=text(first_cell(row,h,"doc_number"))
        # Only original supplier documents should define classification; payments/JEs pollute vendor history.
        if t.lower() not in {"bill","vendor credit"} or not v or not gl_account_is_distribution(a): continue
        vals=[]
        for k in ("debit","credit","total_amt"):
            raw=first_cell(row,h,k)
            if text(raw): vals.append(abs(money(raw)))
        w=max(vals or [0])
        if w<=0: continue
        vk,nk,tk=norm(v),norm(n),norm(t)
        addw(exact,(vk,nk,tk),a,w)
        if nk: addw(invoice,(vk,nk),a,w)
        addw(vendor,vk,a,w); usable+=1
    print(f"General Ledger usable Bill/Vendor Credit distribution rows: {usable}")
    return {"exact":exact,"invoice":invoice,"vendor":vendor,"usable_rows":usable}

def choose_weights(tx,gl):
    vk,nk,tk=norm(tx["vendor"]),norm(tx["invoice_number"]),norm(tx["transaction_type"])
    for weights,source in [
        (gl.get("exact",{}).get((vk,nk,tk)),"exact invoice + type"),
        (gl.get("invoice",{}).get((vk,nk)),"exact invoice"),
        (gl.get("vendor",{}).get(vk),"vendor history"),
    ]:
        if weights: return dict(weights),source
    return {},"unmapped"

def allocate(tx,gl,vm):
    weights,src=choose_weights(tx,gl); bal=max(0.0,float(tx["open_balance"] or 0))
    if not weights:
        cat=classify_vendor(tx["vendor"],vm)
        return [{"account":"Unmapped - review in QuickBooks","category":cat,"open_balance":round(bal,2),"mapping_source":src}]
    total=sum(weights.values()) or 1; out=[]; running=0
    items=sorted(weights.items(),key=lambda x:x[1],reverse=True)
    for i,(a,w) in enumerate(items):
        amt=round(bal-running,2) if i==len(items)-1 else round(bal*w/total,2)
        if i<len(items)-1: running+=amt
        out.append({"account":a,"category":classify_account(a,tx["vendor"],vm),"open_balance":amt,"mapping_source":src})
    return out

def blank_rollup(key,name):
    return {key:name,"total":0.0,"not_yet_due":0.0,"due_this_month":0.0,"overdue_lt_3m":0.0,"overdue_gt_3m":0.0,"bill_count":0}

def load_previous_trend():
    try:
        old=json.load(open(OUTPUT_PATH,encoding="utf-8")); return old.get("monthly_trend",[]) or []
    except Exception:return []
def update_trend(prev,total):
    m=date.today().strftime("%Y-%m"); by={str(x.get("month")):x for x in prev if x.get("month")}; by[m]={"month":m,"total_ap":round(total,2)}; return [by[k] for k in sorted(by)[-6:]]

def build_dashboard(bills_csv, gl_csv, vendor_balance_csv=""):
    vm=load_vendor_map(); bills=parse_bills_object(bills_csv)
    try: gl=parse_general_ledger(gl_csv) if gl_csv.strip() else {"exact":{},"invoice":{},"vendor":{},"usable_rows":0}
    except ValueError as e:
        print(f"::warning title=General Ledger parse warning::{e}"); gl={"exact":{},"invoice":{},"vendor":{},"usable_rows":0}
    control=parse_vendor_balance_optional(vendor_balance_csv)
    today=date.today(); accounts={}; cats={c:blank_rollup("name",c) for c in CATEGORIES}; vendors=defaultdict(float); invoices=[]; aging=defaultdict(float)
    gross=0.0; missing_due=0; unmapped=0; unclassified=0.0
    for tx in bills:
        bal=max(0.0,float(tx["open_balance"] or 0))
        paid=max(0.0,float(tx["original_amount"] or 0)-bal)
        bucket=aging_bucket(tx["due_date"],today) if bal>0 else ""
        if bal>0:
            gross+=bal; aging[bucket]+=bal; vendors[tx["vendor"]]+=bal
            if not tx["due_date"]: missing_due+=1
        allocs=allocate(tx,gl,vm) if bal>0 else []
        if bal>0 and allocs and allocs[0]["mapping_source"]=="unmapped": unmapped+=1
        labels=[]; cat_amounts=defaultdict(float)
        for a in allocs:
            labels.append(a["account"]); cat_amounts[a["category"]]+=a["open_balance"]
            r=accounts.setdefault(a["account"],blank_rollup("account",a["account"])); r["category"]=a["category"]; r["total"]+=a["open_balance"]; r[bucket]+=a["open_balance"] if bucket else 0
        primary_cat=max(cat_amounts,key=cat_amounts.get) if cat_amounts else classify_vendor(tx["vendor"],vm)
        if bal>0:
            cats[primary_cat]["total"]+=bal; cats[primary_cat][bucket]+=bal; cats[primary_cat]["bill_count"]+=1
            if primary_cat=="Unclassified": unclassified+=bal
            for a in set(labels): accounts[a]["bill_count"]+=1
        days_overdue=0
        if tx["due_date"]:
            due=datetime.strptime(tx["due_date"],"%Y-%m-%d").date(); days_overdue=max(0,(today-due).days)
        status="Paid" if bal<=0.005 else ("Overdue" if days_overdue>0 else ("Due this month" if bucket=="due_this_month" else "Not due"))
        invoices.append({
            "vendor":tx["vendor"],"invoice_number":tx["invoice_number"],"invoice_date":tx["invoice_date"],"due_date":tx["due_date"],
            "original_amount":round(tx["original_amount"],2),"amount_paid":round(paid,2),"remaining_balance":round(bal,2),"days_overdue":days_overdue,"status":status,
            "primary_account":labels[0] if labels else "Unmapped - review in QuickBooks","account_labels":labels or ["Unmapped - review in QuickBooks"],"category":primary_cat,
        })
    total=round(gross,2)
    # Bill.Balance is authoritative. Vendor Balance is only an optional cross-check.
    vendor_total=control["total"] if control["available"] else None
    variance=round(total-vendor_total,2) if vendor_total is not None else None
    for d in list(accounts.values())+list(cats.values()):
        for k in ("total","not_yet_due","due_this_month","overdue_lt_3m","overdue_gt_3m"): d[k]=round(d[k],2)
    account_summary=sorted([r for r in accounts.values() if r["total"]>0.005],key=lambda x:x["total"],reverse=True)
    categories=sorted([r for r in cats.values()],key=lambda x:x["total"],reverse=True)
    top_vendors=[{"vendor":v,"balance":round(b,2)} for v,b in sorted(vendors.items(),key=lambda x:x[1],reverse=True)[:12]]
    return {
        "source":"coefficient_google_sheets_quickbooks_bills_object",
        "source_note":"Source: QuickBooks Online (real company) → Coefficient Bills object + General Ledger → Google Sheets",
        "company":"Equestrian Labs, Inc. (dba Corro)","generated_at":datetime.now(timezone.utc).isoformat(),
        "kpis":{"total_ap":total,"gross_open_bills":total,"credits_adjustments":0.0,"aging_total":total,"not_yet_due":round(aging["not_yet_due"],2),"due_this_month":round(aging["due_this_month"],2),"overdue_lt_3m":round(aging["overdue_lt_3m"],2),"overdue_gt_3m":round(aging["overdue_gt_3m"],2)},
        "reconciliation":{"calculated_net_ap":total,"vendor_balance_available":control["available"],"vendor_balance_total":vendor_total,"variance":variance,"reconciled":bool(control["available"] and abs(variance or 0)<=0.02),"note":control["note"]},
        "accounts_summary":account_summary,"categories":categories,"top_vendors":top_vendors,
        "monthly_trend":update_trend(load_previous_trend(),total),"invoices":sorted(invoices,key=lambda x:(x["remaining_balance"]<=0,-x["days_overdue"],-x["remaining_balance"])),
        "quality":{"open_bills":sum(1 for x in invoices if x["remaining_balance"]>0.005),"unmapped_bills":unmapped,"unclassified_balance":round(unclassified,2),"missing_due_date":missing_due,"general_ledger_usable_rows":gl.get("usable_rows",0)},
    }

def main():
    sid=os.getenv("GSHEET_ID",DEFAULT_GSHEET_ID).strip() or DEFAULT_GSHEET_ID
    bills_sheet=os.getenv("GSHEET_BILLS_SHEET",DEFAULT_BILLS_SHEET).strip() or DEFAULT_BILLS_SHEET
    bills_gid=os.getenv("GSHEET_BILLS_GID","").strip()
    gl_sheet=os.getenv("GSHEET_GENERAL_LEDGER_SHEET",DEFAULT_GL_SHEET).strip() or DEFAULT_GL_SHEET
    gl_gid=os.getenv("GSHEET_GENERAL_LEDGER_GID",DEFAULT_GL_GID).strip()
    vb_sheet=os.getenv("GSHEET_VENDOR_BALANCE_SHEET",DEFAULT_VENDOR_BALANCE_SHEET).strip() or DEFAULT_VENDOR_BALANCE_SHEET
    vb_gid=os.getenv("GSHEET_VENDOR_BALANCE_GID",DEFAULT_VENDOR_BALANCE_GID).strip()
    client=GoogleSheetsClient(sid)
    print(f"Reading QuickBooks Bills object import: {bills_sheet}")
    try: bills_csv=client.fetch_csv(sheet_name=bills_sheet,gid=bills_gid)
    except GoogleSheetsError as e:
        raise SystemExit(f"BILLS SOURCE ERROR: {e}. Create a Coefficient QuickBooks Objects & Fields import named '{bills_sheet}' using the Bill object and include Id, DocNumber, TxnDate, DueDate, TotalAmt, Balance and VendorRef.")
    print(f"Reading General Ledger: {gl_sheet}")
    gl_csv=client.fetch_csv(sheet_name=gl_sheet,gid=gl_gid)
    try:
        vb_csv=client.fetch_csv(sheet_name=vb_sheet,gid=vb_gid)
    except Exception as e:
        print(f"::warning title=Vendor Balance optional control unavailable::{e}"); vb_csv=""
    data=build_dashboard(bills_csv,gl_csv,vb_csv)
    os.makedirs(DATA_DIR,exist_ok=True)
    with open(OUTPUT_PATH,"w",encoding="utf-8") as f: json.dump(data,f,indent=2,ensure_ascii=False)
    print(f"Wrote {OUTPUT_PATH}")
    print(f"Total AP from Bill.Balance: ${data['kpis']['total_ap']:,.2f}")
    print(f"Open bills: {data['quality']['open_bills']}")
    print(f"GL distribution rows: {data['quality']['general_ledger_usable_rows']}")
    if data['reconciliation']['vendor_balance_available']:
        print(f"Vendor Balance control: ${data['reconciliation']['vendor_balance_total']:,.2f}; variance ${data['reconciliation']['variance']:,.2f}")
    else:
        print("Vendor Balance control unavailable because the Coefficient report money fields are blank; this does NOT block the dashboard.")

if __name__=="__main__": main()
