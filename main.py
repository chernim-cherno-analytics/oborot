from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
import sqlite3, os, tempfile, re as _re
from datetime import datetime
from typing import Optional

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ─── Telegram ─────────────────────────────────────────────────────────────────
TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT  = os.environ.get("TG_CHAT", "")

async def tg_send(text: str):
    import httpx
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(url, json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"})
    except Exception as e:
        print(f"TG error: {e}")













DB_PATH = "/data/stocks.db"

def get_db():
    os.makedirs("/data", exist_ok=True)
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
    conn.execute("""CREATE TABLE IF NOT EXISTS hidden_items (
        sku_base TEXT PRIMARY KEY,
        hidden_at TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS sales_data (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL, sku_name TEXT NOT NULL,
        qty REAL NOT NULL DEFAULT 0, revenue REAL NOT NULL DEFAULT 0,
        doc_type TEXT DEFAULT 'sale',
        UNIQUE(date, sku_name, doc_type))""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sales_date ON sales_data(date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sales_sku ON sales_data(sku_name)")
    conn.execute("""CREATE TABLE IF NOT EXISTS sku_costs (
        sku_base TEXT PRIMARY KEY,
        cost REAL NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS sku_adjustments (
        project_id TEXT NOT NULL DEFAULT '',
        sku_base TEXT NOT NULL,
        qty_adj INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (project_id, sku_base))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS order_excluded (
        project_id TEXT NOT NULL DEFAULT '',
        sku_base TEXT NOT NULL,
        excluded_at TEXT NOT NULL,
        PRIMARY KEY (project_id, sku_base))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS order_added (
        project_id TEXT NOT NULL DEFAULT '',
        sku_base TEXT NOT NULL,
        added_at TEXT NOT NULL,
        PRIMARY KEY (project_id, sku_base))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS projects (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        arrival_date TEXT NOT NULL,
        created_at TEXT NOT NULL)""")
    conn.commit(); conn.close()

init_db()

def _strip_size(n):
    return _re.sub(r'[\s]*\([^)]*\)[\s]*$', '', str(n)).strip()

_analytics_cache = None
_analytics_cache_key = None

def get_analytics_cache_key(conn):
    row = conn.execute("SELECT COUNT(*) as c, MAX(date) as d FROM stock_snapshots").fetchone()
    return f"{row['c']}_{row['d']}"

ANALYTICS_JSON_PATH = "/data/analytics_cache.json"

def build_analytics_data(conn):
    dates = [r[0] for r in conn.execute("SELECT DISTINCT date FROM stock_snapshots ORDER BY date").fetchall()]
    if not dates:
        return {"dates": [], "stock": {}}
    rows = conn.execute("""
        SELECT date, sku_name, SUM(stock_qty) as qty
        FROM stock_snapshots
        GROUP BY date, sku_name
        ORDER BY date, sku_name
    """).fetchall()
    stock = {}
    for r in rows:
        base = _strip_size(r["sku_name"])
        if base not in stock:
            stock[base] = {}
        stock[base][r["date"]] = stock[base].get(r["date"], 0) + r["qty"]
    return {"dates": dates, "stock": stock}

def rebuild_analytics_json(conn):
    """Build analytics data once and write to disk as a static JSON file."""
    import json
    data = build_analytics_data(conn)
    os.makedirs("/data", exist_ok=True)
    tmp = ANALYTICS_JSON_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, ANALYTICS_JSON_PATH)
    # Invalidate turnover cache so it gets recomputed
    if os.path.exists("/data/turnover_cache.json"):
        os.remove("/data/turnover_cache.json")
    print(f"analytics_cache.json rebuilt: {len(data['dates'])} dates, {len(data['stock'])} SKUs")

def parse_xls(file_path):
    import xlrd
    book = xlrd.open_workbook(file_path)
    sheet = book.sheet_by_index(0)
    date_str = None
    header_row = None
    name_col = None
    stock_col = None
    for i in range(min(15, sheet.nrows)):
        row = [str(sheet.cell_value(i, j)).strip() for j in range(sheet.ncols)]
        for j, val in enumerate(row):
            if 'на момент' in val.lower() and j+1 < len(row):
                date_str = row[j+1]
            if 'на момент:' in val.lower():
                parts = val.split(':', 1)
                if len(parts) > 1 and parts[1].strip():
                    date_str = parts[1].strip()
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
    global _analytics_cache, _analytics_cache_key
    _analytics_cache = None
    _analytics_cache_key = None
    conn2 = get_db()
    rebuild_analytics_json(conn2)
    conn2.close()
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

@app.get("/api/stocks/all")
def get_all_stocks():
    conn = get_db()
    rows = conn.execute("""
        SELECT date, sku_name, SUM(stock_qty) as qty
        FROM stock_snapshots
        GROUP BY date, sku_name
        ORDER BY date, sku_name
    """).fetchall()
    conn.close()
    stock = {}
    dates_set = set()
    for r in rows:
        base = _strip_size(r["sku_name"])
        date = r["date"]
        dates_set.add(date)
        if base not in stock:
            stock[base] = {}
        stock[base][date] = stock[base].get(date, 0) + r["qty"]
    return {"dates": sorted(dates_set), "stock": stock}

@app.get("/api/stats")
def get_stats():
    conn = get_db()
    r1 = conn.execute("SELECT COUNT(*) as c FROM stock_snapshots").fetchone()["c"]
    r2 = conn.execute("SELECT COUNT(DISTINCT sku_name) as c FROM stock_snapshots").fetchone()["c"]
    r3 = conn.execute("SELECT COUNT(DISTINCT date) as c FROM stock_snapshots").fetchone()["c"]
    dr = conn.execute("SELECT MIN(date) as mn, MAX(date) as mx FROM stock_snapshots").fetchone()
    conn.close()
    return {"total_records": r1, "total_skus": r2, "total_dates": r3, "date_from": dr["mn"], "date_to": dr["mx"]}

@app.post("/api/hide")
async def hide_item(data: dict):
    conn = get_db()
    conn.execute("INSERT OR IGNORE INTO hidden_items (sku_base, hidden_at) VALUES (?,?)",
                 (data["sku_base"], datetime.now().isoformat()))
    conn.commit(); conn.close()
    return {"ok": True}

@app.delete("/api/hide/{sku_base}")
def unhide_item(sku_base: str):
    conn = get_db()
    conn.execute("DELETE FROM hidden_items WHERE sku_base=?", (sku_base,))
    conn.commit(); conn.close()
    return {"ok": True}

@app.get("/api/hidden")
def get_hidden():
    conn = get_db()
    rows = conn.execute("SELECT sku_base FROM hidden_items ORDER BY hidden_at DESC").fetchall()
    conn.close()
    return [r["sku_base"] for r in rows]

@app.post("/api/upload-sales")
async def upload_sales(file: UploadFile = File(...)):
    import csv, io
    if not file.filename.lower().endswith(".csv"):
        raise HTTPException(400, "Только CSV файлы")
    content_bytes = await file.read()
    try:
        text = content_bytes.decode("utf-8-sig")
    except:
        text = content_bytes.decode("cp1251")

    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)

    if not rows:
        raise HTTPException(400, "Файл пустой")

    is_return = "возврат" in file.filename.lower() or "возврат" in (rows[0].get("Документ","")).lower()
    doc_type = "return" if is_return else "sale"

    import pandas as pd
    df = pd.DataFrame(rows)

    if "Артикул" in df.columns:
        df = df[df["Артикул"].notna() & (df["Артикул"].astype(str).str.strip() != "")]

    df["Дата"] = pd.to_datetime(df["Дата документа"], dayfirst=True).dt.date
    df["Количество"] = pd.to_numeric(df["Количество"], errors="coerce").fillna(0)
    df["Сумма"] = pd.to_numeric(df["Сумма"], errors="coerce").fillna(0)

    conn = get_db()
    inserted = 0
    for _, row in df.iterrows():
        try:
            before = conn.total_changes
            conn.execute(
                "INSERT OR REPLACE INTO sales_data (date, sku_name, qty, revenue, doc_type) VALUES (?,?,?,?,?)",
                (str(row["Дата"]), str(row["Наименование"]).strip(), float(row["Количество"]), float(row["Сумма"]), doc_type)
            )
            if conn.total_changes > before: inserted += 1
        except: pass
    conn.commit()
    date_from = str(df["Дата"].min())
    date_to = str(df["Дата"].max())
    conn.close()
    return {"inserted": inserted, "doc_type": doc_type, "date_from": date_from, "date_to": date_to}

@app.post("/api/check-bestsellers")
async def check_bestsellers():
    """Проверяет бестселлеры (turn >= 2000) у которых запас < 45 дней и шлёт пуш в Telegram."""
    import httpx
    conn = get_db()
    # Берём последние остатки по каждому SKU
    rows = conn.execute("""
        SELECT sku_name, stock_qty
        FROM stock_snapshots
        WHERE date = (SELECT MAX(date) FROM stock_snapshots)
    """).fetchall()
    # Берём продажи за последние 90 дней для расчёта дневных продаж
    sales = conn.execute("""
        SELECT sku_name, SUM(qty) as total_qty
        FROM sales_data
        WHERE doc_type = 'sale'
          AND date >= date('now', '-90 days')
        GROUP BY sku_name
    """).fetchall()
    conn.close()

    sales_map = {_strip_size(r["sku_name"]): r["total_qty"] for r in sales}

    alerts = []
    for r in rows:
        base = _strip_size(r["sku_name"])
        stock = r["stock_qty"]
        if stock <= 0:
            continue
        qty_90 = sales_map.get(base, 0)
        if qty_90 <= 0:
            continue
        daily = qty_90 / 90
        turn_rub = daily  # используем штуки/день для сравнения
        days_left = stock / daily
        # Только бестселлеры (продаётся хотя бы 1 шт в 2 дня) у которых < 45 дней запаса
        if daily >= 0.5 and days_left < 45:
            order_90 = max(0, round(daily * 90 - stock))
            alerts.append({
                "name": base,
                "stock": round(stock),
                "days_left": round(days_left),
                "daily": round(daily, 1),
                "order_90": order_90,
            })

    if not alerts:
        return {"sent": 0, "message": "Всё в порядке, критических остатков нет"}

    alerts.sort(key=lambda x: x["days_left"])
    lines = ["🚨 <b>Заканчиваются бестселлеры!</b>\n"]
    for a in alerts:
        lines.append(
            f"📦 <b>{a['name']}</b>\n"
            f"   Остаток: {a['stock']} шт · закончится через <b>{a['days_left']} дн.</b>\n"
            f"   К заказу на 90 дней: <b>{a['order_90']} шт</b>\n"
        )
    await tg_send("\n".join(lines))
    return {"sent": len(alerts), "alerts": alerts}


@app.get("/order")
def serve_order():
    if os.path.exists("order.html"):
        return FileResponse("order.html", media_type="text/html")
    return FileResponse("index.html", media_type="text/html")

@app.get("/turnover")
def serve_turnover():
    if os.path.exists("turnover.html"):
        return FileResponse("turnover.html", media_type="text/html")
    return FileResponse("index.html", media_type="text/html")

@app.get("/api/analytics-data")
def get_analytics_data():
    """Serve pre-built analytics JSON from disk. If missing, build it first."""
    if os.path.exists(ANALYTICS_JSON_PATH):
        return FileResponse(ANALYTICS_JSON_PATH, media_type="application/json")
    # First run: build the file
    conn = get_db()
    rebuild_analytics_json(conn)
    conn.close()
    if os.path.exists(ANALYTICS_JSON_PATH):
        return FileResponse(ANALYTICS_JSON_PATH, media_type="application/json")
    # Fallback: return empty
    return {"dates": [], "stock": {}}

@app.post("/api/rebuild-analytics")
def trigger_rebuild():
    """Manually trigger analytics rebuild (admin use)."""
    conn = get_db()
    rebuild_analytics_json(conn)
    conn.close()
    return {"ok": True}

@app.post("/api/invalidate-cache")
def invalidate_cache():
    global _analytics_cache, _analytics_cache_key
    _analytics_cache = None
    _analytics_cache_key = None
    # Also remove disk cache so next request rebuilds it
    if os.path.exists(ANALYTICS_JSON_PATH):
        os.remove(ANALYTICS_JSON_PATH)
    return {"ok": True}

@app.get("/analytics")
def serve_analytics():
    if os.path.exists("analytics.html"):
        return FileResponse("analytics.html", media_type="text/html")
    return FileResponse("index.html", media_type="text/html")


# ─── Costs ────────────────────────────────────────────────────────────────────
@app.get("/api/costs")
def get_costs():
    conn = get_db()
    rows = conn.execute("SELECT sku_base, cost FROM sku_costs").fetchall()
    conn.close()
    return {r["sku_base"]: r["cost"] for r in rows}

@app.post("/api/costs")
async def set_cost(data: dict):
    conn = get_db()
    conn.execute("INSERT OR REPLACE INTO sku_costs (sku_base, cost, updated_at) VALUES (?,?,?)",
                 (data["sku_base"], float(data.get("cost", 0)), datetime.now().isoformat()))
    conn.commit(); conn.close()
    return {"ok": True}

# ─── Adjustments ──────────────────────────────────────────────────────────────
@app.get("/api/adjustments")
def get_adjustments(project_id: str = ""):
    conn = get_db()
    rows = conn.execute("SELECT sku_base, qty_adj FROM sku_adjustments WHERE project_id=?", (project_id,)).fetchall()
    conn.close()
    return {r["sku_base"]: r["qty_adj"] for r in rows}

@app.post("/api/adjustments")
async def set_adjustment(data: dict):
    conn = get_db()
    adj = int(data.get("qty_adj", 0))
    pid = data.get("project_id", "")
    if adj == 0:
        conn.execute("DELETE FROM sku_adjustments WHERE project_id=? AND sku_base=?", (pid, data["sku_base"]))
    else:
        conn.execute("INSERT OR REPLACE INTO sku_adjustments (project_id, sku_base, qty_adj, updated_at) VALUES (?,?,?,?)",
                     (pid, data["sku_base"], adj, datetime.now().isoformat()))
    conn.commit(); conn.close()
    return {"ok": True}

# ─── Order excluded ───────────────────────────────────────────────────────────
@app.get("/api/excluded")
def get_excluded(project_id: str = ""):
    conn = get_db()
    rows = conn.execute("SELECT sku_base FROM order_excluded WHERE project_id=?", (project_id,)).fetchall()
    conn.close()
    return [r["sku_base"] for r in rows]

@app.post("/api/excluded")
async def add_excluded(data: dict):
    conn = get_db()
    pid = data.get("project_id", "")
    conn.execute("INSERT OR IGNORE INTO order_excluded (project_id, sku_base, excluded_at) VALUES (?,?,?)",
                 (pid, data["sku_base"], datetime.now().isoformat()))
    conn.commit(); conn.close()
    return {"ok": True}

@app.delete("/api/excluded/{sku_base}")
def remove_excluded(sku_base: str, project_id: str = ""):
    conn = get_db()
    conn.execute("DELETE FROM order_excluded WHERE project_id=? AND sku_base=?", (project_id, sku_base))
    conn.commit(); conn.close()
    return {"ok": True}

# ─── Order added (possible→active) ───────────────────────────────────────────
@app.get("/api/order-added")
def get_order_added(project_id: str = ""):
    conn = get_db()
    rows = conn.execute("SELECT sku_base FROM order_added WHERE project_id=?", (project_id,)).fetchall()
    conn.close()
    return [r["sku_base"] for r in rows]

@app.post("/api/order-added")
async def add_order_added(data: dict):
    conn = get_db()
    pid = data.get("project_id", "")
    conn.execute("INSERT OR IGNORE INTO order_added (project_id, sku_base, added_at) VALUES (?,?,?)",
                 (pid, data["sku_base"], datetime.now().isoformat()))
    conn.commit(); conn.close()
    return {"ok": True}

# ─── Projects ─────────────────────────────────────────────────────────────────
@app.get("/api/projects")
def list_projects():
    conn = get_db()
    rows = conn.execute("SELECT id, name, arrival_date, created_at FROM projects ORDER BY created_at DESC").fetchall()
    conn.close()
    return [{"id": r["id"], "name": r["name"], "date": r["arrival_date"]} for r in rows]

@app.post("/api/projects")
async def upsert_project(data: dict):
    import uuid as _uuid
    conn = get_db()
    pid = data.get("id") or _uuid.uuid4().hex[:8]
    conn.execute("INSERT OR REPLACE INTO projects (id, name, arrival_date, created_at) VALUES (?,?,?,?)",
                 (pid, data["name"], data["date"], datetime.now().isoformat()))
    conn.commit(); conn.close()
    return {"id": pid, "name": data["name"], "date": data["date"]}

@app.delete("/api/projects/{project_id}")
def delete_project(project_id: str):
    conn = get_db()
    for tbl in ("sku_adjustments", "order_excluded", "order_added"):
        conn.execute(f"DELETE FROM {tbl} WHERE project_id=?", (project_id,))
    conn.execute("DELETE FROM projects WHERE id=?", (project_id,))
    conn.commit(); conn.close()
    return {"ok": True}


# ─── Turnover data: pre-computed per-SKU stats (fast) ────────────────────────
TURNOVER_JSON_PATH = "/data/turnover_cache.json"

@app.get("/api/turnover-data")
def get_turnover_data():
    """Serve analytics cache for turnover page — same data as /api/analytics-data."""
    if os.path.exists(ANALYTICS_JSON_PATH):
        return FileResponse(ANALYTICS_JSON_PATH, media_type="application/json")
    conn = get_db()
    rebuild_analytics_json(conn)
    conn.close()
    if os.path.exists(ANALYTICS_JSON_PATH):
        return FileResponse(ANALYTICS_JSON_PATH, media_type="application/json")
    return {"dates": [], "stock": {}}

@app.get("/{full_path:path}")
def serve_frontend(full_path: str):
    if os.path.exists("index.html"):
        return FileResponse("index.html", media_type="text/html")
    return {"error": "not found"}
