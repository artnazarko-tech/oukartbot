"""Имитация работы бота без Telegram и MQTT: проверка ролей, приглашений, меню, уведомлений.
Запуск: python3 tests/test_bot_flow.py"""
import asyncio
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

TMP = Path(tempfile.mkdtemp())
os.environ.update({
    "BOT_TOKEN": "1:test", "ALLOWED_CHAT_IDS": "1", "STORE_FILE": str(TMP / "store.json"),
    "DATA_FILE": str(TMP / "schedules.json"),
})
# старый файл версии 1: у человека 555 был расписание
(TMP / "schedules.json").write_text(json.dumps({"555": {"times": ["08:00"], "interval_min": 30}}))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bot  # noqa: E402

bot.STORE = bot.bootstrap_store()
S = bot.STORE


def make_update(chat_id, text="", args=None, data=None):
    msg = SimpleNamespace(text=text, reply_text=AsyncMock())
    q = SimpleNamespace(data=data, answer=AsyncMock(), edit_message_text=AsyncMock(),
                        message=SimpleNamespace(reply_text=AsyncMock())) if data else None
    upd = SimpleNamespace(effective_chat=SimpleNamespace(id=chat_id),
                          effective_user=SimpleNamespace(full_name=f"user{chat_id}"),
                          message=msg, callback_query=q)
    ctx = SimpleNamespace(args=args or [], user_data={}, application=MagicMock(),
                          bot=SimpleNamespace(username="testbot", send_message=AsyncMock()))
    return upd, ctx


def run(coro):
    return asyncio.run(coro)


def said(upd):
    return upd.message.reply_text.call_args[0][0]


def edited(upd):
    return upd.callback_query.edit_message_text.call_args[0][0]


def test_migration_and_admin():
    assert S.is_admin(1)
    assert S.role(555, "main") == "guest" and S.schedule(555)["times"] == ["08:00"]
    assert (TMP / "schedules.json").exists()          # старый файл не тронут


def test_status_and_denied():
    bot.state_of("main").update(f"{bot.MQTT_TOPIC_BASE}/battery", b"87", bot.MQTT_TOPIC_BASE)
    bot.state_of("main").update(f"{bot.MQTT_TOPIC_BASE}/ac_input_watts", b"450", bot.MQTT_TOPIC_BASE)
    upd, ctx = make_update(1, "📊 Стан")
    run(bot.on_menu_text(upd, ctx))
    assert "87%" in said(upd) and "Мережа: ✅ є" in said(upd)
    upd, ctx = make_update(999, args=[])               # чужой
    run(bot.cmd_start(upd, ctx))
    assert "Доступ лише за запрошенням" in said(upd)
    upd, ctx = make_update(999)
    run(bot.cmd_status(upd, ctx))                      # молчит
    upd.message.reply_text.assert_not_called()


def test_invite_flow_and_permissions():
    upd, ctx = make_update(1)
    run(bot.cmd_invite(upd, ctx))
    assert ctx.user_data["invite"]["devs"] == {"main"}   # одно устройство — выбрано сразу
    upd2, _ = make_update(1, data="inv:role:owner")
    run(bot.on_button(upd2, ctx))
    upd3, _ = make_update(1, data="inv:make")
    run(bot.on_button(upd3, ctx))
    link = edited(upd3)
    code = link.split("inv_")[1].split("</code>")[0]
    # новый человек переходит по ссылке
    upd4, ctx4 = make_update(7, args=[f"inv_{code}"])
    run(bot.cmd_start(upd4, ctx4))
    assert S.role(7, "main") == "owner" and S.can_control(7, "main")
    # повторно по той же ссылке — другой человек не войдёт
    upd5, ctx5 = make_update(8, args=[f"inv_{code}"])
    run(bot.cmd_start(upd5, ctx5))
    assert S.get_user(8) is None and "недійсне" in said(upd5)
    # не админ не может открыть /users и кнопки админа
    upd6, ctx6 = make_update(7)
    run(bot.cmd_users(upd6, ctx6))
    upd6.message.reply_text.assert_not_called()
    upd7, ctx7 = make_update(7, data="usr:del:1")
    run(bot.on_button(upd7, ctx7))
    assert S.get_user(1) is not None
    upd7.callback_query.answer.assert_called_with("⛔ Лише для адміна")


