"""Безкоштовні роздачі Epic Games Store.

Що робить цей файл (без Telegram — лише дані):
  fetch_free_games()  — питає публічний каталог Epic, які ігри роздають ЗАРАЗ;
  load_screenshots()  — добирає зі сторінки гри кілька скриншотів геймплею;
  format_caption()    — готовий текст повідомлення (назва, опис, до якої дати безкоштовно).

Публічні адреси Epic (ключ не потрібен):
  каталог роздач   https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions
  сторінка гри     https://store-content-ipv4.ak.epicgames.com/api/<мова>/content/products/<slug>
"""
from __future__ import annotations

import html
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger("oukitel-bot.epic")

PROMO_URL = "https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions"
CONTENT_URL = "https://store-content-ipv4.ak.epicgames.com/api/{locale}/content/products/{slug}"
STORE_URL = "https://store.epicgames.com/{locale}/p/{slug}"
# яку обкладинку брати, якщо скриншотів немає (за порядком уподобання)
COVER_TYPES = ("OfferImageWide", "DieselStoreFrontWide", "Thumbnail", "OfferImageTall")
MAX_PHOTOS = 4
CAPTION_LIMIT = 1000        # у Telegram підпис до фото — до 1024 символів


@dataclass
class Game:
    id: str
    title: str
    description: str
    start: datetime
    end: datetime
    url: str | None = None
    price: str | None = None            # звичайна ціна, наприклад «₴ 399,00»
    cover: str | None = None
    slug: str | None = None
    screenshots: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        """Унікальний ключ роздачі: одна й та сама гра може роздаватися знову в інший період."""
        return f"{self.id}:{self.end.strftime('%Y%m%d%H%M')}"

    def photos(self) -> list[str]:
        """Що надсилати як картинки: скриншоти геймплею, а якщо їх немає — обкладинку."""
        if self.screenshots:
            return self.screenshots[:MAX_PHOTOS]
        return [self.cover] if self.cover else []


def _dt(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


def _slug(el: dict) -> str | None:
    mappings = list(el.get("offerMappings") or []) + list((el.get("catalogNs") or {}).get("mappings") or [])
    for m in mappings:
        if m.get("pageSlug"):
            return str(m["pageSlug"]).removesuffix("/home")
    if el.get("productSlug"):
        return str(el["productSlug"]).removesuffix("/home")
    return None


def _cover(el: dict) -> str | None:
    images = {i.get("type"): i.get("url") for i in (el.get("keyImages") or []) if i.get("url")}
    for t in COVER_TYPES:
        if images.get(t):
            return images[t]
    return next(iter(images.values()), None)


def _free_window(el: dict, now: datetime) -> tuple[datetime, datetime] | None:
    """(початок, кінець) роздачі, якщо гра безкоштовна саме зараз, інакше None.
    У Epic «знижка 100%» записана як discountPercentage = 0 (це відсоток, який лишається ЗПЛАТИТИ)."""
    price = ((el.get("price") or {}).get("totalPrice")) or {}
    if "discountPrice" in price and price["discountPrice"] != 0:
        return None
    for block in (el.get("promotions") or {}).get("promotionalOffers") or []:
        for offer in block.get("promotionalOffers") or []:
            try:
                start, end = _dt(offer["startDate"]), _dt(offer["endDate"])
            except (KeyError, ValueError):
                continue
            if start <= now < end and (offer.get("discountSetting") or {}).get("discountPercentage") == 0:
                return start, end
    return None


def parse_free_games(data: dict, locale: str = "uk", now: datetime | None = None) -> list[Game]:
    """Розбирає відповідь каталогу Epic: лишає ігри, які безкоштовні просто зараз."""
    now = now or datetime.now(timezone.utc)
    elements = ((((data or {}).get("data") or {}).get("Catalog") or {}).get("searchStore") or {}).get("elements") or []
    games: list[Game] = []
    for el in elements:
        window = _free_window(el, now)
        if window is None or not el.get("id"):
            continue
        slug = _slug(el)
        games.append(Game(
            id=str(el["id"]), title=str(el.get("title") or "Без назви"),
            description=str(el.get("description") or "").strip(),
            start=window[0], end=window[1], slug=slug,
            url=STORE_URL.format(locale=locale, slug=slug) if slug else None,
            price=((el.get("price") or {}).get("totalPrice") or {}).get("fmtPrice", {}).get("originalPrice"),
            cover=_cover(el),
        ))
    return games


def parse_screenshots(content: dict, limit: int = MAX_PHOTOS) -> list[str]:
    """Дістає картинки з каруселі на сторінці гри (скриншоти й кадри геймплею)."""
    found: list[str] = []

    def walk(node) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if k in ("image", "src") and isinstance(v, str) and v.startswith("http"):
                    found.append(v)
                else:
                    walk(v)
        elif isinstance(node, list):
            for x in node:
                walk(x)

    for page in (content or {}).get("pages") or []:
        walk(((page or {}).get("data") or {}).get("carousel") or {})
    unique = list(dict.fromkeys(found))
    return unique[:limit]


async def fetch_free_games(client: httpx.AsyncClient, locale: str = "uk", country: str = "UA") -> list[Game]:
    resp = await client.get(PROMO_URL, params={"locale": locale, "country": country, "allowCountries": country})
    resp.raise_for_status()
    return parse_free_games(resp.json(), locale)


async def load_screenshots(client: httpx.AsyncClient, game: Game, locale: str = "uk") -> None:
    """Добирає скриншоти. Помилка тут не критична: тоді просто покажемо обкладинку."""
    if not game.slug:
        return
    try:
        resp = await client.get(CONTENT_URL.format(locale=locale, slug=game.slug))
        resp.raise_for_status()
        game.screenshots = parse_screenshots(resp.json())
    except Exception as e:  # noqa: BLE001
        log.warning("Не вдалося отримати скриншоти «%s»: %s", game.title, e)


def _shorten(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(" ,.;:—-")
    return cut + "…"


def _left(end: datetime, now: datetime) -> str:
    seconds = (end - now).total_seconds()
    if seconds <= 0:
        return "вже закінчується"
    days, rest = divmod(int(seconds), 86400)
    hours = rest // 3600
    if days:
        return f"залишилось {days} дн {hours} год"
    return f"залишилось {hours} год" if hours else "залишилось менше години"


def format_caption(game: Game, tz: ZoneInfo | timezone, now: datetime | None = None) -> str:
    """Текст повідомлення (HTML). Вкладається в ліміт підпису до фото."""
    now = now or datetime.now(timezone.utc)
    until = game.end.astimezone(tz).strftime("%d.%m.%Y о %H:%M")

    def build(desc: str) -> str:
        lines = [f"🎮 <b>{html.escape(game.title)}</b>", "Безкоштовно в Epic Games Store"]
        if desc:
            lines += ["", html.escape(desc)]
        lines.append("")
        if game.price:
            lines.append(f"💰 Звичайна ціна: {html.escape(game.price)} → <b>безкоштовно</b>")
        lines.append(f"⏳ Забрати можна до: <b>{until}</b> ({_left(game.end, now)})")
        if game.url:
            lines.append(f'🔗 <a href="{html.escape(game.url, quote=True)}">Забрати в Epic Games Store</a>')
        return "\n".join(lines)

    size = 500
    text = build(_shorten(game.description, size))
    while len(text) > CAPTION_LIMIT and size > 60:
        size -= 60
        text = build(_shorten(game.description, size))
    return text
