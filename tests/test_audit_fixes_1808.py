# -*- coding: utf-8 -*-
"""
Тесты фиксов аудита 18.08 (ветка audit-fixes, база прода eb5f071).

Запуск:  python tests/test_audit_fixes_1808.py

Покрытие:
  6. chart по интервалам сетки дат (фикс 21.08, dwv:3)
  1. f7763df — sync_sales: возвраты агрегируются по (дата, SKU) перед записью
  2. 4b245e7 — dis в календарных днях (веса дат: интервал до следующей, потолок 7)
  3. cfb6a9b — /api/upload: guard от частичной выгрузки (<70% SKU), ?force=1
  4. 15d9b17 — /api/upload-sales: тип документа по каждой строке, deleted в ответе
  5. 081ecc4 — settle_incoming: applicable=false не вычитается, идемпотентность, unapplied
"""
import os
import sys
import json
import sqlite3
import tempfile
import traceback
from datetime import date as _date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SYNC_ENABLED", "0")   # не поднимать APScheduler при импорте
os.makedirs("/data", exist_ok=True)          # main.init_db() при импорте пишет в /data

import main  # noqa: E402  (импорт после подготовки окружения)
import sync  # noqa: E402
import rebuild_history  # noqa: E402

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


class _Patched:
    """Контекст: временная БД + временные пути кэшей вместо продовых глобалей."""

    def __enter__(self):
        self.tmpdir = tempfile.mkdtemp(prefix="oborot_test_")
        self.db_path = os.path.join(self.tmpdir, "stocks.db")
        self.saved = (main.DB_PATH, main.ANALYTICS_JSON_PATH, main.TURNOVER_JSON_PATH,
                      sync.DB_PATH)
        main.DB_PATH = self.db_path
        main.ANALYTICS_JSON_PATH = os.path.join(self.tmpdir, "analytics_cache.json")
        main.TURNOVER_JSON_PATH = os.path.join(self.tmpdir, "turnover_cache.json")
        sync.DB_PATH = self.db_path
        main.init_db()  # создаёт все таблицы во временной БД
        return self

    def __exit__(self, *exc):
        main.DB_PATH, main.ANALYTICS_JSON_PATH, main.TURNOVER_JSON_PATH, sync.DB_PATH = \
            self.saved[0], self.saved[1], self.saved[2], self.saved[3]
        return False

    def conn(self):
        c = sqlite3.connect(self.db_path)
        c.row_factory = sqlite3.Row
        return c


# ────────────────────────────────────────────────────────────────────────────
# 1. Возвраты агрегируются по (дата, SKU) перед записью (фикс f7763df)
# ────────────────────────────────────────────────────────────────────────────
@test
def test_returns_aggregated_before_insert():
    today = _date.today().isoformat()
    old_day = (_date.today() - timedelta(days=10)).isoformat()

    fake_docs = {
        "demand": [(today, 'Куртка "Тест" (M)', 1.0, 7000.0)],
        "retaildemand": [(today, 'Куртка "Тест" (M)', 1.0, 6900.0)],
        # ДВА возврата в один день по одному SKU — раньше второй затирал первый
        "salesreturn": [(today, 'Куртка "Тест" (M)', 1.0, 5000.0),
                        (today, 'Куртка "Тест" (M)', 2.0, 9000.0)],
        "retailsalesreturn": [(today, 'Куртка "Тест" (M)', 1.0, 4000.0)],
    }

    with _Patched() as p:
        # Возврат ВНЕ окна синка (10 дней назад при окне 3) — должен уцелеть
        c = p.conn()
        c.execute("INSERT INTO sales_data (date, sku_name, qty, revenue, doc_type) "
                  "VALUES (?, ?, 1, 1000, 'return')", (old_day, 'Старый возврат (S)'))
        c.commit(); c.close()

        saved_fetch = sync._ms_fetch_sales_docs
        sync._ms_fetch_sales_docs = lambda entity, days_back: list(fake_docs[entity])
        try:
            out = sync.sync_sales(dry=False, days_back=3)
        finally:
            sync._ms_fetch_sales_docs = saved_fetch

        c = p.conn()
        ret = c.execute("SELECT qty, revenue FROM sales_data "
                        "WHERE date=? AND doc_type='return' AND sku_name=?",
                        (today, 'Куртка "Тест" (M)')).fetchall()
        assert len(ret) == 1, f"ожидалась 1 агрегированная строка возврата, есть {len(ret)}"
        # 1+2 (salesreturn, два документа) + 1 (retailsalesreturn) = 4 шт, 18000 руб
        assert abs(ret[0]["qty"] - 4.0) < 1e-9, \
            f"qty возврата = {ret[0]['qty']}, ожидалось 4.0 (второй возврат дня затёрт?)"
        assert abs(ret[0]["revenue"] - 18000.0) < 1e-6, \
            f"revenue возврата = {ret[0]['revenue']}, ожидалось 18000.0"

        sale = c.execute("SELECT qty, revenue FROM sales_data "
                         "WHERE date=? AND doc_type='sale'", (today,)).fetchall()
        assert len(sale) == 1 and abs(sale[0]["qty"] - 2.0) < 1e-9, \
            "продажи (demand+retaildemand) должны агрегироваться в одну строку qty=2"
        assert abs(sale[0]["revenue"] - 13900.0) < 1e-6

        survivors = c.execute("SELECT COUNT(*) c FROM sales_data WHERE date=?",
                              (old_day,)).fetchone()["c"]
        assert survivors == 1, "возврат вне окна синка не должен удаляться"
        c.close()
        assert out["returns_rows"] == 1 and out["sales_rows"] == 1


