"""
Telegram-бот для моніторингу зарядної станції Oukitel (P2001E Plus / P2001 Plus / P5000 ...).

Дані беруться з MQTT-брокера, куди їх публікує міст oukitel-portable-bridge
(https://github.com/VictorCV-DAM/oukitel-portable-bridge).

Можливості:
  /status            — поточний стан (заряд, вхід, вихід, час роботи)
  /schedule 08:00    — щоденний звіт у вказаний час (можна кілька)
  /every 60          — звіт кожні N хвилин (0 — вимкнути)
  /schedules         — показати розклад
  /clear             — очистити розклад
  /alerts            — показати налаштування сповіщень
  /debug             — сирі MQTT-топіки (для діагностики)
Автоматичні сповіщення: низький заряд, повний заряд, зникнення/поява вхідного живлення
("світло зникло / з'явилося"), аварійні стани, відсутність даних.
"""

import asyncio
import html
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import paho.mqtt.client as mqtt
from dotenv import load_dotenv
from telegram import (BotCommand, InlineKeyboardButton, InlineKeyboardMarkup,
                      KeyboardButton, ReplyKeyboardMarkup, Update)
from telegram.error import BadRequest
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

from store import (LOW_LEVELS, NOTIFY_KEYS, ROLE_ADMIN, ROLE_GUEST, ROLE_OWNER, ROLE_TITLES, Store,
                   import_legacy_schedules)

load_dotenv()

# ---------------------------------------------------------------- налаштування
BOT_TOKEN = os.environ["BOT_TOKEN"]
ALLOWED_CHAT_IDS = {int(x) for x in os.getenv("ALLOWED_CHAT_IDS", "").replace(" ", "").split(",") if x}
TZ = ZoneInfo(os.getenv("TZ_NAME", "Europe/Kyiv"))

MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USER = os.getenv("MQTT_USER") or None
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD") or None
MQTT_TOPIC_BASE = os.getenv("MQTT_TOPIC_BASE", "homeassistant/sensor/oukitel").rstrip("/")

FULL_BATTERY = float(os.getenv("ALERT_FULL_BATTERY", "100"))
GRID_MIN_W = float(os.getenv("GRID_MIN_W", "5"))      # вхід з мережі від цієї потужності точно означає «мережа є»
GRID_LOAD_W = 10.0                                    # навантаження від цієї потужності при нульовому вході = «працюємо від батареї»
INPUT_DEBOUNCE_S = int(os.getenv("ALERT_INPUT_DEBOUNCE_S", "60"))
STALE_AFTER_S = int(os.getenv("STALE_AFTER_S", "300"))

DATA_FILE = Path(os.getenv("DATA_FILE", "data/schedules.json"))   # старий файл (версія 1), лише для міграції
STORE_FILE = Path(os.getenv("STORE_FILE", "data/store.json"))
DEFAULT_DEVICE_ID = "main"
DEFAULT_DEVICE_NAME = os.getenv("DEVICE_NAME", "Станція")   # тимчасова назва, поки справжню не знайдено
# Журнал(и) моста: звідти береться справжня назва станції (міст пише її лише в лог, не в MQTT)
BRIDGE_LOG_FILES = [p.strip() for p in os.getenv("BRIDGE_LOG_FILES", "/bridge/oukitel_bridge.log").split(",") if p.strip()]
UI_VERSION = 3   # при зміні меню/важливих правил бот один раз повідомляє про це всіх

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
log = logging.getLogger("oukitel-bot")
logging.getLogger("httpx").setLevel(logging.WARNING)


# ---------------------------------------------------------------- стан станції
class StationState:
    """Останні значення з MQTT, потокобезпечно."""

    def __init__(self):
        self._lock = threading.Lock()
        self.values: dict[str, object] = {}
        self.raw: dict[str, str] = {}
        self.updated: float = 0.0
        self.meta: dict[str, str] = {}      # що міст повідомив про пристрій: model, serial
        self.grid_last: bool | None = None  # остання ВІДОМА наявність мережі (коли станція простоює, її не видно)

    def update(self, topic: str, payload: bytes, base: str) -> None:
        key = topic_to_key(topic, base)
        if key is None:
            return
        text = payload.decode("utf-8", errors="replace").strip()
        value = parse_value(text)
        with self._lock:
            self.raw[topic] = text
            if isinstance(value, dict):
                # JSON-об'єкт: беремо всі вкладені поля як окремі ключі
                for k, v in value.items():
                    self.values[k] = parse_value(str(v)) if not isinstance(v, (int, float)) else v
            else:
                self.values[key] = value
            self.updated = time.time()
            sig = grid_signal(self.values)
            if sig in ("on", "off"):
                self.grid_last = sig == "on"

    def grid_status(self) -> tuple[bool | None, bool]:
        """(мережа є?, це точно?). None — даних немає."""
        with self._lock:
            return grid_status_from(self.values, self.grid_last)

    def snapshot(self) -> tuple[dict, float]:
        with self._lock:
            return dict(self.values), self.updated

    def raw_snapshot(self) -> dict:
        with self._lock:
            return dict(self.raw)


def topic_to_key(topic: str, base: str) -> str | None:
    """'homeassistant/sensor/oukitel/battery/state' -> 'battery'. Конфіг-топіки ігноруються."""
    if not topic.startswith(base):
        return None
    rest = topic[len(base):].strip("/")
    parts = [p for p in rest.split("/") if p]
    if not parts or parts[-1] in ("config", "attributes", "set", "command"):
        return None
    if parts[-1] == "state" and len(parts) > 1:
        parts = parts[:-1]
    return "_".join(parts) if parts else "state"


def parse_value(text: str):
    t = text.strip()
    try:
        return float(t)
    except ValueError:
        pass
    if t.startswith("{"):
        try:
            return json.loads(t)
        except json.JSONDecodeError:
            pass
    return t


# Стан кожного пристрою окремо: {"main": StationState, ...}
def parse_device_info(cfg: dict) -> dict[str, str]:
    """З опису сенсора (Home Assistant discovery) дістає серійний номер станції.
    Назва там шаблонна («Oukitel P2001»), тому її не беремо."""
    dev = cfg.get("device")
    if not isinstance(dev, dict):
        return {}
    info = {}
    for field, key in (("model", "model"), ("serial", "serial_number")):
        val = dev.get(key)
        if isinstance(val, str) and val.strip():
            info[field] = val.strip()
    if "serial" not in info:                      # запасний варіант — MAC із connections
        for conn in dev.get("connections") or []:
            if isinstance(conn, list) and len(conn) == 2 and conn[0] == "mac":
                info["serial"] = str(conn[1])
    return info


LOG_NAME_RE = re.compile(r"Power station detected:\s*(.+?)\s*\(DK:\s*([0-9A-Fa-f:]+)")


def read_bridge_names(paths: list[str], max_bytes: int = 8_000_000) -> dict[str, str]:
    """Шукає в журналах моста рядки 'Power station detected: <назва> (DK: <код>...)'.
    Повертає {код без двокрапок у нижньому регістрі: назва}. Код збігається з MAC (серійним номером) станції."""
    names: dict[str, str] = {}
    for path in paths:
        try:
            size = os.path.getsize(path)
            with open(path, "rb") as f:
                f.seek(max(0, size - max_bytes))
                text = f.read().decode("utf-8", errors="replace")
        except OSError:          # файлу немає, це тека тощо — просто пропускаємо
            continue
        for m in LOG_NAME_RE.finditer(text):
            names[m.group(2).replace(":", "").lower()] = m.group(1).strip()
    return names


def serial_key(serial: str) -> str:
    return serial.replace(":", "").lower()


STATES: dict[str, StationState] = {}
ADMIN_NOTICES: list[str] = []       # повідомлення для адмінів від MQTT-потоку; розсилає check_alerts
STORE: Store = None  # type: ignore[assignment]  # створюється в main()


def state_of(dev_id: str) -> StationState:
    return STATES.setdefault(dev_id, StationState())


