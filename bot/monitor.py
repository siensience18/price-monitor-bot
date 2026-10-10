import asyncio
import html
import logging

import httpx
from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError

from .db import Database, Item
from .parsers import PriceNotFound, create_client, fetch_product

log = logging.getLogger(__name__)

MAX_CONCURRENT_CHECKS = 5
ERRORS_BEFORE_WARNING = 5


def format_price(price: float | None, currency: str | None = None) -> str:
    if price is None:
        return "—"
    text = f"{price:,.2f}".replace(",", " ").replace(".00", "")
    symbol = {"RUB": "₽", "USD": "$", "EUR": "€", "GBP": "£", "KZT": "₸", "BYN": "Br"}.get(currency or "RUB", currency)
    return f"{text} {symbol}"


def should_notify(price: float, threshold: float, notified_price: float | None) -> tuple[bool, float | None]:
    """Решает, писать ли пользователю, и возвращает новое значение notified_price.

    Уведомляем, когда цена опустилась до порога или ниже, и повторно — если она упала ещё сильнее.
    Когда цена снова поднимается выше порога, состояние сбрасывается, чтобы следующее падение тоже
    пришло уведомлением.
    """
    if price > threshold:
        return False, None
    if notified_price is None or price < notified_price:
        return True, price
    return False, notified_price


def drop_message(item: Item, price: float, currency: str | None, title: str | None) -> str:
    name = html.escape(title or item.title or "Товар")
    lines = [
        "📉 <b>Цена упала!</b>",
        f'<a href="{html.escape(item.url, quote=True)}">{name}</a>',
        "",
        f"Сейчас: <b>{format_price(price, currency)}</b>",
        f"Ваш порог: {format_price(item.threshold, currency)}",
    ]
    if item.last_price and item.last_price > price:
        lines.append(f"Было: {format_price(item.last_price, currency)}")
    return "\n".join(lines)


class PriceMonitor:
    def __init__(self, bot: Bot, db: Database, interval_minutes: int):
        self.bot = bot
        self.db = db
        self.interval = interval_minutes * 60
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT_CHECKS)

    async def check_item(self, client: httpx.AsyncClient, item: Item) -> None:
        async with self._semaphore:
            try:
                info = await fetch_product(client, item.url)
            except PriceNotFound as exc:
                errors = await self.db.register_error(item.id)
                log.warning("Товар #%s: %s", item.id, exc)
                if errors == ERRORS_BEFORE_WARNING:
                    await self._send(
                        item.user_id,
                        f"⚠️ Уже {errors} раз подряд не удаётся получить цену для товара #{item.id}:\n"
                        f"{html.escape(item.url)}\nПричина: {html.escape(str(exc))}",
                    )
                return
            except Exception:
                log.exception("Неожиданная ошибка при проверке товара #%s", item.id)
                await self.db.register_error(item.id)
                return

        notify, notified_price = should_notify(info.price, item.threshold, item.notified_price)
        if notify:
            await self._send(item.user_id, drop_message(item, info.price, info.currency or item.currency, info.title))
        await self.db.update_check_result(item.id, info.price, info.title, info.currency, notified_price)

    async def check_items(self, items: list[Item]) -> None:
        async with create_client() as client:
            await asyncio.gather(*(self.check_item(client, item) for item in items))

    async def run(self) -> None:
        log.info("Мониторинг запущен, интервал %s мин.", self.interval // 60)
        while True:
            await asyncio.sleep(self.interval)
            try:
                items = await self.db.all_items()
                log.info("Проверка %s товаров", len(items))
                await self.check_items(items)
            except Exception:
                log.exception("Ошибка в цикле мониторинга")

    async def _send(self, user_id: int, text: str) -> None:
        try:
            await self.bot.send_message(user_id, text, disable_web_page_preview=True)
        except TelegramForbiddenError:
            log.info("Пользователь %s заблокировал бота", user_id)
        except Exception:
            log.exception("Не удалось отправить сообщение пользователю %s", user_id)
