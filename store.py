"""
Сховище: пристрої, користувачі, ролі та запрошення.

Усе лежить в одному файлі data/store.json (на сервері він не потрапляє в git).

Ролі:
  admin — бачить і може все (задається в .env через ALLOWED_CHAT_IDS);
  owner — повне керування призначеними йому пристроями;
  guest — лише перегляд призначених пристроїв.
Роль owner/guest задається окремо для кожного пристрою:
одна людина може бути власником однієї станції та гостем іншої.
"""

import json
import os
import secrets
import threading
import time
from pathlib import Path

ROLE_ADMIN = "admin"
ROLE_OWNER = "owner"
ROLE_GUEST = "guest"
ROLE_TITLES = {ROLE_ADMIN: "Адмін", ROLE_OWNER: "Власник", ROLE_GUEST: "Гість"}

INVITE_TTL_S = 48 * 3600


def _empty() -> dict:
    return {"version": 2, "devices": {}, "users": {}, "invites": {}}


class Store:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._lock = threading.RLock()
        self.data = self._load()
        if self._ensure_indexes():
            self.save()

    # ------------------------------------------------------------ файл
    def _load(self) -> dict:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return _empty()
        data = _empty()
        data.update(raw)
        return data

    def _ensure_indexes(self) -> bool:
        """Кожна станція має постійний номер (№1, №2...). Старим записам номери видаються тут."""
        changed = False
        used = [d["index"] for d in self.data["devices"].values() if "index" in d]
        nxt = max(used, default=0) + 1
        for d in self.data["devices"].values():
            if "index" not in d:
                d["index"] = nxt
                nxt += 1
                changed = True
        return changed

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)   # атомарно: файл не буде "напівзаписаним"

    # ------------------------------------------------------------ пристрої
    def devices(self) -> dict:
        with self._lock:
            return {k: dict(v) for k, v in self.data["devices"].items()}

    def add_device(self, dev_id: str, name: str, topic_base: str) -> None:
        with self._lock:
            used = [d.get("index", 0) for d in self.data["devices"].values()]
            self.data["devices"][dev_id] = {
                "name": name, "topic_base": topic_base.rstrip("/"),
                "index": max(used, default=0) + 1,
            }
            self.save()

    def device_name(self, dev_id: str) -> str:
        """Справжня назва станції (її повідомляє міст). Однакова для всіх користувачів."""
        return self.data["devices"].get(dev_id, {}).get("name", dev_id)

    def update_device_name(self, dev_id: str, name: str) -> bool:
        """Оновлює назву, яку повідомив міст. Повертає True, якщо вона змінилася."""
        with self._lock:
            dev = self.data["devices"].get(dev_id)
            if dev is None or not name or dev.get("name") == name:
                return False
            dev["name"] = name
            self.save()
            return True

    def full_name(self, dev_id: str) -> str:
        """'Oukitel P2001E Plus №2' — для адміна й для розрізнення однакових станцій."""
        dev = self.data["devices"].get(dev_id, {})
        return f"{dev.get('name', dev_id)} №{dev.get('index', '?')}"

    # ------------------------------------------------------------ особисті назви
    def alias(self, chat_id: int, dev_id: str) -> str | None:
        user = self.get_user(chat_id)
        return (user or {}).get("aliases", {}).get(dev_id)

    def set_alias(self, chat_id: int, dev_id: str, alias: str | None) -> None:
        """Особиста назва станції: її бачить лише цей користувач."""
        with self._lock:
            user = self.get_user(chat_id)
            if user is None:
                return
            aliases = user.setdefault("aliases", {})
            if alias:
                aliases[dev_id] = alias
            else:
                aliases.pop(dev_id, None)
            self.save()

    def label(self, chat_id: int, dev_id: str) -> str:
        """Як назвати станцію саме для цієї людини:
        її власна назва → інакше назва з моста (з номером, якщо в неї є дві однакові)."""
        alias = self.alias(chat_id, dev_id)
        if alias:
            return alias
        name = self.device_name(dev_id)
        same = [d for d in self.devices_for(chat_id) if self.device_name(d) == name]
        return self.full_name(dev_id) if len(same) > 1 else name

    # ------------------------------------------------------------ користувачі
    def get_user(self, chat_id: int) -> dict | None:
        return self.data["users"].get(str(chat_id))

    def add_user(self, chat_id: int, name: str = "", admin: bool = False) -> dict:
        with self._lock:
            user = self.data["users"].setdefault(str(chat_id), {
                "name": name, "admin": False, "access": {},
                "schedule": {"times": [], "interval_min": 0},
            })
            if name:
                user["name"] = name
            if admin:
                user["admin"] = True
            self.save()
            return user

    def ensure_admin(self, chat_id: int, name: str = "") -> None:
        self.add_user(chat_id, name=name, admin=True)

    def all_users(self) -> list[tuple[int, dict]]:
        with self._lock:
            return [(int(cid), u) for cid, u in self.data["users"].items()]

    def is_admin(self, chat_id: int) -> bool:
        user = self.get_user(chat_id)
        return bool(user and user.get("admin"))

    def role(self, chat_id: int, dev_id: str) -> str | None:
        user = self.get_user(chat_id)
        if user is None or dev_id not in self.data["devices"]:
            return None
        if user.get("admin"):
            return ROLE_ADMIN
        return user.get("access", {}).get(dev_id)

    def can_control(self, chat_id: int, dev_id: str) -> bool:
        return self.role(chat_id, dev_id) in (ROLE_ADMIN, ROLE_OWNER)

    def devices_for(self, chat_id: int) -> list[str]:
        return [d for d in self.data["devices"] if self.role(chat_id, d)]

    def users_for_device(self, dev_id: str) -> list[int]:
        return [cid for cid, _ in self.all_users() if self.role(cid, dev_id)]

    def set_access(self, chat_id: int, dev_id: str, role: str | None) -> None:
        with self._lock:
            user = self.get_user(chat_id)
            if user is None:
                return
            if role is None:
                user["access"].pop(dev_id, None)
            else:
                user["access"][dev_id] = role
            self.save()

    def remove_user(self, chat_id: int) -> None:
        with self._lock:
            self.data["users"].pop(str(chat_id), None)
            self.save()

    def schedule(self, chat_id: int) -> dict:
        user = self.get_user(chat_id)
        if user is None:
            return {"times": [], "interval_min": 0}
        return user.setdefault("schedule", {"times": [], "interval_min": 0})

    # ------------------------------------------------------------ запрошення
    def create_invite(self, role: str, devices: list[str], created_by: int, ttl: int = INVITE_TTL_S) -> str:
        """Одноразове запрошення. Адмінів запрошенням створити не можна — вони задаються в .env."""
        if role not in (ROLE_OWNER, ROLE_GUEST):
            raise ValueError("Запрошення можливе лише для ролей owner / guest")
        with self._lock:
            self._purge_invites()
            code = secrets.token_urlsafe(9)
            self.data["invites"][code] = {
                "role": role, "devices": list(devices),
                "created_by": created_by, "expires": time.time() + ttl,
            }
            self.save()
            return code

    def use_invite(self, code: str, chat_id: int, name: str) -> dict | None:
        with self._lock:
            self._purge_invites()
            inv = self.data["invites"].pop(code, None)   # одноразове: видаляємо одразу
            if inv is None:
                return None
            user = self.add_user(chat_id, name=name)
            for dev_id in inv["devices"]:
                if dev_id in self.data["devices"]:
                    user["access"][dev_id] = inv["role"]
            self.save()
            return user

    def _purge_invites(self) -> None:
        now = time.time()
        for code in [c for c, i in self.data["invites"].items() if i["expires"] < now]:
            del self.data["invites"][code]


def import_legacy_schedules(store: Store, legacy: dict, dev_id: str) -> int:
    """Перенос розкладів зі старого data/schedules.json (версія 1).
    Старий файл не змінюється, тож відкат на main нічого не втрачає."""
    count = 0
    for chat_id, cfg in legacy.items():
        user = store.add_user(int(chat_id))
        user["access"].setdefault(dev_id, ROLE_GUEST)
        user["schedule"] = {"times": list(cfg.get("times", [])), "interval_min": int(cfg.get("interval_min", 0) or 0)}
        count += 1
    store.save()
    return count
