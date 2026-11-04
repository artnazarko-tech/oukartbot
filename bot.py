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

import json
import logging
import os
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

LOW_BATTERY = float(os.getenv("ALERT_LOW_BATTERY", "20"))
FULL_BATTERY = float(os.getenv("ALERT_FULL_BATTERY", "100"))
INPUT_ON_W = float(os.getenv("ALERT_INPUT_ON_W", "30"))      # вхід вважається "є" від цієї потужності
INPUT_DEBOUNCE_S = int(os.getenv("ALERT_INPUT_DEBOUNCE_S", "60"))
STALE_AFTER_S = int(os.getenv("STALE_AFTER_S", "300"))

DATA_FILE = Path(os.getenv("DATA_FILE", "data/schedules.json"))

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

    def update(self, topic: str, payload: bytes) -> None:
        key = topic_to_key(topic)
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

    def snapshot(self) -> tuple[dict, float]:
        with self._lock:
            return dict(self.values), self.updated

    def raw_snapshot(self) -> dict:
        with self._lock:
            return dict(self.raw)


def topic_to_key(topic: str) -> str | None:
    """'homeassistant/sensor/oukitel/battery/state' -> 'battery'. Конфіг-топіки ігноруються."""
    if not topic.startswith(MQTT_TOPIC_BASE):
        return None
    rest = topic[len(MQTT_TOPIC_BASE):].strip("/")
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


STATE = StationState()


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


def format_status(values: dict, updated: float) -> str:
    if not updated:
        return "⏳ Даних від станції ще немає. Перевір, що міст oukitel-portable-bridge запущено і він пише в MQTT (/debug)."

    bat = num(values, "battery", "battery_level", "soc")
    tin = num(values, "input_watts", "total_input_power", "input_power")
    tout = num(values, "output_watts", "total_output_power", "output_power")
    ac_in = num(values, "ac_input_watts", "ac_input_power")
    dc_in = num(values, "dc_input_watts", "dc_solar_input_power", "solar_input_watts")
    ac_out = num(values, "ac_output_power")
    dc_out = num(values, "dc_output_power")
    temp = num(values, "temperature", "temp")
    remain = num(values, "remain_time", "remaining_discharge_time")
    charge_t = num(values, "remain_charging_time", "remaining_charge_time")
    fault = values.get("hardware_fault_status")

    lines = ["⚡ <b>Зарядна станція</b>"]
    if bat is not None:
        lines.append(f"{battery_icon(bat)} Заряд: <b>{bat:.0f}%</b>")

    if tin is not None and tout is not None:
        net = tin - tout
        if net > 5:
            lines.append(f"📈 Заряджається (+{net:.0f} Вт)")
        elif net < -5:
            lines.append(f"📉 Розряджається ({net:.0f} Вт)")
        else:
            lines.append("⏸ Баланс ~0")

    lines.append("")
    lines.append(f"⬇️ Вхід: <b>{fmt_w(tin)}</b>")
    if ac_in is not None or dc_in is not None:
        lines.append(f"   🔌 Мережа: {fmt_w(ac_in)}   ☀️ Сонце: {fmt_w(dc_in)}")
    lines.append(f"⬆️ Вихід: <b>{fmt_w(tout)}</b>")
    if ac_out is not None or dc_out is not None:
        lines.append(f"   🔌 AC: {fmt_w(ac_out)}   🚗 DC 12В: {fmt_w(dc_out)}")

    if tin is not None and tout is not None and tout > tin and remain:
        lines.append(f"\n⏱ Вистачить на: {fmt_minutes(remain)}")
    elif charge_t:
        lines.append(f"\n⏱ До повного заряду: {fmt_minutes(charge_t)}")

    if temp is not None:
        lines.append(f"🌡 Температура: {temp:.0f} °C")
    if isinstance(fault, str) and fault and fault.lower() != "normal":
        lines.append(f"⚠️ Стан: <b>{fault}</b>")

    age = time.time() - updated
    ts = datetime.fromtimestamp(updated, TZ).strftime("%H:%M:%S")
    stale = " ⚠️ дані застарілі" if age > STALE_AFTER_S else ""
    lines.append(f"\n<i>Оновлено о {ts}{stale}</i>")
    return "\n".join(lines)