# ────────────────────────────────────────────────────────────────────────────
# 2. dis в календарных днях (фикс 4b245e7)
# ────────────────────────────────────────────────────────────────────────────
def _expected_dis(dates):
    """Независимая реализация формулы весов из фикса: вес даты = интервал до
    следующей даты сетки (пол 1, потолок 7), последняя дата = 1. Позиция в
    стоке (>=3) на каждую дату — dis = сумма весов."""
    total = 0
    for i, d in enumerate(dates):
        if i + 1 < len(dates):
            gap = (_date.fromisoformat(dates[i + 1]) - _date.fromisoformat(d)).days
            total += min(max(gap, 1), 7)
        else:
            total += 1
    return total


@test
def test_dis_in_calendar_days():
    # 13 понедельников 2024-01-01..2024-03-25 (недельная сетка истории)
    mondays = [(_date(2024, 1, 1) + timedelta(weeks=i)).isoformat() for i in range(13)]
    assert mondays[-1] == "2024-03-25" and len(mondays) == 13
    # затем ежедневная сетка: 10 дат 2024-03-26..2024-04-04
    daily = [(_date(2024, 3, 26) + timedelta(days=i)).isoformat() for i in range(10)]
    dates = mondays + daily

    # Ожидание из формулы весов: 12 интервалов Пн→Пн по 7 дн. + последний
    # понедельник до первой дневной даты (1 дн.) + 9 дневных интервалов по
    # 1 дн. + последняя дата = 1.  Итого 12*7 + 1 + 9 + 1 = 95.
    expected = _expected_dis(dates)
    assert expected == 12 * 7 + 1 + 9 + 1 == 95, f"самопроверка формулы: {expected}"

    with _Patched() as p:
        c = p.conn()
        for d in dates:
            c.execute("INSERT INTO stock_snapshots (date, sku_name, stock_qty, uploaded_at) "
                      "VALUES (?, ?, 5, '')", (d, "Тестовая куртка"))
        c.commit()
        main.rebuild_analytics_json(c)
        c.close()

        with open(main.TURNOVER_JSON_PATH, encoding="utf-8") as f:
            turnover = json.load(f)
        got = turnover["skus"]["Тестовая куртка"]["dis"]
        assert got == expected, (
            f"dis = {got}, ожидалось {expected} календарных дней "
            f"(старый баг: 23 даты сетки = 23 «дня»)")
        # Сезонные дни согласованы с dis
        sea = turnover["skus"]["Тестовая куртка"]["sea_days"]
        assert sum(sea.values()) == expected, f"sum(sea_days) = {sum(sea.values())} != dis"


