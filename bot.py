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

import html
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

from store import (ROLE_ADMIN, ROLE_GUEST, ROLE_OWNER, ROLE_TITLES, Store,
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

LOW_BATTERY = float(os.getenv("ALERT_LOW_BATTERY", "20"))
FULL_BATTERY = float(os.getenv("ALERT_FULL_BATTERY", "100"))
INPUT_ON_W = float(os.getenv("ALERT_INPUT_ON_W", "30"))      # вхід вважається "є" від цієї потужності
INPUT_DEBOUNCE_S = int(os.getenv("ALERT_INPUT_DEBOUNCE_S", "60"))
STALE_AFTER_S = int(os.getenv("STALE_AFTER_S", "300"))

DATA_FILE = Path(os.getenv("DATA_FILE", "data/schedules.json"))   # старий файл (версія 1), лише для міграції
STORE_FILE = Path(os.getenv("STORE_FILE", "data/store.json"))
DEFAULT_DEVICE_ID = "main"
DEFAULT_DEVICE_NAME = os.getenv("DEVICE_NAME", "Oukitel P2001E Plus")

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
        self.meta: dict[str, str] = {}      # що міст повідомив про пристрій: name, model, serial

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
    """З опису сенсора (Home Assistant discovery) дістає назву, модель і серійний номер станції."""
    dev = cfg.get("device")
    if not isinstance(dev, dict):
        return {}
    info = {}
    for field, key in (("name", "name"), ("model", "model"), ("serial", "serial_number")):
        val = dev.get(key)
        if isinstance(val, str) and val.strip():
            info[field] = val.strip()
    if "serial" not in info:                      # запасний варіант — MAC із connections
        for conn in dev.get("connections") or []:
            if isinstance(conn, list) and len(conn) == 2 and conn[0] == "mac":
                info["serial"] = str(conn[1])
    return info


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


def format_status(values: dict, updated: float, name: str = "Зарядна станція") -> str:
    if not updated:
        return f"⚡ <b>{name}</b>\n⏳ Даних від станції ще немає. Спробуй за хвилину."

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

    lines = [f"⚡ <b>{name}</b>"]
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


# ---------------------------------------------------------------- допоміжне
B = InlineKeyboardButton


def esc(text: object) -> str:
    return html.escape(str(text))


def status_text(dev_id: str, chat_id: int) -> str:
    values, updated = state_of(dev_id).snapshot()
    return format_status(values, updated, esc(STORE.label(chat_id, dev_id)))


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


# ---------------------------------------------------------------- кнопки (константи)
BTN_STATUS = "📊 Стан"
BTN_SCHEDULE = "⏰ Розклад"
BTN_ALERTS = "🔔 Сповіщення"
BTN_HELP = "ℹ️ Допомога"
BTN_USERS = "👥 Користувачі"


def menu_for(chat_id: int) -> ReplyKeyboardMarkup:
    """Постійне меню внизу екрана. Адмін додатково бачить «Користувачі»."""
    last = [KeyboardButton(BTN_HELP)]
    if STORE.is_admin(chat_id):
        last.append(KeyboardButton(BTN_USERS))
    return ReplyKeyboardMarkup(
        [[KeyboardButton(BTN_STATUS)], [KeyboardButton(BTN_SCHEDULE), KeyboardButton(BTN_ALERTS)], last],
        resize_keyboard=True, is_persistent=True,
    )


def status_kb(dev_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[B("🔄 Оновити", callback_data=f"status:refresh:{dev_id}"),
                                  B("✏️ Назва", callback_data=f"ren:{dev_id}")]])


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
    "📊 <b>Стан</b> — поточні показники станції: заряд, вхід і вихід у Вт, "
    "скільки ще пропрацює або скільки до повного заряду. Кнопка «🔄 Оновити» бере свіжі дані.\n\n"
    "✏️ <b>Назва</b> — кнопка під станом (або /rename). Можна назвати станцію зручно, наприклад «Дача». "
    "Нову назву бачиш тільки ти.\n\n"
    "⏰ <b>Розклад</b> — бот сам надсилатиме стан у потрібний час або кожні кілька хвилин. "
    "Натискай на час, щоб увімкнути чи вимкнути його. Свій час: /schedule 08:30\n\n"
    "🔔 <b>Сповіщення</b> — що бот пише сам: світло з'явилося чи зникло, низький і повний заряд, "
    "помилки станції, немає зв'язку."
)
HELP_ADMIN = (
    "\n\n👥 <b>Користувачі</b> — хто має доступ і до яких станцій; тут можна змінити роль чи видалити людину.\n\n"
    "➕ /invite — запросити людину: обираєш роль і станції та отримуєш посилання.\n"
    "• <b>Власник</b> — керує обраними станціями.\n"
    "• <b>Гість</b> — лише дивиться."
)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    text = HELP_COMMON + (HELP_ADMIN if is_admin(update) else "")
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    devs = STORE.devices_for(update.effective_chat.id)
    if not devs:
        await update.message.reply_text("У тебе поки немає доступу до жодної станції. Попроси адміна.")
    elif len(devs) == 1:
        await update.message.reply_text(status_text(devs[0], update.effective_chat.id), parse_mode="HTML",
                                        reply_markup=status_kb(devs[0]))
    else:
        await update.message.reply_text("Яка станція?", reply_markup=InlineKeyboardMarkup(
            [[B(STORE.label(update.effective_chat.id, d), callback_data=f"dev:status:{d}")] for d in devs]))


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
    if not allowed(update) or not is_admin(update):
        return
    chunks = []
    for dev_id, info in STORE.devices().items():
        base = info["topic_base"]
        raw = state_of(dev_id).raw_snapshot()
        bridge = state_of(dev_id).meta.get("name", "не отримано")
        meta = state_of(dev_id).meta
        head = (f"[{STORE.full_name(dev_id)}] id={dev_id}, назва від моста: {bridge}, "
                f"модель: {meta.get('model', '—')}, серійний: {meta.get('serial', '—')}")
        if not raw:
            chunks.append(f"{head}\nMQTT-повідомлень не отримано.\nБрокер: {MQTT_HOST}:{MQTT_PORT}\nТопік: {base}/#")
            continue
        lines = [f"{t.removeprefix(base + '/')} = {v[:60]}" for t, v in sorted(raw.items())]
        chunks.append(f"{head}\n" + "\n".join(lines))
    text = "\n\n".join(chunks)
    for i in range(0, len(text), 3800):
        await update.message.reply_text(text[i:i + 3800])