# ---------------------------------------------------------------- розклади
def load_schedules() -> dict:
    try:
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_schedules(data: dict) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


async def send_report(context: ContextTypes.DEFAULT_TYPE) -> None:
    values, updated = STATE.snapshot()
    await context.bot.send_message(context.job.chat_id, format_status(values, updated), parse_mode="HTML")


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
    return not ALLOWED_CHAT_IDS or update.effective_chat.id in ALLOWED_CHAT_IDS


def alert_chats(app: Application) -> set[int]:
    chats = set(ALLOWED_CHAT_IDS)
    chats.update(int(c) for c in app.bot_data.get("schedules", {}).keys())
    chats.update(app.bot_data.get("seen_chats", set()))
    return chats


# ---------------------------------------------------------------- команди
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not allowed(update):
        await update.message.reply_text(f"⛔ Доступ заборонено. Твій chat_id: {chat_id}")
        return
    context.application.bot_data.setdefault("seen_chats", set()).add(chat_id)
    await update.message.reply_text(
        "👋 Я стежу за зарядною станцією Oukitel.\n\n"
        "Користуйся кнопками внизу 👇\n\n"
        "Команди теж працюють:\n"
        "/status — поточний стан\n"
        "/schedule 08:00 — щоденний звіт у свій час\n"
        "/every 60 — звіт кожні N хвилин\n"
        "/debug — сирі дані MQTT\n\n"
        f"Твій chat_id: <code>{chat_id}</code>",
        parse_mode="HTML",
        reply_markup=MAIN_MENU,
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    values, updated = STATE.snapshot()
    await update.message.reply_text(format_status(values, updated), parse_mode="HTML",
                                    reply_markup=STATUS_KB)


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
    data = context.application.bot_data["schedules"]
    cfg = data.setdefault(str(chat_id), {"times": [], "interval_min": 0})
    cfg["times"] = sorted(set(cfg["times"]) | set(times))
    save_schedules(data)
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
    data = context.application.bot_data["schedules"]
    cfg = data.setdefault(str(chat_id), {"times": [], "interval_min": 0})
    cfg["interval_min"] = minutes
    save_schedules(data)
    register_jobs(context.application, chat_id, cfg)
    await update.message.reply_text(
        f"✅ Звіт кожні {minutes} хв" if minutes else "✅ Періодичні звіти вимкнено"
    )


async def cmd_schedules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    cfg = context.application.bot_data["schedules"].get(str(update.effective_chat.id))
    if not cfg or (not cfg.get("times") and not cfg.get("interval_min")):
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
    data = context.application.bot_data["schedules"]
    data[str(chat_id)] = {"times": [], "interval_min": 0}
    save_schedules(data)
    register_jobs(context.application, chat_id, data[str(chat_id)])
    await update.message.reply_text("🗑 Розклад очищено (сповіщення про події залишаються)")


async def cmd_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    await update.message.reply_text(
        "🔔 Автоматичні сповіщення:\n"
        f"• заряд нижче {LOW_BATTERY:.0f}%\n"
        f"• заряд досяг {FULL_BATTERY:.0f}%\n"
        f"• зникло / з'явилося живлення з мережі (поріг {INPUT_ON_W:.0f} Вт, затримка {INPUT_DEBOUNCE_S} с)\n"
        "• аварійні стани станції\n"
        f"• немає даних довше {STALE_AFTER_S // 60} хв\n\n"
        "Пороги змінюються у файлі .env"
    )


async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    raw = STATE.raw_snapshot()
    if not raw:
        await update.message.reply_text(
            f"MQTT-повідомлень не отримано.\nБрокер: {MQTT_HOST}:{MQTT_PORT}\nТопік: {MQTT_TOPIC_BASE}/#"
        )
        return
    lines = [f"{t.removeprefix(MQTT_TOPIC_BASE + '/')} = {v[:60]}" for t, v in sorted(raw.items())]
    text = "\n".join(lines)
    for i in range(0, len(text), 3800):
        await update.message.reply_text(text[i:i + 3800])


# ---------------------------------------------------------------- кнопки
BTN_STATUS = "📊 Стан"
BTN_SCHEDULE = "⏰ Розклад"
BTN_ALERTS = "🔔 Сповіщення"
BTN_HELP = "ℹ️ Допомога"

# Постійне меню внизу екрана (замість клавіатури)
MAIN_MENU = ReplyKeyboardMarkup(
    [[KeyboardButton(BTN_STATUS)], [KeyboardButton(BTN_SCHEDULE), KeyboardButton(BTN_ALERTS)],
     [KeyboardButton(BTN_HELP)]],
    resize_keyboard=True,
    is_persistent=True,
)

# Кнопка під повідомленням зі станом
STATUS_KB = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Оновити", callback_data="status:refresh")]])

PRESET_TIMES = ["07:00", "09:00", "12:00", "15:00", "18:00", "21:00", "23:00"]
PRESET_INTERVALS = [(0, "вимк"), (30, "30 хв"), (60, "1 год"), (180, "3 год")]


def get_cfg(app: Application, chat_id: int) -> dict:
    return app.bot_data["schedules"].setdefault(str(chat_id), {"times": [], "interval_min": 0})


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
        InlineKeyboardButton(("✅ " if t in chosen else "") + t, callback_data=f"sch:t:{t}")
        for t in PRESET_TIMES
    ]
    rows = [time_btns[i:i + 4] for i in range(0, len(time_btns), 4)]
    # нестандартні часи, додані командою, теж показуємо, щоб їх можна було прибрати
    custom = [t for t in sorted(chosen) if t not in PRESET_TIMES]
    if custom:
        rows.append([InlineKeyboardButton(f"✅ {t}", callback_data=f"sch:t:{t}") for t in custom][:4])
    cur = cfg.get("interval_min") or 0
    rows.append([
        InlineKeyboardButton(("● " if cur == m else "") + label, callback_data=f"sch:i:{m}")
        for m, label in PRESET_INTERVALS
    ])
    rows.append([InlineKeyboardButton("🗑 Очистити все", callback_data="sch:clear")])
    return InlineKeyboardMarkup(rows)


