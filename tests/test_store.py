"""Перевірка сховища користувачів і ролей. Запуск: python3 tests/test_store.py"""
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from store import ROLE_ADMIN, ROLE_GUEST, ROLE_OWNER, Store, import_legacy_schedules  # noqa: E402


def fresh() -> Store:
    s = Store(Path(tempfile.mkdtemp()) / "store.json")
    s.add_device("main", "Моя 1 кВт", "homeassistant/sensor/oukitel")
    s.add_device("mama", "Мама 2 кВт", "homeassistant/sensor/oukitel_mama")
    return s


def test_roles():
    s = fresh()
    s.ensure_admin(1, "Арт")
    s.add_user(2, "Мама")
    s.set_access(2, "mama", ROLE_OWNER)
    s.set_access(2, "main", ROLE_GUEST)
    assert s.role(1, "main") == ROLE_ADMIN and s.role(1, "mama") == ROLE_ADMIN
    assert s.role(2, "mama") == ROLE_OWNER and s.role(2, "main") == ROLE_GUEST
    assert s.can_control(2, "mama") and not s.can_control(2, "main")
    assert s.role(99, "main") is None                     # чужий
    assert s.devices_for(2) == ["main", "mama"]
    assert sorted(s.users_for_device("mama")) == [1, 2]   # адмін бачить усе
    s.set_access(2, "main", None)
    assert s.devices_for(2) == ["mama"]


def test_invite_one_time():
    s = fresh()
    s.ensure_admin(1)
    code = s.create_invite(ROLE_GUEST, ["main"], created_by=1)
    assert s.use_invite(code, 5, "Тато")["access"] == {"main": ROLE_GUEST}
    assert s.use_invite(code, 6, "Хтось") is None          # вдруге не працює
    assert s.get_user(6) is None


def test_invite_expired_and_admin_forbidden():
    s = fresh()
    code = s.create_invite(ROLE_OWNER, ["main"], created_by=1, ttl=-1)
    assert s.use_invite(code, 5, "x") is None
    try:
        s.create_invite(ROLE_ADMIN, ["main"], created_by=1)
        raise AssertionError("адмін-запрошення мало бути заборонене")
    except ValueError:
        pass


def test_persistence_and_atomic_save():
    s = fresh()
    s.add_user(7, "Брат")
    s.set_access(7, "main", ROLE_GUEST)
    s2 = Store(s.path)
    assert s2.role(7, "main") == ROLE_GUEST
    assert not s.path.with_suffix(".tmp").exists()


def test_legacy_import():
    s = fresh()
    n = import_legacy_schedules(s, {"123": {"times": ["08:00"], "interval_min": 60}}, "main")
    assert n == 1
    assert s.schedule(123) == {"times": ["08:00"], "interval_min": 60}
    assert s.role(123, "main") == ROLE_GUEST
    s.ensure_admin(123)                                    # стає адміном, розклад зберігається
    assert s.role(123, "main") == ROLE_ADMIN and s.schedule(123)["times"] == ["08:00"]


def test_indexes_aliases_labels():
    s = fresh()
    assert [s.devices()[d]["index"] for d in ("main", "mama")] == [1, 2]
    s.add_device("dacha", "Мама 2 кВт", "homeassistant/sensor/oukitel_dacha")         # однакова назва
    assert s.devices()["dacha"]["index"] == 3
    s.add_user(2, "Мама"); s.set_access(2, "mama", ROLE_OWNER); s.set_access(2, "dacha", ROLE_OWNER)
    assert s.label(2, "mama") == "Мама 2 кВт №2" and s.label(2, "dacha") == "Мама 2 кВт №3"
    s.set_access(2, "dacha", None)
    assert s.label(2, "mama") == "Мама 2 кВт"                    # дублів у неї немає — без номера
    s.set_alias(2, "mama", "Кухня")
    assert s.label(2, "mama") == "Кухня" and s.device_name("mama") == "Мама 2 кВт"
    assert Store(s.path).label(2, "mama") == "Кухня"             # зберігається
    s.set_alias(2, "mama", None)
    assert s.label(2, "mama") == "Мама 2 кВт"


def test_old_store_gets_indexes():
    s = fresh()
    for d in s.data["devices"].values():
        d.pop("index")                                           # як у файлі, створеному етапом 1
    s.save()
    s2 = Store(s.path)
    assert sorted(d["index"] for d in s2.devices().values()) == [1, 2]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("OK", name)
    print("Усі перевірки пройдені")
