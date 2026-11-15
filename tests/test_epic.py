"""Перевірка розбору роздач Epic Games. Запуск: python3 tests/test_epic.py
Відповіді Epic тут підставні (структура як у справжньому каталозі), у мережу тест не ходить."""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import epic  # noqa: E402

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%S.000Z")   # noqa: E731


def offer(start, end, pct=0):
    return {"promotionalOffers": [{"startDate": iso(start), "endDate": iso(end), "discountSetting": {"discountType": "PERCENTAGE", "discountPercentage": pct}}]}


FREE = {
    "title": "Гра <Free> & Co", "id": "abc123", "description": "Короткий опис гри. " * 40,
    "keyImages": [{"type": "Thumbnail", "url": "https://cdn/thumb.jpg"}, {"type": "OfferImageWide", "url": "https://cdn/wide.jpg"}],
    "offerMappings": [{"pageSlug": "free-game/home", "pageType": "productHome"}],
    "price": {"totalPrice": {"discountPrice": 0, "originalPrice": 39900, "fmtPrice": {"originalPrice": "₴399.00", "discountPrice": "0"}}},
    "promotions": {"promotionalOffers": [offer(NOW - timedelta(days=1), NOW + timedelta(days=6, hours=3))], "upcomingPromotionalOffers": []},
}
UPCOMING = {
    "title": "Наступна гра", "id": "next1", "description": "Ще не роздається", "keyImages": [],
    "price": {"totalPrice": {"discountPrice": 39900, "originalPrice": 39900, "fmtPrice": {"originalPrice": "₴399.00"}}},
    "promotions": {"promotionalOffers": [], "upcomingPromotionalOffers": [offer(NOW + timedelta(days=6), NOW + timedelta(days=13))]},
}
DISCOUNT = dict(FREE, id="sale1", title="Знижка 50%",
                price={"totalPrice": {"discountPrice": 19900, "originalPrice": 39900}},
                promotions={"promotionalOffers": [offer(NOW - timedelta(days=1), NOW + timedelta(days=2), pct=50)]})
EXPIRED = dict(FREE, id="old1", title="Минула",
               promotions={"promotionalOffers": [offer(NOW - timedelta(days=9), NOW - timedelta(days=2))]})
NO_PROMO = {"title": "Завжди безкоштовна", "id": "f2p", "keyImages": [], "promotions": None}
DATA = {"data": {"Catalog": {"searchStore": {"elements": [FREE, UPCOMING, DISCOUNT, EXPIRED, NO_PROMO]}}}}


def test_only_games_free_right_now():
    games = epic.parse_free_games(DATA, "uk", NOW)
    assert [g.title for g in games] == ["Гра <Free> & Co"]            # ні майбутня, ні зі знижкою 50%, ні закінчена
    g = games[0]
    assert g.url == "https://store.epicgames.com/uk/p/free-game" and g.cover == "https://cdn/wide.jpg"
    assert g.price == "₴399.00" and g.end == NOW + timedelta(days=6, hours=3)
    assert g.key == "abc123:" + g.end.strftime("%Y%m%d%H%M")
    assert epic.parse_free_games({}, "uk", NOW) == [] and epic.parse_free_games({"data": None}, "uk", NOW) == []
    assert epic.parse_free_games({"data": {"Catalog": {"searchStore": {"elements": [{"id": "x", "promotions": {"promotionalOffers": [{"promotionalOffers": [{"startDate": "bad"}]}]}}]}}}}, "uk", NOW) == []


def test_new_period_gets_new_key():
    g1 = epic.parse_free_games(DATA, "uk", NOW)[0]
    again = dict(FREE, promotions={"promotionalOffers": [offer(NOW + timedelta(days=30), NOW + timedelta(days=37))]})
    g2 = epic.parse_free_games({"data": {"Catalog": {"searchStore": {"elements": [again]}}}}, "uk", NOW + timedelta(days=31))[0]
    assert g1.id == g2.id and g1.key != g2.key                        # повторна роздача тієї ж гри — це нова подія


def test_photos_prefer_screenshots_then_cover():
    g = epic.parse_free_games(DATA, "uk", NOW)[0]
    assert g.photos() == ["https://cdn/wide.jpg"]
    g.screenshots = [f"https://cdn/s{i}.jpg" for i in range(6)]
    assert g.photos() == [f"https://cdn/s{i}.jpg" for i in range(4)]
    g.cover = None; g.screenshots = []
    assert g.photos() == []


def test_parse_screenshots():
    content = {"pages": [{"data": {"carousel": {"items": [
        {"video": "https://v/1.mp4", "image": "https://cdn/a.jpg"}, {"image": "https://cdn/b.jpg"},
        {"image": "https://cdn/a.jpg"}, {"image": "not-a-url"}, {"image": "https://cdn/c.jpg"},
        {"image": "https://cdn/d.jpg"}, {"image": "https://cdn/e.jpg"}]}}}, {"data": {"about": {"image": "https://cdn/ignore.jpg"}}}]}
    assert epic.parse_screenshots(content) == ["https://cdn/a.jpg", "https://cdn/b.jpg", "https://cdn/c.jpg", "https://cdn/d.jpg"]
    assert epic.parse_screenshots({}) == [] and epic.parse_screenshots({"pages": [None, {"data": None}]}) == []


def test_caption_content_and_limit():
    g = epic.parse_free_games(DATA, "uk", NOW)[0]
    tz = ZoneInfo("Europe/Kyiv")
    text = epic.format_caption(g, tz, NOW)
    end_local = g.end.astimezone(tz).strftime("%d.%m.%Y о %H:%M")
    assert "🎮 <b>Гра &lt;Free&gt; &amp; Co</b>" in text                # HTML екранується
    assert end_local in text and "залишилось 6 дн 3 год" in text       # до якої дати і скільки лишилось
    assert "₴399.00 → <b>безкоштовно</b>" in text
    assert '<a href="https://store.epicgames.com/uk/p/free-game">' in text
    assert len(text) <= epic.CAPTION_LIMIT and "…" in text             # довгий опис обрізано, посилання й дата лишились
    short = epic.format_caption(epic.Game("i", "T", "", NOW, NOW + timedelta(minutes=30)), tz, NOW)
    assert "залишилось менше години" in short and "🔗" not in short and "💰" not in short


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("OK", name)
    print("Усі перевірки пройдені")