def test_users_management():
    upd, ctx = make_update(1, data="usr:cyc:7:main")
    run(bot.on_button(upd, ctx))                       # owner -> нет доступа
    assert S.role(7, "main") is None
    run(bot.on_button(*make_update(1, data="usr:cyc:7:main")))
    assert S.role(7, "main") == "guest"
    upd, ctx = make_update(1, data="usr:cyc:1:main")   # админа менять нельзя
    run(bot.on_button(upd, ctx))
    assert S.is_admin(1)
    upd, ctx = make_update(1, data="usr:del:7")
    run(bot.on_button(upd, ctx))
    assert S.get_user(7) is None


def test_two_devices_routing_and_alerts():
    S.add_device("mama", "Мама 2 кВт", "homeassistant/sensor/oukitel_mama")
    S.set_access(555, "mama", "guest")
    found = bot.device_for_topic("homeassistant/sensor/oukitel_mama/battery")
    assert found == ("mama", "homeassistant/sensor/oukitel_mama")
    assert bot.device_for_topic("homeassistant/sensor/oukitel/battery")[0] == "main"
    assert bot.device_for_topic("homeassistant/sensor/other/battery") is None
    bot.state_of("mama").update("homeassistant/sensor/oukitel_mama/battery", b"15", found[1])
    assert bot.state_of("main").snapshot()[0]["battery"] == 87          # данные не смешиваются
    assert bot.state_of("mama").snapshot()[0]["battery"] == 15
    # выбор станции при нескольких
    upd, ctx = make_update(1, "📊 Стан")
    run(bot.cmd_status(upd, ctx))
    assert "Яка станція" in said(upd)
    upd, ctx = make_update(555, data="dev:status:mama")
    run(bot.on_button(upd, ctx))
    assert "15%" in edited(upd) and "Мама" in edited(upd)
    # у 555 нет доступа к чужому
    S.set_access(555, "mama", None)
    upd, ctx = make_update(555, data="dev:status:mama")
    run(bot.on_button(upd, ctx))
    upd.callback_query.answer.assert_called_with("⛔ Немає доступу до цієї станції")


def test_alert_recipients_and_prefix():
    app = MagicMock(); app.bot_data = {}; app.bot.send_message = AsyncMock()
    ctx = SimpleNamespace(application=app)
    base = "homeassistant/sensor/oukitel_mama"
    bot.state_of("mama").update(f"{base}/battery", b"50", base)
    run(bot.check_alerts(ctx))
    app.bot.send_message.assert_not_called()                              # за замовчуванням усе вимкнено
    S.set_notify(1, "mama", "low", True)
    run(bot.check_alerts(ctx))                                            # вмикання — без миттєвого повідомлення
    app.bot.send_message.assert_not_called()
    bot.state_of("mama").update(f"{base}/battery", b"10", base)
    run(bot.check_alerts(ctx))
    sent = [(c[0][0], c[0][1]) for c in app.bot.send_message.call_args_list]
    assert any(cid == 1 and "Мама" in t and "знизився до <b>10%" in t for cid, t in sent)   # адмін, з підписом станції
    assert not any(cid == 555 for cid, _ in sent)                         # у 555 доступу до mama вже немає


def test_start_and_help_texts():
    upd, ctx = make_update(1)
    run(bot.cmd_start(upd, ctx))
    t = said(upd)
    assert "Я стежу за домашніми девайсами" in t and "Твій доступ" in t and "/help" in t
    assert "chat_id" not in t and "/debug" not in t                 # технічного нема
    upd, ctx = make_update(1)
    run(bot.cmd_help(upd, ctx))
    assert "/invite" in said(upd) and "/debug" not in said(upd)
    upd, ctx = make_update(555)
    run(bot.cmd_help(upd, ctx))
    assert "/invite" not in said(upd) and "Інструкція" in said(upd)