def num(values: dict, *keys: str) -> float | None:
    for k in keys:
        v = values.get(k)
        if isinstance(v, (int, float)):
            return float(v)
    return None


# ---------------------------------------------------------------- форматування
def fmt_minutes(m: float | None) -> str:
    if m is None or m <= 0:
        return "—"
    m = int(m)
    if m >= 99 * 60:
        return "—"
    h, mm = divmod(m, 60)
    return f"{h} год {mm:02d} хв" if h else f"{mm} хв"


def fmt_w(w: float | None) -> str:
    return "—" if w is None else f"{w:.0f} Вт"


def battery_icon(p: float) -> str:
    return "🔋" if p >= 30 else "🪫"


FAULT_UA = {
    "High Temperature Warning": "Висока температура (попередження)",
    "Over-Temperature": "Перегрів",
    "Under-Temperature": "Занадто низька температура",
    "Low Battery Warning": "Низький заряд (попередження)",
    "Critical Low Battery": "Критично низький заряд",
    "Overload Protection": "Спрацював захист від перевантаження",
    "Hardware Fault": "Апаратна несправність",
}


def fault_list(values: dict) -> list[str]:
    """Список активних помилок станції (порожній, якщо все гаразд)."""
    fault = values.get("hardware_fault_status")
    if isinstance(fault, str) and fault.strip() and fault.strip().lower() != "normal":
        return [FAULT_UA.get(fault.strip(), fault.strip())]
    return []


def grid_watts(values: dict) -> float | None:
    """Потужність входу саме з мережі (AC). Якщо мост її окремо не віддає — загальний вхід."""
    w = num(values, "ac_input_watts", "ac_input_power")
    return w if w is not None else num(values, "input_watts", "total_input_power")


def grid_signal(values: dict) -> str | None:
    """Що можна сказати про мережу за поточними показниками станції:
    'on' — точно є, 'off' — точно немає, 'idle' — заряджена й нічого не живить (за потужністю не відрізнити),
    None — даних немає. Прапорця «є мережа» мост не віддає, тому міркуємо за потужністю."""
    grid = grid_watts(values)
    if grid is None:
        return None
    if grid >= GRID_MIN_W:
        return "on"
    tout = num(values, "output_watts", "total_output_power", "output_power")
    if tout is not None and tout >= GRID_LOAD_W:
        return "off"          # споживаємо, а входу немає: при живій мережі вхід був би не меншим за навантаження
    bat = num(values, "battery", "battery_level", "soc")
    limit = num(values, "ac_charging_limit") or 100.0
    if bat is not None and bat < limit - 10:
        return "off"          # при живій мережі станція вже заряджалась би
    return "idle"


def grid_status_from(values: dict, last_known: bool | None) -> tuple[bool | None, bool]:
    """(є мережа?, достовірно?). У стані 'idle' беремо останній відомий стан,
    а якщо його немає — вважаємо, що мережа є (повна батарея без споживання майже завжди означає живу мережу)."""
    sig = grid_signal(values)
    if sig is None:
        return None, False
    if sig == "idle":
        return (True if last_known is None else last_known), False
    return sig == "on", True


def grid_line(grid: tuple[bool | None, bool], short: bool = True) -> str | None:
    present, certain = grid
    if present is None:
        return None
    word = "є" if present else "немає"
    if short:
        return f"Мережа: {'✅' if present else '❌'} {word}" + ("" if certain else " (ймовірно)")
    return f"Мережа: {word}" + ("" if certain else " (ймовірно: станція заряджена й нічого не живить, точно визначити неможливо)")


def updated_line(updated: float) -> tuple[str, str]:
    ts = datetime.fromtimestamp(updated, TZ).strftime("%d.%m.%Y %H:%M:%S")
    stale = " (дані застарілі)" if time.time() - updated > STALE_AFTER_S else ""
    return ts, stale


def no_data(name: str) -> str:
    return f"⚡ <b>{name}</b>\n⏳ Даних від станції ще немає. Спробуй за хвилину."


def format_status(values: dict, updated: float, name: str = "Станція", grid=None) -> str:
    """Короткий стан: з картинками, для швидкого погляду."""
    if not updated:
        return no_data(name)
    lines = [f"⚡ <b>{name}</b>"]

    gline = grid_line(grid if grid is not None else grid_status_from(values, None))
    if gline:
        lines.append(gline)

    bat = num(values, "battery", "battery_level", "soc")
    temp = num(values, "temperature", "temp")
    parts = []
    if bat is not None:
        parts.append(f"{battery_icon(bat)} <b>{bat:.0f}%</b>")
    if temp is not None:
        parts.append(f"🌡 {temp:.0f} °C")
    if parts:
        lines.append("   ".join(parts))

    tin = num(values, "input_watts", "total_input_power", "input_power")
    tout = num(values, "output_watts", "total_output_power", "output_power")
    lines.append(f"⬇️ Вхід: <b>{fmt_w(tin)}</b>   ⬆️ Вихід: <b>{fmt_w(tout)}</b>")

    faults = fault_list(values)
    if faults:
        lines.append(f"Помилки: {len(faults)}")
        lines += [f"⚠️ {f}" for f in faults]
    else:
        lines.append("Помилки: ✅ немає")

    if str(values.get("availability", "online")).lower() == "offline":
        lines.append("📴 Станція не на зв'язку (спить або без Wi-Fi), дані можуть бути старими")

    ts, stale = updated_line(updated)
    lines.append(f"\n🕐 Оновлено: {ts}{stale}")
    return "\n".join(lines)


def _txt(values: dict, key: str) -> str | None:
    v = values.get(key)
    return None if v is None or str(v).strip() in ("", "—") else str(v).strip()


def _onoff(v: str | None) -> str | None:
    return None if v is None else ("увімкнено" if v.upper() == "ON" else "вимкнено")


def format_detail(values: dict, updated: float, name: str = "Станція", grid=None) -> str:
    """Детальний стан: суха інформація без картинок."""
    if not updated:
        return no_data(name)
    L = [f"<b>{name}</b> — детальний стан", ""]

    gline = grid_line(grid if grid is not None else grid_status_from(values, None), short=False)
    if gline:
        L.append(gline)
    bat = num(values, "battery", "battery_level", "soc")
    if bat is not None:
        L.append(f"Заряд батареї: {bat:.0f}%")
    temp = num(values, "temperature", "temp")
    if temp is not None:
        L.append(f"Температура: {temp:.0f} °C")

    tin = num(values, "input_watts", "total_input_power", "input_power")
    tout = num(values, "output_watts", "total_output_power", "output_power")
    ac_in, dc_in = num(values, "ac_input_watts", "ac_input_power"), num(values, "dc_input_watts", "dc_solar_input_power")
    L.append("")
    L.append(f"Вхід: {fmt_w(tin)}" + (f" (мережа {fmt_w(ac_in)}, сонце/DC {fmt_w(dc_in)})" if ac_in is not None or dc_in is not None else ""))
    ac_out, dc_out = num(values, "ac_output_power"), num(values, "dc_output_power")
    L.append(f"Вихід: {fmt_w(tout)}" + (f" (AC {fmt_w(ac_out)}, DC 12 В {fmt_w(dc_out)})" if ac_out is not None or dc_out is not None else ""))
    if tin is not None and tout is not None:
        net = tin - tout
        L.append(f"Потужність заряду батареї: {net:+.0f} Вт " + ("(заряджається)" if net > 5 else "(розряджається)" if net < -5 else "(баланс)"))
    limit = num(values, "ac_charging_limit")
    if limit is not None:
        L.append(f"Ліміт зарядки з мережі: {limit:.0f}%")

    L.append("")
    ac_v, freq, set_v = num(values, "ac_output_voltage"), _txt(values, "output_frequency"), _txt(values, "output_voltage")
    out_bits = []
    if ac_v is not None:
        out_bits.append(f"{ac_v:.0f} В")
    if freq:
        out_bits.append(freq.replace("Hz", " Гц"))
    if out_bits:
        L.append("Вихід AC зараз: " + ", ".join(out_bits))
    if set_v:
        L.append("Задана напруга виходу: " + set_v.replace("V", " В"))
    for label, key in (("Вихід AC", "ac_switch"), ("Вихід DC 12 В", "dc_switch"), ("USB", "usb_switch")):
        state = _onoff(_txt(values, key))
        if state:
            L.append(f"{label}: {state}")
    dv, da = num(values, "dc_output_voltage"), num(values, "dc_output_current")
    if dv is not None and da is not None:
        L.append(f"DC 12 В: {dv:.1f} В, {da:.1f} А")

    ports = [(f"Type-C {i}", num(values, f"typec{i}_power")) for i in range(1, 5)]
    ports += [("USB-A", num(values, "usb_a_power")), ("USB-C QC", num(values, "usb_c_qc_power"))]
    ports = [(n, p) for n, p in ports if p is not None]
    if ports:
        L.append("Порти: " + ", ".join(f"{n} {p:.0f} Вт" for n, p in ports))

    remain, charge_t = num(values, "remain_time", "remaining_discharge_time"), num(values, "remain_charging_time", "remaining_charge_time")
    if tin is not None and tout is not None and tout > tin and remain:
        L.append(f"Вистачить на: {fmt_minutes(remain)}")
    elif charge_t:
        L.append(f"До повного заряду: {fmt_minutes(charge_t)}")

    L.append("")
    wifi, mode = num(values, "wifi_signal"), _txt(values, "connection_mode")
    if wifi is not None:
        L.append(f"Wi-Fi: {wifi:.0f} дБм")
    if mode:
        L.append(f"Підключення до станції: {mode}")
    ver = [f"{n} {_txt(values, k)}" for n, k in (("BMS", "bms_version"), ("інвертор", "inverter_version")) if _txt(values, k)]
    if ver:
        L.append("Версії: " + ", ".join(ver))
    avail = _txt(values, "availability")
    if avail:
        L.append("Зв'язок станції з хмарою: " + ("є" if avail.lower() == "online" else "немає"))

    faults = fault_list(values)
    L.append("Помилки: " + ("; ".join(faults) if faults else "немає"))
    ts, stale = updated_line(updated)
    L.append(f"\nОновлено: {ts}{stale}")
    return "\n".join(L)


