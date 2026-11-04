"""
История показаний станции для раздела «Статистика».

Бот раз в ~30 секунд записывает заряд, вход и выход (таблица samples) и моменты отключения/появления света
(таблица events) в SQLite-файл data/history.db. Из них считаются: энергия на входе и выходе, отключения света,
максимальная мощность, среднее время зарядки.
"""

import sqlite3
import threading
from pathlib import Path

SAMPLE_STEP_S = 30        # обычный шаг записи; хвост после последней записи блока не длиннее шага
MAX_GAP_S = 90            # пропуск в данных длиннее этого (бот был выключен и т.п.) в энергию не засчитывается
CHARGE_MIN_NET_W = 20.0   # «заряжается»: вход минус выход больше этого значения
CHARGE_END_GRACE_S = 180  # зарядка закончилась, если условия не выполнялись дольше этого
CHARGE_MIN_DURATION_S = 300
CHARGE_MIN_GAIN_PCT = 2.0  # короткие «подзарядки» без заметного прироста заряда не считаем отдельными сессиями


class History:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS samples(
                dev TEXT NOT NULL, ts INTEGER NOT NULL,
                battery REAL, input_w REAL, output_w REAL, grid INTEGER);
            CREATE INDEX IF NOT EXISTS samples_dev_ts ON samples(dev, ts);
            CREATE TABLE IF NOT EXISTS events(
                dev TEXT NOT NULL, ts INTEGER NOT NULL, kind TEXT NOT NULL, began INTEGER);
            CREATE INDEX IF NOT EXISTS events_dev_ts ON events(dev, ts);
        """)
        self._db.commit()

    def add_sample(self, dev: str, ts: float, battery, input_w, output_w, grid) -> None:
        with self._lock:
            self._db.execute("INSERT INTO samples VALUES (?,?,?,?,?,?)",
                             (dev, int(ts), battery, input_w, output_w, grid))
            self._db.commit()

    def add_event(self, dev: str, ts: float, kind: str, began: float | None = None) -> None:
        with self._lock:
            self._db.execute("INSERT INTO events VALUES (?,?,?,?)", (dev, int(ts), kind, int(began or ts)))
            self._db.commit()

    def samples(self, dev: str, t0: float, t1: float) -> list[tuple]:
        with self._lock:
            return self._db.execute(
                "SELECT ts, battery, input_w, output_w, grid FROM samples WHERE dev=? AND ts>=? AND ts<=? ORDER BY ts",
                (dev, int(t0), int(t1))).fetchall()

    def events(self, dev: str, t0: float, t1: float) -> list[tuple]:
        with self._lock:
            return self._db.execute(
                "SELECT ts, kind, began FROM events WHERE dev=? AND ts>=? AND ts<=? ORDER BY ts",
                (dev, int(t0), int(t1))).fetchall()

    def first_ts(self, dev: str) -> int | None:
        with self._lock:
            row = self._db.execute("SELECT MIN(ts) FROM samples WHERE dev=?", (dev,)).fetchone()
        return row[0] if row else None

    def purge(self, before_ts: float) -> None:
        with self._lock:
            self._db.execute("DELETE FROM samples WHERE ts<?", (int(before_ts),))
            self._db.execute("DELETE FROM events WHERE ts<?", (int(before_ts),))
            self._db.commit()


def outage_intervals(events: list[tuple], now: float) -> list[tuple[float, float]]:
    """Из пар «свет пропал» → «свет появился» делает интервалы. Незакрытое отключение длится до now."""
    result, start = [], None
    for ts, kind, began in sorted(events):
        if kind == "light_off" and start is None:
            start = began or ts
        elif kind == "light_on" and start is not None:
            result.append((start, began or ts))
            start = None
    if start is not None:
        result.append((start, now))
    return result


def compute_stats(samples: list[tuple], events: list[tuple], t0: float, t1: float, now: float) -> dict:
    """samples: (ts, battery, input_w, output_w, grid) по возрастанию ts, в пределах [t0, t1]."""
    in_ws = out_ws = 0.0
    max_out = None
    for i, (ts, _bat, in_w, out_w, _grid) in enumerate(samples):
        nxt = samples[i + 1][0] if i + 1 < len(samples) else now
        gap = nxt - ts
        dt = gap if gap <= MAX_GAP_S else SAMPLE_STEP_S   # длинный пропуск = данных нет, считаем один шаг
        dt = max(0.0, min(dt, SAMPLE_STEP_S if i + 1 == len(samples) else MAX_GAP_S))
        in_ws += (in_w or 0.0) * dt
        out_ws += (out_w or 0.0) * dt
        if out_w is not None and (max_out is None or out_w > max_out):
            max_out = out_w

    # отключения света
    spans = outage_intervals(events, now)
    outages = [(s, e) for s, e in spans if t0 <= s <= t1]
    outage_total = sum(max(0.0, min(e, t1) - max(s, t0)) for s, e in spans)

    # сессии зарядки: вход заметно больше выхода (идёт зарядка батареи)
    sessions, start, last_ok, bat_start, bat_last, prev_ts = [], None, None, None, None, None
    def close():
        nonlocal start, last_ok, bat_start, bat_last
        if start is not None and last_ok - start >= CHARGE_MIN_DURATION_S \
                and (bat_last or 0) - (bat_start or 0) >= CHARGE_MIN_GAIN_PCT:
            sessions.append(last_ok - start)
        start = last_ok = bat_start = bat_last = None
    for ts, bat, in_w, out_w, _grid in samples:
        if prev_ts is not None and ts - prev_ts > MAX_GAP_S:        # пропуск данных обрывает сессию
            close()
        prev_ts = ts
        charging = (in_w or 0.0) - (out_w or 0.0) > CHARGE_MIN_NET_W
        if charging:
            if start is None:
                start, bat_start = ts, bat
            last_ok, bat_last = ts, bat
        elif start is not None and ts - last_ok > CHARGE_END_GRACE_S:
            close()
    close()

    return {
        "in_wh": in_ws / 3600.0, "out_wh": out_ws / 3600.0, "max_out_w": max_out,
        "outage_count": len(outages), "outage_total_s": outage_total,
        "charge_count": len(sessions), "charge_avg_s": sum(sessions) / len(sessions) if sessions else None,
        "samples": len(samples),
    }