def test_bridge_name_and_index():
    assert S.devices()["main"]["index"] == 1 and S.devices()["mama"]["index"] == 2
    cfg = {"name": "Oukitel Battery", "state_topic": "homeassistant/sensor/oukitel/battery",
           "device": {"name": "Oukitel P2001", "model": "P2001 Plus", "serial_number": "AA:BB:01",
                      "connections": [["mac", "AA:BB:01"]]}}
    bot.handle_discovery(json.dumps(cfg).encode())
    assert S.device_name("main") != "Oukitel P2001"                  # шаблонну назву з MQTT НЕ беремо
    assert S.devices()["main"]["serial"] == "AA:BB:01" and bot.state_of("main").meta["model"] == "P2001 Plus"
    other = dict(cfg, state_topic="homeassistant/sensor/oukitel_mama/battery",
                 device={"name": "x", "serial_number": "CC:DD:02"})
    bot.handle_discovery(json.dumps(other).encode())
    assert S.devices()["mama"]["serial"] == "CC:DD:02" and S.device_name("mama") == "Мама 2 кВт"
    bot.handle_discovery(json.dumps(dict(cfg, state_topic="other/topic")).encode())   # чужий топік
    bot.handle_discovery(b"not json")
    # справжня назва — з журналу моста, код DK збігається з MAC
    logf = TMP / "bridge.log"
    logf.write_text("[19:30:00] start\n[19:32:18] 🔋 Power station detected: P1000PLUS/P1500PLUS_E9B3 "
                    "(DK: aabb01, PK: p11uve, authKey: present)\n[19:36:00] ⚠️ Device offline\n", encoding="utf-8")
    bot.BRIDGE_LOG_FILES = [str(logf), str(TMP / "немає.log"), str(TMP)]            # відсутній файл і тека не заважають
    assert bot.read_bridge_names(bot.BRIDGE_LOG_FILES) == {"aabb01": "P1000PLUS/P1500PLUS_E9B3"}
    run(bot.sync_bridge_names(None))
    assert S.device_name("main") == "P1000PLUS/P1500PLUS_E9B3" and S.devices()["main"]["name_src"] == "log"
    assert S.device_name("mama") == "Мама 2 кВт"                                    # у mama в журналі немає — без змін
    # ручна назва сильніша за журнал; "-" повертає журнал
    S.set_device_name("main", "Дім", "manual")
    assert S.set_device_name("main", "P1000", "log") is False and S.device_name("main") == "Дім"
    S.clear_manual_name("main")
    run(bot.sync_bridge_names(None))
    assert S.device_name("main") == "P1000PLUS/P1500PLUS_E9B3"
    # серійний змінився -> попередження адміну
    swapped = dict(cfg, device={"serial_number": "ZZ:99"})
    bot.handle_discovery(json.dumps(swapped).encode())
    assert len(bot.ADMIN_NOTICES) == 1 and "AA:BB:01" in bot.ADMIN_NOTICES[0]
    app = MagicMock(); app.bot_data = {}; app.bot.send_message = AsyncMock()
    run(bot.check_alerts(SimpleNamespace(application=app)))
    assert not bot.ADMIN_NOTICES
    assert any(c[0][0] == 1 and "серійний" in c[0][1] for c in app.bot.send_message.call_args_list)
    S.update_serial("main", "AA:BB:01")
    S.set_device_name("main", "Oukitel P2001", "manual")      # для наступних тестів