# ---------------------------------------------------------------- допоміжне
B = InlineKeyboardButton


def esc(text: object) -> str:
    return html.escape(str(text))


def status_text(dev_id: str, chat_id: int) -> str:
    st = state_of(dev_id)
    values, updated = st.snapshot()
    return format_status(values, updated, esc(STORE.label(chat_id, dev_id)), st.grid_status())


def detail_text(dev_id: str, chat_id: int) -> str:
    st = state_of(dev_id)
    values, updated = st.snapshot()
    return format_detail(values, updated, esc(STORE.label(chat_id, dev_id)), st.grid_status())


def role_summary(chat_id: int, full: bool = False) -> str:
    """'Адмін' або 'Моя 1 кВт: Власник, Мама 2 кВт: Гість'. full=True — з номерами станцій (для адміна)."""
    if STORE.is_admin(chat_id):
        return ROLE_TITLES[ROLE_ADMIN]
    parts = [f"{esc(STORE.full_name(d) if full else STORE.label(chat_id, d))}: {ROLE_TITLES[STORE.role(chat_id, d)]}"
             for d in STORE.devices_for(chat_id)]
    return ", ".join(parts) or "без доступу"


# ---------------------------------------------------------------- розклади
def load_legacy_schedules() -> dict:
    """Старий файл розкладів (версія 1) — потрібен лише для одноразової міграції."""
    try:
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


async def send_report(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = context.job.chat_id
    for dev_id in STORE.devices_for(chat_id):
        await context.bot.send_message(chat_id, status_text(dev_id, chat_id), parse_mode="HTML")


def register_jobs(app: Application, chat_id: int, cfg: dict) -> None:
    jq = app.job_queue
    for job in jq.get_jobs_by_name(f"report-{chat_id}"):
        job.schedule_removal()
    for t in cfg.get("times", []):
        hh, mm = map(int, t.split(":"))
        jq.run_daily(send_report, dtime(hh, mm, tzinfo=TZ), chat_id=chat_id, name=f"report-{chat_id}")
    interval = int(cfg.get("interval_min", 0) or 0)
    if interval > 0:
        jq.run_repeating(send_report, interval * 60, first=interval * 60, chat_id=chat_id, name=f"report-{chat_id}")


# ---------------------------------------------------------------- доступ
def allowed(update: Update) -> bool:
    return STORE.get_user(update.effective_chat.id) is not None


def is_admin(update: Update) -> bool:
    return STORE.is_admin(update.effective_chat.id)


# ---------------------------------------------------------------- кнопки та екрани
BTN_STATUS = "📊 Стан"
BTN_CONTROL = "🎛 Керування"
BTN_ALERTS = "🔔 Сповіщення"
BTN_STATS = "📈 Статистика"
BTN_HELP = "ℹ️ Допомога"
BTN_USERS = "👥 Користувачі"
BTN_SCHEDULE = "⏰ Розклад"      # стара кнопка (версія 1): лишаємо, щоб не зламати старе меню у людей

View = tuple[str, "InlineKeyboardMarkup | None"]
HOME = B("🏠 Головне меню", callback_data="m:main")


def menu_for(chat_id: int) -> ReplyKeyboardMarkup:
    """Постійне меню внизу екрана. Адмін додатково бачить «Користувачі»."""
    last = [KeyboardButton(BTN_HELP)]
    if STORE.is_admin(chat_id):
        last.append(KeyboardButton(BTN_USERS))
    return ReplyKeyboardMarkup(
        [[KeyboardButton(BTN_STATUS), KeyboardButton(BTN_CONTROL)],
         [KeyboardButton(BTN_ALERTS), KeyboardButton(BTN_STATS)], last],
        resize_keyboard=True, is_persistent=True,
    )


def main_menu_view(chat_id: int) -> View:
    rows = [[B(BTN_STATUS, callback_data="m:status"), B(BTN_CONTROL, callback_data="m:control")],
            [B(BTN_ALERTS, callback_data="m:alerts"), B(BTN_STATS, callback_data="m:stats")],
            [B(BTN_HELP, callback_data="m:help")]]
    if STORE.is_admin(chat_id):
        rows[-1].append(B(BTN_USERS, callback_data="m:users"))
    return "🏠 <b>Головне меню</b>\nОбери розділ:", InlineKeyboardMarkup(rows)


def status_entry_view(chat_id: int) -> View:
    """«Стан»: одна станція — одразу її стан, кілька — спочатку вибір."""
    devs = STORE.devices_for(chat_id)
    if not devs:
        return "У тебе поки немає доступу до жодної станції. Попроси адміна.", InlineKeyboardMarkup([[HOME]])
    if len(devs) == 1:
        return status_view(devs[0], chat_id)
    rows = [[B(STORE.label(chat_id, d), callback_data=f"st:{d}")] for d in devs] + [[HOME]]
    return "Яка станція?", InlineKeyboardMarkup(rows)


def status_view(dev_id: str, chat_id: int) -> View:
    back = "m:status" if len(STORE.devices_for(chat_id)) > 1 else "m:main"
    kb = InlineKeyboardMarkup([
        [B("🔄 Оновити", callback_data=f"st:{dev_id}"), B("✏️ Назва", callback_data=f"ren:{dev_id}")],
        [B("📋 Детальний стан", callback_data=f"sd:{dev_id}")],
        [B("⬅️ Назад", callback_data=back), HOME],
    ])
    return status_text(dev_id, chat_id), kb


def detail_view(dev_id: str, chat_id: int) -> View:
    kb = InlineKeyboardMarkup([
        [B("🔄 Оновити", callback_data=f"sd:{dev_id}")],
        [B("⬅️ Назад", callback_data=f"st:{dev_id}"), HOME],
    ])
    return detail_text(dev_id, chat_id), kb


NOTIFY_TITLES = {
    "light_on": "Світло з'явилося",
    "light_off": "Світло зникло",
    "full": "Станція заряджена",
    "error": "Помилка станції",
    "stale": f"Немає даних довше {STALE_AFTER_S // 60} хв",
}


def notify_title(key: str, prefs: dict) -> str:
    return f"Заряд станції {prefs['low_level']}%" if key == "low" else NOTIFY_TITLES[key]


def alerts_view(chat_id: int) -> View:
    """«Сповіщення»: одна станція — одразу її налаштування, кілька — спочатку вибір."""
    devs = STORE.devices_for(chat_id)
    if not devs:
        return "У тебе поки немає доступу до жодної станції. Попроси адміна.", InlineKeyboardMarkup([[HOME]])
    if len(devs) == 1:
        return notify_view(devs[0], chat_id)
    rows = [[B(STORE.label(chat_id, d), callback_data=f"nt:{d}")] for d in devs]
    rows.append([B("⏰ Розклад звітів", callback_data="sch:open")])
    rows.append([HOME])
    return "🔔 <b>Сповіщення</b>\n\nДля якої станції налаштувати?", InlineKeyboardMarkup(rows)


def notify_view(dev_id: str, chat_id: int) -> View:
    prefs = STORE.notify(chat_id, dev_id)
    on = [notify_title(k, prefs) for k in NOTIFY_KEYS if prefs[k]]
    current = "\n".join(f"✅ {t}" for t in on) if on else "Усі сповіщення вимкнені."
    text = (
        f"🔔 <b>Сповіщення — {esc(STORE.label(chat_id, dev_id))}</b>\n\n"
        f"<b>Зараз увімкнено:</b>\n{current}\n\n"
        "Натисни на сповіщення, щоб увімкнути чи вимкнути його. "
        "Для заряду нижче обери поріг — коли попереджати."
    )
    rows = []
    for key in NOTIFY_KEYS:
        rows.append([B(("✅ " if prefs[key] else "⬜ ") + notify_title(key, prefs), callback_data=f"nt:{dev_id}:{key}")])
        if key == "low":
            rows.append([B(("● " if prefs["low_level"] == lvl else "") + f"{lvl}%", callback_data=f"nl:{dev_id}:{lvl}")
                         for lvl in LOW_LEVELS])
    rows.append([B("⏰ Розклад звітів", callback_data="sch:open")])
    back = "m:alerts" if len(STORE.devices_for(chat_id)) > 1 else "m:main"
    rows.append([B("⬅️ Назад", callback_data=back), HOME])
    return text, InlineKeyboardMarkup(rows)


def soon_view(title: str) -> View:
    return f"{title}\n\n🚧 Цей розділ ще в розробці. Скоро буде!", InlineKeyboardMarkup([[HOME]])


def schedule_view(chat_id: int) -> View:
    cfg = STORE.schedule(chat_id)
    return schedule_text(cfg), schedule_kb(cfg)


async def reply_view(update: Update, view: View) -> None:
    text, kb = view
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)


