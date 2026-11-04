"""Проверка подсчёта статистики. Запуск: python3 tests/test_history.py"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from history import History, compute_stats, outage_intervals  # noqa: E402


def series(start, seconds, step, battery, in_w, out_w):
    """Равномерные показания каждые step секунд."""
    return [(start + i * step, battery(i), in_w, out_w, None) for i in range(seconds // step)]


def test_energy_and_max():
    # час ровно: вход 600 Вт, выход 200 Вт, шаг 30 с
    s = series(0, 3600, 30, lambda i: 50, 600, 200)
    s[10] = (s[10][0], 50, 600, 1250, None)                      # пик выхода
    r = compute_stats(s, [], 0, 3600, 3600)
    assert abs(r["in_wh"] - 600) < 1 and abs(r["out_wh"] - (200 + 1050 * 30 / 3600)) < 1
    assert r["max_out_w"] == 1250


def test_gap_not_counted():
    # 10 минут данных, 2 часа тишины (бот выключен), 10 минут данных: пропуск в энергию не идёт
    s = series(0, 600, 30, lambda i: 50, 360, 360) + series(7800, 600, 30, lambda i: 50, 360, 360)
    r = compute_stats(s, [], 0, 8400, 8400)
    assert abs(r["in_wh"] - 360 * 1200 / 3600) < 5


def test_outages_pairs_and_ongoing():
    ev = [(1000, "light_off", 940), (2000, "light_on", 1990), (5000, "light_off", 4900)]
    assert outage_intervals(ev, 6000) == [(940, 1990), (4900, 6000)]
    r = compute_stats([], ev, 0, 6000, 6000)
    assert r["outage_count"] == 2 and r["outage_total_s"] == (1990 - 940) + (6000 - 4900)
    # отключение, начавшееся до периода: считается в длительность (обрезано), но не в количество
    r2 = compute_stats([], ev, 1500, 6000, 6000)
    assert r2["outage_count"] == 1 and r2["outage_total_s"] == (1990 - 1500) + 1100


def test_charge_sessions():
    # 2 часа зарядки (+30%), 10 минут простоя, потом ещё час зарядки (+20%)
    a = series(0, 7200, 30, lambda i: 40 + 30 * i / 240, 500, 100)
    b = series(7800, 3600, 30, lambda i: 70 + 20 * i / 120, 500, 100)
    idle = series(7200, 600, 30, lambda i: 70, 100, 100)
    r = compute_stats(a + idle + b, [], 0, 11400, 11400)
    assert r["charge_count"] == 2 and abs(r["charge_avg_s"] - (7200 + 3600) / 2) < 120
    # короткая подзарядка без прироста заряда сессией не считается
    blip = series(0, 400, 30, lambda i: 99, 300, 100)
    assert compute_stats(blip, [], 0, 400, 400)["charge_count"] == 0
    # обрыв данных разделяет сессии
    gap = series(0, 1800, 30, lambda i: 40 + i / 3, 500, 100) + series(5000, 1800, 30, lambda i: 80 + i / 10, 500, 100)
    assert compute_stats(gap, [], 0, 7000, 7000)["charge_count"] == 2
    assert compute_stats([], [], 0, 100, 100)["charge_avg_s"] is None


def test_db_roundtrip():
    h = History(Path(tempfile.mkdtemp()) / "h.db")
    h.add_sample("main", 100, 90, 300, 100, 1)
    h.add_sample("main", 130, 91, 310, 90, None)
    h.add_sample("other", 100, 50, 0, 0, 0)
    h.add_event("main", 120, "light_off", 100)
    assert [r[0] for r in h.samples("main", 0, 200)] == [100, 130] and h.first_ts("main") == 100
    assert h.events("main", 0, 200) == [(120, "light_off", 100)] and h.first_ts("nope") is None
    h.purge(125)
    assert [r[0] for r in h.samples("main", 0, 200)] == [130] and h.events("main", 0, 200) == []


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("OK", name)
    print("Усі перевірки пройдені")