# ────────────────────────────────────────────────────────────────────────────
# 3. Guard /api/upload: частичная выгрузка отклоняется, force=1 проходит (cfb6a9b)
# ────────────────────────────────────────────────────────────────────────────
@test
def test_upload_partial_guard():
    from fastapi.testclient import TestClient

    with _Patched() as p:
        c = p.conn()
        for i in range(100):  # последний снапшот: 100 SKU c остатком > 0
            c.execute("INSERT INTO stock_snapshots (date, sku_name, stock_qty, uploaded_at) "
                      "VALUES ('2026-08-17', ?, 3, '')", (f"SKU-{i:03d} (M)",))
        c.commit(); c.close()

        partial_rows = [{"sku_name": f"SKU-{i:03d} (M)", "stock_qty": 1.0} for i in range(10)]
        saved_parse = main.parse_xls
        main.parse_xls = lambda path: ("2026-08-18", list(partial_rows))
        try:
            client = TestClient(main.app)
            # 10 SKU из 100 (<70%) на новую дату — 400
            r = client.post("/api/upload",
                            files={"file": ("stock.xls", b"stub", "application/vnd.ms-excel")})
            assert r.status_code == 400, f"частичная выгрузка прошла: {r.status_code} {r.text}"
            assert "ЧАСТИЧНУЮ" in r.json()["detail"], r.json()["detail"]

            c = p.conn()
            cnt = c.execute("SELECT COUNT(*) c FROM stock_snapshots WHERE date='2026-08-18'"
                            ).fetchone()["c"]
            c.close()
            assert cnt == 0, "отклонённая загрузка не должна писать строки"

            # ?force=1 — осознанная загрузка проходит
            r = client.post("/api/upload?force=1",
                            files={"file": ("stock.xls", b"stub", "application/vnd.ms-excel")})
            assert r.status_code == 200, f"force=1 не прошёл: {r.status_code} {r.text}"
            assert r.json()["date"] == "2026-08-18" and r.json()["inserted"] == 10

            # Частичный файл на СТАРУЮ дату (меньше последней) — guard не мешает
            main.parse_xls = lambda path: ("2026-08-01", list(partial_rows))
            r = client.post("/api/upload",
                            files={"file": ("stock.xls", b"stub", "application/vnd.ms-excel")})
            assert r.status_code == 200, f"старая дата не должна блокироваться: {r.text}"
        finally:
            main.parse_xls = saved_parse


# ────────────────────────────────────────────────────────────────────────────
# 4. /api/upload-sales: смешанный CSV, типы по колонке «Документ», deleted (15d9b17)
# ────────────────────────────────────────────────────────────────────────────
@test
def test_upload_sales_mixed_csv():
    from fastapi.testclient import TestClient

    csv_text = (
        "Дата документа,Документ,Наименование,Артикул,Количество,Сумма\n"
        "01.08.2026 10:00,Отгрузка №100,Куртка (M),ART1,2,\"10 000,50\"\n"
        "01.08.2026 11:00,Отгрузка №101,Куртка (M),ART1,1,\"2 000,00\"\n"
        "01.08.2026 12:00,Возврат покупателя №5,Куртка (M),ART1,1,\"5 000,25\"\n"
    )

    with _Patched() as p:
        c = p.conn()
        # Старая строка продаж на ту же дату — должна быть заменена (и посчитана в deleted)
        c.execute("INSERT INTO sales_data (date, sku_name, qty, revenue, doc_type) "
                  "VALUES ('2026-08-01', 'Куртка (M)', 99, 999999, 'sale')")
        # Возврат на ЧУЖУЮ дату — не должен пострадать
        c.execute("INSERT INTO sales_data (date, sku_name, qty, revenue, doc_type) "
                  "VALUES ('2026-07-15', 'Куртка (M)', 1, 100, 'return')")
        c.commit(); c.close()

        client = TestClient(main.app)
        # Имя файла БЕЗ слова «возврат» — тип обязан определяться по строкам
        r = client.post("/api/upload-sales",
                        files={"file": ("mixed_sales.csv", csv_text.encode("utf-8"), "text/csv")})
        assert r.status_code == 200, r.text
        body = r.json()
        assert sorted(body["types"]) == ["return", "sale"], body
        assert "deleted" in body, "в ответе нет deleted"
        assert body["deleted"]["sale"]["rows"] == 1, body["deleted"]
        assert body["deleted"]["sale"]["dates"] == ["2026-08-01"]
        assert body["inserted"] == 2  # (дата, SKU, sale) + (дата, SKU, return)

        c = p.conn()
        sale = c.execute("SELECT qty, revenue FROM sales_data "
                         "WHERE date='2026-08-01' AND doc_type='sale'").fetchone()
        assert abs(sale["qty"] - 3.0) < 1e-9 and abs(sale["revenue"] - 12000.50) < 1e-6, \
            f"продажа: {dict(sale)}"
        ret = c.execute("SELECT qty, revenue FROM sales_data "
                        "WHERE date='2026-08-01' AND doc_type='return'").fetchone()
        assert ret is not None, "строка возврата из смешанного CSV не записана"
        assert abs(ret["qty"] - 1.0) < 1e-9 and abs(ret["revenue"] - 5000.25) < 1e-6, \
            f"возврат: {dict(ret)}"
        old_ret = c.execute("SELECT COUNT(*) c FROM sales_data "
                            "WHERE date='2026-07-15' AND doc_type='return'").fetchone()["c"]
        assert old_ret == 1, "возврат на не задетую файлом дату удалён"
        c.close()