PRESET_TIMES = ["07:00", "09:00", "12:00", "15:00", "18:00", "21:00", "23:00"]
PRESET_INTERVALS = [(0, "вимк"), (30, "30 хв"), (60, "1 год"), (180, "3 год")]


# ---------------------------------------------------------------- команди
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    name = update.effective_user.full_name if update.effective_user else ""

    # перше підключення: якщо адміна ще немає і ALLOWED_CHAT_IDS порожній — перший стає адміном
    if STORE.get_user(chat_id) is None and not ALLOWED_CHAT_IDS \
            and not any(u.get("admin") for _, u in STORE.all_users()):
        STORE.ensure_admin(chat_id, name)
        log.warning("Адміна не було — %s призначено адміном. Додай його chat_id у ALLOWED_CHAT_IDS", chat_id)

    # запрошення: t.me/<бот>?start=inv_КОД
    if context.args and context.args[0].startswith("inv_"):
        if STORE.use_invite(context.args[0][4:], chat_id, name):
            await update.message.reply_text("✅ Запрошення прийнято!")
        elif not allowed(update):
            await update.message.reply_text("⛔ Запрошення недійсне або вже використане. Попроси адміна створити нове.")
            return

    if not allowed(update):
        await update.message.reply_text(
            f"⛔ Доступ лише за запрошенням. Твій chat_id: {chat_id}\n"
            "Попроси адміна надіслати тобі посилання-запрошення."
        )
        return

    await update.message.reply_text(
        "👋 Я стежу за домашніми девайсами.\n\n"
        f"Твій доступ: <b>{role_summary(chat_id)}</b>\n\n"
        "Що я вмію і як цим користуватися — в інструкції: /help",
        parse_mode="HTML",
        reply_markup=menu_for(chat_id),
    )


HELP_COMMON = (
    "📖 <b>Інструкція</b>\n\n"
    "📊 <b>Стан</b> — головні показники станції: чи є світло, заряд, температура, вхід і вихід у Вт, помилки. "
    "Кнопка «📋 Детальний стан» показує докладніше: частоту, порти, версії тощо. «🔄 Оновити» бере свіжі дані.\n\n"
    "✏️ <b>Назва</b> — кнопка під станом (або /rename). Можна назвати станцію зручно, наприклад «Дача». "
    "Нову назву бачиш тільки ти.\n\n"
    "🔔 <b>Сповіщення</b> — що бот має писати сам. Спочатку всі вимкнені: обери станцію й увімкни потрібне — "
    "світло з'явилося чи зникло (з тим, скільки світла не було), станція заряджена, заряд упав до порога "
    "(поріг обираєш сам), помилка станції, немає даних. Налаштування окремі для кожної людини й станції. "
    "Там же «⏰ Розклад звітів»: бот сам надсилатиме стан у потрібний час або кожні кілька хвилин "
    "(свій час: /schedule 08:30).\n\n"
    "🎛 <b>Керування</b> і 📈 <b>Статистика</b> — скоро."
)
HELP_ADMIN = (
    "\n\n👥 <b>Користувачі</b> — хто має доступ і до яких станцій; тут можна змінити роль чи видалити людину.\n\n"
    "➕ /invite — запросити людину: обираєш роль і станції та отримуєш посилання.\n"
    "• <b>Власник</b> — керує обраними станціями.\n"
    "• <b>Гість</b> — лише дивиться.\n\n"
    "🏷 /devname — офіційна назва станції (однакова для всіх; зазвичай бот бере її сам)."
)


def help_view(chat_id: int) -> View:
    return HELP_COMMON + (HELP_ADMIN if STORE.is_admin(chat_id) else ""), InlineKeyboardMarkup([[HOME]])


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    await reply_view(update, help_view(update.effective_chat.id))


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    await reply_view(update, status_entry_view(update.effective_chat.id))


async def cmd_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text("Вкажи час: /schedule 08:00 (можна кілька: /schedule 08:00 20:00)")
        return
    times = []
    for a in context.args:
        try:
            hh, mm = map(int, a.split(":"))
            assert 0 <= hh < 24 and 0 <= mm < 60
            times.append(f"{hh:02d}:{mm:02d}")
        except Exception:
            await update.message.reply_text(f"Не розумію час «{a}». Формат: ГГ:ХХ")
            return
    cfg = STORE.schedule(chat_id)
    cfg["times"] = sorted(set(cfg["times"]) | set(times))
    STORE.save()
    register_jobs(context.application, chat_id, cfg)
    await update.message.reply_text(f"✅ Щоденні звіти о: {', '.join(cfg['times'])}")


