# -*- coding: utf-8 -*-
"""
sync.py — автосинк данных МойСклад → SQLite сайта.

Остатки: НАПРЯМУЮ из МойСклад API (через rebuild_history.stock_on_date) —
отчёт остатков зеркала LensSklad может отставать от реальности.
Продажи/возвраты: из зеркала Postgres (LensSklad) — документы там свежие.

Пишет в те же таблицы и в том же формате, что ручные загрузчики:
  - stock_snapshots (date, sku_name, stock_qty)  — снапшот остатков на сегодня
  - stock_bystore (date, store, sku_name, qty)   — по складам (хронология)
  - sales_data (date, sku_name, qty, revenue, doc_type)  — продажи/возвраты по дням
"""

import os
import sqlite3
from datetime import date, datetime, timedelta

PG_URL = os.environ.get("PG_URL", "")
DB_PATH = "/data/stocks.db"

SCHEMA = {
    "demand":        "lendemand",
    "retaildemand":  "lenretaildemand",
    "salesreturn":   "lensalesreturn",
}

SALES_DAYS_BACK = int(os.environ.get("SYNC_DAYS_BACK", "3"))
MIN_STOCK_ROWS = 10   # защита: не пишем снапшот, если данных подозрительно мало


# ── подключения ───────────────────────────────────────────────────────────────

def get_pg():
    import psycopg2
    import psycopg2.extras
    conn = psycopg2.connect(PG_URL, sslmode="prefer", connect_timeout=15)
    conn.set_session(readonly=True)
    return conn


def get_sqlite():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ── инспектор схемы PG (для отладки) ─────────────────────────────────────────

def inspect_schema(prefix: str = "len"):
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


def _init_bystore_table(lite):
    lite.execute("""CREATE TABLE IF NOT EXISTS stock_bystore (
        date TEXT NOT NULL, store TEXT NOT NULL, sku_name TEXT NOT NULL,
        qty REAL NOT NULL DEFAULT 0,
        PRIMARY KEY (date, store, sku_name))""")
    lite.execute("CREATE INDEX IF NOT EXISTS idx_bs_sku ON stock_bystore(sku_name)")


# ── остатки: снапшот на сегодня — НАПРЯМУЮ ИЗ МОЙСКЛАДА ─────────────────────

def sync_stock(dry: bool = False):
    """Снапшот остатков по 3 торговым складам из живого МойСклад API:
    суммарно — в stock_snapshots, по складам — в stock_bystore."""
    import rebuild_history as rh
    # Дату передаём явно ПО МСК (ревью 18.08): без аргумента stock_on_date
    # берёт date.today() в UTC — ночью (00:00–03:00 МСК) выборка шла бы на
    # конец предыдущего дня, а метка снапшота (ниже) была бы уже новой датой.
    from datetime import timezone as _tzz, timedelta as _tdd
    _today_msk = datetime.now(_tzz(_tdd(hours=3))).date().isoformat()
    data = rh.stock_on_date(_today_msk)  # из МойСклада (кэш 10 мин)
    stores = data["stores"]
    rows = []
    for name, per in data["skus"].items():
        for st, q in zip(stores, per):
            if q:
                rows.append((str(name), str(st), float(q)))

    if len(rows) < MIN_STOCK_ROWS:
        raise RuntimeError(f"Остатки из МойСклада подозрительно пусты ({len(rows)} строк) — снапшот не записан")

    totals = {}
    for n, st, q in rows:
        totals[n] = totals.get(n, 0.0) + q

    # Дата снапшота — по МОСКОВСКОМУ времени (аудит 18.08): сервер Render живёт
    # в UTC, и ручной синк между 00:00 и 03:00 МСК записывал остатки на
    # ВЧЕРАШНЮЮ дату (date.today() в UTC), портя вчерашний снапшот.
    from datetime import timezone as _tz, timedelta as _td2
    today = datetime.now(_tz(_td2(hours=3))).date().isoformat()
    now = datetime.now().isoformat()
    if dry:
        return {"stock_skus": len(totals), "stock_total_qty": sum(totals.values()),
                "stores": stores, "date": today, "source": "moysklad-live", "dry": True}

    lite = get_sqlite()
    _init_bystore_table(lite)
    # Явные нули: позиция, у которой последний записанный остаток был >0,
    # а сегодня её нет в отчёте МойСклада, — распродана. Пишем 0 на сегодня,
    # иначе фронты (turnover/analytics/forecast/order) вечно тянут последний
    # положительный остаток («фантомный сток») и dis продолжает тикать.
    # Правило самоизлечивающееся: после записи нуля последняя строка = 0,
    # и позиция больше не попадает в gone.
    gone_rows = []
    try:
        cur = lite.execute(
            "SELECT s.sku_name, s.stock_qty FROM stock_snapshots s "
            "JOIN (SELECT sku_name, MAX(date) md FROM stock_snapshots "
            "      WHERE date < ? GROUP BY sku_name) m "
            "  ON m.sku_name = s.sku_name AND m.md = s.date "
            "WHERE s.stock_qty > 0", (today,))
        for n, q in cur.fetchall():
            if n not in totals:
                gone_rows.append((today, str(n), 0.0, now))
    except Exception:
        gone_rows = []
    lite.executemany(
        "INSERT OR REPLACE INTO stock_snapshots (date, sku_name, stock_qty, uploaded_at) "
        "VALUES (?, ?, ?, ?)",
        [(today, n, q, now) for n, q in totals.items()] + gone_rows
    )
    lite.executemany(
        "INSERT OR REPLACE INTO stock_bystore (date, store, sku_name, qty) "
        "VALUES (?, ?, ?, ?)",
        [(today, st, n, q) for n, st, q in rows]
    )
    lite.commit(); lite.close()
    return {"stock_skus": len(totals), "bystore_rows": len(rows), "date": today,
            "zeroed_gone": len(gone_rows), "source": "moysklad-live"}


