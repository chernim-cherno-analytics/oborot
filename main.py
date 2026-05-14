from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
import sqlite3, os, tempfile
from datetime import datetime
from typing import Optional

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
DB_PATH = "data/stocks.db"

def get_db():
    os.makedirs("data", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.execute("""CREATE TABLE IF NOT EXISTS stock_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL, sku_name TEXT NOT NULL,
        stock_qty REAL NOT NULL DEFAULT 0, uploaded_at TEXT NOT NULL,
        UNIQUE(date, sku_name))""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_date ON stock_snapshots(date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sku ON stock_snapshots(sku_name)")
    conn.commit(); conn.close()

init_db()

def parse_xls(file_path):
    import xlrd
    book = xlrd.open_workbook(file_path)
    sheet = book.sheet_by_index(0)
    
    date_str = None
    header_row = None
    name_col = None
    stock_col = None
    
    # Find date and header
    for i in range(min(15, sheet.nrows)):
        row = [str(sheet.cell_value(i, j)).strip() for j in range(sheet.ncols)]
        # Find date
        for j, val in enumerate(row):
            if 'на момент' in val.lower() and j+1 < len(row):
                date_str = row[j+1]
            # Also check if date is in same cell after colon
            if 'на момент:' in val.lower():
                parts = val.split(':', 1)
                if len(parts) > 1 and parts[1].strip():
                    date_str = parts[1].strip()
        # Find header row
        if any('аименование' in v for v in row):
            header_row = i
            for j, v in enumerate(row):
                if 'аименование' in v: name_col = j
                if 'статок' in v and 'умм' not in v.lower(): stock_col = j
            break
    
    if not date_str:
        raise ValueError("Не найдена дата в файле")
    if header_row is None or name_col is None or stock_col is None:
        raise ValueError("Не найдена таблица с остатками")
    
    import pandas as pd
    report_date = pd.to_datetime(date_str.split()[0], dayfirst=True).normalize()
    
    rows = []
    for i in range(header_row + 1, sheet.nrows):
        name = str(sheet.cell_value(i, name_col)).strip()
        if not name or name == 'nan' or name == 'Наименование': continue
        try:
            qty = float(sheet.cell_value(i, stock_col))
        except:
            qty = 0.0
        rows.append({'sku_name': name, 'stock_qty': qty})
    
    if not rows:
        raise ValueError("Таблица пустая")
    
    return report_date.strftime('%Y-%m-%d'), rows

@app.post("/api/upload")
async def upload_stock(file: UploadFile = File(...)):
    if not file.filename.lower().endswith('.xls'):
        raise HTTPException(400, "Только файлы .xls из МоегоСклада")
    with tempfile.NamedTemporaryFile(suffix='.xls', delete=False) as tmp:
        tmp.write(await file.read()); tmp_path = tmp.name
    try:
        date_str, rows = parse_xls(tmp_path)
    except Exception as e:
        os.unlink(tmp_path); raise HTTPException(400, str(e))
    finally:
        if os.path.exists(tmp_path): os.unlink(tmp_path)
    conn = get_db()
    uploaded_at = datetime.now().isoformat()
    inserted = 0
    for row in rows:
        before = conn.total_changes
        conn.execute("INSERT OR IGNORE INTO stock_snapshots (date,sku_name,stock_qty,uploaded_at) VALUES (?,?,?,?)",
                     (date_str, row['sku_name'], row['stock_qty'], uploaded_at))
        if conn.total_changes > before: inserted += 1
    conn.commit(); conn.close()
    return {"date": date_str, "inserted": inserted, "skipped": len(rows)-inserted, "total_skus": len(rows)}

@app.get("/api/dates")
def get_dates():
    conn = get_db()
    rows = conn.execute("SELECT date, COUNT(DISTINCT sku_name) as sku_count FROM stock_snapshots GROUP BY date ORDER BY date DESC").fetchall()
    conn.close()
    return [{"date": r["date"], "sku_count": r["sku_count"]} for r in rows]

@app.get("/api/stocks")
def get_stocks(date: Optional[str]=None, search: Optional[str]=None, page: int=1, per_page: int=50):
    conn = get_db()
    if not date:
        row = conn.execute("SELECT MAX(date) as d FROM stock_snapshots").fetchone()
        date = row["d"]
    if not date:
        return {"date": None, "items": [], "total": 0, "pages": 0}
    cond = ["date = ?"]; params = [date]
    if search:
        cond.append("LOWER(sku_name) LIKE ?"); params.append(f"%{search.lower()}%")
    where = " AND ".join(cond)
    total = conn.execute(f"SELECT COUNT(*) as c FROM stock_snapshots WHERE {where}", params).fetchone()["c"]
    rows = conn.execute(f"SELECT sku_name, stock_qty FROM stock_snapshots WHERE {where} ORDER BY sku_name LIMIT ? OFFSET ?",
                        params+[per_page,(page-1)*per_page]).fetchall()
    conn.close()
    return {"date": date, "items": [{"sku_name": r["sku_name"], "stock_qty": r["stock_qty"]} for r in rows],
            "total": total, "pages": -(-total//per_page)}

@app.get("/api/stats")
def get_stats():
    conn = get_db()
    r1 = conn.execute("SELECT COUNT(*) as c FROM stock_snapshots").fetchone()["c"]
    r2 = conn.execute("SELECT COUNT(DISTINCT sku_name) as c FROM stock_snapshots").fetchone()["c"]
    r3 = conn.execute("SELECT COUNT(DISTINCT date) as c FROM stock_snapshots").fetchone()["c"]
    dr = conn.execute("SELECT MIN(date) as mn, MAX(date) as mx FROM stock_snapshots").fetchone()
    conn.close()
    return {"total_records": r1, "total_skus": r2, "total_dates": r3, "date_from": dr["mn"], "date_to": dr["mx"]}

@app.get("/{full_path:path}")
def serve_frontend(full_path: str):
    if os.path.exists("index.html"):
        return FileResponse("index.html")
    return {"error": "not found"}