async def cmd_every(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    chat_id = update.effective_chat.id
    try:
        minutes = int(context.args[0])
        assert minutes >= 0
    except Exception:
        await update.message.reply_text("Формат: /every 60  (хвилини, 0 — вимкнути)")
        return
    cfg = STORE.schedule(chat_id)
    cfg["interval_min"] = minutes
    STORE.save()
    register_jobs(context.application, chat_id, cfg)
    await update.message.reply_text(
        f"✅ Звіт кожні {minutes} хв" if minutes else "✅ Періодичні звіти вимкнено"
    )


async def cmd_schedules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    cfg = STORE.schedule(update.effective_chat.id)
    if not cfg.get("times") and not cfg.get("interval_min"):
        await update.message.reply_text("Розклад порожній. Додай: /schedule 08:00 або /every 60")
        return
    parts = []
    if cfg.get("times"):
        parts.append("🕗 Щодня о: " + ", ".join(cfg["times"]))
    if cfg.get("interval_min"):
        parts.append(f"🔁 Кожні {cfg['interval_min']} хв")
    await update.message.reply_text("\n".join(parts))


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    chat_id = update.effective_chat.id
    cfg = STORE.schedule(chat_id)
    cfg["times"], cfg["interval_min"] = [], 0
    STORE.save()
    register_jobs(context.application, chat_id, cfg)
    await update.message.reply_text("🗑 Розклад очищено (сповіщення про події залишаються)")


async def cmd_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    await reply_view(update, alerts_view(update.effective_chat.id))


async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update) or not is_admin(update):
        return
    chunks = []
    for dev_id, info in STORE.devices().items():
        base = info["topic_base"]
        raw = state_of(dev_id).raw_snapshot()
        meta = state_of(dev_id).meta
        src = {"log": "з журналу моста", "manual": "задана адміном"}.get(info.get("name_src"), "тимчасова")
        head = (f"[{STORE.full_name(dev_id)}] id={dev_id}, назва: {src}, "
                f"модель: {meta.get('model', '—')}, серійний: {meta.get('serial', '—')}")
        if not raw:
            chunks.append(f"{head}\nMQTT-повідомлень не отримано.\nБрокер: {MQTT_HOST}:{MQTT_PORT}\nТопік: {base}/#")
            continue
        lines = [f"{t.removeprefix(base + '/')} = {v[:60]}" for t, v in sorted(raw.items())]
        chunks.append(f"{head}\n" + "\n".join(lines))
    text = "\n\n".join(chunks)
    for i in range(0, len(text), 3800):
        await update.message.reply_text(text[i:i + 3800])


# ---------------------------------------------------------------- назви станцій
def rename_prompt(chat_id: int, dev_id: str, official: bool = False) -> str:
    if official:
        return (
            f"🏷 Офіційна назва станції <b>{esc(STORE.device_name(dev_id))}</b> (її бачать усі, у кого немає власної).\n\n"
            "Напиши нову назву (до 40 символів). Щоб знову брати її з журналу моста — надішли «-»."
        )
    return (
        f"✏️ Як називати станцію <b>{esc(STORE.device_name(dev_id))}</b>?\n\n"
        "Напиши нову назву (до 40 символів). Її бачитимеш тільки ти.\n"
        "Щоб повернути стандартну назву — надішли «-»."
    )


def start_rename(context: ContextTypes.DEFAULT_TYPE, dev_id: str, official: bool) -> None:
    context.user_data["rename"] = dev_id
    context.user_data["rename_scope"] = "official" if official else "personal"


def stop_rename(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("rename", None)
    context.user_data.pop("rename_scope", None)


async def _rename_command(update: Update, context: ContextTypes.DEFAULT_TYPE, official: bool) -> None:
    chat_id = update.effective_chat.id
    devs = STORE.devices_for(chat_id)
    prefix = "dn" if official else "ren"
    if not devs:
        await update.message.reply_text("У тебе поки немає доступу до жодної станції.")
    elif len(devs) == 1:
        start_rename(context, devs[0], official)
        await update.message.reply_text(rename_prompt(chat_id, devs[0], official), parse_mode="HTML")
    else:
        await update.message.reply_text("Яку станцію назвати?", reply_markup=InlineKeyboardMarkup(
            [[B(STORE.label(chat_id, d) if not official else STORE.full_name(d), callback_data=f"{prefix}:{d}")]
             for d in devs]))


async def cmd_rename(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if allowed(update):
        await _rename_command(update, context, official=False)


async def cmd_devname(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if allowed(update) and is_admin(update):
        await _rename_command(update, context, official=True)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Звичайний текст: приймаємо лише якщо чекаємо нову назву станції."""
    if not allowed(update):
        return
    dev_id = context.user_data.get("rename")
    if not dev_id:
        return
    official = context.user_data.get("rename_scope") == "official"
    chat_id = update.effective_chat.id
    if not STORE.role(chat_id, dev_id) or (official and not is_admin(update)):
        stop_rename(context)
        return
    text = " ".join((update.message.text or "").split())   # прибираємо переноси й зайві пробіли
    if text in ("-", "—"):
        if official:
            STORE.clear_manual_name(dev_id)
            reply = "↩️ Добре, далі братиму назву з журналу моста (протягом кількох хвилин)."
        else:
            STORE.set_alias(chat_id, dev_id, None)
            reply = f"↩️ Повернув стандартну назву: <b>{esc(STORE.label(chat_id, dev_id))}</b>"
    elif 0 < len(text) <= 40:
        if official:
            STORE.set_device_name(dev_id, text, "manual")
            reply = f"✅ Готово! Офіційна назва: <b>{esc(text)}</b>."
        else:
            STORE.set_alias(chat_id, dev_id, text)
            reply = f"✅ Готово! Тепер станція називається <b>{esc(text)}</b>."
    else:
        await update.message.reply_text("Назва має бути від 1 до 40 символів. Спробуй ще раз або надішли «-».")
        return
    stop_rename(context)
    await update.message.reply_text(reply, parse_mode="HTML")


async def sync_bridge_names(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Підтягує справжні назви станцій із журналу моста (для станцій, у яких назву ще не знайшли/не задали)."""
    pending = {d: serial_key(i["serial"]) for d, i in STORE.devices().items()
               if i.get("serial") and i.get("name_src") is None}
    if not pending:
        return
    names = await asyncio.to_thread(read_bridge_names, BRIDGE_LOG_FILES)
    for dev_id, key in pending.items():
        if key in names and STORE.set_device_name(dev_id, names[key], "log"):
            log.info("Назва станції %s з журналу моста: %s", dev_id, names[key])


# ---------------------------------------------------------------- запрошення та користувачі (адмін)
def invite_view(inv: dict) -> tuple[str, InlineKeyboardMarkup]:
    devs = STORE.devices()
    chosen = [d for d in devs if d in inv["devs"]]
    names = ", ".join(esc(STORE.full_name(d)) for d in chosen) or "не обрано"
    text = (
        "➕ <b>Нове запрошення</b>\n\n"
        f"Роль: <b>{ROLE_TITLES[inv['role']]}</b>\n"
        f"Станції: <b>{names}</b>\n\n"
        "Власник керує обраними станціями, гість лише дивиться.\n"
        "Обери й натисни «Створити посилання»."
    )
    rows = [[B(("● " if inv["role"] == r else "") + ROLE_TITLES[r], callback_data=f"inv:role:{r}")
             for r in (ROLE_GUEST, ROLE_OWNER)]]
    for d, info in devs.items():
        rows.append([B(("✅ " if d in inv["devs"] else "⬜ ") + STORE.full_name(d), callback_data=f"inv:dev:{d}")])
    rows.append([B("🔗 Створити посилання", callback_data="inv:make")])
    return text, InlineKeyboardMarkup(rows)


async def cmd_invite(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update) or not is_admin(update):
        return
    devs = list(STORE.devices())
    inv = {"role": ROLE_GUEST, "devs": set(devs) if len(devs) == 1 else set()}
    context.user_data["invite"] = inv
    text, kb = invite_view(inv)
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)


def users_view() -> tuple[str, InlineKeyboardMarkup]:
    users = STORE.all_users()
    lines = ["👥 <b>Користувачі</b>\n"]
    rows = []
    for cid, u in users:
        title = u.get("name") or str(cid)
        lines.append(f"• {esc(title)} — {role_summary(cid, full=True)}")
        rows.append([B(f"👤 {title}", callback_data=f"usr:open:{cid}")])
    lines.append("\nНатисни на людину, щоб змінити доступ. Нових людей додає /invite.")
    rows.append([HOME])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def user_card(cid: int) -> tuple[str, InlineKeyboardMarkup]:
    u = STORE.get_user(cid) or {}
    title = u.get("name") or str(cid)
    rows = []
    if u.get("admin"):
        text = f"👤 <b>{esc(title)}</b>\n\nАдмін: доступ до всього. Задається в .env (ALLOWED_CHAT_IDS)."
    else:
        text = (f"👤 <b>{esc(title)}</b>\n\nНатискай на станцію, щоб змінити роль:\n"
                "— немає доступу → Гість → Власник → немає доступу")
        for d, info in STORE.devices().items():
            r = STORE.role(cid, d)
            label = ROLE_TITLES[r] if r else "—"
            rows.append([B(f"{STORE.full_name(d)}: {label}", callback_data=f"usr:cyc:{cid}:{d}")])
        rows.append([B("🗑 Видалити користувача", callback_data=f"usr:del:{cid}")])
    rows.append([B("⬅️ Назад", callback_data="usr:list")])
    return text, InlineKeyboardMarkup(rows)


async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update) or not is_admin(update):
        return
    text, kb = users_view()
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)


