# -*- coding: utf-8 -*-
"""
sync.py — автосинк данных МойСклад (PostgreSQL, выгрузка LensSklad) → SQLite сайта.

Пишет в те же таблицы и в том же формате, что ручные загрузчики:
  - stock_snapshots (date, sku_name, stock_qty)  — снапшот остатков на сегодня
  - sales_data (date, sku_name, qty, revenue, doc_type)  — продажи/возвраты по дням

Ничего в существующей логике сайта не меняет. Идемпотентен: повторный запуск
за тот же день даёт тот же результат (REPLACE / delete+insert).

Перед первым боевым запуском:
  1. Дождаться окончания первичной выгрузки LensSklad.
  2. GET /api/sync-inspect  — посмотреть реальные имена таблиц/колонок в PG
     и при расхождении поправить блок SCHEMA ниже.
  3. POST /api/sync-now?dry=1  — прогон без записи, вернёт сверку.
  4. POST /api/sync-now        — боевой прогон, затем сверить цифры на сайте.
"""

import os
import json
import sqlite3
from datetime import date, datetime, timedelta

PG_URL = os.environ.get("PG_URL", "")
DB_PATH = "/data/stocks.db"

# ── SCHEMA: имена таблиц/колонок в PG (префикс LensSklad = "len") ─────────────
# LensSklad выгружает сущности "практически как в API МойСклад".
# Если /api/sync-inspect покажет другие имена — правьте только этот блок.
# Имена таблиц сверены с вкладкой «Таблицы» LensSklad 09.07.2026:
# lendemand / lendemand_position, lenretaildemand / lenretaildemand_position,
# lensalesreturn / lensalesreturn_position, lenreport_stock_bystore,
# lenproduct, lenvariant, lencustomerorder, lenstore, lenmove...
# Имена КОЛОНОК — проверить через /api/sync-inspect после окончания выгрузки.
SCHEMA = {
    "stock_table":   "lenreport_stock_bystore",   # остатки по складам (сверено)
    "stock_name":    "name",                      # колонка с названием — проверить в inspect
    "stock_qty":     "stock",                     # колонка с количеством — проверить в inspect
    "demand":        "lendemand",                 # отгрузки, шапки (сверено)
    "retaildemand":  "lenretaildemand",           # розничные продажи, шапки (сверено)
    "salesreturn":   "lensalesreturn",            # возвраты, шапки (сверено)
    "positions_suffix": "_position",              # позиции: lendemand_position (сверено)
    "pos_parent_id": "{parent}_id",               # FK позиции на шапку — проверить в inspect
    "pos_assortment":"assortment_name",           # название позиции — проверить в inspect
    "moment":        "moment",                    # дата документа в шапке
    "qty":           "quantity",
    "price":         "price",                     # в КОПЕЙКАХ (API МойСклад)
    "discount":      "discount",                  # % скидки на позицию
}

SALES_DAYS_BACK = int(os.environ.get("SYNC_DAYS_BACK", "3"))
MIN_STOCK_ROWS = 10   # защита: не пишем снапшот, если в PG подозрительно пусто


# ── подключения ───────────────────────────────────────────────────────────────

def get_pg():
    import psycopg2
    import psycopg2.extras
    conn = psycopg2.connect(PG_URL, sslmode="prefer", connect_timeout=15)
    conn.set_session(readonly=True)   # из PG только читаем — гарантия безопасности
    return conn


def get_sqlite():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ── инспектор схемы PG (для первичной настройки) ─────────────────────────────

def inspect_schema(prefix: str = "len"):
    """Список таблиц len_* и их колонок — чтобы выверить SCHEMA."""
    pg = get_pg()
    cur = pg.cursor()
    cur.execute("""
        SELECT table_name, column_name, data_type
        FROM information_schema.columns
        WHERE table_schema='public' AND table_name LIKE %s
        ORDER BY table_name, ordinal_position
    """, (prefix + "%",))
    out = {}
    for t, c, dt in cur.fetchall():
        out.setdefault(t, []).append(f"{c} ({dt})")
    cur.execute("""
        SELECT relname, n_live_tup FROM pg_stat_user_tables
        WHERE relname LIKE %s ORDER BY n_live_tup DESC
    """, (prefix + "%",))
    counts = {r[0]: r[1] for r in cur.fetchall()}
    pg.close()
    return {"tables": out, "row_counts": counts}


# ── остатки: снапшот на сегодня ───────────────────────────────────────────────