# ── продажи/возвраты за последние N дней (из зеркала — документы свежие) ─────

def _fetch_docs(pg, head_table: str, days_back: int):
    pos_table = head_table + "_position"
    cutoff = (date.today() - timedelta(days=days_back)).isoformat()
    cur = pg.cursor()
    cur.execute(f"""
        SELECT (h.moment)::date AS d,
               COALESCE(v.name, pr.name) AS sku,
               SUM(p.quantity) AS qty,
               SUM(p.price * p.quantity * (1 - COALESCE(p.discount,0)/100.0)) / 100.0 AS rev
        FROM {pos_table} p
        JOIN {head_table} h ON h.id = p.id
        LEFT JOIN lenvariant v ON v.id = RIGHT(p.assortment_id, 36)
        LEFT JOIN lenproduct pr ON pr.id = RIGHT(p.assortment_id, 36)
        WHERE (h.moment)::date >= %s
          AND COALESCE(v.name, pr.name) IS NOT NULL
        GROUP BY 1, 2
    """, (cutoff,))
    rows = cur.fetchall()
    return [(r[0].isoformat(), str(r[1]), float(r[2] or 0), float(r[3] or 0)) for r in rows if r[1]]


def _ms_fetch_sales_docs(entity: str, days_back: int):
    """Документы продаж/возвратов НАПРЯМУЮ из МойСклад API (не из зеркала).

    Причина перехода 03.08.2026: у зеркала LensSklad позиции розничных чеков
    (lenretaildemand_position) не содержат ссылки на чек — JOIN отдавал 0 строк,
    и розница двух магазинов молча выпадала из аналитики с момента включения
    автосинка (15.07). Прямой запрос в МС убирает эту зависимость целиком.

    Возвращает [(date, sku_name, qty, revenue_rub)], только проведённые документы.
    """
    import httpx
    import rebuild_history as rh
    cutoff = (date.today() - timedelta(days=days_back)).isoformat()
    out = []
    offset = 0
    while True:
        r = None
        for _attempt in range(6):
            r = httpx.get(f"{rh.MS_API_BASE}/entity/{entity}",
                          params={"filter": f"moment>={cutoff} 00:00:00",
                                  "expand": "positions.assortment",
                                  "limit": 50, "offset": offset},
                          headers=rh._ms_headers(), timeout=90)
            if r.status_code == 429:
                import time as _t
                _t.sleep(3.5)
                continue
            r.raise_for_status()
            break
        else:
            raise RuntimeError(f"МойСклад: слишком много 429 ({entity})")
        docs = r.json().get("rows", [])
        for doc in docs:
            if doc.get("applicable") is False:
                continue  # черновики/распроведённые не считаем
            d = (doc.get("moment") or "")[:10]
            if not d:
                continue
            posmeta = doc.get("positions") or {}
            pos = posmeta.get("rows") or []
            total = int((posmeta.get("meta") or {}).get("size") or len(pos))
            if total > len(pos):
                # expand отдаёт не более ~100 позиций — дочитываем пагинацией
                pos = rh._fetch_all_positions(entity, doc["id"], total)
            for p in pos:
                nm = ((p.get("assortment") or {}).get("name")) or ""
                if not nm:
                    continue
                qty = float(p.get("quantity") or 0)
                price = float(p.get("price") or 0)          # копейки
                disc = float(p.get("discount") or 0)         # проценты
                out.append((d, nm, qty, price * qty * (1 - disc / 100.0) / 100.0))
        if len(docs) < 50:
            break
        offset += 50
    return out