# ---------------------------------------------------------------- кнопки під повідомленнями
def schedule_text(cfg: dict) -> str:
    times = ", ".join(cfg.get("times") or []) or "немає"
    interval = cfg.get("interval_min") or 0
    every = f"кожні {interval} хв" if interval else "вимкнено"
    return (
        "⏰ <b>Розклад звітів</b>\n\n"
        f"🕗 Щодня о: <b>{times}</b>\n"
        f"🔁 Періодично: <b>{every}</b>\n\n"
        "Натискай на час, щоб увімкнути ✅ або вимкнути його.\n"
        "Свій час: <code>/schedule 08:30</code>"
    )


def schedule_kb(cfg: dict) -> InlineKeyboardMarkup:
    chosen = set(cfg.get("times") or [])
    time_btns = [
        B(("✅ " if t in chosen else "") + t, callback_data=f"sch:t:{t}")
        for t in PRESET_TIMES
    ]
    rows = [time_btns[i:i + 4] for i in range(0, len(time_btns), 4)]
    # нестандартні часи, додані командою, теж показуємо, щоб їх можна було прибрати
    custom = [t for t in sorted(chosen) if t not in PRESET_TIMES]
    if custom:
        rows.append([B(f"✅ {t}", callback_data=f"sch:t:{t}") for t in custom][:4])
    cur = cfg.get("interval_min") or 0
    rows.append([
        B(("● " if cur == m else "") + label, callback_data=f"sch:i:{m}")
        for m, label in PRESET_INTERVALS
    ])
    rows.append([B("🗑 Очистити все", callback_data="sch:clear")])
    rows.append([B("⬅️ Назад", callback_data="m:alerts"), HOME])
    return InlineKeyboardMarkup(rows)


async def on_menu_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обробка натискань на кнопки нижнього меню."""
    if not allowed(update):
        return
    chat_id = update.effective_chat.id
    stop_rename(context)      # натиснув кнопку меню — вводити назву вже не чекаємо
    text = update.message.text
    if text == BTN_STATUS:
        view = status_entry_view(chat_id)
    elif text == BTN_CONTROL:
        view = soon_view("🎛 <b>Керування</b>")
    elif text == BTN_ALERTS:
        view = alerts_view(chat_id)
    elif text == BTN_STATS:
        view = soon_view("📈 <b>Статистика</b>")
    elif text == BTN_HELP:
        view = help_view(chat_id)
    elif text == BTN_SCHEDULE:
        view = schedule_view(chat_id)
    elif text == BTN_USERS and is_admin(update):
        view = users_view()
    else:
        return
    await reply_view(update, view)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обробка натискань на кнопки під повідомленнями."""
    query = update.callback_query
    if not allowed(update):
        await query.answer("⛔ Немає доступу")
        return
    data = query.data or ""
    chat_id = update.effective_chat.id
    app = context.application

    try:
        # --- головне меню та розділи
        if data.startswith("m:"):
            name = data[2:]
            if name == "users" and not is_admin(update):
                await query.answer("⛔ Лише для адміна")
                return
            views = {
                "main": lambda: main_menu_view(chat_id),
                "status": lambda: status_entry_view(chat_id),
                "control": lambda: soon_view("🎛 <b>Керування</b>"),
                "alerts": lambda: alerts_view(chat_id),
                "stats": lambda: soon_view("📈 <b>Статистика</b>"),
                "help": lambda: help_view(chat_id),
                "users": users_view,
            }
            if name not in views:
                await query.answer()
                return
            text, kb = views[name]()
            await query.answer()
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
            return

        # --- стан станції (st: — короткий, sd: — детальний; status:refresh:/dev:status: — старі кнопки)
        if data.startswith(("st:", "sd:", "status:refresh:", "dev:status:")):
            dev_id = data.split(":")[-1]
            if not STORE.role(chat_id, dev_id):
                await query.answer("⛔ Немає доступу до цієї станції")
                return
            text, kb = detail_view(dev_id, chat_id) if data.startswith("sd:") else status_view(dev_id, chat_id)
            await query.answer("Оновлено")
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
            return

        # --- налаштування сповіщень: nt:<станція> — відкрити, nt:<станція>:<ключ> — перемкнути, nl:<станція>:<поріг>
        if data.startswith(("nt:", "nl:")):
            _, dev_id, *rest = data.split(":")
            if not STORE.role(chat_id, dev_id):
                await query.answer("⛔ Немає доступу до цієї станції")
                return
            if data.startswith("nt:") and rest and rest[0] in NOTIFY_KEYS:
                prefs = STORE.notify(chat_id, dev_id)
                STORE.set_notify(chat_id, dev_id, rest[0], not prefs[rest[0]])
                await query.answer("Увімкнено" if not prefs[rest[0]] else "Вимкнено")
            elif data.startswith("nl:") and rest and rest[0].isdigit() and int(rest[0]) in LOW_LEVELS:
                STORE.set_notify(chat_id, dev_id, "low_level", int(rest[0]))
                await query.answer(f"Поріг: {rest[0]}%")
            else:
                await query.answer()
            text, kb = notify_view(dev_id, chat_id)
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
            return

        # --- назва станції: ren: — особиста, dn: — офіційна (лише адмін)
        if data.startswith(("ren:", "dn:")):
            official = data.startswith("dn:")
            dev_id = data.split(":", 1)[1]
            if not STORE.role(chat_id, dev_id) or (official and not is_admin(update)):
                await query.answer("⛔ Немає доступу до цієї станції")
                return
            start_rename(context, dev_id, official)
            await query.answer()
            await query.message.reply_text(rename_prompt(chat_id, dev_id, official), parse_mode="HTML")
            return

        # --- розклад
        if data.startswith("sch:"):
            cfg = STORE.schedule(chat_id)
            _, kind, *rest = data.split(":", 2)
            if kind == "t":
                t = rest[0]
                times = set(cfg.get("times") or [])
                times.symmetric_difference_update({t})   # був — прибрати, не було — додати
                cfg["times"] = sorted(times)
                await query.answer(f"{t} {'додано' if t in times else 'прибрано'}")
            elif kind == "i":
                cfg["interval_min"] = int(rest[0])
                await query.answer("Збережено")
            elif kind == "clear":
                cfg["times"], cfg["interval_min"] = [], 0
                await query.answer("Розклад очищено")
            elif kind == "open":
                await query.answer()
            STORE.save()
            register_jobs(app, chat_id, cfg)
            await query.edit_message_text(schedule_text(cfg), parse_mode="HTML", reply_markup=schedule_kb(cfg))
            return

        # --- далі лише для адміна
        if data.startswith(("inv:", "usr:")) and not is_admin(update):
            await query.answer("⛔ Лише для адміна")
            return

        if data.startswith("inv:"):
            inv = context.user_data.get("invite")
            if inv is None:
                await query.answer("Запрошення застаріло. Почни знову: /invite", show_alert=True)
                return
            _, kind, *rest = data.split(":", 2)
            if kind == "role" and rest[0] in (ROLE_GUEST, ROLE_OWNER):
                inv["role"] = rest[0]
            elif kind == "dev" and rest[0] in STORE.devices():
                inv["devs"].symmetric_difference_update({rest[0]})
            elif kind == "make":
                if not inv["devs"]:
                    await query.answer("Обери хоча б одну станцію", show_alert=True)
                    return
                code = STORE.create_invite(inv["role"], sorted(inv["devs"]), created_by=chat_id)
                link = f"https://t.me/{context.bot.username}?start=inv_{code}"
                context.user_data.pop("invite", None)
                await query.answer("Готово")
                await query.edit_message_text(
                    f"🔗 Посилання для <b>{ROLE_TITLES[inv['role']]}</b>:\n\n<code>{link}</code>\n\n"
                    "Одноразове, діє 48 годин. Надішли його людині — вона натисне й побачить «Запустити».",
                    parse_mode="HTML",
                )
                return
            text, kb = invite_view(inv)
            await query.answer()
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
            return

        if data.startswith("usr:"):
            _, kind, *rest = data.split(":", 2)
            if kind == "list":
                text, kb = users_view()
            else:
                cid_s, _, dev_id = rest[0].partition(":")
                cid = int(cid_s)
                user = STORE.get_user(cid)
                if user is None:
                    await query.answer("Користувача не знайдено")
                    text, kb = users_view()
                elif kind == "open":
                    text, kb = user_card(cid)
                elif user.get("admin"):
                    await query.answer("Адміна тут змінити не можна")
                    text, kb = user_card(cid)
                elif kind == "cyc" and dev_id in STORE.devices():
                    nxt = {None: ROLE_GUEST, ROLE_GUEST: ROLE_OWNER, ROLE_OWNER: None}[STORE.role(cid, dev_id)]
                    STORE.set_access(cid, dev_id, nxt)
                    text, kb = user_card(cid)
                elif kind == "del":
                    STORE.remove_user(cid)
                    register_jobs(app, cid, {"times": [], "interval_min": 0})   # прибрати його будильники
                    await query.answer("Користувача видалено")
                    text, kb = users_view()
                else:
                    text, kb = user_card(cid)
            await query.answer()
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
            return

        await query.answer()
    except BadRequest as e:
        # Telegram сварится, якщо текст не змінився — це не помилка
        if "not modified" not in str(e).lower():
            raise


