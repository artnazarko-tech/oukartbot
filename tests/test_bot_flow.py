"""Имитация работы бота без Telegram и MQTT: проверка ролей, приглашений, меню, уведомлений.
Запуск: python3 tests/test_bot_flow.py"""
import asyncio
import json
import os
import sys
import tempfile
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
    q = SimpleNamespace(data=data, answer=AsyncMock(), edit_message_text=AsyncMock()) if data else None
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
    assert "87%" in said(upd) and "Oukitel" in said(upd)
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
    # алерты по устройству независимы
    app = MagicMock(); app.bot_data = {}
    msgs = bot.collect_alerts(app, "mama")
    assert any("Низький заряд" in m for m in msgs)
    assert not any("Низький" in m for m in bot.collect_alerts(app, "main"))


def test_alert_recipients_and_prefix():
    app = MagicMock(); app.bot_data = {}; app.bot.send_message = AsyncMock()
    ctx = SimpleNamespace(application=app)
    bot.state_of("mama").update("homeassistant/sensor/oukitel_mama/battery", b"10", "homeassistant/sensor/oukitel_mama")
    run(bot.check_alerts(ctx))
    sent = [(c[0][0], c[0][1]) for c in app.bot.send_message.call_args_list]
    assert any(cid == 1 and "Мама" in t for cid, t in sent)              # админ получил с подписью станции
    assert not any(cid == 555 for cid, _ in sent)                        # у 555 доступа к mama уже нет


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("OK", name)
    print("Усі сценарії пройдені")
