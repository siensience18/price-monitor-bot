import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    bot_token: str
    check_interval_minutes: int
    db_path: str
    max_items_per_user: int


def load_config() -> Config:
    load_dotenv()
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("BOT_TOKEN не задан. Скопируйте .env.example в .env и укажите токен.")
    return Config(
        bot_token=token,
        check_interval_minutes=max(1, int(os.getenv("CHECK_INTERVAL_MINUTES", "30"))),
        db_path=os.getenv("DB_PATH", "data/bot.db"),
        max_items_per_user=max(1, int(os.getenv("MAX_ITEMS_PER_USER", "20"))),
    )