# ────────────────────────────────────────────────────────────────────────────
# 5. settle_incoming: черновики, идемпотентность, unapplied (фикс 081ecc4)
# ────────────────────────────────────────────────────────────────────────────
class _FakeResp:
    def __init__(self, payload):
        self._p = payload
        self.status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


def _fake_ms_get(url, params=None, headers=None, timeout=None):
    if "/entity/supply" in url:
        if params and params.get("offset", 0) == 0:
            return _FakeResp({"rows": [
                {   # черновик / распроведённый — вычитаться НЕ должен
                    "id": "draft1", "applicable": False,
                    "positions": {"meta": {"size": 1}, "rows": [
                        {"assortment": {"name": "Тестовая куртка (M)"}, "quantity": 5}]},
                },
                {   # проведённый — вычитается из «Заказано»
                    "id": "doc1", "applicable": True,
                    "positions": {"meta": {"size": 1}, "rows": [
                        {"assortment": {"name": "Тестовая куртка (M)"}, "quantity": 3}]},
                },
                {   # проведённый, но по SKU «Заказано» пусто — попадает в unapplied
                    "id": "doc2", "applicable": True,
                    "positions": {"meta": {"size": 1}, "rows": [
                        {"assortment": {"name": "Позиция без заказа (S)"}, "quantity": 4}]},
                },
            ]})
        return _FakeResp({"rows": []})
    if "/entity/enter" in url:
        return _FakeResp({"rows": []})
    raise AssertionError(f"неожиданный URL в settle: {url}")


