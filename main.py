from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import pandas as pd
import sqlite3
import os
import subprocess
import tempfile
from datetime import datetime
from typing import Optional

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=[”*”], allow_methods=[”*”], allow_headers=[”*”])

DB_PATH = “data/stocks.db”

def get_db():
os.makedirs(“data”, exist_ok=True)
conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
return conn

def init_db():
conn = get_db()
conn.execute(””“CREATE TABLE IF NOT EXISTS stock_snapshots (
id INTEGER PRIMARY KEY AUTOINCREMENT,
date TEXT NOT NULL, sku_name TEXT NOT NULL,
stock_qty REAL NOT NULL DEFAULT 0, uploaded_at TEXT NOT NULL,
UNIQUE(date, sku_name))”””)
conn.execute(“CREATE INDEX IF NOT EXISTS idx_date ON stock_snapshots(date)”)
conn.execute(“CREATE INDEX IF NOT EXISTS idx_sku ON stock_snapshots(sku_name)”)
conn.commit(); conn.close()

init_db()

def parse_xls(file_path):
with tempfile.TemporaryDirectory() as tmpdir:
subprocess.run([‘libreoffice’,’–headless’,’–convert-to’,‘csv’,’–outdir’,tmpdir,file_path],
capture_output=True, timeout=60)
csv_name = os.path.basename(file_path).replace(’.xls’,’.csv’).replace(’.XLS’,’.csv’)
csv_path = os.path.join(tmpdir, csv_name)
if not os.path.exists(csv_path):
raise ValueError(“Не удалось сконвертировать файл”)
raw = pd.read_csv(csv_path, nrows=8, header=None, on_bad_lines=‘skip’)
date_str = None
for _, row in raw.iterrows():
if str(row.get(2,’’)).strip() == ‘на момент:’:
date_str = str(row.get(3,’’)).strip(); break
if not date_str:
raise ValueError(“Не найдена дата. Убедитесь что это отчёт остатков из МоегоСклада”)
report_date = pd.to_datetime(date_str, dayfirst=True).normalize()
df = pd.read_csv(csv_path, skiprows=9, header=None, on_bad_lines=‘skip’)
df.columns = [‘drop’,‘Код’,‘Артикул’,‘Наименование’,‘Ед_изм’,‘Доступно’,‘Резерв’,
‘Ожидание’,‘Остаток’,‘Себестоимость’,‘Сумма_себест’,‘Цена_продажи’,‘Сумма_продажи’,‘Дней_на_складе’]
df = df[df[‘Артикул’].notna() & (df[‘Артикул’].astype(str) != ‘Артикул’)]
df[‘Остаток’] = pd.to_numeric(df[‘Остаток’], errors=‘coerce’).fillna(0)
df[‘date’] = report_date.strftime(’%Y-%m-%d’)
return report_date.strftime(’%Y-%m-%d’), df[[‘Наименование’,‘Остаток’,‘date’]]

@app.post(”/api/upload”)
async def upload_stock(file: UploadFile = File(…)):
if not file.filename.lower().endswith(’.xls’):
raise HTTPException(400, “Только файлы .xls из МоегоСклада”)
with tempfile.NamedTemporaryFile(suffix=’.xls’, delete=False) as tmp:
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
conn.execute(“INSERT OR IGNORE INTO stock_snapshots (date,sku_name,stock_qty,uploaded_at) VALUES (?,?,?,?)”,
(date_str, str(row[‘Наименование’]), float(row[‘Остаток’]), uploaded_at))
if conn.total_changes > before: inserted += 1
conn.commit(); conn.close()
return {“date”: date_str, “inserted”: inserted, “skipped”: len(df)-inserted, “total_skus”: len(df)}

@app.get(”/api/dates”)
def get_dates():
conn = get_db()
rows = conn.execute(“SELECT date, COUNT(DISTINCT sku_name) as sku_count FROM stock_snapshots GROUP BY date ORDER BY date DESC”).fetchall()
conn.close()
return [{“date”: r[“date”], “sku_count”: r[“sku_count”]} for r in rows]

@app.get(”/api/stocks”)
def get_stocks(date: Optional[str]=None, search: Optional[str]=None, page: int=1, per_page: int=50):
conn = get_db()
if not date:
row = conn.execute(“SELECT MAX(date) as d FROM stock_snapshots”).fetchone()
date = row[“d”]
if not date:
return {“date”: None, “items”: [], “total”: 0, “pages”: 0}
cond = [“date = ?”]; params = [date]
if search:
cond.append(“LOWER(sku_name) LIKE ?”); params.append(f”%{search.lower()}%”)
where = “ AND “.join(cond)
total = conn.execute(f”SELECT COUNT(*) as c FROM stock_snapshots WHERE {where}”, params).fetchone()[“c”]
rows = conn.execute(f”SELECT sku_name, stock_qty FROM stock_snapshots WHERE {where} ORDER BY sku_name LIMIT ? OFFSET ?”,
params+[per_page,(page-1)*per_page]).fetchall()
conn.close()
return {“date”: date, “items”: [{“sku_name”: r[“sku_name”], “stock_qty”: r[“stock_qty”]} for r in rows],
“total”: total, “pages”: -(-total//per_page)}

@app.get(”/api/stats”)
def get_stats():
conn = get_db()
total_records = conn.execute(“SELECT COUNT(*) as c FROM stock_snapshots”).fetchone()[“c”]
total_skus = conn.execute(“SELECT COUNT(DISTINCT sku_name) as c FROM stock_snapshots”).fetchone()[“c”]
total_dates = conn.execute(“SELECT COUNT(DISTINCT date) as c FROM stock_snapshots”).fetchone()[“c”]
dr = conn.execute(“SELECT MIN(date) as mn, MAX(date) as mx FROM stock_snapshots”).fetchone()
conn.close()
return {“total_records”: total_records, “total_skus”: total_skus, “total_dates”: total_dates,
“date_from”: dr[“mn”], “date_to”: dr[“mx”]}

if os.path.exists(“frontend/dist”):
app.mount(”/assets”, StaticFiles(directory=“frontend/dist/assets”), name=“assets”)
@app.get(”/{full_path:path}”)
def serve_frontend(full_path: str):
return FileResponse(“frontend/dist/index.html”)