def test_alias_duplicates_and_rename_flow():
    S.add_device("dacha", "Oukitel P2001", "homeassistant/sensor/oukitel_dacha")      # така сама назва
    S.set_access(555, "main", "guest"); S.set_access(555, "dacha", "owner")
    assert S.label(555, "main") == "Oukitel P2001 №1" and S.label(555, "dacha") == "Oukitel P2001 №3"
    # перейменування: кнопка -> текст
    upd, ctx = make_update(555, data="ren:dacha")
    run(bot.on_button(upd, ctx))
    assert ctx.user_data["rename"] == "dacha"
    upd2, _ = make_update(555, "Дача <b>")
    run(bot.on_text(upd2, ctx))
    assert S.label(555, "dacha") == "Дача <b>" and "rename" not in ctx.user_data
    assert "&lt;b&gt;" in said(upd2)                                               # html екранується
    assert S.label(1, "dacha") == "Oukitel P2001 №3"                               # у інших — без змін
    # статус показує особисту назву
    upd3, ctx3 = make_update(555, data="status:refresh:dacha")
    run(bot.on_button(upd3, ctx3))
    assert "Дача" in edited(upd3)
    # скидання і занадто довга назва
    ctx.user_data["rename"] = "dacha"
    run(bot.on_text(make_update(555, "x" * 41)[0], ctx))
    assert S.alias(555, "dacha") == "Дача <b>"
    run(bot.on_text(make_update(555, "-")[0], ctx))
    assert S.alias(555, "dacha") is None
    # чужий текст без очікування — ігнорується
    upd4, ctx4 = make_update(555, "привіт")
    run(bot.on_text(upd4, ctx4))
    upd4.message.reply_text.assert_not_called()
    # чужа станція не перейменовується
    upd5, ctx5 = make_update(555, data="ren:nonexistent")
    run(bot.on_button(upd5, ctx5))
    assert "rename" not in ctx5.user_data


