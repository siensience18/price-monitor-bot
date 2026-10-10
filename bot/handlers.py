import html
import re

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .config import Config
from .db import Database, Item
from .monitor import PriceMonitor, format_price, should_notify
from .parsers import PriceNotFound, ProductInfo, create_client, fetch_product, parse_price_string

router = Router()

URL_RE = re.compile(r"https?://[^\s<>\"]+", re.I)

HELP_TEXT = (
    "👋 Я слежу за ценами на товары и пишу, когда цена опускается до вашего порога.\n\n"
    "<b>Как добавить товар</b>\n"
    "Пришлите ссылку и желаемую цену одним сообщением:\n"
    "<code>https://example.com/product/123 1500</code>\n"
    "Или просто ссылку — я покажу текущую цену и спрошу порог.\n\n"
    "<b>Команды</b>\n"
    "/list — мои товары\n"
    "/check — проверить цены прямо сейчас\n"
    "/threshold <i>номер</i> <i>цена</i> — изменить порог\n"
    "/remove <i>номер</i> — перестать следить\n"
    "/help — эта справка\n\n"
    "Поддерживаются магазины с разметкой schema.org (большинство интернет-магазинов) и Wildberries."
)


class AddItem(StatesGroup):
    waiting_threshold = State()


def item_line(item: Item) -> str:
    title = html.escape((item.title or item.url)[:80])
    status = "✅" if item.last_price is not None and item.last_price <= item.threshold else "⏳"
    return (
        f"{status} <b>#{item.id}</b> <a href=\"{html.escape(item.url, quote=True)}\">{title}</a>\n"
        f"    сейчас {format_price(item.last_price, item.currency)}"
        f" · порог {format_price(item.threshold, item.currency)}"
    )


def items_keyboard(items: list[Item]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"🗑 Удалить #{item.id}", callback_data=f"del:{item.id}")]
            for item in items
        ]
    )


async def save_item(message: Message, db: Database, url: str, threshold: float, info: ProductInfo) -> None:
    user_id = message.from_user.id
    existing = await db.find_by_url(user_id, url)
    if existing:
        item_id = existing.id
        await db.update_threshold(item_id, threshold)
        header = f"🔄 Товар #{item_id} уже отслеживается — порог обновлён."
    else:
        item_id = await db.add_item(user_id, url, threshold, info.title, info.currency, info.price)
        header = f"✅ Товар #{item_id} добавлен в отслеживание."

    notify, notified_price = should_notify(info.price, threshold, None)
    await db.update_check_result(item_id, info.price, info.title, info.currency, notified_price)

    text = (
        f"{header}\n\n"
        f"{html.escape(info.title or url)}\n"
        f"Текущая цена: <b>{format_price(info.price, info.currency)}</b>\n"
        f"Порог: {format_price(threshold, info.currency)}"
    )
    if notify:
        text += "\n\n📉 Цена уже на уровне порога или ниже! Сообщу, если она упадёт ещё."
    else:
        text += "\n\nНапишу, как только цена опустится до порога."
    await message.answer(text, disable_web_page_preview=True)