STORE_FILTERS = [x.strip().lower() for x in os.environ.get(
    "STORES", "мясницк,горохов,интернет").split(",") if x.strip()]


def _init_bystore_table(lite):
    lite.execute("""CREATE TABLE IF NOT EXISTS stock_bystore (
        date TEXT NOT NULL, store TEXT NOT NULL, sku_name TEXT NOT NULL,
        qty REAL NOT NULL DEFAULT 0,
        PRIMARY KEY (date, store, sku_name))""")
    lite.execute("CREATE INDEX IF NOT EXISTS idx_bs_sku ON stock_bystore(sku_name)")


def sync_stock(dry: bool = False):
    """Снапшот остатков ТОЛЬКО по нужным складам (как на сайте):
    суммарно — в stock_snapshots, по складам — в stock_bystore (хронология)."""
    pg = get_pg()
    cur = pg.cursor()
    # r.id = id модификации/товара; имя через lenvariant/lenproduct
    cur.execute("""SELECT COALESCE(v.name, p.name, pp.name) AS sku,
                          st.name AS store, SUM(COALESCE(r.stock,0))
                   FROM lenreport_stock_bystore r
                   JOIN lenstore st ON st.id = r.store_id
                   LEFT JOIN lenvariant v ON v.id = r.id
                   LEFT JOIN lenproduct p ON p.id = r.id
                   LEFT JOIN lenproduct pp ON pp.id = r.product_id
                   WHERE COALESCE(v.name, p.name, pp.name) IS NOT NULL
                   GROUP BY 1, 2""")
    rows = [(str(n), str(st), float(q or 0)) for n, st, q in cur.fetchall()
            if n and st and any(f in str(st).lower() for f in STORE_FILTERS)]
    pg.close()

    if len(rows) < MIN_STOCK_ROWS:
        raise RuntimeError(f"Остатки в PG подозрительно пусты ({len(rows)} строк) — снапшот не записан")

    totals = {}
    for n, st, q in rows:
        totals[n] = totals.get(n, 0.0) + q

    today = date.today().isoformat()
    now = datetime.now().isoformat()
    if dry:
        return {"stock_skus": len(totals), "stock_total_qty": sum(totals.values()),
                "stores": sorted({st for _, st, _ in rows}), "date": today, "dry": True}

    lite = get_sqlite()
    _init_bystore_table(lite)
    lite.executemany(
        "INSERT OR REPLACE INTO stock_snapshots (date, sku_name, stock_qty, uploaded_at) "
        "VALUES (?, ?, ?, ?)",
        [(today, n, q, now) for n, q in totals.items()]
    )
    lite.executemany(
        "INSERT OR REPLACE INTO stock_bystore (date, store, sku_name, qty) "
        "VALUES (?, ?, ?, ?)",
        [(today, st, n, q) for n, st, q in rows]
    )
    lite.commit(); lite.close()
    return {"stock_skus": len(totals), "bystore_rows": len(rows), "date": today}


# ── продажи/возвраты за последние N дней ─────────────────────────────────────

def _fetch_docs(pg, head_table: str, days_back: int):
    """Позиции документов, агрегированные по (день, имя SKU).
    Выручка = price*quantity*(1-discount/100)/100  (копейки → рубли)."""
    s = SCHEMA
    parent = head_table.replace("len", "", 1)
    pos_table = head_table + s["positions_suffix"]
    fk = s["pos_parent_id"].format(parent=parent)
    cutoff = (date.today() - timedelta(days=days_back)).isoformat()
    cur = pg.cursor()
    cur.execute(f"""
        SELECT (h.{s['moment']})::date AS d,
               p.{s['pos_assortment']} AS sku,
               SUM(p.{s['qty']}) AS qty,
               SUM(p.{s['price']} * p.{s['qty']} * (1 - COALESCE(p.{s['discount']},0)/100.0)) / 100.0 AS rev
        FROM {pos_table} p
        JOIN {head_table} h ON h.id = p.{fk}
        WHERE (h.{s['moment']})::date >= %s
        GROUP BY 1, 2
    """, (cutoff,))
    rows = cur.fetchall()
    return [(r[0].isoformat(), str(r[1]), float(r[2] or 0), float(r[3] or 0)) for r in rows if r[1]]


