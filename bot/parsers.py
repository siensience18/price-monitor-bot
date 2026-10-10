"""Получение цены товара по ссылке.

Порядок поиска цены на произвольной странице:
1. Разметка schema.org в JSON-LD (<script type="application/ld+json">) — есть у большинства магазинов.
2. Мета-теги product:price:amount / og:price:amount.
3. Микроразметка itemprop="price".
4. Эвристика по элементам с "price" в class/id.

Для Wildberries страница рендерится на клиенте, поэтому цена берётся из их публичного API карточек.
"""

import json
import re
from dataclasses import dataclass
from typing import Any, Iterator
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}

CURRENCY_SYMBOLS = {
    "₽": "RUB", "руб": "RUB", "р.": "RUB", "$": "USD", "€": "EUR", "£": "GBP", "₸": "KZT", "Br": "BYN",
}


class PriceNotFound(Exception):
    pass


@dataclass
class ProductInfo:
    price: float
    title: str | None = None
    currency: str | None = None


def parse_price_string(value: Any) -> float | None:
    """Превращает строку вида '1 299,90 ₽', '1,299.90', '12.990' или число в float."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None

    text = str(value).replace("\xa0", " ").replace("\u202f", " ").strip()
    match = re.search(r"\d[\d\s.,']*", text)
    if not match:
        return None
    num = re.sub(r"[\s']", "", match.group(0)).rstrip(".,")
    if not num:
        return None

    if "," in num and "." in num:
        if num.rfind(",") > num.rfind("."):
            num = num.replace(".", "").replace(",", ".")
        else:
            num = num.replace(",", "")
    elif "," in num or "." in num:
        sep = "," if "," in num else "."
        # 1.299 / 12,990,000 — разделители тысяч; 1299,90 / 10.5 — дробная часть
        if re.fullmatch(rf"\d{{1,3}}(\{sep}\d{{3}})+", num):
            num = num.replace(sep, "")
        else:
            head, _, tail = num.rpartition(sep)
            num = head.replace(sep, "") + "." + tail

    try:
        price = float(num)
    except ValueError:
        return None
    return price if price > 0 else None


def detect_currency(text: str | None) -> str | None:
    if not text:
        return None
    for symbol, code in CURRENCY_SYMBOLS.items():
        if symbol in text:
            return code
    return None


# ---------- JSON-LD ----------

def _walk_json(node: Any) -> Iterator[dict]:
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk_json(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_json(value)


def _is_product(node: dict) -> bool:
    types = node.get("@type")
    if isinstance(types, str):
        types = [types]
    return isinstance(types, list) and any(str(t).lower() in ("product", "productgroup") for t in types)


def _price_from_offers(offers: Any) -> tuple[float | None, str | None]:
    candidates: list[tuple[float, str | None]] = []
    for offer in _walk_json(offers):
        currency = offer.get("priceCurrency")
        for key in ("price", "lowPrice"):
            price = parse_price_string(offer.get(key))
            if price:
                candidates.append((price, currency))
        spec = offer.get("priceSpecification")
        if isinstance(spec, dict):
            price = parse_price_string(spec.get("price"))
            if price:
                candidates.append((price, spec.get("priceCurrency") or currency))
    if not candidates:
        return None, None
    return min(candidates, key=lambda c: c[0])


def extract_from_json_ld(soup: BeautifulSoup) -> ProductInfo | None:
    for script in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
        raw = script.string or script.get_text()
        if not raw:
            continue
        try:
            data = json.loads(raw.strip())
        except (json.JSONDecodeError, ValueError):
            continue
        for node in _walk_json(data):
            if not _is_product(node):
                continue
            price, currency = _price_from_offers(node.get("offers"))
            if price:
                name = node.get("name")
                return ProductInfo(price=price, title=str(name).strip() if name else None, currency=currency)
    return None


# ---------- Мета-теги и микроразметка ----------

def extract_from_meta(soup: BeautifulSoup) -> ProductInfo | None:
    for prop in ("product:price:amount", "og:price:amount", "price"):
        tag = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
        if tag and (price := parse_price_string(tag.get("content"))):
            cur_tag = soup.find("meta", attrs={"property": prop.replace("amount", "currency")})
            return ProductInfo(price=price, currency=cur_tag.get("content") if cur_tag else None)
    return None


def extract_from_microdata(soup: BeautifulSoup) -> ProductInfo | None:
    for tag in soup.find_all(attrs={"itemprop": "price"}):
        raw = tag.get("content") or tag.get_text(" ", strip=True)
        if price := parse_price_string(raw):
            cur_tag = soup.find(attrs={"itemprop": "priceCurrency"})
            currency = cur_tag.get("content") if cur_tag else detect_currency(tag.get_text())
            return ProductInfo(price=price, currency=currency)
    return None


OLD_PRICE_RE = re.compile(r"old|was|strike|cross|before|prev|regular|economy|discount-amount", re.I)
PRICE_CLASS_RE = re.compile(r"price", re.I)


def extract_from_price_classes(soup: BeautifulSoup) -> ProductInfo | None:
    """Последний шанс: элементы с 'price' в class/id, в тексте которых есть валюта."""
    for tag in soup.find_all(attrs={"class": PRICE_CLASS_RE}) + soup.find_all(attrs={"id": PRICE_CLASS_RE}):
        attrs = " ".join(tag.get("class", [])) + " " + (tag.get("id") or "")
        if OLD_PRICE_RE.search(attrs) or tag.find_parent(["del", "s", "strike"]):
            continue
        text = tag.get_text(" ", strip=True)
        currency = detect_currency(text)
        if not currency or len(text) > 40:
            continue
        if price := parse_price_string(text):
            return ProductInfo(price=price, currency=currency)
    return None


def extract_title(soup: BeautifulSoup) -> str | None:
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        return h1.get_text(strip=True)
    og = soup.find("meta", attrs={"property": "og:title"})
    if og and og.get("content"):
        return og["content"].strip()
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    return None


def extract_from_html(html: str) -> ProductInfo:
    soup = BeautifulSoup(html, "html.parser")
    for extractor in (extract_from_json_ld, extract_from_meta, extract_from_microdata, extract_from_price_classes):
        info = extractor(soup)
        if info:
            info.title = info.title or extract_title(soup)
            return info
    raise PriceNotFound("Не удалось найти цену на странице")


# ---------- Wildberries ----------

WB_API_URLS = (
    "https://card.wb.ru/cards/v4/detail?appType=1&curr=rub&dest=-1257786&nm={nm}",
    "https://card.wb.ru/cards/v2/detail?appType=1&curr=rub&dest=-1257786&nm={nm}",
)


def wildberries_article(url: str) -> str | None:
    host = urlparse(url).netloc.lower()
    if "wildberries." not in host and not host.endswith("wb.ru"):
        return None
    match = re.search(r"/catalog/(\d+)", url)
    return match.group(1) if match else None


def parse_wildberries_response(data: dict) -> ProductInfo | None:
    products = data.get("products") or (data.get("data") or {}).get("products") or []
    if not products:
        return None
    product = products[0]
    prices = []
    for size in product.get("sizes", []):
        price = (size.get("price") or {}).get("product")
        if price:
            prices.append(price / 100)
    if not prices and product.get("salePriceU"):
        prices.append(product["salePriceU"] / 100)
    if not prices:
        return None
    title = " ".join(filter(None, [product.get("brand"), product.get("name")])) or None
    return ProductInfo(price=min(prices), title=title, currency="RUB")


async def fetch_wildberries(client: httpx.AsyncClient, nm: str) -> ProductInfo:
    for api_url in WB_API_URLS:
        try:
            resp = await client.get(api_url.format(nm=nm))
            resp.raise_for_status()
            info = parse_wildberries_response(resp.json())
        except (httpx.HTTPError, ValueError):
            continue
        if info:
            return info
    raise PriceNotFound("Товар на Wildberries не найден или нет в наличии")


# ---------- Точка входа ----------

def create_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(headers=HEADERS, timeout=20, follow_redirects=True)


async def fetch_product(client: httpx.AsyncClient, url: str) -> ProductInfo:
    if nm := wildberries_article(url):
        return await fetch_wildberries(client, nm)

    try:
        resp = await client.get(url)
    except httpx.HTTPError as exc:
        raise PriceNotFound(f"Не удалось открыть страницу: {exc.__class__.__name__}") from exc
    if resp.status_code >= 400:
        raise PriceNotFound(f"Сайт ответил ошибкой {resp.status_code}")
    return extract_from_html(resp.text)