# ---------------------------------------------------------------- особиста назва станції
def rename_prompt(chat_id: int, dev_id: str) -> str:
    return (
        f"✏️ Як називати станцію <b>{esc(STORE.device_name(dev_id))}</b>?\n\n"
        "Напиши нову назву (до 40 символів). Її бачитимеш тільки ти.\n"
        "Щоб повернути стандартну назву — надішли «-»."
    )


async def cmd_rename(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    chat_id = update.effective_chat.id
    devs = STORE.devices_for(chat_id)
    if not devs:
        await update.message.reply_text("У тебе поки немає доступу до жодної станції.")
    elif len(devs) == 1:
        context.user_data["rename"] = devs[0]
        await update.message.reply_text(rename_prompt(chat_id, devs[0]), parse_mode="HTML")
    else:
        await update.message.reply_text("Яку станцію назвати?", reply_markup=InlineKeyboardMarkup(
            [[B(STORE.label(chat_id, d), callback_data=f"ren:{d}")] for d in devs]))


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Звичайний текст: приймаємо лише якщо чекаємо нову назву станції."""
    if not allowed(update):
        return
    dev_id = context.user_data.get("rename")
    if not dev_id:
        return
    chat_id = update.effective_chat.id
    if not STORE.role(chat_id, dev_id):
        context.user_data.pop("rename", None)
        return
    text = " ".join((update.message.text or "").split())   # прибираємо переноси й зайві пробіли
    if text in ("-", "—"):
        STORE.set_alias(chat_id, dev_id, None)
        reply = f"↩️ Повернув стандартну назву: <b>{esc(STORE.label(chat_id, dev_id))}</b>"
    elif 0 < len(text) <= 40:
        STORE.set_alias(chat_id, dev_id, text)
        reply = f"✅ Готово! Тепер станція називається <b>{esc(text)}</b>."
    else:
        await update.message.reply_text("Назва має бути від 1 до 40 символів. Спробуй ще раз або надішли «-».")
        return
    context.user_data.pop("rename", None)
    await update.message.reply_text(reply, parse_mode="HTML")


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
    return InlineKeyboardMarkup(rows)


async def show_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    cfg = STORE.schedule(update.effective_chat.id)
    await update.message.reply_text(schedule_text(cfg), parse_mode="HTML", reply_markup=schedule_kb(cfg))


async def on_menu_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обробка натискань на кнопки нижнього меню."""
    if not allowed(update):
        return
    text = update.message.text
    context.user_data.pop("rename", None)      # натиснув кнопку меню — вводити назву вже не чекаємо
    if text == BTN_STATUS:
        await cmd_status(update, context)
    elif text == BTN_SCHEDULE:
        await show_schedule(update, context)
    elif text == BTN_ALERTS:
        await cmd_alerts(update, context)
    elif text == BTN_HELP:
        await cmd_help(update, context)
    elif text == BTN_USERS:
        await cmd_users(update, context)


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
        # --- стан станції
        if data.startswith(("status:refresh:", "dev:status:")):
            dev_id = data.split(":", 2)[2]
            if not STORE.role(chat_id, dev_id):
                await query.answer("⛔ Немає доступу до цієї станції")
                return
            await query.answer("Оновлено")
            await query.edit_message_text(status_text(dev_id, chat_id), parse_mode="HTML",
                                          reply_markup=status_kb(dev_id))
            return

        # --- особиста назва станції
        if data.startswith("ren:"):
            dev_id = data.split(":", 1)[1]
            if not STORE.role(chat_id, dev_id):
                await query.answer("⛔ Немає доступу до цієї станції")
                return
            context.user_data["rename"] = dev_id
            await query.answer()
            await query.message.reply_text(rename_prompt(chat_id, dev_id), parse_mode="HTML")
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
def collect_alerts(app: Application, dev_id: str) -> list[str]:
    """Порівнює поточний стан пристрою з попереднім і повертає список нових повідомлень."""
    st = app.bot_data.setdefault("alert_state", {}).setdefault(dev_id, {})
    values, updated = state_of(dev_id).snapshot()
    msgs: list[str] = []
    now = time.time()

    # немає даних
    if not updated:
        return msgs
    stale = now - updated > STALE_AFTER_S
    if stale and not st.get("stale"):
        msgs.append(f"📡 Від станції немає даних уже {int((now - updated) // 60)} хв")
    elif not stale and st.get("stale"):
        msgs.append("📡 Зв'язок зі станцією відновлено")
    st["stale"] = stale
    if stale:
        return msgs

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

    return msgs


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
    devices = STORE.devices()
    for dev_id, info in devices.items():
        msgs = collect_alerts(app, dev_id)
        if not msgs:
            continue
        body = "\n".join(msgs)
        for chat_id in STORE.users_for_device(dev_id):
            text = body
            if len(STORE.devices_for(chat_id)) > 1:   # у людини кілька станцій — підписуємо, про яку мова
                text = f"<b>{esc(STORE.label(chat_id, dev_id))}</b>\n{body}"
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
    res = STORE.update_device_info(dev_id, info.get("name"), info.get("serial"))
    if res["name_changed"]:
        log.info("Назва станції %s від моста: %s", dev_id, info.get("name"))
    if res["old_serial"]:
        log.warning("Серійний номер станції %s змінився: %s -> %s", dev_id, res["old_serial"], info.get("serial"))
        ADMIN_NOTICES.append(
            f"⚠️ Станція <b>{esc(STORE.full_name(dev_id))}</b> тепер повідомляє інший серійний номер "
            f"(було <code>{esc(res['old_serial'])}</code>, стало <code>{esc(info.get('serial'))}</code>). "
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
    app.add_handler(CommandHandler("invite", cmd_invite))
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CommandHandler("debug", cmd_debug))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(
        filters.Text([BTN_STATUS, BTN_SCHEDULE, BTN_ALERTS, BTN_HELP, BTN_USERS]), on_menu_text))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    log.info("Бот запущено")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