# ---------------------------------------------------------------- сповіщення
def fmt_duration(seconds: float) -> str:
    m = int(seconds // 60)
    if m < 1:
        return "менше хвилини"
    h, mm = divmod(m, 60)
    return f"{h} год {mm:02d} хв" if h else f"{mm} хв"


def device_events(app: Application, dev_id: str) -> list[tuple[str, dict]]:
    """Порівнює поточний стан станції з попереднім і повертає ПОДІЇ (одні для всіх людей).
    Кому саме їх надсилати — вирішує check_alerts за налаштуваннями кожної людини."""
    st = app.bot_data.setdefault("alert_state", {}).setdefault(dev_id, {})
    values, updated = state_of(dev_id).snapshot()
    events: list[tuple[str, dict]] = []
    now = time.time()

    # немає даних
    if not updated:
        return events
    stale = now - updated > STALE_AFTER_S
    if stale and not st.get("stale"):
        events.append(("stale", {"minutes": int((now - updated) // 60)}))
    elif not stale and st.get("stale"):
        events.append(("stale_back", {}))
    st["stale"] = stale
    if stale:
        return events

    bat = num(values, "battery", "battery_level", "soc")
    if bat is not None:
        # «Станція заряджена»: спрацьовує один раз і знову «взводиться» лише після падіння до 95%
        # (або до 97% після відключення світла) — тож цикли 98→100% у режимі ДБЖ не дають спаму
        if bat >= FULL_BATTERY and not st.get("full"):
            events.append(("full", {"bat": bat}))
            st["full"] = True
            st["outage_since_full"] = False
        elif st.get("full") and bat <= FULL_BATTERY - (3 if st.get("outage_since_full") else 5):
            st["full"] = False        # після відключення світла достатньо впасти до 97%, інакше — до 95%

    # світло: враховуємо лише достовірні ознаки; 'idle' (заряджена й простоює) стан не змінює
    sig = grid_signal(values)
    if sig == "idle" and "grid_on" not in st:
        st["grid_on"] = True                  # повна батарея без навантаження — вважаємо, що мережа є
    if sig in ("on", "off"):
        on = sig == "on"
        if "grid_on" not in st:
            st["grid_on"] = on
        elif on != st["grid_on"]:
            since = st.setdefault("grid_change_since", now)
            if now - since >= INPUT_DEBOUNCE_S:       # затримка, щоб не реагувати на короткі стрибки
                st["grid_on"] = on
                st.pop("grid_change_since", None)
                tout = num(values, "output_watts", "total_output_power", "output_power")
                remain = num(values, "remain_time", "remaining_discharge_time")
                if on:
                    began = st.pop("off_since", None)
                    events.append(("light_on", {"bat": bat, "outage_s": now - began if began else None}))
                else:
                    st["off_since"] = since
                    st["outage_since_full"] = True     # було відключення: «заряджена» знову спрацює вже після спаду до 97%
                    left = fmt_minutes(remain) if tout and tout > 0 and remain else None
                    events.append(("light_off", {"bat": bat, "left": left if left != "—" else None}))
        else:
            st.pop("grid_change_since", None)
    elif sig == "idle":
        st.pop("grid_change_since", None)         # простій перериває «відлік» зміни стану

    # помилки
    fault = fault_list(values)
    text = "; ".join(fault)
    if text and st.get("fault") != text:
        events.append(("error", {"text": text}))
    elif not text and st.get("fault"):
        events.append(("error_clear", {}))
    st["fault"] = text or None

    return events


# яка подія керується яким перемикачем у налаштуваннях людини
EVENT_SETTING = {"stale_back": "stale", "error_clear": "error"}


def render_event(kind: str, d: dict) -> str:
    ts = datetime.now(TZ).strftime("%H:%M")
    bat = f" Заряд: {d['bat']:.0f}%." if d.get("bat") is not None else ""
    if kind == "light_on":
        out = f" Світла не було {fmt_duration(d['outage_s'])}." if d.get("outage_s") and d["outage_s"] >= 60 else ""
        return f"💡 Світло з'явилося о {ts}.{out}{bat}"
    if kind == "light_off":
        left = f" Вистачить приблизно на {d['left']}." if d.get("left") else ""
        return f"🔌 Світло зникло о {ts}. Працюємо від батареї.{bat}{left}"
    if kind == "full":
        return f"🔋 Станцію заряджено: <b>{d['bat']:.0f}%</b>"
    if kind == "error":
        return f"⚠️ Помилка станції: <b>{esc(d['text'])}</b>"
    if kind == "error_clear":
        return "✅ Помилку усунуто"
    if kind == "stale":
        return f"📡 Від станції немає даних уже {d['minutes']} хв"
    if kind == "stale_back":
        return "📡 Зв'язок зі станцією відновлено"
    raise ValueError(kind)


def low_battery_text(bat: float, level: int, values: dict) -> str:
    tout = num(values, "output_watts", "total_output_power", "output_power")
    remain = num(values, "remain_time", "remaining_discharge_time")
    left = fmt_minutes(remain) if tout and tout > 0 and remain else "—"
    tail = f" Вистачить приблизно на {left}." if left != "—" else ""
    return f"🪫 Заряд станції знизився до <b>{bat:.0f}%</b> (поріг {level}%).{tail}"


async def check_alerts(context: ContextTypes.DEFAULT_TYPE) -> None:
    app = context.application
    while ADMIN_NOTICES:                       # службові повідомлення для адмінів
        notice = ADMIN_NOTICES.pop(0)
        for cid, user in STORE.all_users():
            if user.get("admin"):
                try:
                    await app.bot.send_message(cid, notice, parse_mode="HTML")
                except Exception as e:  # noqa: BLE001
                    log.warning("Не вдалося надіслати в %s: %s", cid, e)

    for dev_id in STORE.devices():
        events = device_events(app, dev_id)
        st = app.bot_data["alert_state"].setdefault(dev_id, {})
        values, _ = state_of(dev_id).snapshot()
        bat = num(values, "battery", "battery_level", "soc")
        lows = st.setdefault("low", {})        # окремий стан «низького заряду» для кожної людини: у всіх свій поріг

        for chat_id in STORE.users_for_device(dev_id):
            prefs = STORE.notify(chat_id, dev_id)
            texts = [render_event(kind, data) for kind, data in events if prefs.get(EVENT_SETTING.get(kind, kind))]

            if not prefs["low"]:
                lows.pop(chat_id, None)        # вимкнено — забуваємо стан, щоб при вмиканні не було зайвого повідомлення
            elif bat is not None and not st.get("stale"):
                level, rec = prefs["low_level"], lows.get(chat_id)
                if rec is None or rec["level"] != level:
                    lows[chat_id] = {"level": level, "fired": bat <= level}   # вмикання/зміна порогу — без миттєвого сповіщення
                elif bat <= level and not rec["fired"]:
                    texts.append(low_battery_text(bat, level, values))
                    rec["fired"] = True
                elif bat >= level + 5:
                    rec["fired"] = False       # знову «взведено»: заряд піднявся на 5% вище порога

            if not texts:
                continue
            text = "\n".join(texts)
            if len(STORE.devices_for(chat_id)) > 1:   # у людини кілька станцій — підписуємо, про яку мова
                text = f"<b>{esc(STORE.label(chat_id, dev_id))}</b>\n{text}"
            try:
                await app.bot.send_message(chat_id, text, parse_mode="HTML")
            except Exception as e:  # noqa: BLE001
                log.warning("Не вдалося надіслати в %s: %s", chat_id, e)


# ---------------------------------------------------------------- MQTT
def device_for_topic(topic: str) -> tuple[str, str] | None:
    """Знаходить пристрій за топіком (береться найдовший збіг префікса)."""
    best = None
    for dev_id, info in STORE.devices().items():
        base = info["topic_base"]
        if topic.startswith(base + "/") and (best is None or len(base) > len(best[1])):
            best = (dev_id, base)
    return best


def handle_discovery(payload: bytes) -> None:
    """Опис сенсора від моста → назва та серійний номер станції. До станції прив'язуємо за її топіком."""
    try:
        cfg = json.loads(payload.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return
    if not isinstance(cfg, dict):
        return
    found = None
    for key in ("state_topic", "command_topic", "availability_topic"):
        if isinstance(cfg.get(key), str):
            found = device_for_topic(cfg[key])
            if found:
                break
    info = parse_device_info(cfg)
    if not found or not info:
        return
    dev_id = found[0]
    state_of(dev_id).meta.update(info)
    old_serial = STORE.update_serial(dev_id, info.get("serial"))
    if old_serial:
        log.warning("Серійний номер станції %s змінився: %s -> %s", dev_id, old_serial, info.get("serial"))
        ADMIN_NOTICES.append(
            f"⚠️ Станція <b>{esc(STORE.full_name(dev_id))}</b> тепер повідомляє інший серійний номер "
            f"(було <code>{esc(old_serial)}</code>, стало <code>{esc(info.get('serial'))}</code>). "
            "Можливо, міст підключився до іншого пристрою акаунта — перевір."
        )


def start_mqtt() -> mqtt.Client:
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="oukitel-telegram-bot")
    if MQTT_USER:
        client.username_pw_set(MQTT_USER, MQTT_PASSWORD)

    def on_connect(c, userdata, flags, reason_code, properties):
        prefixes = set()
        for dev_id, info in STORE.devices().items():
            log.info("MQTT підключено (%s), підписка на %s/# (%s)", reason_code, info["topic_base"], dev_id)
            c.subscribe(f"{info['topic_base']}/#")
            prefixes.add(info["topic_base"].split("/")[0])
        for prefix in prefixes:     # описи пристроїв міст кладе окремо: <prefix>/sensor/oukitel_battery/config
            c.subscribe(f"{prefix}/+/+/config")

    def on_message(c, userdata, msg):
        if msg.topic.endswith("/config"):
            handle_discovery(msg.payload)
            return
        found = device_for_topic(msg.topic)
        if found:
            state = state_of(found[0])
            state.update(msg.topic, msg.payload, found[1])

    client.on_connect = on_connect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=2, max_delay=60)
    client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=60)
    client.loop_start()
    return client


# ---------------------------------------------------------------- запуск
def bootstrap_store() -> Store:
    """Відкриває сховище; при першому запуску v2 переносить дані зі старого schedules.json."""
    first_run = not STORE_FILE.exists()
    store = Store(STORE_FILE)
    if not store.devices():
        store.add_device(DEFAULT_DEVICE_ID, DEFAULT_DEVICE_NAME, MQTT_TOPIC_BASE)
    if first_run:
        n = import_legacy_schedules(store, load_legacy_schedules(), DEFAULT_DEVICE_ID)
        log.info("Створено %s, перенесено розкладів зі старого файлу: %s", STORE_FILE, n)
    for chat_id in ALLOWED_CHAT_IDS:
        store.ensure_admin(chat_id)
    if not any(u.get("admin") for _, u in store.all_users()):
        log.warning("Адміністратора немає: перший, хто напише /start, ним стане. Додай свій chat_id у ALLOWED_CHAT_IDS")
    return store


async def post_init(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("status", "Поточний стан станції"),
        BotCommand("rename", "Назвати станцію по-своєму"),
        BotCommand("devname", "Офіційна назва станції (адмін)"),
        BotCommand("help", "Інструкція"),
        BotCommand("schedule", "Щоденний звіт: /schedule 08:00"),
        BotCommand("every", "Звіт кожні N хв: /every 60"),
        BotCommand("schedules", "Мій розклад"),
        BotCommand("clear", "Очистити розклад"),
        BotCommand("alerts", "Автоматичні сповіщення"),
        BotCommand("invite", "Запросити людину (адмін)"),
        BotCommand("users", "Користувачі (адмін)"),
        BotCommand("debug", "Сирі дані MQTT (адмін)"),
    ])
    for chat_id, user in STORE.all_users():
        register_jobs(app, chat_id, user.get("schedule", {}))
    app.job_queue.run_repeating(check_alerts, interval=15, first=30, name="alerts")
    app.job_queue.run_repeating(sync_bridge_names, interval=600, first=20, name="bridge-names")
    if STORE.data.get("ui_version") != UI_VERSION:     # меню змінилося — один раз надсилаємо всім нову клавіатуру
        for chat_id, _ in STORE.all_users():
            try:
                await app.bot.send_message(chat_id, "🔄 Бот оновився. Сповіщення тепер налаштовуються окремо для кожної людини й станції, "
                                           "а за замовчуванням усі вимкнені: відкрий «🔔 Сповіщення» й увімкни потрібні. "
                                           "Інструкція — /help",
                                           reply_markup=menu_for(chat_id))
            except Exception as e:  # noqa: BLE001
                log.warning("Не вдалося оновити меню в %s: %s", chat_id, e)
        STORE.data["ui_version"] = UI_VERSION
        STORE.save()


def main() -> None:
    global STORE
    STORE = bootstrap_store()
    start_mqtt()
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("schedule", cmd_schedule))
    app.add_handler(CommandHandler("every", cmd_every))
    app.add_handler(CommandHandler("schedules", cmd_schedules))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("alerts", cmd_alerts))
    app.add_handler(CommandHandler("rename", cmd_rename))
    app.add_handler(CommandHandler("devname", cmd_devname))
    app.add_handler(CommandHandler("invite", cmd_invite))
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CommandHandler("debug", cmd_debug))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(
        filters.Text([BTN_STATUS, BTN_CONTROL, BTN_ALERTS, BTN_STATS, BTN_SCHEDULE, BTN_HELP, BTN_USERS]), on_menu_text))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    log.info("Бот запущено")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