def _buttons(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def test_status_and_detail_format():
    v = {"battery": 87, "temperature": 31, "input_watts": 450, "ac_input_watts": 450, "output_watts": 120,
         "hardware_fault_status": "Normal", "availability": "online", "ac_charging_limit": 100,
         "output_frequency": "50Hz", "ac_output_voltage": 230, "ac_switch": "ON", "dc_switch": "OFF",
         "wifi_signal": -46, "bms_version": 206, "inverter_version": 106, "connection_mode": "Cloud",
         "typec1_power": 0, "usb_a_power": 5, "dc_output_voltage": 0.0, "dc_output_current": 0.0}
    t = bot.format_status(v, time.time(), "Дача")
    lines = t.split("\n")
    assert lines[0] == "⚡ <b>Дача</b>" and lines[1] == "Мережа: ✅ є"
    assert "87%" in lines[2] and "31 °C" in lines[2]
    assert "Вхід: <b>450 Вт</b>" in lines[3] and "Вихід: <b>120 Вт</b>" in lines[3]
    assert "Помилки: ✅ немає" in t and re.search(r"Оновлено: \d\d\.\d\d\.\d{4} \d\d:\d\d", t)
    bad = dict(v, ac_input_watts=0, input_watts=0, hardware_fault_status="Overload Protection", availability="offline")
    t2 = bot.format_status(bad, time.time() - 1000, "Дача")
    assert "Мережа: ❌ немає" in t2 and "Помилки: 1" in t2 and "⚠️ Спрацював захист" in t2
    assert "не на зв'язку" in t2 and "застарілі" in t2
    d = bot.format_detail(v, time.time(), "Дача")
    assert "детальний стан" in d and "50 Гц" in d and "Ліміт зарядки з мережі: 100%" in d
    assert "Wi-Fi: -46 дБм" in d and "BMS 206" in d and "Вихід AC: увімкнено" in d and "Потужність заряду батареї: +330 Вт" in d
    assert not any(ch in d for ch in "✅❌⚠️🔋🌡⬇⬆")            # «суха» інформація без картинок
    assert "ще немає" in bot.format_detail({}, 0, "Дача")


def test_menu_navigation():
    labels = [b.text for row in bot.menu_for(1).keyboard for b in row]
    assert labels[:4] == ["📊 Стан", "🎛 Керування", "🔔 Сповіщення", "📈 Статистика"] and "👥 Користувачі" in labels
    assert "👥 Користувачі" not in [b.text for row in bot.menu_for(555).keyboard for b in row]
    for data, expect in (("m:main", "Головне меню"), ("m:control", "в розробці"), ("m:stats", "в розробці"),
                         ("m:alerts", "Сповіщення"), ("sch:open", "Розклад звітів"), ("m:help", "Інструкція"),
                         ("m:users", "Користувачі"), ("sd:main", "детальний стан"), ("st:main", "Мережа")):
        upd, ctx = make_update(1, data=data)
        run(bot.on_button(upd, ctx))
        assert expect in edited(upd), data
    # кнопки екрану «Стан»: оновити, назва, детальний, назад (до вибору станції — їх кілька), головне меню
    kb = bot.status_view("main", 1)[1]
    assert _buttons(kb) == ["st:main", "ren:main", "sd:main", "m:status", "m:main"]
    assert _buttons(bot.detail_view("main", 1)[1]) == ["sd:main", "st:main", "m:main"]
    assert "sch:open" in _buttons(bot.alerts_view(1)[1])
    # доступ: чужа станція і розділ адміна
    upd, ctx = make_update(555, data="m:users")
    run(bot.on_button(upd, ctx))
    upd.callback_query.answer.assert_called_with("⛔ Лише для адміна")
    upd, ctx = make_update(555, data="sd:nonexistent")
    run(bot.on_button(upd, ctx))
    upd.callback_query.answer.assert_called_with("⛔ Немає доступу до цієї станції")
    # кнопка меню знімає очікування назви
    upd, ctx = make_update(1, "📈 Статистика")
    ctx.user_data["rename"] = "main"
    run(bot.on_menu_text(upd, ctx))
    assert "rename" not in ctx.user_data and "в розробці" in said(upd)
    # старе меню (версія 1) ще працює
    upd, ctx = make_update(1, "⏰ Розклад")
    run(bot.on_menu_text(upd, ctx))
    assert "Розклад звітів" in said(upd)


def test_devname_flow_admin_only():
    upd, ctx = make_update(555, data="dn:dacha")
    run(bot.on_button(upd, ctx))
    assert "rename" not in ctx.user_data                         # не адмін
    upd, ctx = make_update(1, data="dn:main")
    run(bot.on_button(upd, ctx))
    assert ctx.user_data["rename_scope"] == "official"
    run(bot.on_text(make_update(1, "Дім")[0], ctx))
    assert S.device_name("main") == "Дім" and S.devices()["main"]["name_src"] == "manual"
    upd, ctx = make_update(1, data="dn:main")
    run(bot.on_button(upd, ctx))
    run(bot.on_text(make_update(1, "-")[0], ctx))
    assert S.devices()["main"]["name_src"] is None
    S.set_device_name("main", "Oukitel P2001", "manual")


def test_grid_detection_without_input_power():
    g = bot.grid_signal
    assert g({}) is None
    assert g({"ac_input_watts": 450, "output_watts": 120, "battery": 60}) == "on"
    assert g({"ac_input_watts": 20, "output_watts": 20, "battery": 100}) == "on"            # прохідне живлення з малим навантаженням
    # навантаження є, входу немає — живимось від батареї, навіть якщо заряд 98–100%
    assert g({"ac_input_watts": 0, "output_watts": 120, "battery": 100}) == "off"
    assert g({"ac_input_watts": 0, "output_watts": 300, "battery": 98}) == "off"
    # нічого не підключено: відключення видно за просадкою заряду нижче ліміту
    assert g({"ac_input_watts": 0, "output_watts": 0, "battery": 100, "ac_charging_limit": 100}) == "idle"   # скриншот: 100%, простій
    assert g({"ac_input_watts": 0, "output_watts": 0, "battery": 99}) == "idle"
    assert g({"ac_input_watts": 0, "output_watts": 0, "battery": 98}) == "off"
    assert g({"ac_input_watts": 0, "output_watts": 0, "battery": 60, "ac_charging_limit": 100}) == "off"
    assert g({"ac_input_watts": 0, "output_watts": 0, "battery": 80, "ac_charging_limit": 80}) == "idle"   # досягла свого ліміту
    assert g({"ac_input_watts": 0, "output_watts": 0, "battery": 77, "ac_charging_limit": 80}) == "off"
    assert g({"input_watts": 300, "battery": 50}) == "on"                                   # немає ac_input_watts — беремо загальний вхід
    # реальні показники станції: прохідне живлення — ac_input_watts=0, але загальний вхід = навантаження
    real = {"ac_input_watts": 0, "input_watts": 231, "output_watts": 231, "dc_input_watts": 0, "battery": 100, "ac_charging_limit": 100}
    assert bot.grid_watts(real) == 231 and g(real) == "on"
    assert g(dict(real, input_watts=231, dc_input_watts=231)) == "off"                      # весь вхід — сонце, мережі нема
    assert bot.grid_watts({"ac_input_watts": 40, "input_watts": 340, "dc_input_watts": 300}) == 40
    assert bot.grid_watts({"ac_input_watts": 0, "input_watts": 100, "dc_input_watts": 300}) == 0   # не буває від'ємної
    # пам'ять: простій не змінює останній відомий стан
    assert bot.grid_status_from({"ac_input_watts": 0, "output_watts": 0, "battery": 100}, None) == (True, False)
    assert bot.grid_status_from({"ac_input_watts": 0, "output_watts": 0, "battery": 100}, False) == (False, False)
    assert bot.grid_status_from({"ac_input_watts": 0, "output_watts": 50, "battery": 70}, True) == (False, True)
    st = bot.StationState()
    base = "homeassistant/sensor/x"
    for k, v in (("battery", b"100"), ("output_watts", b"0"), ("ac_input_watts", b"300")):
        st.update(f"{base}/{k}", v, base)
    assert st.grid_status() == (True, True)
    st.update(f"{base}/ac_input_watts", b"0", base)          # зарядилась, простоює
    assert st.grid_status() == (True, False)                 # пам'ятаємо, що мережа була
    st.update(f"{base}/output_watts", b"80", base)           # з'явилося навантаження, а входу немає — живимось від батареї
    assert st.grid_status() == (False, True)
    st.update(f"{base}/output_watts", b"0", base)            # навантаження зникло, заряд 100% — тримаємо "немає", але неточно
    assert st.grid_status() == (False, False)
    st.update(f"{base}/battery", b"90", base)                # заряд просів нижче ліміту, входу немає — точно "немає"
    assert st.grid_status() == (False, True)


def test_grid_text_and_alerts_with_idle():
    idle = {"battery": 100, "temperature": 29, "input_watts": 0, "ac_input_watts": 0, "output_watts": 0,
            "hardware_fault_status": "Normal"}
    assert "Мережа: ✅ є (ймовірно)" in bot.format_status(idle, time.time(), "X")
    assert "ймовірно" in bot.format_detail(idle, time.time(), "X")
    assert "Мережа: ✅ є\n" in bot.format_status(dict(idle, ac_input_watts=30), time.time(), "X")
    # сповіщення: простій не вважається відключенням
    bot.INPUT_DEBOUNCE_S = 0; bot.GRID_OFF_DEBOUNCE_S = 0
    app = MagicMock(); app.bot_data = {}
    st = bot.state_of("dacha"); st.values = {}; st.updated = 0
    base = "homeassistant/sensor/oukitel_dacha"
    def feed(**kw):
        for k, v in kw.items():
            st.update(f"{base}/{k}", str(v).encode(), base)
    kinds = lambda: [k for k, _ in bot.device_events(app, "dacha")]            # noqa: E731
    feed(battery=100, ac_input_watts=0, output_watts=0)
    assert not {"light_on", "light_off"} & set(kinds())                              # простій: тихо
    assert app.bot_data["alert_state"]["dacha"]["grid_on"] is True                   # базова лінія: мережа є
    feed(output_watts=60)                                                            # навантаження є, входу немає — живимось від батареї
    assert "light_off" in kinds()
    feed(output_watts=0)                                                             # знову простій — повторів нема
    assert not {"light_on", "light_off"} & set(kinds())
    feed(ac_input_watts=200)
    assert "light_on" in kinds()


def _collect(app):
    return [(c[0][0], c[0][1]) for c in app.bot.send_message.call_args_list]


def test_notification_settings_screens():
    labels = lambda kb: [b.text for row in kb.inline_keyboard for b in row]      # noqa: E731
    text, kb = bot.alerts_view(1)                                                 # у адміна кілька станцій
    assert "Для якої станції" in text and "nt:main" in _buttons(kb)
    upd, ctx = make_update(1, data="nt:main")
    run(bot.on_button(upd, ctx))
    assert "Усі сповіщення вимкнені" in edited(upd)
    kb = upd.callback_query.edit_message_text.call_args[1]["reply_markup"]
    assert "⬜ Світло з'явилося" in labels(kb) and "⬜ Заряд станції 20%" in labels(kb)
    assert [b for b in labels(kb) if b.endswith("%") and b[0] in "1234●"][:3] == ["10%", "15%", "● 20%"]
    upd, ctx = make_update(1, data="nt:main:light_off")
    run(bot.on_button(upd, ctx))
    assert "✅ Світло зникло" in edited(upd)
    upd, ctx = make_update(1, data="nl:main:30")
    run(bot.on_button(upd, ctx))
    assert S.notify(1, "main")["low_level"] == 30 and "⬜ Заряд станції 30%" in labels(
        upd.callback_query.edit_message_text.call_args[1]["reply_markup"])
    run(bot.on_button(*make_update(1, data="nl:main:99")))                       # невірний поріг ігнорується
    assert S.notify(1, "main")["low_level"] == 30
    run(bot.on_button(*make_update(1, data="nt:main:bogus")))                    # невідомий ключ — нічого не міняє
    upd, ctx = make_update(555, data="nt:mama:full")
    run(bot.on_button(upd, ctx))
    upd.callback_query.answer.assert_called_with("⛔ Немає доступу до цієї станції")
    S.set_notify(1, "main", "light_off", False)


def test_event_fanout_antispam_and_per_user_low():
    S.add_device("t1", "T1", "homeassistant/sensor/t1")
    S.set_access(555, "t1", "guest")
    for k in ("light_on", "light_off", "full", "error", "stale", "low"):
        S.set_notify(1, "t1", k, True)
    S.set_notify(555, "t1", "full", True)
    S.set_notify(555, "t1", "low", True); S.set_notify(555, "t1", "low_level", 15)
    S.set_notify(1, "t1", "low_level", 30)
    bot.INPUT_DEBOUNCE_S = 0; bot.GRID_OFF_DEBOUNCE_S = 0
    app = MagicMock(); app.bot_data = {}; app.bot.send_message = AsyncMock()
    ctx = SimpleNamespace(application=app)
    base = "homeassistant/sensor/t1"
    st = bot.state_of("t1")
    feed = lambda **kw: [st.update(f"{base}/{k}", str(v).encode(), base) for k, v in kw.items()]   # noqa: E731

    def step(**kw):
        app.bot.send_message.reset_mock()
        feed(**kw)
        run(bot.check_alerts(ctx))
        return {cid: t for cid, t in _collect(app) if "T1" in t}

    assert step(battery=80, ac_input_watts=300, output_watts=0, hardware_fault_status="Normal") == {}   # перше спостереження — тихо
    got = step(battery=100)
    assert "заряджено" in got[1] and "заряджено" in got[555]                         # обидва підписані на «заряджена»
    assert step(battery=98) == {} and step(battery=100) == {}                        # цикли 98↔100 — без спаму
    assert step(battery=94) == {}
    assert "заряджено" in step(battery=100)[1]                                       # впала до 94% → знову взведено

    # відключення світла: 555 про нього не просив
    got = step(ac_input_watts=0, output_watts=100, remain_time=380)                  # навантаження є, входу немає
    assert "Світло зникло" in got[1] and "6 год 20 хв" in got[1] and 555 not in got
    assert step(battery=99) == {} and step(battery=96) == {}                         # після відключення «заряджена» взводиться на 97%
    app.bot_data["alert_state"]["t1"]["off_since"] = time.time() - 8100
    got = step(ac_input_watts=200, output_watts=0)
    assert "Світло з'явилося" in got[1] and "Світла не було 2 год 15 хв" in got[1]
    assert "заряджено" not in step(battery=99).get(1, "")
    assert "заряджено" in step(battery=100)[1]                                       # знову заряджена — сповіщення є
    assert bot.fmt_duration(30) == "менше хвилини" and bot.fmt_duration(8100) == "2 год 15 хв"

    # помилки
    assert "Помилка станції: <b>Спрацював захист" in step(hardware_fault_status="Overload Protection")[1]
    assert step(hardware_fault_status="Overload Protection") == {}                  # без повторів
    assert "Помилку усунуто" in step(hardware_fault_status="Normal")[1]

    # низький заряд: у кожного свій поріг (адмін 30%, 555 — 15%), повтор лише після підйому на 5%
    for who in (1, 555):                                                             # перемикач порога скидає стан без повідомлення
        S.set_notify(who, "t1", "low_level", S.notify(who, "t1")["low_level"])
    assert step(battery=50) == {}
    got = step(battery=28)
    assert "знизився до <b>28%" in got[1] and 555 not in got
    assert "знизився до <b>14%" in step(battery=14)[555] and 1 not in step(battery=13)
    assert step(battery=36) == {}                                                    # адмін знову «взведений» (≥35)
    assert "знизився до <b>29%" in step(battery=29)[1]

    # немає даних
    st.updated = time.time() - 1000
    app.bot.send_message.reset_mock(); run(bot.check_alerts(ctx))
    assert "немає даних уже" in dict(_collect(app))[1] and 555 not in dict(_collect(app))
    assert "Зв'язок зі станцією відновлено" in step(battery=60)[1]
    S.set_access(555, "t1", None)


def test_alert_state_survives_restart():
    S.add_device("t2", "T2", "homeassistant/sensor/t2")
    S.set_notify(1, "t2", "full", True)
    S.set_notify(1, "t2", "low", True)
    base = "homeassistant/sensor/t2"
    st = bot.state_of("t2")
    feed = lambda **kw: [st.update(f"{base}/{k}", str(v).encode(), base) for k, v in kw.items()]   # noqa: E731

    def make_app(saved=None):
        app = MagicMock(); app.bot_data = {"alert_state": bot.restore_alert_state(saved or {})}
        app.bot.send_message = AsyncMock()
        return app

    # запуск, коли станція вже на 100%: «заряджена» не надсилається (раніше це був спам після кожного оновлення бота)
    feed(battery=100, ac_input_watts=300, output_watts=0)
    app = make_app()
    run(bot.check_alerts(SimpleNamespace(application=app)))
    assert not [t for _, t in _collect(app) if "T2" in t]
    run(bot.check_alerts(SimpleNamespace(application=app)))                          # у тому числі при наступних циклах
    assert not [t for _, t in _collect(app) if "T2" in t]
    # «низький заряд» у людини — стан зберігається з числовим ключем chat_id
    feed(battery=50)
    run(bot.check_alerts(SimpleNamespace(application=app)))
    saved = STORE_DATA()["alert_state"]["t2"]
    assert saved["full"] is False and "1" in saved["low"]                            # у файлі ключ — рядок
    app2 = make_app(saved={"t2": saved})                                             # «перезапуск»
    assert 1 in app2.bot_data["alert_state"]["t2"]["low"]                            # відновлено як число
    feed(battery=100)
    run(bot.check_alerts(SimpleNamespace(application=app2)))
    assert any("заряджено" in t for _, t in _collect(app2))                          # справжнє досягнення 100% — повідомляє
    app3 = make_app(saved=STORE_DATA()["alert_state"])                               # ще один «перезапуск» на 100%
    run(bot.check_alerts(SimpleNamespace(application=app3)))
    assert not [t for _, t in _collect(app3) if "T2" in t]


def STORE_DATA():
    return json.loads(S.path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("OK", name)
    print("Усі сценарії пройдені")