@test
def test_settle_incoming():
    import httpx

    os.environ.setdefault("MS_TOKEN", "test-token")
    with _Patched() as p:
        c = p.conn()
        c.execute("INSERT INTO sku_adjustments (project_id, sku_base, qty_adj, updated_at) "
                  "VALUES ('analytics-ordered', 'Тестовая куртка', 10, '')")
        c.commit(); c.close()

        saved_get = httpx.get
        saved_cache_t = rebuild_history._settle_cache.get("t", 0.0)
        httpx.get = _fake_ms_get
        try:
            out1 = rebuild_history.settle_incoming(force=1)
        finally:
            httpx.get = saved_get
        assert out1.get("ok") and not out1.get("busy"), out1

        c = p.conn()
        settled = {r["sku_base"] for r in c.execute(
            "SELECT sku_base FROM order_added WHERE project_id='settled-docs'")}
        qty = c.execute("SELECT qty_adj FROM sku_adjustments "
                        "WHERE project_id='analytics-ordered' AND sku_base='Тестовая куртка'"
                        ).fetchone()["qty_adj"]
        c.close()

        # Черновик не учтён и не вычтен; после проведения зачтётся штатно
        assert "supply:draft1" not in settled, "черновик помечен учтённым!"
        assert "supply:doc1" in settled and "supply:doc2" in settled, settled
        assert qty == 7, f"Заказано = {qty}, ожидалось 10 - 3 = 7 (черновик вычтен?)"
        assert out1["applied"]["Тестовая куртка"] == {"was": 10, "received": 3.0, "now": 7}, out1

        # unapplied: приёмка есть, «Заказано» пусто — виден в отчёте, документ учтён
        assert out1.get("unapplied", {}).get("Позиция без заказа") == 4, out1.get("unapplied")

        # Повторный вызов — идемпотентен: ничего не вычитается второй раз
        rebuild_history._settle_cache["t"] = 0.0
        httpx.get = _fake_ms_get
        try:
            out2 = rebuild_history.settle_incoming(force=1)
        finally:
            httpx.get = saved_get
            rebuild_history._settle_cache["t"] = saved_cache_t
        assert out2.get("ok"), out2
        assert out2["applied"] == {}, f"повторный вызов снова вычел: {out2['applied']}"

        c = p.conn()
        qty2 = c.execute("SELECT qty_adj FROM sku_adjustments "
                         "WHERE project_id='analytics-ordered' AND sku_base='Тестовая куртка'"
                         ).fetchone()["qty_adj"]
        settled2 = {r["sku_base"] for r in c.execute(
            "SELECT sku_base FROM order_added WHERE project_id='settled-docs'")}
        c.close()
        assert qty2 == 7, f"двойное вычитание: Заказано = {qty2} после второго вызова"
        assert "supply:draft1" not in settled2, "черновик учтён на втором проходе"



# ────────────────────────────────────────────────────────────────────────────
# 6. chart: выручка разложена по интервалам сетки дат (фикс 21.08)
# ────────────────────────────────────────────────────────────────────────────
@test
def test_chart_bucketed_by_grid_interval():
    # Недельная сетка: 3 понедельника, затем дневная: 4 даты
    mondays = [(_date(2024, 1, 1) + timedelta(weeks=i)).isoformat() for i in range(3)]  # 01,08,15
    daily = [(_date(2024, 1, 16) + timedelta(days=i)).isoformat() for i in range(4)]    # 16..19
    dates = mondays + daily
    with _Patched() as p:
        c = p.conn()
        for d in dates:
            c.execute("INSERT INTO stock_snapshots (date, sku_name, stock_qty, uploaded_at) "
                      "VALUES (?, ?, 5, '')", (d, "Тестовая куртка"))
        # 100 ₽ каждый день с 2023-12-30 по 2024-01-19 (+ один день после сетки)
        d0 = _date(2023, 12, 30)
        for i in range(22):
            d = (d0 + timedelta(days=i)).isoformat()
            c.execute("INSERT INTO sales_data (date, sku_name, qty, revenue, doc_type) "
                      "VALUES (?, 'Тестовая куртка', 1, 100, 'sale')", (d,))
        c.commit()
        main.rebuild_analytics_json(c)
        c.close()
        with open(main.TURNOVER_JSON_PATH, encoding="utf-8") as f:
            t = json.load(f)
        assert t["dwv"] == 3, t.get("dwv")
        chart = t["skus"]["Тестовая куртка"]["chart"]
        # 01..07 -> 700, 08..14 -> 700, 15 -> 100 (интервал до 16 = 1 день),
        # 16,17,18,19 -> по 100; продажа 20.01 (после последнего снапшота) не учтена
        assert chart == [700, 700, 100, 100, 100, 100, 100], chart
        # продажи до первой даты сетки (30.12, 31.12) и после последней — нигде
        assert sum(chart) == 1900, sum(chart)
        # Согласованность с dis: сумма весов = 7+7+1+1+1+1+1 = 19
        assert t["skus"]["Тестовая куртка"]["dis"] == 19

# ────────────────────────────────────────────────────────────────────────────
def run():
    passed, failed = 0, 0
    failures = []
    for fn in TESTS:
        try:
            fn()
            passed += 1
            print(f"PASS  {fn.__name__}")
        except Exception:
            failed += 1
            tb = traceback.format_exc()
            failures.append((fn.__name__, tb))
            print(f"FAIL  {fn.__name__}\n{tb}")
    print(f"\n{passed} passed, {failed} failed из {len(TESTS)}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(run())
