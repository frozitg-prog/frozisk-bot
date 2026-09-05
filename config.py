import os

BOT_TOKEN = os.getenv("BOT_TOKEN", "8901838682:AAEPzBIJCtNP9aEgcQfAT4OpDYklqbYVKL4")
BOT_USERNAME = os.getenv("BOT_USERNAME", "FroziSkbot")

ADMIN_IDS = [
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "5077189331").split(",")
    if x.strip()
]

CURRENCY = os.getenv("CURRENCY", "G")

DEFAULT_REWARD_JOIN = 10
DEFAULT_REWARD_FORM = 50
DEFAULT_MIN_WITHDRAW = 100

DATABASE_PATH = os.getenv("DATABASE_PATH", "bot.db")

DATABASE_URL = os.getenv("DATABASE_URL", "")

_proxy = os.getenv("BOT_PROXY", "http://127.0.0.1:1443")
PROXY = _proxy if _proxy and _proxy.lower() != "none" else None