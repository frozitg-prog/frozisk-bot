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
DEFAULT_SKIN = "AK-47 | Redline (Field-Tested)"

DEFAULT_ROULETTE_CHANCE = 30
DEFAULT_ROULETTE_MULT = 3
DEFAULT_ROULETTE_MIN = 20

DEFAULT_STREAK_BASE = 100
DEFAULT_STREAK_STEP = 30
MAX_STREAK_DAYS = 30

DATABASE_PATH = os.getenv("DATABASE_PATH", "bot.db")

DATABASE_URL = os.getenv("DATABASE_URL", "")

_proxy = os.getenv("BOT_PROXY", "http://127.0.0.1:1443")
PROXY = _proxy if _proxy and _proxy.lower() != "none" else None