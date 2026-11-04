# Telegram-бот для Oukitel P2001E Plus

Бот показує заряд, вхідну та вихідну потужність станції — по команді `/status`, за розкладом і автоматично при подіях (зникло/з'явилося світло, низький заряд, повний заряд, аварія).

## Як це працює

```
Станція (Wi‑Fi) ──► oukitel-portable-bridge ──► MQTT (mosquitto) ──► цей бот ──► Telegram
```

У Oukitel немає офіційного API. Дані бере спільнотний міст
[oukitel-portable-bridge](https://github.com/VictorCV-DAM/oukitel-portable-bridge):
він підключається до станції напряму по локальній мережі (TCP 6607) або, якщо не бачить її, — через хмару застосунку **Wonderfree** (той самий логін/пароль).
Бот лише підписується на MQTT-топіки моста.

> ⚠️ Офіційно міст перевірено на P2001 Plus / P2001 / P5000 / BP2000. P2001E Plus працює через той самий застосунок Wonderfree, тож має підійти, але це варто перевірити командою `/debug` після запуску.

## Що потрібно

- Комп'ютер, що працює постійно: Raspberry Pi, міні‑ПК, старий ноутбук або VPS (для VPS працюватиме лише хмарний режим).
- Станція підключена до Wi‑Fi через застосунок Wonderfree.
- Python 3.10+ **або** Docker.

## Крок 1. Створи бота

1. Напиши [@BotFather](https://t.me/BotFather) → `/newbot` → отримай токен.
2. Скопіюй `.env.example` у `.env` і встав `BOT_TOKEN`.

## Крок 2. Міст

```bash
git clone https://github.com/VictorCV-DAM/oukitel-portable-bridge.git
cp oukitel-portable-bridge/config.example.json oukitel-portable-bridge/config.json
```

У `config.json` вкажи:
- `cloud.email`, `cloud.password` — від застосунку Wonderfree, `region: "EU"`;
- `mqtt.broker` — `127.0.0.1`, `user`/`password` — порожні;
- `mqtt.topic_base` — залиш `homeassistant/sensor/oukitel` (такий самий у `.env` бота).

## Крок 3а. Запуск через Docker (найпростіше)

```bash
docker compose up -d --build
docker compose logs -f bot
```

## Крок 3б. Запуск без Docker (Raspberry Pi / Linux)

```bash
sudo apt install mosquitto
# термінал 1 — міст
cd oukitel-portable-bridge && pip install -r requirements.txt && python3 oukitel_cloud_mqtt.py
# термінал 2 — бот
pip install -r requirements.txt && python3 bot.py
```

Для роботи 24/7 оформи обидва як systemd-сервіси (приклад є в README моста).

## Крок 4. Налаштування в Telegram

1. Напиши боту `/start` — він покаже твій `chat_id`.
2. Впиши його в `ALLOWED_CHAT_IDS` у `.env` і перезапусти бота (щоб ніхто інший не бачив твою станцію).
3. `/debug` — переконайся, що дані приходять.

## Кнопки

Внизу чату є меню: **📊 Стан**, **⏰ Розклад**, **🔔 Сповіщення**, **ℹ️ Допомога**.
Під станом є кнопка **🔄 Оновити**, а в розкладі можна вмикати час і інтервал натисканням.

## Команди

| Команда | Що робить |
|---|---|
| `/status` | Поточний стан: заряд, вхід (мережа/сонце), вихід (AC/DC), скільки ще пропрацює |
| `/schedule 08:00 20:00` | Щоденні звіти у вказаний час |
| `/every 60` | Звіт кожні N хвилин (`/every 0` — вимкнути) |
| `/schedules` | Показати розклад |
| `/clear` | Очистити розклад |
| `/alerts` | Пороги автоматичних сповіщень |
| `/debug` | Сирі MQTT-дані для діагностики |

## Автоматичні сповіщення

- 🔌 **Живлення зникло / 💡 з'явилося** — за вхідною потужністю з мережі (зручно під час відключень).
- 🪫 Заряд нижче `ALERT_LOW_BATTERY`.
- 🔋 Заряд досяг `ALERT_FULL_BATTERY`.
- ⚠️ Аварійні стани (перегрів, перевантаження тощо).
- 📡 Немає даних від станції довше `STALE_AFTER_S`.

## Якщо щось не так

- `/status` пише «даних ще немає» → дивись логи моста; перевір логін Wonderfree і регіон.
- `/debug` показує дані, але в `/status` прочерки → назви топіків відрізняються; надішли вивід `/debug`, і назви полів легко додати у функцію `format_status`.
- Станція «засинає» — міст сам надсилає keep-alive; переконайся, що Wi‑Fi станції стабільний.