def sync_sales(dry: bool = False, days_back: int = SALES_DAYS_BACK):
    # Продажи: отгрузки (в т.ч. интернет-магазин/маркетплейсы) + розничные чеки.
    sales = _ms_fetch_sales_docs("demand", days_back)
    retail = _ms_fetch_sales_docs("retaildemand", days_back)
    retail_rows = len(retail)
    sales += retail
    # Защита от пустого окна: если МС не вернул НИ ОДНОЙ продажи за days_back
    # дней — это почти наверняка сбой (токен/сеть), а не реальность. Не
    # перезаписываем окно нулями.
    if not sales:
        raise RuntimeError(f"МойСклад: за {days_back} дн. не получено ни одной продажи — окно не перезаписано")
    returns_error = None
    returns = []
    try:
        returns = _ms_fetch_sales_docs("salesreturn", days_back)
        # Розничные возвраты раньше не учитывались вовсе (в зеркале их позиции
        # были пусты) — теперь берём напрямую.
        returns += _ms_fetch_sales_docs("retailsalesreturn", days_back)
    except Exception as e:
        # Не глотаем молча: сбой возвратов помечается в результате и в Телеграме,
        # возвраты за окно в этом случае НЕ трогаем (см. guard ниже).
        returns = []
        returns_error = str(e)
        print(f"sync_sales: возвраты НЕ синхронизированы: {e}")

    agg = {}
    for d, sku, qty, rev in sales:
        k = (d, sku)
        cur_q, cur_r = agg.get(k, (0.0, 0.0))
        agg[k] = (cur_q + qty, cur_r + rev)

    # Возвраты агрегируем так же, как продажи: _ms_fetch_sales_docs отдаёт
    # строку на КАЖДУЮ позицию каждого документа, а sales_data имеет
    # UNIQUE(date, sku_name, doc_type) — без агрегации INSERT OR REPLACE
    # оставлял только последний возврат дня по SKU, остальные молча терялись
    # (нетто-выручка и оборачиваемость завышались). Регрессия с 03.08.2026.
    ragg = {}
    for d, sku, qty, rev in returns:
        k = (d, sku)
        cur_q, cur_r = ragg.get(k, (0.0, 0.0))
        ragg[k] = (cur_q + qty, cur_r + rev)

    dates_sales = sorted({d for d, _ in agg})
    dates_ret = sorted({d for d, _ in ragg})

    if dry:
        return {
            "sales_days": dates_sales, "sales_rows": len(agg),
            "sales_revenue": round(sum(r for _, r in agg.values()), 2),
            "returns_days": dates_ret, "returns_rows": len(ragg),
            "returns_revenue": round(sum(r for _, r in ragg.values()), 2),
            "dry": True,
        }

    # Чистим по всему окну синка, а не только по датам из свежей выборки:
    # раньше день, в котором ВСЕ документы удалили/распровели в МойСкладе,
    # не попадал в выборку, DELETE для него не выполнялся — и стёртые продажи
    # оставались в sales_data навсегда (фантомная выручка в отчётах).
    win_cutoff = (date.today() - timedelta(days=days_back)).isoformat()
    lite = get_sqlite()
    lite.execute("DELETE FROM sales_data WHERE date >= ? AND doc_type='sale'", (win_cutoff,))
    lite.executemany(
        "INSERT OR REPLACE INTO sales_data (date, sku_name, qty, revenue, doc_type) "
        "VALUES (?, ?, ?, ?, 'sale')",
        [(d, sku, q, r) for (d, sku), (q, r) in agg.items()]
    )
    if returns_error is None:
        # Возвраты трогаем только при успешной выборке — иначе окно очистилось
        # бы впустую и возвраты за 3 дня исчезли бы из отчётов.
        lite.execute("DELETE FROM sales_data WHERE date >= ? AND doc_type='return'", (win_cutoff,))
        lite.executemany(
            "INSERT OR REPLACE INTO sales_data (date, sku_name, qty, revenue, doc_type) "
            "VALUES (?, ?, ?, ?, 'return')",
            [(d, sku, q, r) for (d, sku), (q, r) in ragg.items()]
        )
    lite.commit(); lite.close()
    out = {"sales_days": dates_sales, "sales_rows": len(agg),
           "returns_days": dates_ret, "returns_rows": len(ragg),
           "retail_rows": retail_rows, "source": "moysklad-direct"}
    if returns_error:
        out["returns_error"] = returns_error
    if retail_rows == 0:
        # Сторожок: чеки розницы отсутствуют во всём окне. Магазины обычно
        # торгуют ежедневно — молчать про это нельзя (см. историю с зеркалом).
        out["retail_warning"] = f"за {days_back} дн. нет ни одного розничного чека"
    return out


