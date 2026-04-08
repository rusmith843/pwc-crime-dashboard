#!/usr/bin/env python3
"""
PWC Arrests → Dashboard API Ingestion Script

Extracts arrest records from a PWC PDF and posts them directly
to your running dashboard's database via the API.

Usage:
    python3 ingest_to_api.py <pdf_path> <api_url>

Examples:
    # Local dashboard
    python3 ingest_to_api.py arrests.pdf http://localhost:8000

    # Hosted on Render
    python3 ingest_to_api.py arrests.pdf https://pwc-crime-dashboard.onrender.com
"""

import sys, json, re, hashlib, urllib.request, urllib.error
from collections import defaultdict
from pathlib import Path

try:
    import pdfplumber
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install",
                           "pdfplumber", "--break-system-packages", "-q"])
    import pdfplumber

# Column boundaries
COL_DATE=(0,55); COL_SEX=(55,80); COL_DOB=(80,135); COL_AGE=(135,160)
COL_NAME=(160,325); COL_RESIDENCE=(325,505); COL_OFFENSE=(505,720); COL_CASENO=(720,800)

def _in_col(w, col): return col[0] <= w["x0"] < col[1]
def _wc(rw, col):    return " ".join(w["text"] for w in rw if _in_col(w, col))
def _hash(r):
    return hashlib.md5(f"{r['arrest_date']}|{r['name']}|{r['case_no']}|{r['offenses']}".encode()).hexdigest()

def _finalize(rec):
    rec["offenses"]  = " | ".join(o.strip() for o in rec["offenses"] if o.strip())
    rec["name"]      = rec["name"].strip()
    rec["residence"] = rec["residence"].strip()
    try:    rec["age"] = int(rec["age"])
    except: rec["age"] = None
    rec["record_hash"] = _hash(rec)
    return rec

def extract(pdf_path):
    records, current = [], None
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            rows = defaultdict(list)
            for w in page.extract_words():
                rows[round(w["top"], 1)].append(w)
            for top in sorted(rows):
                rw = sorted(rows[top], key=lambda w: w["x0"])
                dw = [w for w in rw if _in_col(w, COL_DATE)]
                sw = [w for w in rw if _in_col(w, COL_SEX)]
                is_new = (dw and re.match(r"^\d{1,2}/\d{1,2}/\d{4}$", dw[0]["text"])
                          and sw and sw[0]["text"] in ("M","F"))
                if is_new:
                    if current: records.append(_finalize(current))
                    ot = _wc(rw, COL_OFFENSE)
                    current = {
                        "arrest_date": dw[0]["text"], "sex": sw[0]["text"],
                        "dob": _wc(rw,COL_DOB), "age": _wc(rw,COL_AGE),
                        "name": _wc(rw,COL_NAME), "residence": _wc(rw,COL_RESIDENCE),
                        "offenses": [ot] if ot else [], "case_no": _wc(rw,COL_CASENO),
                    }
                elif current:
                    nf=_wc(rw,COL_NAME); rf=_wc(rw,COL_RESIDENCE)
                    of=_wc(rw,COL_OFFENSE); cf=_wc(rw,COL_CASENO)
                    if nf: current["name"] += " " + nf
                    if rf and rf not in current["residence"]:
                        current["residence"] += (" " if current["residence"] else "") + rf
                    if of: current["offenses"].append(of)
                    if cf and not current["case_no"]: current["case_no"] = cf
        if current: records.append(_finalize(current))
    return records

def post_to_api(records, filename, api_url):
    payload = json.dumps({"records": records, "filename": filename}).encode()
    req = urllib.request.Request(
        api_url.rstrip("/") + "/api/ingest", data=payload,
        headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"API error {e.code}: {e.read().decode()}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Cannot reach the dashboard at {api_url}\n  → Is the server running?")

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    pdf_path, api_url = sys.argv[1], sys.argv[2]
    print(f"📄 Extracting from: {pdf_path}")
    records = extract(pdf_path)
    print(f"   Found {len(records)} arrest records")
    if not records:
        print("⚠️  No records found — is this a valid PWC arrest PDF?"); sys.exit(1)
    print(f"📡 Posting to: {api_url}")
    result = post_to_api(records, Path(pdf_path).name, api_url)
    print(f"\n✓  Added:   {result.get('added','?')} new records")
    print(f"   Skipped: {result.get('skipped','?')} duplicates")
    print(f"   Total:   {result.get('total','?')} in PDF")