@router.message(CommandStart())
@router.message(Command("help"))
async def cmd_help(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(HELP_TEXT, disable_web_page_preview=True)


@router.message(Command("list"))
async def cmd_list(message: Message, db: Database) -> None:
    items = await db.list_items(message.from_user.id)
    if not items:
        await message.answer("Пока ничего не отслеживается. Пришлите ссылку на товар и желаемую цену.")
        return
    text = "📋 <b>Ваши товары</b>\n\n" + "\n\n".join(item_line(i) for i in items)
    await message.answer(text, reply_markup=items_keyboard(items), disable_web_page_preview=True)


@router.message(Command("check"))
async def cmd_check(message: Message, db: Database, monitor: PriceMonitor) -> None:
    items = await db.list_items(message.from_user.id)
    if not items:
        await message.answer("Список пуст — проверять нечего.")
        return
    status = await message.answer(f"🔎 Проверяю {len(items)} товар(ов)…")
    await monitor.check_items(items)
    await status.delete()
    await cmd_list(message, db)


@router.message(Command("remove"))
async def cmd_remove(message: Message, command: CommandObject, db: Database) -> None:
    if not command.args or not command.args.strip().lstrip("#").isdigit():
        await message.answer("Укажите номер товара: <code>/remove 3</code>. Номера есть в /list.")
        return
    item_id = int(command.args.strip().lstrip("#"))
    if await db.delete_item(item_id, message.from_user.id):
        await message.answer(f"🗑 Товар #{item_id} больше не отслеживается.")
    else:
        await message.answer(f"Товар #{item_id} не найден.")


@router.message(Command("threshold"))
async def cmd_threshold(message: Message, command: CommandObject, db: Database) -> None:
    parts = (command.args or "").split(maxsplit=1)
    threshold = parse_price_string(parts[1]) if len(parts) == 2 else None
    if not parts or not parts[0].lstrip("#").isdigit() or threshold is None:
        await message.answer("Формат: <code>/threshold 3 1500</code> — номер товара и новый порог.")
        return
    item = await db.get_item(int(parts[0].lstrip("#")), message.from_user.id)
    if not item:
        await message.answer("Товар с таким номером не найден.")
        return
    await db.update_threshold(item.id, threshold)
    await message.answer(f"Порог для товара #{item.id} теперь {format_price(threshold, item.currency)}.")


@router.callback_query(F.data.startswith("del:"))
async def cb_delete(callback: CallbackQuery, db: Database) -> None:
    item_id = int(callback.data.split(":", 1)[1])
    deleted = await db.delete_item(item_id, callback.from_user.id)
    await callback.answer(f"Товар #{item_id} удалён" if deleted else "Товар уже удалён")
    items = await db.list_items(callback.from_user.id)
    if items:
        text = "📋 <b>Ваши товары</b>\n\n" + "\n\n".join(item_line(i) for i in items)
        await callback.message.edit_text(text, reply_markup=items_keyboard(items), disable_web_page_preview=True)
    else:
        await callback.message.edit_text("Список пуст. Пришлите ссылку на товар, чтобы начать отслеживание.")


@router.message(F.text.regexp(URL_RE))
async def on_link(message: Message, state: FSMContext, db: Database, config: Config) -> None:
    await state.clear()
    url = URL_RE.search(message.text).group(0).rstrip(".,;)")
    threshold = parse_price_string(URL_RE.sub(" ", message.text))

    existing = await db.find_by_url(message.from_user.id, url)
    if not existing and await db.count_items(message.from_user.id) >= config.max_items_per_user:
        await message.answer(
            f"Достигнут лимит в {config.max_items_per_user} товаров. Удалите что-нибудь через /list."
        )
        return

    status = await message.answer("🔎 Проверяю цену…")
    try:
        async with create_client() as client:
            info = await fetch_product(client, url)
    except PriceNotFound as exc:
        await status.edit_text(
            f"😕 Не получилось определить цену: {html.escape(str(exc))}.\n"
            "Возможно, сайт защищён от ботов или не использует стандартную разметку товара."
        )
        return
    await status.delete()

    if threshold is None:
        await state.set_state(AddItem.waiting_threshold)
        await state.update_data(url=url, price=info.price, title=info.title, currency=info.currency)
        await message.answer(
            f"{html.escape(info.title or url)}\n"
            f"Текущая цена: <b>{format_price(info.price, info.currency)}</b>\n\n"
            "Какую цену ждём? Пришлите число, например <code>1500</code>.",
            disable_web_page_preview=True,
        )
        return

    await save_item(message, db, url, threshold, info)


@router.message(AddItem.waiting_threshold, F.text)
async def on_threshold(message: Message, state: FSMContext, db: Database) -> None:
    threshold = parse_price_string(message.text)
    if threshold is None:
        await message.answer("Не понял число. Пришлите цену, например <code>1500</code>, или /help для отмены.")
        return
    data = await state.get_data()
    await state.clear()
    info = ProductInfo(price=data["price"], title=data.get("title"), currency=data.get("currency"))
    await save_item(message, db, data["url"], threshold, info)


@router.message()
async def fallback(message: Message) -> None:
    await message.answer("Пришлите ссылку на товар и желаемую цену, например:\n"
                         "<code>https://example.com/product/123 1500</code>\n\nСправка: /help")
