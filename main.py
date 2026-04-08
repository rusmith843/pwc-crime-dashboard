#!/usr/bin/env python3
"""
PWC Crime Dashboard — Backend API Server
FastAPI + SQLite backend for the Prince William County arrest data dashboard.

Run locally:  python main.py
API docs:     http://localhost:8000/docs
"""

import os, sys, json, re, sqlite3, tempfile, time, hashlib
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional

# ── Auto-install dependencies ──────────────────────────────────────────────────
def _pip(*pkgs):
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", *pkgs,
                           "--break-system-packages", "-q"])

try:
    import pdfplumber
except ImportError:
    _pip("pdfplumber"); import pdfplumber

try:
    from fastapi import FastAPI, UploadFile, File, HTTPException, Query, BackgroundTasks
    from fastapi.staticfiles import StaticFiles
    from fastapi.responses import JSONResponse, FileResponse
    from fastapi.middleware.cors import CORSMiddleware
    import uvicorn
except ImportError:
    _pip("fastapi", "uvicorn[standard]", "python-multipart")
    from fastapi import FastAPI, UploadFile, File, HTTPException, Query, BackgroundTasks
    from fastapi.staticfiles import StaticFiles
    from fastapi.responses import JSONResponse, FileResponse
    from fastapi.middleware.cors import CORSMiddleware
    import uvicorn

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data"))
DB_PATH  = DATA_DIR / "arrests.db"
DATA_DIR.mkdir(exist_ok=True)

