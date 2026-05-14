from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
import pandas as pd
import sqlite3
import os
import tempfile
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
    # Read raw xls as html (MoySklad exports xls as HTML table)
    try:
        tables = pd.read_html(file_path, encoding='utf-8')
    except Exception:
        try:
            tables = pd.read_html(file_path, encoding='cp1251')
        except Exception as e:
            raise ValueError(f"Не удалось прочитать файл: {e}")

    if not tables:
        raise ValueError("Файл пустой или не распознан")

    # Find the main table
    df = None
    date_str = None

    for tbl in tables:
        # Look for date row
        for i, row in tbl.iterrows():
            for val in row.values:
                v = str(val).strip()
                if 'на момент:' in v.lower():
                    # date is likely in next cell
                    vals = list(row.values)
                    for j, cell in enumerate(vals):
                        if 'на момент:' in str(cell).lower() and j+1 < len(vals):
                            date_str = str(vals[j+1]).strip()
                            break

        # Find header row with "Наименование"
        for i, row in tbl.iterrows():
            row_vals = [str(v).strip() for v in row.values]
            if any('аименование' in v for v in row_vals):
                # This is the header row, data starts after
                tbl.columns = tbl.iloc[i]
                tbl = tbl.iloc[i+1:].reset_index(drop=True)
                df = tbl
                break
        if df is not None:
            break

    if df is None or date_str is None:
        raise ValueError("Не найдена дата или таблица в файле. Убедитесь что это отчёт остатков из МоегоСклада")

    # Find name and stock columns
    name_col = None
    stock_col = None
    for col in df.columns:
        c = str(col).strip()
        if 'аименование' in c:
            name_col = col
        if 'статок' in c and 'сумм' not in c.lower():
            stock_col = col

    if name_col is None or stock_col is None:
        raise ValueError("Не найдены колонки Наименование/Остаток")

    df = df[[name_col, stock_col]].copy()
    df.columns = ['sku_name', 'stock_qty']
    df = df[df['sku_name'].notna() & (df['sku_name'].astype(str) != 'nan')]
    df = df[df['sku_name'].astype(str).str.strip() != '']
    df['stock_qty'] = pd.to_numeric(df['stock_qty'], errors='coerce').fillna(0)

    try:
        report_date = pd.to_datetime(date_str, dayfirst=True).normalize()
    except Exception:
        raise ValueError(f"Не удалось распознать дату: {date_str}")

    return report_date.strftime('%Y-%m-%d'), df

@app.post("/api/upload")
async def upload_stock(file: UploadFile = File(...)):
    if not file.filename.lower().endswith('.xls'):
        raise HTTPException(400, "Только файлы .xls из МоегоСклада")
    with tempfile.NamedTemporaryFile(suffix='.xls', delete=False) as tmp:
        tmp.write(await file.read()); tmp_path = tmp.name
    try:
        date_str, df = parse_xls(tmp_path)
    except ValueError as e:
        os.unlink(tmp_path); raise HTTPException(400, str(e))
    finally:
        if os.path.exists(tmp_path): os.unlink(tmp_path)
    conn = get_db()
    uploaded_at = datetime.now().isoformat()
    inserted = 0
    for _, row in df.iterrows():
        before = conn.total_changes
        conn.execute("INSERT OR IGNORE INTO stock_snapshots (date,sku_name,stock_qty,uploaded_at) VALUES (?,?,?,?)",
                     (date_str, str(row['sku_name']).strip(), float(row['stock_qty']), uploaded_at))
        if conn.total_changes > before: inserted += 1
    conn.commit(); conn.close()
    return {"date": date_str, "inserted": inserted, "skipped": len(df)-inserted, "total_skus": len(df)}

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
    total_records = conn.execute("SELECT COUNT(*) as c FROM stock_snapshots").fetchone()["c"]
    total_skus = conn.execute("SELECT COUNT(DISTINCT sku_name) as c FROM stock_snapshots").fetchone()["c"]
    total_dates = conn.execute("SELECT COUNT(DISTINCT date) as c FROM stock_snapshots").fetchone()["c"]
    dr = conn.execute("SELECT MIN(date) as mn, MAX(date) as mx FROM stock_snapshots").fetchone()
    conn.close()
    return {"total_records": total_records, "total_skus": total_skus, "total_dates": total_dates,
            "date_from": dr["mn"], "date_to": dr["mx"]}

@app.get("/{full_path:path}")
def serve_frontend(full_path: str):
    if os.path.exists("index.html"):
        return FileResponse("index.html")
    return {"error": "Frontend not found"}