def sync_sales(dry: bool = False, days_back: int = SALES_DAYS_BACK):
    pg = get_pg()
    sales = _fetch_docs(pg, SCHEMA["demand"], days_back)
    try:
        sales += _fetch_docs(pg, SCHEMA["retaildemand"], days_back)
    except Exception:
        pg.rollback()   # розницы может не быть — не критично
    try:
        returns = _fetch_docs(pg, SCHEMA["salesreturn"], days_back)
    except Exception:
        pg.rollback()
        returns = []
    pg.close()

    # схлопываем demand+retaildemand по (день, SKU)
    agg = {}
    for d, sku, qty, rev in sales:
        k = (d, sku)
        cur_q, cur_r = agg.get(k, (0.0, 0.0))
        agg[k] = (cur_q + qty, cur_r + rev)

    dates_sales = sorted({d for d, _ in agg})
    dates_ret = sorted({d for d, _, _, _ in returns})

    if dry:
        return {
            "sales_days": dates_sales, "sales_rows": len(agg),
            "sales_revenue": round(sum(r for _, r in agg.values()), 2),
            "returns_days": dates_ret, "returns_rows": len(returns),
            "returns_revenue": round(sum(r[3] for r in returns), 2),
            "dry": True,
        }

    lite = get_sqlite()
    # тот же механизм, что в ручном загрузчике: delete по датам → insert
    for d in dates_sales:
        lite.execute("DELETE FROM sales_data WHERE date=? AND doc_type='sale'", (d,))
    lite.executemany(
        "INSERT OR REPLACE INTO sales_data (date, sku_name, qty, revenue, doc_type) "
        "VALUES (?, ?, ?, ?, 'sale')",
        [(d, sku, q, r) for (d, sku), (q, r) in agg.items()]
    )
    for d in dates_ret:
        lite.execute("DELETE FROM sales_data WHERE date=? AND doc_type='return'", (d,))
    lite.executemany(
        "INSERT OR REPLACE INTO sales_data (date, sku_name, qty, revenue, doc_type) "
        "VALUES (?, ?, ?, ?, 'return')",
        [(d, sku, q, r) for d, sku, q, r in returns]
    )
    lite.commit(); lite.close()
    return {"sales_days": dates_sales, "sales_rows": len(agg),
            "returns_days": dates_ret, "returns_rows": len(returns)}


# ── сверка имён SKU (этап проверки перед включением) ─────────────────────────

def verify_names(limit: int = 50):
    """Имена из PG, которых нет в stock_snapshots сайта (кандидаты в SKU_ALIASES)."""
    s = SCHEMA
    pg = get_pg()
    cur = pg.cursor()
    cur.execute(f"SELECT DISTINCT {s['stock_name']} FROM {s['stock_table']}")
    pg_names = {str(r[0]) for r in cur.fetchall() if r[0]}
    pg.close()

    lite = get_sqlite()
    site_names = {r[0] for r in lite.execute("SELECT DISTINCT sku_name FROM stock_snapshots")}
    lite.close()

    only_pg = sorted(pg_names - site_names)[:limit]
    only_site = sorted(site_names - pg_names)[:limit]
    return {"pg_total": len(pg_names), "site_total": len(site_names),
            "matched": len(pg_names & site_names),
            "only_in_pg": only_pg, "only_on_site": only_site}


# ── общий запуск ─────────────────────────────────────────────────────────────

def sync_all(dry: bool = False):
    started = datetime.now().isoformat(timespec="seconds")
    result = {"started": started}
    try:
        result["stock"] = sync_stock(dry=dry)
        result["sales"] = sync_sales(dry=dry)
        if not dry:
            import main as site
            conn = site.get_db()
            site.rebuild_analytics_json(conn)
            conn.close()
            result["caches"] = "rebuilt"
        result["ok"] = True
        _notify(f"✅ Синк ОК {started}\n"
                f"Остатки: {result['stock'].get('stock_skus')} SKU\n"
                f"Продажи: {result['sales'].get('sales_rows')} строк за {len(result['sales'].get('sales_days', []))} дн.",
                dry)
    except Exception as e:
        result["ok"] = False
        result["error"] = str(e)
        _notify(f"❌ Синк УПАЛ {started}: {e}", dry)
    return result


def _notify(text: str, dry: bool):
    """Синхронная отправка в Telegram (работает и из фонового планировщика)."""
    if dry:
        return
    tok = os.environ.get("TG_TOKEN", "")
    chat = os.environ.get("TG_CHAT", "")
    if not tok or not chat:
        return
    try:
        import httpx
        httpx.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                   json={"chat_id": chat, "text": text}, timeout=10)
    except Exception:
        pass  # телега не критична
