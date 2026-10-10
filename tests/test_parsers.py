import pytest

from bot.monitor import format_price, should_notify
from bot.parsers import (
    PriceNotFound,
    extract_from_html,
    parse_price_string,
    parse_wildberries_response,
    wildberries_article,
)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("1 299,90 ₽", 1299.90),
        ("1\xa0299 ₽", 1299.0),
        ("1,299.90", 1299.90),
        ("12.990", 12990.0),
        ("12 990 руб.", 12990.0),
        ("10.5", 10.5),
        ("1299,9", 1299.9),
        ("2.499.000", 2499000.0),
        ("$49.99", 49.99),
        (1500, 1500.0),
        ("1500", 1500.0),
        ("нет в наличии", None),
        ("0", None),
        (None, None),
    ],
)
def test_parse_price_string(raw, expected):
    assert parse_price_string(raw) == expected


def test_json_ld_product():
    html = """
    <html><head><title>Магазин</title>
    <script type="application/ld+json">
    {"@context": "https://schema.org", "@graph": [
        {"@type": "BreadcrumbList"},
        {"@type": "Product", "name": "Наушники X",
         "offers": [{"@type": "Offer", "price": "4990.00", "priceCurrency": "RUB"},
                    {"@type": "Offer", "price": "5490", "priceCurrency": "RUB"}]}
    ]}
    </script></head></html>
    """
    info = extract_from_html(html)
    assert info.price == 4990.0
    assert info.title == "Наушники X"
    assert info.currency == "RUB"


def test_json_ld_aggregate_offer():
    html = """<script type="application/ld+json">
    {"@type": "Product", "name": "Phone",
     "offers": {"@type": "AggregateOffer", "lowPrice": 19999, "highPrice": 25999, "priceCurrency": "RUB"}}
    </script>"""
    assert extract_from_html(html).price == 19999.0


def test_meta_tags():
    html = """<html><head>
    <meta property="og:title" content="Кофемашина">
    <meta property="product:price:amount" content="32990">
    <meta property="product:price:currency" content="RUB">
    </head></html>"""
    info = extract_from_html(html)
    assert (info.price, info.title, info.currency) == (32990.0, "Кофемашина", "RUB")


def test_microdata():
    html = """<html><head><title>Чайник</title></head><body>
    <div itemscope itemtype="https://schema.org/Product">
      <span itemprop="price" content="2 590">2 590 ₽</span>
      <meta itemprop="priceCurrency" content="RUB">
    </div></body></html>"""
    info = extract_from_html(html)
    assert info.price == 2590.0
    assert info.title == "Чайник"


def test_price_class_fallback_skips_old_price():
    html = """<html><body><h1>Рюкзак</h1>
    <span class="product-price__old">5 000 ₽</span>
    <del><span class="price">4 800 ₽</span></del>
    <div class="product-price__current">3 490 ₽</div>
    </body></html>"""
    info = extract_from_html(html)
    assert (info.price, info.title, info.currency) == (3490.0, "Рюкзак", "RUB")


def test_no_price():
    with pytest.raises(PriceNotFound):
        extract_from_html("<html><body>Привет</body></html>")


def test_wildberries():
    assert wildberries_article("https://www.wildberries.ru/catalog/123456/detail.aspx?size=1") == "123456"
    assert wildberries_article("https://example.com/catalog/123456/") is None

    data = {"products": [{"brand": "Brand", "name": "Куртка",
                          "sizes": [{"price": {"basic": 1000000, "product": 549900}},
                                    {"price": {"basic": 1000000, "product": 599900}}]}]}
    info = parse_wildberries_response(data)
    assert (info.price, info.title, info.currency) == (5499.0, "Brand Куртка", "RUB")
    assert parse_wildberries_response({"data": {"products": []}}) is None


def test_should_notify():
    assert should_notify(1200, 1000, None) == (False, None)
    assert should_notify(1000, 1000, None) == (True, 1000)
    assert should_notify(990, 1000, 1000) == (True, 990)
    assert should_notify(995, 1000, 990) == (False, 990)
    assert should_notify(1100, 1000, 990) == (False, None)


def test_format_price():
    assert format_price(12990, "RUB") == "12 990 ₽"
    assert format_price(49.99, "USD") == "49.99 $"
    assert format_price(None) == "—"