# ── FastAPI app ────────────────────────────────────────────────────────────────
app = FastAPI(title="PWC Crime Dashboard", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

# ── Database ───────────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS arrests (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                arrest_date TEXT,
                sex         TEXT,
                dob         TEXT,
                age         INTEGER,
                name        TEXT,
                residence   TEXT,
                offenses    TEXT,
                case_no     TEXT,
                record_hash TEXT UNIQUE,
                lat         REAL,
                lng         REAL,
                imported_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS pdf_imports (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                filename        TEXT,
                imported_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                records_added   INTEGER DEFAULT 0,
                records_skipped INTEGER DEFAULT 0,
                total_extracted INTEGER DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_arrest_date ON arrests(arrest_date);
            CREATE INDEX IF NOT EXISTS idx_sex         ON arrests(sex);
            CREATE INDEX IF NOT EXISTS idx_age         ON arrests(age);
        """)

init_db()

# ── PDF Extraction (mirrors existing skill logic) ──────────────────────────────
COL_DATE      = (0,   55)
COL_SEX       = (55,  80)
COL_DOB       = (80,  135)
COL_AGE       = (135, 160)
COL_NAME      = (160, 325)
COL_RESIDENCE = (325, 505)
COL_OFFENSE   = (505, 720)
COL_CASENO    = (720, 800)

def _in_col(word, col):
    return col[0] <= word["x0"] < col[1]

def _words_in_col(row_words, col):
    return " ".join(w["text"] for w in row_words if _in_col(w, col))

def _record_hash(r: dict) -> str:
    key = f"{r['arrest_date']}|{r['name']}|{r['case_no']}|{r['offenses']}"
    return hashlib.md5(key.encode()).hexdigest()

def _finalize(record: dict) -> dict:
    record["offenses"]  = " | ".join(o.strip() for o in record["offenses"] if o.strip())
    record["name"]      = record["name"].strip()
    record["residence"] = record["residence"].strip()
    try:
        record["age"] = int(record["age"])
    except (ValueError, TypeError):
        record["age"] = None
    record["record_hash"] = _record_hash(record)
    return record

def extract_from_pdf(pdf_path: str) -> list[dict]:
    records, current = [], None
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            rows = defaultdict(list)
            for w in page.extract_words():
                rows[round(w["top"], 1)].append(w)

            for top in sorted(rows):
                rw = sorted(rows[top], key=lambda w: w["x0"])
                date_words = [w for w in rw if _in_col(w, COL_DATE)]
                sex_words  = [w for w in rw if _in_col(w, COL_SEX)]

                is_new = (
                    date_words
                    and re.match(r"^\d{1,2}/\d{1,2}/\d{4}$", date_words[0]["text"])
                    and sex_words and sex_words[0]["text"] in ("M", "F")
                )

                if is_new:
                    if current:
                        records.append(_finalize(current))
                    offense_text = _words_in_col(rw, COL_OFFENSE)
                    current = {
                        "arrest_date": date_words[0]["text"],
                        "sex":         sex_words[0]["text"],
                        "dob":         _words_in_col(rw, COL_DOB),
                        "age":         _words_in_col(rw, COL_AGE),
                        "name":        _words_in_col(rw, COL_NAME),
                        "residence":   _words_in_col(rw, COL_RESIDENCE),
                        "offenses":    [offense_text] if offense_text else [],
                        "case_no":     _words_in_col(rw, COL_CASENO),
                    }
                elif current is not None:
                    name_frag = _words_in_col(rw, COL_NAME)
                    res_frag  = _words_in_col(rw, COL_RESIDENCE)
                    off_frag  = _words_in_col(rw, COL_OFFENSE)
                    case_frag = _words_in_col(rw, COL_CASENO)
                    if name_frag: current["name"] += " " + name_frag
                    if res_frag and res_frag not in current["residence"]:
                        current["residence"] += (" " if current["residence"] else "") + res_frag
                    if off_frag: current["offenses"].append(off_frag)
                    if case_frag and not current["case_no"]: current["case_no"] = case_frag

        if current:
            records.append(_finalize(current))
    return records

def insert_records(records: list[dict], filename: str) -> dict:
    added = skipped = 0
    with get_db() as conn:
        for r in records:
            try:
                conn.execute("""
                    INSERT INTO arrests
                        (arrest_date, sex, dob, age, name, residence, offenses, case_no, record_hash)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (r["arrest_date"], r["sex"], r["dob"], r.get("age"),
                      r["name"], r["residence"], r["offenses"], r["case_no"],
                      r["record_hash"]))
                added += 1
            except sqlite3.IntegrityError:
                skipped += 1
        conn.execute("""
            INSERT INTO pdf_imports (filename, records_added, records_skipped, total_extracted)
            VALUES (?, ?, ?, ?)
        """, (filename, added, skipped, len(records)))
    return {"added": added, "skipped": skipped, "total": len(records)}

# ── Background geocoding ───────────────────────────────────────────────────────
_geocode_cache: dict[str, tuple] = {}

def _geocode_nominatim(address: str) -> tuple[float, float] | None:
    if address in ("UNKNOWN", "", None):
        return None
    if address in _geocode_cache:
        return _geocode_cache[address]
    try:
        import urllib.request, urllib.parse
        query = urllib.parse.urlencode({"q": address + ", Prince William County, VA",
                                        "format": "json", "limit": "1"})
        req = urllib.request.Request(
            f"https://nominatim.openstreetmap.org/search?{query}",
            headers={"User-Agent": "PWC-Crime-Dashboard/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        if data:
            result = (float(data[0]["lat"]), float(data[0]["lon"]))
            _geocode_cache[address] = result
            return result
    except Exception:
        pass
    return None

def geocode_background():
    """Geocode up to 50 ungeocoded records per call (respects Nominatim rate limit)."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, residence FROM arrests WHERE lat IS NULL AND residence != 'UNKNOWN' LIMIT 50"
        ).fetchall()
    for row in rows:
        result = _geocode_nominatim(row["residence"])
        if result:
            with get_db() as conn:
                conn.execute("UPDATE arrests SET lat=?, lng=? WHERE id=?",
                             (result[0], result[1], row["id"]))
        time.sleep(1.1)  # Nominatim: max 1 req/sec

# ── API Endpoints ──────────────────────────────────────────────────────────────

@app.post("/api/upload")
async def upload_pdf(background_tasks: BackgroundTasks, file: UploadFile = File(...)):
    """Upload a PWC arrest PDF — extracts and stores all records."""
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF files are accepted.")
    content = await file.read()
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(content)
        tmp_path = tmp.name
    try:
        records = extract_from_pdf(tmp_path)
    except Exception as e:
        os.unlink(tmp_path)
        raise HTTPException(500, f"PDF extraction failed: {e}")
    os.unlink(tmp_path)
    result = insert_records(records, file.filename)
    background_tasks.add_task(geocode_background)
    return {**result, "filename": file.filename}

@app.post("/api/ingest")
async def ingest_json(payload: dict, background_tasks: BackgroundTasks):
    """Accept pre-extracted JSON records (used by the Claude skill)."""
    records = payload.get("records", [])
    if not records:
        raise HTTPException(400, "No records provided.")
    for r in records:
        r["record_hash"] = _record_hash(r)
    result = insert_records(records, payload.get("filename", "skill-ingest"))
    background_tasks.add_task(geocode_background)
    return result

@app.get("/api/arrests")
def get_arrests(
    sex: Optional[str]     = Query(None),
    offense: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str]   = Query(None),
    limit: int = Query(1000, le=5000),
    offset: int = Query(0)
):
    """Return arrest records with optional filters."""
    sql  = "SELECT * FROM arrests WHERE 1=1"
    args = []
    if sex:       sql += " AND sex=?";              args.append(sex.upper())
    if offense:   sql += " AND offenses LIKE ?";    args.append(f"%{offense}%")
    if date_from: sql += " AND arrest_date >= ?";   args.append(date_from)
    if date_to:   sql += " AND arrest_date <= ?";   args.append(date_to)
    sql += " ORDER BY arrest_date DESC LIMIT ? OFFSET ?"
    args += [limit, offset]
    with get_db() as conn:
        rows = conn.execute(sql, args).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM arrests").fetchone()[0]
    return {"total": total, "records": [dict(r) for r in rows]}

@app.get("/api/stats")
def get_stats():
    """Summary statistics for the dashboard."""
    with get_db() as conn:
        total   = conn.execute("SELECT COUNT(*) FROM arrests").fetchone()[0]
        male    = conn.execute("SELECT COUNT(*) FROM arrests WHERE sex='M'").fetchone()[0]
        female  = conn.execute("SELECT COUNT(*) FROM arrests WHERE sex='F'").fetchone()[0]
        avg_age = conn.execute("SELECT AVG(age) FROM arrests WHERE age IS NOT NULL").fetchone()[0]

        # Top offenses (split on " | ")
        offense_rows = conn.execute("SELECT offenses FROM arrests WHERE offenses != ''").fetchall()
        offense_counts: dict[str, int] = defaultdict(int)
        for row in offense_rows:
            for charge in row["offenses"].split(" | "):
                charge = charge.strip()
                if charge:
                    offense_counts[charge] += 1
        top_offenses = sorted(offense_counts.items(), key=lambda x: -x[1])[:15]

        # Arrests by month
        month_rows = conn.execute("""
            SELECT substr(arrest_date, 1, 7) as month, COUNT(*) as cnt
            FROM arrests WHERE arrest_date != ''
            GROUP BY month ORDER BY month
        """).fetchall()

        # Age groups
        age_groups = {
            "Under 18": conn.execute("SELECT COUNT(*) FROM arrests WHERE age < 18").fetchone()[0],
            "18–25":    conn.execute("SELECT COUNT(*) FROM arrests WHERE age BETWEEN 18 AND 25").fetchone()[0],
            "26–35":    conn.execute("SELECT COUNT(*) FROM arrests WHERE age BETWEEN 26 AND 35").fetchone()[0],
            "36–45":    conn.execute("SELECT COUNT(*) FROM arrests WHERE age BETWEEN 36 AND 45").fetchone()[0],
            "46–55":    conn.execute("SELECT COUNT(*) FROM arrests WHERE age BETWEEN 46 AND 55").fetchone()[0],
            "56+":      conn.execute("SELECT COUNT(*) FROM arrests WHERE age >= 56").fetchone()[0],
        }

        # Map data (geocoded records)
        map_rows = conn.execute(
            "SELECT lat, lng, name, arrest_date, offenses FROM arrests WHERE lat IS NOT NULL"
        ).fetchall()

    return {
        "total":        total,
        "male":         male,
        "female":       female,
        "avg_age":      round(avg_age, 1) if avg_age else None,
        "top_offenses": [{"offense": o, "count": c} for o, c in top_offenses],
        "by_month":     [{"month": r["month"], "count": r["cnt"]} for r in month_rows],
        "age_groups":   age_groups,
        "map_points":   [dict(r) for r in map_rows],
    }

@app.get("/api/imports")
def get_imports():
    """Import history — list of PDFs that have been uploaded."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM pdf_imports ORDER BY imported_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]

@app.post("/api/geocode")
async def trigger_geocode(background_tasks: BackgroundTasks):
    """Manually trigger background geocoding of ungeocoded records."""
    with get_db() as conn:
        pending = conn.execute(
            "SELECT COUNT(*) FROM arrests WHERE lat IS NULL AND residence != 'UNKNOWN' AND residence != ''"
        ).fetchone()[0]
    background_tasks.add_task(geocode_background)
    return {"message": f"Geocoding {pending} records in the background."}

# ── Serve frontend ─────────────────────────────────────────────────────────────
STATIC_DIR = BASE_DIR / "static"
STATIC_DIR.mkdir(exist_ok=True)

@app.get("/")
def serve_index():
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    return JSONResponse({"error": "Frontend not found. Make sure static/index.html exists."}, 404)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# ── Run ────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    print(f"\n🚔 PWC Crime Dashboard starting on http://localhost:{port}\n")
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