async def show_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    cfg = get_cfg(context.application, update.effective_chat.id)
    await update.message.reply_text(schedule_text(cfg), parse_mode="HTML", reply_markup=schedule_kb(cfg))


async def on_menu_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обробка натискань на кнопки нижнього меню."""
    if not allowed(update):
        return
    text = update.message.text
    if text == BTN_STATUS:
        await cmd_status(update, context)
    elif text == BTN_SCHEDULE:
        await show_schedule(update, context)
    elif text == BTN_ALERTS:
        await cmd_alerts(update, context)
    elif text == BTN_HELP:
        await cmd_start(update, context)


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
        if data == "status:refresh":
            values, updated = STATE.snapshot()
            await query.answer("Оновлено")
            await query.edit_message_text(format_status(values, updated), parse_mode="HTML",
                                          reply_markup=STATUS_KB)
            return

        if data.startswith("sch:"):
            cfg = get_cfg(app, chat_id)
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
            save_schedules(app.bot_data["schedules"])
            register_jobs(app, chat_id, cfg)
            await query.edit_message_text(schedule_text(cfg), parse_mode="HTML", reply_markup=schedule_kb(cfg))
            return

        await query.answer()
    except BadRequest as e:
        # Telegram сварится, якщо текст не змінився — це не помилка
        if "not modified" not in str(e).lower():
            raise


# ---------------------------------------------------------------- сповіщення
async def check_alerts(context: ContextTypes.DEFAULT_TYPE) -> None:
    app = context.application
    st = app.bot_data.setdefault("alert_state", {})
    values, updated = STATE.snapshot()
    msgs: list[str] = []
    now = time.time()

    # немає даних
    if updated:
        stale = now - updated > STALE_AFTER_S
        if stale and not st.get("stale"):
            msgs.append(f"📡 Від станції немає даних уже {int((now - updated) // 60)} хв")
        elif not stale and st.get("stale"):
            msgs.append("📡 Зв'язок зі станцією відновлено")
        st["stale"] = stale
        if stale:
            await _broadcast(app, msgs)
            return
    else:
        return

    bat = num(values, "battery", "battery_level", "soc")
    if bat is not None:
        if bat <= LOW_BATTERY and not st.get("low"):
            msgs.append(f"🪫 Низький заряд: <b>{bat:.0f}%</b>")
            st["low"] = True
        elif bat >= LOW_BATTERY + 5:
            st["low"] = False
        if bat >= FULL_BATTERY and not st.get("full"):
            msgs.append(f"🔋 Станцію заряджено: <b>{bat:.0f}%</b>")
            st["full"] = True
        elif bat <= FULL_BATTERY - 5:
            st["full"] = False

    # живлення з мережі (з затримкою, щоб не реагувати на короткі стрибки)
    grid = num(values, "ac_input_watts", "ac_input_power")
    if grid is None:
        grid = num(values, "input_watts", "total_input_power")
    if grid is not None:
        on = grid >= INPUT_ON_W
        if "grid_on" not in st:
            st["grid_on"] = on
        elif on != st["grid_on"]:
            since = st.setdefault("grid_change_since", now)
            if now - since >= INPUT_DEBOUNCE_S:
                st["grid_on"] = on
                st.pop("grid_change_since", None)
                ts = datetime.now(TZ).strftime("%H:%M")
                bat_txt = f" Заряд: {bat:.0f}%." if bat is not None else ""
                msgs.append(
                    f"💡 Живлення з'явилося о {ts} (вхід {grid:.0f} Вт).{bat_txt}" if on
                    else f"🔌 Живлення зникло о {ts}. Працюємо від батареї.{bat_txt}"
                )
        else:
            st.pop("grid_change_since", None)

    fault = values.get("hardware_fault_status")
    if isinstance(fault, str) and fault:
        if fault.lower() != "normal" and st.get("fault") != fault:
            msgs.append(f"⚠️ Станція повідомляє: <b>{fault}</b>")
        st["fault"] = fault if fault.lower() != "normal" else None

    await _broadcast(app, msgs)


async def _broadcast(app: Application, msgs: list[str]) -> None:
    if not msgs:
        return
    text = "\n".join(msgs)
    for chat_id in alert_chats(app):
        try:
            await app.bot.send_message(chat_id, text, parse_mode="HTML")
        except Exception as e:  # noqa: BLE001
            log.warning("Не вдалося надіслати в %s: %s", chat_id, e)


# ---------------------------------------------------------------- MQTT
def start_mqtt() -> mqtt.Client:
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="oukitel-telegram-bot")
    if MQTT_USER:
        client.username_pw_set(MQTT_USER, MQTT_PASSWORD)

    def on_connect(c, userdata, flags, reason_code, properties):
        log.info("MQTT підключено (%s), підписка на %s/#", reason_code, MQTT_TOPIC_BASE)
        c.subscribe(f"{MQTT_TOPIC_BASE}/#")

    def on_message(c, userdata, msg):
        STATE.update(msg.topic, msg.payload)

    client.on_connect = on_connect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=2, max_delay=60)
    client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=60)
    client.loop_start()
    return client


# ---------------------------------------------------------------- запуск
async def post_init(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("status", "Поточний стан станції"),
        BotCommand("schedule", "Щоденний звіт: /schedule 08:00"),
        BotCommand("every", "Звіт кожні N хв: /every 60"),
        BotCommand("schedules", "Мій розклад"),
        BotCommand("clear", "Очистити розклад"),
        BotCommand("alerts", "Автоматичні сповіщення"),
        BotCommand("debug", "Сирі дані MQTT"),
    ])
    app.bot_data["schedules"] = load_schedules()
    for chat_id, cfg in app.bot_data["schedules"].items():
        register_jobs(app, int(chat_id), cfg)
    app.job_queue.run_repeating(check_alerts, interval=15, first=30, name="alerts")


def main() -> None:
    start_mqtt()
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("schedule", cmd_schedule))
    app.add_handler(CommandHandler("every", cmd_every))
    app.add_handler(CommandHandler("schedules", cmd_schedules))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("alerts", cmd_alerts))
    app.add_handler(CommandHandler("debug", cmd_debug))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.Text([BTN_STATUS, BTN_SCHEDULE, BTN_ALERTS, BTN_HELP]), on_menu_text))
    log.info("Бот запущено")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