# ── сверка имён SKU ───────────────────────────────────────────────────────────

def verify_names(limit: int = 50):
    import rebuild_history as rh
    data = rh.stock_on_date()
    pg_names = set(data["skus"].keys())

    lite = get_sqlite()
    site_names = {r[0] for r in lite.execute("SELECT DISTINCT sku_name FROM stock_snapshots")}
    lite.close()

    only_pg = sorted(pg_names - site_names)[:limit]
    only_site = sorted(site_names - pg_names)[:limit]
    return {"pg_total": len(pg_names), "site_total": len(site_names),
            "matched": len(pg_names & site_names),
            "only_in_pg": only_pg, "only_on_site": only_site}


# ── общий запуск ─────────────────────────────────────────────────────────────

def sync_all(dry: bool = False, days_back: int = 0):
    started = datetime.now().isoformat(timespec="seconds")
    result = {"started": started}
    try:
        result["stock"] = sync_stock(dry=dry)
        # Падение продаж НЕ должно оставлять кэши непересобранными: раньше
        # исключение в sync_sales улетало в общий except, и /api/analytics-data
        # весь день отдавал вчерашний файл — свежие остатки и явные нули
        # распроданных позиций (gone_rows) до фронтов не доезжали.
        sales_error = None
        try:
            result["sales"] = sync_sales(dry=dry, days_back=(days_back or SALES_DAYS_BACK))
        except Exception as e:
            sales_error = str(e)
            result["sales"] = {"error": sales_error}
        if not dry:
            try:
                import rebuild_history as rh
                result["settle"] = rh.settle_incoming(force=1)   # приёмки → вычет из «Заказано»
            except Exception as e:
                result["settle"] = {"error": str(e)}
        if not dry:
            import main as site
            conn = site.get_db()
            site.rebuild_analytics_json(conn)
            conn.close()
            result["caches"] = "rebuilt"
        result["ok"] = sales_error is None
        _ret_warn = ""
        if result.get("sales", {}).get("returns_error"):
            _ret_warn += f"\n⚠️ Возвраты НЕ синхронизированы: {result['sales']['returns_error']}"
        if result.get("sales", {}).get("retail_warning"):
            _ret_warn += f"\n⚠️ Розница: {result['sales']['retail_warning']}"
        if sales_error:
            result["error"] = f"sync_sales: {sales_error}"
            _notify(f"⚠️ Синк ЧАСТИЧНО {started}\n"
                    f"Остатки ОК: {result['stock'].get('stock_skus')} SKU, кэши пересобраны.\n"
                    f"❌ Продажи УПАЛИ: {sales_error}\n"
                    f"Окно продаж не перезаписано — дозаполнится при следующем успешном синке.",
                    dry)
        else:
            _notify(f"✅ Синк ОК {started}\n"
                    f"Остатки (МойСклад напрямую): {result['stock'].get('stock_skus')} SKU\n"
                    f"Продажи (МойСклад напрямую): {result['sales'].get('sales_rows')} строк за {len(result['sales'].get('sales_days', []))} дн., "
                    f"из них розничных позиций: {result['sales'].get('retail_rows')}"
                    + _ret_warn,
                    dry)
    except Exception as e:
        result["ok"] = False
        result["error"] = str(e)
        _notify(f"❌ Синк УПАЛ {started}: {e}", dry)
    return result


def _notify(text: str, dry: bool):
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
        pass
