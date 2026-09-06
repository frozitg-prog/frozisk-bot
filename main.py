import asyncio
import logging
import os
import random
import re
import secrets
import time
from datetime import date, timedelta

from aiohttp import web
from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    MessageEntity)

import config
import database as db

logging.basicConfig(level=logging.INFO)

router = Router()
bot = None
last_bet = {}
promo_info = {}

MINES_GRIDS = {
    "3": 3,
    "4": 4,
    "5": 5,
    "6": 6,
}
MINES_MULT = 1.5
mines_games = {}

_moderation_cache = {}
_MOD_CACHE_TTL = 20

_active_fc = None
_FC_TTL = 600


async def _fc_cancel_task(task):
    if task is None:
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


async def _moderation_blocked_async(from_user, block_muted):
    if not from_user:
        return False
    if is_admin(from_user.id):
        return False
    uid = from_user.id
    now = time.time()
    cached = _moderation_cache.get(uid)
    if cached and now - cached[0] < _MOD_CACHE_TTL:
        banned, muted = cached[1], cached[2]
        return banned or (block_muted and muted)
    try:
        user = await asyncio.wait_for(
            asyncio.to_thread(db.get_user, uid), timeout=5
        )
    except Exception:
        return False
    if not user:
        return False
    banned = bool(user.get("banned"))
    muted = bool(user.get("muted"))
    _moderation_cache[uid] = (now, banned, muted)
    if banned or (block_muted and muted):
        return True
    return False


@router.message.outer_middleware()
async def moderation_msg_middleware(handler, message, data):
    if await _moderation_blocked_async(message.from_user, block_muted=True):
        return
    return await handler(message, data)


@router.callback_query.outer_middleware()
async def moderation_cb_middleware(handler, event, data):
    if await _moderation_blocked_async(event.from_user, block_muted=False):
        await event.answer()
        return
    return await handler(event, data)


class Withdraw(StatesGroup):
    price = State()
    screenshot = State()


class Promo(StatesGroup):
    code = State()


class CreatePromo(StatesGroup):
    code = State()
    amount = State()
    uses = State()


class AdminBalance(StatesGroup):
    amount = State()


class AdminPanel(StatesGroup):
    target = State()
    amount = State()


class Post(StatesGroup):
    text = State()


class Roulette(StatesGroup):
    bet = State()


class Mines(StatesGroup):
    bet = State()
    grid = State()


class Slots(StatesGroup):
    bet = State()


def main_menu():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👛 Мой баланс", callback_data="balance"),
                InlineKeyboardButton(text="💸 Вывести", callback_data="start_withdraw"),
                InlineKeyboardButton(text="🎰 Рулетка", callback_data="roulette"),
            ],
            [
                InlineKeyboardButton(text="🎁 Промокод", callback_data="promo_menu"),
                InlineKeyboardButton(text="💰 Заработать голду", callback_data="earn_gold"),
            ],
            [
                InlineKeyboardButton(text="🔥 Стрик", callback_data="streak"),
                InlineKeyboardButton(text="🏆 Топ", callback_data="top"),
            ],
        ]
    )


def is_admin(user_id):
    return user_id in config.ADMIN_IDS


_WORD_PRANKS = [
    (1_000_000, ["миллион", "миллиона", "миллионов"]),
    (1_000_000_000, ["миллиард", "миллиарда", "миллиардов"]),
    (1_000_000_000_000, ["триллион", "триллиона", "триллионов"]),
    (1_000_000_000_000_000, ["квадриллион", "квадриллиона", "квадриллионов"]),
    (1_000_000_000_000_000_000, ["квинтиллион", "квинтиллиона", "квинтиллионов"]),
    (1e21, ["секстиллион", "секстиллиона", "секстиллионов"]),
    (1e24, ["септиллион", "септиллиона", "септиллионов"]),
    (1e27, ["октиллион", "октиллиона", "октиллионов"]),
    (1e30, ["нониллион", "нониллиона", "нониллионов"]),
    (1e33, ["дециллион", "дециллиона", "дециллионов"]),
    (1e36, ["ундециллион", "ундециллиона", "ундециллионов"]),
    (1e39, ["дуодециллион", "дуодециллиона", "дуодециллионов"]),
    (1e42, ["тредециллион", "тредециллиона", "тредециллионов"]),
    (1e45, ["кваттуордециллион", "кваттуордециллиона", "кваттуордециллионов"]),
    (1e48, ["квиндециллион", "квиндециллиона", "квиндециллионов"]),
    (1e51, ["сексдециллион", "сексдециллиона", "сексдециллионов"]),
    (1e54, ["септендециллион", "септендециллиона", "септендециллионов"]),
    (1e57, ["октодециллион", "октодециллиона", "октодециллионов"]),
    (1e60, ["новемдециллион", "новемдециллиона", "новемдециллионов"]),
    (1e63, ["вигинтиллион", "вигинтиллиона", "вигинтиллионов"]),
]

_WORD_THOUSAND = ["тысяча", "тысячи", "тысяч"]


def fmt_num(v):
    f = abs(float(v))
    neg = "-" if float(v) < 0 else ""
    for divisor, forms in reversed(_WORD_PRANKS):
        if f >= divisor:
            return neg + _word_amount(f / divisor, forms)
    if f >= 1_000:
        return neg + _word_amount(f / 1_000, _WORD_THOUSAND)
    if float(v).is_integer():
        return str(int(v))
    return f"{float(v):.8f}".rstrip("0").rstrip(".")


fmt_short = fmt_num


def _trim_dec(d):
    if d == int(d):
        return str(int(d))
    return f"{d:.2f}".rstrip("0").rstrip(".")


def _word_amount(d, forms):
    rd = round(d, 2)
    s = _trim_dec(rd)
    if rd != int(rd):
        word = forms[1]
    else:
        n = int(rd)
        mod = n % 10
        if mod == 1 and n % 100 != 11:
            word = forms[0]
        elif 2 <= mod <= 4 and n % 100 not in (12, 13, 14):
            word = forms[1]
        else:
            word = forms[2]
    return f"{s} {word}"


def _withdraw_enabled():
    return db.get_setting("withdraw_enabled", "1") not in (0, "0", "false", "off", "no")


def admin_main_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📊 Статистика", callback_data="adm_stats"),
                InlineKeyboardButton(text="💸 Выводы", callback_data="adm_wd"),
            ],
            [
                InlineKeyboardButton(text="👤 Пользователь", callback_data="adm_user"),
                InlineKeyboardButton(text="💼 Баланс", callback_data="adm_bal"),
            ],
            [
                InlineKeyboardButton(text="🎁 Промокоды", callback_data="adm_codes"),
                InlineKeyboardButton(text="📢 Задания", callback_data="adm_tasks"),
            ],
            [InlineKeyboardButton(text="⚙️ Настройки", callback_data="adm_settings")],
            [InlineKeyboardButton(text="🎴 Казино-слот", callback_data="adm_casino")],
        ]
    )


def admin_sub_kb(buttons, back="adm_main"):
    kb = list(buttons)
    kb.append([InlineKeyboardButton(text="↩️ Назад", callback_data=back)])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def _admin_add_notify(user_id, amount):
    cur = db.get_setting("currency", config.CURRENCY)
    try:
        return (
            f"💎 Администрация перевела вам "
            f"<b>+{fmt_num(amount)} {cur}</b>!\n"
            f"Ваш баланс: <b>{fmt_num(db.get_user(user_id)['balance'])} {cur}</b>."
        )
    except Exception:
        return None


async def _send_admin_add(user_id, amount):
    text = _admin_add_notify(user_id, amount)
    if text:
        try:
            await bot.send_message(user_id, text)
        except Exception:
            pass


def withdrawal_actions_kb(wd_id):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Выплатить", callback_data=f"wd_approve:{wd_id}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"wd_reject:{wd_id}"),
            ]
        ]
    )


def format_wd(wd):
    user = db.get_user(wd["user_id"])
    name = user["first_name"] if user else "?"
    username = f"@{user['username']}" if user and user["username"] else f"ID {wd['user_id']}"
    cur = db.get_setting("currency", config.CURRENCY)
    return (
        f"Заявка на вывод №{wd['id']}\n"
        f"Цена: {fmt_num(wd['amount'])} {cur}\n"
        f"Скин / паттерн: {wd.get('skin') or '—'}\n"
        f"Пользователь: {name} ({username})\n"
        f"Дата: {wd['created_at']}"
    )


@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject):
    user = message.from_user
    ref_id = None
    if command.args and command.args.isdigit():
        ref_id = int(command.args)

    is_new = db.add_user(user.id, user.username, user.first_name or "", ref_id)

    if is_new and ref_id and ref_id != user.id:
        referrer = db.get_user(ref_id)
        if referrer:
            reward = db.get_setting("reward_join", config.DEFAULT_REWARD_JOIN)
            db.add_balance(ref_id, reward)
            await bot.send_message(
                ref_id,
                f"🎉 По вашей реферальной ссылке пришёл новый пользователь!\n"
                f"Начислено: +{fmt_num(reward)} {db.get_setting('currency', config.CURRENCY)}")

    bonus = ""
    if is_new and ref_id:
        invite_bonus = db.get_setting("invite_bonus", 777)
        if invite_bonus and invite_bonus > 0:
            db.add_balance(user.id, invite_bonus)
            bonus = (
                f"\n🎁 Бонус за переход по реферальной ссылке: "
                f"+{fmt_num(invite_bonus)} {db.get_setting('currency', config.CURRENCY)} зачислены на баланс!"
            )

    await message.answer(
        f"Привет, {user.first_name}!\n"
        "Зарабатывайте голду за приглашённых, выполняйте задания и выводите её."
        f"{bonus}",
        reply_markup=main_menu())


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    if not is_admin(message.from_user.id):
        return
    s = db.stats()
    cur = db.get_setting("currency", config.CURRENCY)
    await message.answer(
        f"📊 Статистика\n"
        f"Пользователей: {s['users']}\n"
        f"Рефералов приведено: {s['total_referrals']}\n"
        f"Общий баланс: {fmt_num(s['total_balance'])} {cur}\n"
        f"Выводов в ожидании: {s['pending_wds']}\n\n"
        f"Награда за вступление: {fmt_num(db.get_setting('reward_join', config.DEFAULT_REWARD_JOIN))} "
        f"{db.get_setting('currency', config.CURRENCY)}\n"
        f"Мин. вывод: {fmt_num(db.get_setting('min_withdraw', config.DEFAULT_MIN_WITHDRAW))} "
        f"{db.get_setting('currency', config.CURRENCY)}"
    )


@router.message(Command("post"))
async def cmd_post(message: Message, command: CommandObject, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    text = command.args
    if text:
        await state.clear()
        await broadcast_to_all(text)
        await message.answer(f"📣 Сообщение отправлено всем пользователям.")
        return
    await state.set_state(Post.text)
    await message.answer("Введите текст для рассылки (или отправьте одним сообщением):")


@router.message(Post.text, F.text)
async def post_text(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    await broadcast_to_all(message.text)
    await message.answer("📣 Сообщение отправлено всем пользователям.")


@router.message(Command("set_join"))
async def cmd_set_join(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    if not command.args or not command.args.isdigit():
        await message.answer("Использование: /set_join <сумма>")
        return
    db.set_setting("reward_join", int(command.args))
    await message.answer("Награда за вступление обновлена.")


@router.message(Command("add_code"))
async def cmd_add_code(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    args = command.args.split() if command.args else []
    if len(args) < 2 or not args[1].isdigit():
        await message.answer(
            "Использование: /add_code <КОД> <количество голды> [кол-во активаций]"
        )
        return
    code = args[0].upper()
    amount = int(args[1])
    max_uses = int(args[2]) if len(args) > 2 and args[2].isdigit() else 1
    db.add_code(code, amount, max_uses=max_uses, owner_id=message.from_user.id)
    cur = db.get_setting("currency", config.CURRENCY)
    uses = "без лимита" if max_uses <= 0 else f"{max_uses} активаций"
    await message.answer(f"Промокод {code} создан на {fmt_num(amount)} {cur}. Активаций: {uses}.")


@router.message(Command("codes"))
async def cmd_codes(message: Message):
    if not is_admin(message.from_user.id):
        return
    rows = db.list_codes(limit=20)
    if not rows:
        await message.answer("Промокодов нет. Создайте: /add_code <КОД> <голда>")
        return
    cur = db.get_setting("currency", config.CURRENCY)
    lines = []
    for r in rows:
        total = r.get("max_uses") if r.get("max_uses") is not None else 1
        left = "∞" if total == 0 else max(total - r["used"], 0)
        state = "✅ использован" if total != 0 and r["used"] >= total else f"🆕 {r['used']}/{total}"
        lines.append(f"{r['code']} — {fmt_num(r['amount'])} {cur} — {state}")
    await message.answer("🎁 Промокоды:\n" + "\n".join(lines))


_FC_LINK_ERROR = (
    "Бот не может найти чат комментариев этого канала. "
    "Включите комментарии у канала и добавьте бота админом туда."
)


async def _parse_fc_amount(raw):
    raw = (raw or "").strip().replace(",", ".")
    try:
        amount = float(raw)
    except (TypeError, ValueError):
        return None
    if amount <= 0:
        return None
    return amount


async def _fc_resolve_channel(value):
    value = (value or "").strip()
    if not value:
        return None
    try:
        if value.lstrip("-").isdigit():
            chat = await bot.get_chat(int(value))
        else:
            chat = await bot.get_chat(value)
    except Exception:
        return None
    if chat.type != "channel":
        return None
    return chat.id


async def _fc_post(path, amount):
    global _active_fc
    if _active_fc:
        old = _active_fc
        await _fc_cancel_task(old.get("task"))
        _active_fc = None
        try:
            await bot.send_message(
                old["group_id"],
                "⏹️ Раздача отменена — запущена новая.",
                message_thread_id=old["post_id"])
        except Exception:
            pass

    cur = db.get_setting("currency", config.CURRENCY)
    announced = await bot.send_message(
        path,
        f"🤑 ФАСТ-КОММЕНТ\n\n"
        f"Напишите первым комментарий в комментариях "
        f"и получите <b>{fmt_num(amount)} {cur}</b>\n\n"
        f"⏳ Кто успеет первым — того и награда!")
    post_id = announced.message_id
    group_id = db.get_setting("fc_group_id")
    if not group_id:
        try:
            chat = await bot.get_chat(path)
            group_id = chat.linked_chat_id
        except Exception:
            group_id = None
    if not group_id:
        try:
            await bot.edit_message_text(
                path, post_id, f"⚠️ {_FC_LINK_ERROR}"
            )
        except Exception:
            pass
        return _FC_LINK_ERROR

    try:
        await bot.send_message(
            group_id,
            f"🏁 <b>ФАСТ-КОММЕНТ стартовал!</b>\n\n"
            f"Напишите первым комментарий здесь и получите "
            f"<b>{fmt_num(amount)} {cur}</b>.")
    except Exception:
        try:
            await bot.edit_message_text(
                path,
                post_id,
                "⚠️ Бот не может писать в чат комментариев этого канала. "
                "Добавьте бота админом в чат комментариев.")
        except Exception:
            pass
        return (
            "Бот не может писать в чат комментариев этого канала. "
            "Добавьте бота админом в чат комментариев."
        )

    logging.info(
        "FC start: chat=%s post=%s amount=%s",
        group_id, post_id, amount)

    _active_fc = {
        "channel_id": path,
        "group_id": group_id,
        "post_id": post_id,
        "thread_id": post_id,
        "amount": amount,
        "started": time.time(),
    }

    async def _expire():
        await asyncio.sleep(_FC_TTL)
        global _active_fc
        fc = _active_fc
        if fc and fc is not None and fc.get("post_id") == post_id and fc.get("amount") == amount:
            _active_fc = None
            try:
                await bot.send_message(
                    group_id,
                    "⏰ Время вышло — никто не успел написать первым. Повторим?",
                    message_thread_id=post_id)
            except Exception:
                pass

    _active_fc["task"] = asyncio.create_task(_expire())
    return ""


@router.message(Command("fc"))
async def cmd_fc(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    parts = (command.args or "").split()
    amount = await _parse_fc_amount(parts[0] if parts else None)
    if amount is None:
        await message.answer(
            "Использование: /fc <сумма голды> [юзер]\n"
            "Без юзера — за счёт бота. С юзером — спишется с его баланса."
        )
        return

    payer = None
    if len(parts) > 1:
        payer = resolve_user(parts[1])
        if not payer:
            await message.answer("Пользователь не найден.")
            return
        payer_user = db.get_user(payer["id"])
        if not payer_user or payer_user["balance"] < amount:
            await message.answer(
                f"На балансе пользователя {payer['first_name']} недостаточно голды."
            )
            return

    if message.chat.type == "channel":
        channel = message.chat.id
        db.set_setting("fc_channel_id", channel)
    else:
        channel = db.get_setting("fc_channel_id")
        if not channel:
            await message.answer(
                "Канал не настроен. Укажите его: /fc_set <id или @канал> "
                "(либо запустите /fc прямо в канале)"
            )
            return
        channel = int(channel)

    err = await _fc_post(channel, amount)
    if not err and payer:
        db.add_balance(payer["id"], -amount)
        cur = db.get_setting("currency", config.CURRENCY)
        await message.answer(
            f"💸 Снято {fmt_num(amount)} {cur} с баланса {payer['first_name']} "
            f"(@{payer['username'] or payer['id']})."
        )

    if not err:
        cur = db.get_setting("currency", config.CURRENCY)
        src = (
            f"за счёт {payer['first_name']}"
            if payer else "за счёт бота"
        )
        await message.answer(
            f"🚀 Фаст-коммент запущен ({src})! "
            f"Первый комментарий в чате получит {fmt_num(amount)} {cur}."
        )
    else:
        await message.answer(f"⚠️ {err}")


@router.message(Command("fc_set"))
async def cmd_fc_set(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    channel = await _fc_resolve_channel(command.args)
    if channel is None:
        await message.answer(
            "Не удалось найти канал. Укажите @ник или id канала, "
            "в котором бот является админом."
        )
        return
    db.set_setting("fc_channel_id", channel)
    await message.answer("Канал для фаст-комментов сохранён.")


@router.message(Command("fc_chat"))
async def cmd_fc_chat(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    args = command.args.split() if command.args else []
    if len(args) < 1 or not args[0].lstrip("-").isdigit():
        await message.answer(
            "Использование: /fc_chat <номер чата комментариев>\n"
            "Например: /fc_chat -1004463161383"
        )
        return
    group_id = int(args[0])
    db.set_setting("fc_group_id", group_id)
    await message.answer("Чат комментариев для фаст-комментов сохранён.")


@router.message(Command("fc_cancel"))
async def cmd_fc_cancel(message: Message):
    if not is_admin(message.from_user.id):
        return
    global _active_fc
    if not _active_fc:
        await message.answer("Активной раздачи нет.")
        return
    fc = _active_fc
    _active_fc = None
    await _fc_cancel_task(fc.get("task"))
    try:
        await bot.send_message(
            fc["group_id"],
            "⏹️ Раздача отменена.",
            message_thread_id=fc["post_id"])
    except Exception:
        pass
    await message.answer("Фаст-коммент отменён.")


@router.message(Command("set_currency"))
async def cmd_set_currency(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    if not command.args:
        await message.answer("Использование: /set_currency <название>")
        return
    db.set_setting("currency", command.args.strip())
    await message.answer(f"Валюта установлена: {command.args.strip()}")


@router.message(Command("set_skin"))
async def cmd_set_skin(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    if not command.args:
        await message.answer("Использование: /set_skin <текст скина>")
        return
    db.set_setting("withdraw_skin", command.args.strip())
    await message.answer(f"Скин установлен: {command.args.strip()}")


@router.message(Command("packinfo"))
async def cmd_packinfo(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    name = (command.args or "").strip()
    if not name:
        await message.answer("Использование: /packinfo <имя пака>\nНапример: /packinfo TgAndroidIcons")
        return
    try:
        st = await bot.get_sticker_set(name)
    except Exception as e:
        await message.answer(f"Не удалось получить пак: {e}")
        return
    lines = [f"Пак: {st.name}  ·  {st.title}", f"Стикеров: {len(st.stickers)}", ""]
    for i, s in enumerate(st.stickers, 1):
        ceid = getattr(s, "custom_emoji_id", None) or "—"
        lines.append(f"{i}. {s.emoji} → {ceid}")
    text = "\n".join(lines)
    for chunk in [text[i:i + 3800] for i in range(0, len(text), 3800)]:
        await message.answer(chunk)


@router.message(Command("admin", "a"))
async def cmd_admin(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    await message.answer(
        "🛠 Админ-панель. Выберите действие:", reply_markup=admin_main_kb()
    )


@router.callback_query(F.data == "adm_main")
async def cq_adm_main(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return
    await state.clear()
    await cb.answer()
    await cb.message.edit_text(
        "🛠 Админ-панель. Выберите действие:", reply_markup=admin_main_kb()
    )


@router.callback_query(F.data == "adm_stats")
async def cq_adm_stats(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    await cb.answer()
    s = db.stats()
    cur = db.get_setting("currency", config.CURRENCY)
    await cb.message.edit_text(
        f"📊 Статистика\n"
        f"Пользователей: {s['users']}\n"
        f"Рефералов приведено: {s['total_referrals']}\n"
        f"Общий баланс: {fmt_num(s['total_balance'])} {cur}\n"
        f"Выводов в ожидании: {s['pending_wds']}\n\n"
        f"Награда за вступление: {fmt_num(db.get_setting('reward_join', config.DEFAULT_REWARD_JOIN))} {cur}\n"
        f"Мин. вывод: {fmt_num(db.get_setting('min_withdraw', config.DEFAULT_MIN_WITHDRAW))} {cur}\n"
        f"Валюта: {cur}\n"
        f"Скин для вывода: {db.get_setting('withdraw_skin', config.DEFAULT_SKIN)}",
        reply_markup=admin_sub_kb([], "adm_main"))


@router.callback_query(F.data == "adm_wd")
async def cq_adm_wd(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    await cb.answer()
    enabled = _withdraw_enabled()
    toggle_text = (
        "🔒 Выводы включены — ВЫКЛЮЧИТЬ"
        if enabled
        else "🔓 Выводы отключены — ВКЛЮЧИТЬ"
    )
    await cb.message.edit_text(
        "💸 Выводы:",
        reply_markup=admin_sub_kb(
            [
                [
                    InlineKeyboardButton(text="📋 Все", callback_data="adm_wd_all"),
                    InlineKeyboardButton(text="⏳ Ожидают", callback_data="adm_wd_pending"),
                ],
                [InlineKeyboardButton(text=toggle_text, callback_data="adm_wd_toggle")],
            ],
            "adm_main"))


@router.callback_query(F.data == "adm_wd_toggle")
async def cq_adm_wd_toggle(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    current = _withdraw_enabled()
    db.set_setting("withdraw_enabled", "0" if current else "1")
    await cq_adm_wd(cb)


@router.callback_query(F.data.startswith("adm_wd_"))
async def cq_adm_wd_list(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    status = {"adm_wd_all": None, "adm_wd_pending": "pending"}.get(cb.data)
    rows = db.list_withdrawals(status=status, limit=15)
    if not rows:
        await cb.answer("Выводов нет", show_alert=True)
        return
    cur = db.get_setting("currency", config.CURRENCY)
    label = {"pending": "⏳", "paid": "✅", "rejected": "❌"}
    lines = []
    kb = []
    for r in rows:
        user = db.get_user(r["user_id"])
        uname = f"@{user['username']}" if user and user["username"] else f"ID {r['user_id']}"
        lines.append(
            f"#{r['id']} {label.get(r['status'], '')} · {fmt_num(r['amount'])} {cur} · "
            f"{uname} · {r['created_at'][:10]}"
        )
        kb.append(
            [InlineKeyboardButton(text=f"👀 №{r['id']}", callback_data=f"adm_wdshow:{r['id']}")]
        )
    await cb.answer()
    await cb.message.edit_text(
        "💸 Выводы:\n\n" + "\n".join(lines),
        reply_markup=admin_sub_kb(kb, "adm_wd"))


@router.callback_query(F.data.startswith("adm_wdshow:"))
async def cq_adm_wdshow(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    wd_id = int(cb.data.split(":")[1])
    wd = db.get_withdrawal(wd_id)
    if not wd:
        await cb.answer("Вывод не найден", show_alert=True)
        return
    await cb.answer()
    kb = withdrawal_actions_kb(wd_id)
    if wd.get("screenshot"):
        await bot.send_photo(
            cb.from_user.id,
            photo=wd["screenshot"],
            caption=format_wd(wd),
            reply_markup=kb)
    else:
        await cb.message.answer(format_wd(wd), reply_markup=kb)


@router.callback_query(F.data == "adm_bal")
async def cq_adm_bal(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    await cb.answer()
    await cb.message.edit_text(
        "💼 Баланс:",
        reply_markup=admin_sub_kb(
            [
                [
                    InlineKeyboardButton(text="➕ Начислить", callback_data="adm_bal_add"),
                    InlineKeyboardButton(text="➖ Списать", callback_data="adm_bal_sub"),
                ],
                [InlineKeyboardButton(text="🔄 Обнулить", callback_data="adm_bal_zero")],
                [InlineKeyboardButton(text="🌐 Для всех", callback_data="adm_bal_all")],
            ],
            "adm_main"))


@router.callback_query(F.data == "adm_bal_all")
async def cq_adm_bal_all(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    await cb.answer()
    await cb.message.edit_text(
        "🌐 Глобальные действия. Балансы админов не затрагиваются:",
        reply_markup=admin_sub_kb(
            [
                [
                    InlineKeyboardButton(text="➕ Всем прибавить", callback_data="adm_bal_all_add"),
                    InlineKeyboardButton(text="➖ Всем убрать", callback_data="adm_bal_all_sub"),
                ],
                [InlineKeyboardButton(text="💥 Обнулить ВСЕМ", callback_data="adm_bal_all_zero")],
            ],
            "adm_bal"))


@router.callback_query(F.data.startswith("adm_bal_"))
async def cq_adm_bal_act(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return
    action = {
        "adm_bal_add": "bal_add",
        "adm_bal_sub": "bal_sub",
        "adm_bal_zero": "bal_zero",
        "adm_bal_all_add": "all_add",
        "adm_bal_all_sub": "all_sub",
    }.get(cb.data)
    if not action:
        return
    if action in ("all_add", "all_sub"):
        await state.set_state(AdminPanel.amount)
        await state.update_data(ap_action=action)
        await cb.answer()
        await cb.message.edit_text(
            "Введите сумму (прибавить/убрать у всех):"
        )
        return
    await state.set_state(AdminPanel.target)
    await state.update_data(ap_action=action)
    await cb.answer()
    await cb.message.edit_text("Введите ID или @юзернейм пользователя:")


@router.callback_query(F.data == "adm_bal_all_zero")
async def cq_adm_bal_all_zero(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    await cb.answer()
    await cb.message.edit_text(
        "⚠️ Обнулить баланс ВСЕМ пользователям (кроме админов)?",
        reply_markup=admin_sub_kb(
            [
                [
                    InlineKeyboardButton(
                        text="✅ Да, обнулить всем",
                        callback_data="adm_bal_all_zero_go"),
                ],
            ],
            "adm_bal_all"))


@router.callback_query(F.data == "adm_bal_all_zero_go")
async def cq_adm_bal_all_zero_go(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    n = db.reset_all_balances(config.ADMIN_IDS)
    await cb.answer()
    await cb.message.edit_text(
        f"💥 Обнулён баланс {n} пользователям.\n"
        "🗑 Ожидающие заявки на вывод и все промокоды удалены.",
        reply_markup=admin_sub_kb([], "adm_bal"))


@router.callback_query(F.data == "adm_user")
async def cq_adm_user(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return
    await state.set_state(AdminPanel.target)
    await state.update_data(ap_action="user")
    await cb.answer()
    await cb.message.edit_text("Введите ID или @юзернейм пользователя:")


@router.callback_query(F.data == "adm_codes")
async def cq_adm_codes(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    await cb.answer()
    await cb.message.edit_text(
        "🎁 Промокоды:",
        reply_markup=admin_sub_kb(
            [
                [
                    InlineKeyboardButton(text="📋 Список", callback_data="adm_codes_list"),
                    InlineKeyboardButton(text="➕ Создать", callback_data="adm_codes_add"),
                ],
                [
                    InlineKeyboardButton(text="🎲 Рандомный промокод", callback_data="adm_codes_rand"),
                    InlineKeyboardButton(text="🗑 Удалить", callback_data="adm_codes_del"),
                ],
            ],
            "adm_main"))


def gen_promo_code(length=8):
    chars = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(chars) for _ in range(length))


def parse_range(text):
    import re

    m = re.search(r"(\d+(?:[.,]\d+)?)\s*[-—-]\s*(\d+(?:[.,]\d+)?)", text)
    if not m:
        return None
    lo = float(m.group(1).replace(",", "."))
    hi = float(m.group(2).replace(",", "."))
    if lo > hi:
        lo, hi = hi, lo
    return lo, hi


def promo_code_text(code, amount, uses, cur, author=None):
    uses_s = "без лимита" if uses == 0 else f"{uses} активации"
    text = (
        f"🎁 НОВЫЙ ПРОМОКОД!\n"
        f"Награда: {fmt_num(amount)} {cur}\n"
        f"Активаций: {uses_s}\n"
        f"Код: <code>{code}</code>"
    )
    if author:
        text += f"\n👤 Автор: {author}"
    return text


async def broadcast_to_all(text):
    users = db.get_all_user_ids()
    sent = 0
    for uid in users:
        try:
            await bot.send_message(uid, text)
            sent += 1
        except Exception:
            pass
    return sent


def promo_author_name(owner_id):
    if is_admin(owner_id):
        return "Администрация"
    user = db.get_user(owner_id)
    if not user:
        return f"ID {owner_id}"
    if user.get("username"):
        return f"@{user['username']}"
    return user.get("first_name") or f"ID {owner_id}"


@router.callback_query(F.data == "adm_codes_rand")
async def cq_adm_codes_rand(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return
    await state.set_state(AdminPanel.amount)
    await state.update_data(ap_action="rand_amount")
    await cb.answer()
    await cb.message.edit_text(
        "🎲 Рандомный промокод.\n"
        "Введите диапазон голды, например: <code>200-1000</code>"
    )


@router.callback_query(F.data == "adm_codes_del")
async def cq_adm_codes_del(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return
    await state.set_state(AdminPanel.target)
    await state.update_data(ap_action="code_del")
    await cb.answer()
    await cb.message.edit_text(
        "Введите код промокода для удаления (или его часть из списка):"
    )


@router.callback_query(F.data == "adm_codes_list")
async def cq_adm_codes_list(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    rows = db.list_codes(limit=20)
    if not rows:
        await cb.answer("Промокодов нет", show_alert=True)
        return
    cur = db.get_setting("currency", config.CURRENCY)
    lines = []
    for r in rows:
        total = r.get("max_uses") if r.get("max_uses") is not None else 1
        if total == 0:
            state = f"🆕 {r['used']}/∞"
        elif r["used"] >= total:
            state = f"✅ {r['used']}/{total} использован"
        else:
            state = f"🆕 {r['used']}/{total}"
        lines.append(f"{r['code']} — {fmt_num(r['amount'])} {cur} — {state}")
    await cb.answer()
    await cb.message.edit_text(
        "🎁 Промокоды:\n" + "\n".join(lines),
        reply_markup=admin_sub_kb([], "adm_codes"))


@router.callback_query(F.data == "adm_codes_add")
async def cq_adm_codes_add(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return
    await state.set_state(AdminPanel.target)
    await state.update_data(ap_action="code")
    await cb.answer()
    await cb.message.edit_text("Введите промокод (латиницей):")


@router.callback_query(F.data.startswith("adm_code_uses:"))
async def cq_adm_code_uses(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return
    max_uses = int(cb.data.split(":")[1])
    data = await state.get_data()
    await state.clear()
    cur = db.get_setting("currency", config.CURRENCY)
    db.add_code(data["ap_code"], data["ap_code_amount"], max_uses=max_uses, owner_id=cb.from_user.id)
    uses = "без лимита (∞)" if max_uses <= 0 else f"{max_uses} активаций"
    await cb.answer()
    await cb.message.edit_text(
        f"🎁 Промокод {data['ap_code']} создан на {fmt_num(data['ap_code_amount'])} {cur}.\n"
        f"Активаций: {uses}.",
        reply_markup=admin_sub_kb([], "adm_codes"))


def promo_result_kb(code):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📤 Отправить промокод юзерам",
                    callback_data=f"promo_send:{code}")
            ],
            [
                InlineKeyboardButton(
                    text="📋 Показать для копирования",
                    callback_data=f"promo_copy:{code}")
            ],
            [InlineKeyboardButton(text="↩️ К промокодам", callback_data="adm_codes")],
        ]
    )


@router.callback_query(F.data.startswith("promo_send:"))
async def cq_promo_send(cb: CallbackQuery):
    code = cb.data.split(":", 1)[1]
    row = db.get_code(code)
    info = promo_info.get(code)
    owner = None
    if row and row.get("owner_id"):
        owner = row["owner_id"]
    elif info:
        owner = info[2]
    if not owner:
        await cb.answer("Информация о промокоде не найдена", show_alert=True)
        return
    if not is_admin(cb.from_user.id) and owner != cb.from_user.id:
        await cb.answer("Нельзя отправить чужой промокод", show_alert=True)
        return
    if row:
        amount = row["amount"]
        uses = row["max_uses"]
    else:
        amount, uses, _ = info
    cur = db.get_setting("currency", config.CURRENCY)
    text = promo_code_text(code, amount, uses, cur, author=promo_author_name(owner))
    users = db.get_all_user_ids()
    sent = 0
    for uid in users:
        try:
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="📋 Копировать",
                            copy_text=CopyTextButton(text=code))
                    ]
                ]
            )
            await bot.send_message(uid, text, reply_markup=kb)
            sent += 1
        except Exception:
            pass
    if is_admin(cb.from_user.id):
        await cb.answer()
        await cb.message.edit_text(
            f"📤 Промокод отправлен {sent} из {len(users)} пользователям.",
            reply_markup=admin_sub_kb([], "adm_codes"))
    else:
        await cb.answer()
        await cb.message.answer(
            f"📤 Промокод отправлен {sent} из {len(users)} пользователям!"
        )


@router.callback_query(F.data.startswith("promo_copy:"))
async def cq_promo_copy(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    code = cb.data.split(":", 1)[1]
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📋 Копировать",
                    copy_text=CopyTextButton(text=code))
            ]
        ]
    )
    await cb.answer()
    await cb.message.edit_text(
        f"📋 Промокод (нажмите на код или кнопку, чтобы скопировать):\n<code>{code}</code>",
        reply_markup=kb)


@router.callback_query(F.data == "adm_tasks")
async def cq_adm_tasks(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    await cb.answer()
    await cb.message.edit_text(
        "📢 Задания:",
        reply_markup=admin_sub_kb(
            [
                [
                    InlineKeyboardButton(text="📋 Список", callback_data="adm_tasks_list"),
                    InlineKeyboardButton(text="➕ Создать", callback_data="adm_tasks_add"),
                    InlineKeyboardButton(text="🗑 Удалить", callback_data="adm_tasks_del"),
                ],
            ],
            "adm_main"))


@router.callback_query(F.data == "adm_tasks_list")
async def cq_adm_tasks_list(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    rows = db.list_tasks(active=None)
    if not rows:
        await cb.answer("Заданий нет", show_alert=True)
        return
    cur = db.get_setting("currency", config.CURRENCY)
    lines = []
    for t in rows:
        state = "🟢 активно" if t["active"] else "🔴 отключено"
        lines.append(f"#{t['id']} @{t['sponsor']} — {fmt_num(t['reward'])} {cur} — {state}")
    await cb.answer()
    await cb.message.edit_text(
        "📢 Задания:\n" + "\n".join(lines),
        reply_markup=admin_sub_kb([], "adm_tasks"))


@router.callback_query(F.data == "adm_tasks_add")
async def cq_adm_tasks_add(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return
    await state.set_state(AdminPanel.target)
    await state.update_data(ap_action="task_chan")
    await cb.answer()
    await cb.message.edit_text("Введите юзернейм канала (например: @channel):")


@router.callback_query(F.data == "adm_tasks_del")
async def cq_adm_tasks_del(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return
    await state.set_state(AdminPanel.amount)
    await state.update_data(ap_action="task_del")
    await cb.answer()
    await cb.message.edit_text("Введите номер задания для удаления:")


@router.callback_query(F.data == "adm_settings")
async def cq_adm_settings(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    cur = db.get_setting("currency", config.CURRENCY)
    chance = db.get_setting("roulette_chance", config.DEFAULT_ROULETTE_CHANCE)
    mult = db.get_setting("roulette_mult", config.DEFAULT_ROULETTE_MULT)
    await cb.answer()
    await cb.message.edit_text(
        "⚙️ Настройки. Нажмите, чтобы изменить:",
        reply_markup=admin_sub_kb(
            [
                [
                    InlineKeyboardButton(
                        text=f"💵 Мин. вывод: {db.get_setting('min_withdraw', config.DEFAULT_MIN_WITHDRAW)} {cur}",
                        callback_data="adm_set_minwd"),
                    InlineKeyboardButton(
                        text=f"🎁 За вступление: {fmt_num(db.get_setting('reward_join', config.DEFAULT_REWARD_JOIN))} {cur}",
                        callback_data="adm_set_join"),
                ],
                [
                    InlineKeyboardButton(
                        text=f"👥 Бонус за инвайт: {db.get_setting('invite_bonus', 777)} {cur}",
                        callback_data="adm_set_invite"),
                    InlineKeyboardButton(
                        text=f"💱 Валюта: {cur}",
                        callback_data="adm_set_currency"),
                ],
                [InlineKeyboardButton(text="🔫 Скин для вывода", callback_data="adm_set_skin")],
                [InlineKeyboardButton(text=f"🎰 Шанс рулетки: {chance}%", callback_data="adm_set_roulette")],
                [InlineKeyboardButton(text=f"🎲 Множитель рулетки: x{fmt_num(mult)}", callback_data="adm_set_mult")],
                [InlineKeyboardButton(text=f"🔥 Стрик 1-й день: {db.get_setting('streak_base', config.DEFAULT_STREAK_BASE)} {cur}", callback_data="adm_set_streak_base")],
                [InlineKeyboardButton(text=f"🔥 Стрик прирост/день: +{db.get_setting('streak_step', config.DEFAULT_STREAK_STEP)} {cur}", callback_data="adm_set_streak_step")],
                [InlineKeyboardButton(text=f"🎁 Мин. сумма промо: {fmt_num(db.get_setting('promo_min_amount', config.DEFAULT_PROMO_MIN_AMOUNT))} {cur}", callback_data="adm_set_promo_min")],
                [InlineKeyboardButton(text=f"🎁 Мин. активаций промо: {db.get_setting('promo_min_uses', config.DEFAULT_PROMO_MIN_USES)}", callback_data="adm_set_promo_uses")],
                [InlineKeyboardButton(text=f"🎁 КД создания промо: {db.get_setting('promo_cd', config.DEFAULT_PROMO_CD)} сек", callback_data="adm_set_promo_cd")],
                [InlineKeyboardButton(text=f"💣 Мин 3×3: {db.get_setting('mines_3', config.DEFAULT_MINES_3)} мин", callback_data="adm_set_mines_3")],
                [InlineKeyboardButton(text=f"💣 Мин 4×4: {db.get_setting('mines_4', config.DEFAULT_MINES_4)} мин", callback_data="adm_set_mines_4")],
                [InlineKeyboardButton(text=f"💣 Мин 5×5: {db.get_setting('mines_5', config.DEFAULT_MINES_5)} мин", callback_data="adm_set_mines_5")],
                [InlineKeyboardButton(text=f"💣 Мин 6×6: {db.get_setting('mines_6', config.DEFAULT_MINES_6)} мин", callback_data="adm_set_mines_6")],
            ],
            "adm_main"))


@router.callback_query(F.data == "adm_casino")
async def cq_adm_casino(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    await cb.answer()
    kb = admin_sub_kb(
        [
            [InlineKeyboardButton(text=f"🎰 Каша: {db.get_setting('slots_prob_mush', 40)}%", callback_data="adm_casino_set:prob_mush")],
            [InlineKeyboardButton(text=f"7️⃣ Три семёрки: {db.get_setting('slots_prob_7', 2)}% (×{db.get_setting('slots_mult_7', '5')})", callback_data="adm_casino_set:prob_7")],
            [InlineKeyboardButton(text=f"🍋 Три лимона: {db.get_setting('slots_prob_lemon', 8)}% (×{db.get_setting('slots_mult_lemon', '2')})", callback_data="adm_casino_set:prob_lemon")],
            [InlineKeyboardButton(text=f"🎰 Три бара: {db.get_setting('slots_prob_bar', 15)}% (×{db.get_setting('slots_mult_bar', '1.5')})", callback_data="adm_casino_set:prob_bar")],
            [InlineKeyboardButton(text=f"🍒 Три вишни: {db.get_setting('slots_prob_cherry', 35)}% (×{db.get_setting('slots_mult_cherry', '1')})", callback_data="adm_casino_set:prob_cherry")],
            [InlineKeyboardButton(text=f"🎬 Множитель 7️⃣: ×{db.get_setting('slots_mult_7', '5')}", callback_data="adm_casino_set:mult_7")],
            [InlineKeyboardButton(text=f"🎬 Множитель 🍋: ×{db.get_setting('slots_mult_lemon', '2')}", callback_data="adm_casino_set:mult_lemon")],
            [InlineKeyboardButton(text=f"🎬 Множитель бара: ×{db.get_setting('slots_mult_bar', '1.5')}", callback_data="adm_casino_set:mult_bar")],
            [InlineKeyboardButton(text=f"🎬 Множитель вишни: ×{db.get_setting('slots_mult_cherry', '1')}", callback_data="adm_casino_set:mult_cherry")],
            [InlineKeyboardButton(text="🖼 Стикер (file_id)", callback_data="adm_casino_set:sticker")],
        ],
        "adm_main")
    await cb.message.edit_text("🎴 Настройки казино-слота:", reply_markup=kb)


@router.callback_query(F.data.startswith("adm_casino_set:"))
async def cq_adm_casino_set(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return
    param = cb.data.split(":", 1)[1]
    if param not in ("prob_mush", "prob_7", "prob_lemon", "prob_bar", "prob_cherry",
                     "mult_7", "mult_lemon", "mult_bar", "mult_cherry", "sticker"):
        return
    await state.set_state(AdminPanel.amount)
    await state.update_data(casino_param=param)
    await cb.answer()
    await cb.message.edit_text("Введите новое значение:")


@router.callback_query(F.data.startswith("adm_set_"))
async def cq_adm_set(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return
    action = {
        "adm_set_minwd": "set_minwd",
        "adm_set_join": "set_join",
        "adm_set_invite": "set_invite",
        "adm_set_currency": "set_currency",
        "adm_set_skin": "set_skin",
        "adm_set_roulette": "set_roulette",
        "adm_set_mult": "set_mult",
        "adm_set_streak_base": "set_streak_base",
        "adm_set_streak_step": "set_streak_step",
        "adm_set_promo_min": "set_promo_min",
        "adm_set_promo_uses": "set_promo_uses",
        "adm_set_promo_cd": "set_promo_cd",
        "adm_set_mines_3": "set_mines_3",
        "adm_set_mines_4": "set_mines_4",
        "adm_set_mines_5": "set_mines_5",
        "adm_set_mines_6": "set_mines_6",
    }.get(cb.data)
    if not action:
        return
    await state.set_state(AdminPanel.amount)
    await state.update_data(ap_action=action)
    await cb.answer()
    await cb.message.edit_text("Введите новое значение:")


@router.message(AdminPanel.target, F.text)
async def adm_panel_target(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    action = data.get("ap_action")
    text = message.text.strip()

    if action == "user":
        user = resolve_user(text)
        if not user:
            await message.answer("Пользователь не найден. Попробуйте ещё раз.")
            return
        await state.clear()
        cur = db.get_setting("currency", config.CURRENCY)
        referrals = db.count_referrals(user["id"])
        username = f"@{user['username']}" if user["username"] else "—"
        link = f"https://t.me/{config.BOT_USERNAME}?start={user['id']}"
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="➕ Добавить баланс", callback_data=f"ub_add:{user['id']}"),
                    InlineKeyboardButton(text="🔄 Обнулить", callback_data=f"ub_zero:{user['id']}"),
                ],
                [InlineKeyboardButton(text="💸 Активные выводы", callback_data=f"ub_wd:{user['id']}")],
            ]
        )
        await message.answer(
            f"👤 Информация о пользователе\n"
            f"ID: {user['id']}\n"
            f"Имя: {user['first_name']}\n"
            f"Ник: {username}\n"
f"Баланс: {fmt_num(user['balance'])} {cur}\n"
            f"Пригласил: {referrals} чел.\n"
            f"Пришёл по реф.: {user['ref_id'] if user['ref_id'] else '—'}\n"
            f"🔗 {link}\n"
            f"Дата регистрации: {user['created_at'][:16]}",
            reply_markup=kb)
        return

    user = resolve_user(text)
    if not user:
        await message.answer("Пользователь не найден. Попробуйте ещё раз.")
        return

    if action in ("bal_add", "bal_sub", "bal_zero"):
        await state.update_data(ap_target=user["id"])
        if action == "bal_zero":
            db.set_balance(user["id"], 0)
            await state.clear()
            await message.answer(
                f"🔄 Баланс пользователя {user['first_name']} (@{user['username'] or user['id']}) обнулён."
            )
            return
        verb = "начисления" if action == "bal_add" else "списания"
        await state.set_state(AdminPanel.amount)
        await message.answer(
            f"Введите количество {db.get_setting('currency', config.CURRENCY)} для {verb}:"
        )
        return

    if action == "code":
        await state.update_data(ap_code=text.upper())
        await state.set_state(AdminPanel.amount)
        await message.answer("Введите количество голды за промокод:")
        return

    if action == "task_chan":
        await state.update_data(ap_task_chan=text.lstrip("@"))
        await state.set_state(AdminPanel.amount)
        await message.answer("Введите количество голды за подписку:")
        return

    if action == "code_del":
        db.delete_code(text.upper())
        await state.clear()
        await message.answer(f"🗑 Промокод {text.upper()} удалён. Активировать его больше нельзя.")
        return

    user = resolve_user(text)
    if not user:
        await message.answer("Пользователь не найден. Попробуйте ещё раз.")
        return


def parse_ban_target(value):
    if str(value).isdigit():
        return int(value)
    user = resolve_user(value)
    if user:
        return user["id"]
    return None


def _db_error_msg(exc):
    text = str(exc) or type(exc).__name__
    return f"⚠️ Ошибка БД: {text[:150]}"


async def cmd_ban_unban(message: Message, command: CommandObject, banned: bool, action_name: str):
    if not is_admin(message.from_user.id):
        return
    args = command.args.split() if command.args else []
    if not args:
        await message.answer(f"Использование: /{action_name} <id или @ник>")
        return
    try:
        target = parse_ban_target(args[0])
    except Exception as e:
        await message.answer(_db_error_msg(e))
        return
    if not target:
        await message.answer("Пользователь не найден.")
        return
    try:
        db.set_ban(target, banned)
    except Exception as e:
        await message.answer(_db_error_msg(e))
        return
    _moderation_cache.pop(target, None)
    await message.answer(
        f"🔨 Пользователь {target} забанен." if banned
        else f"✅ Пользователь {target} разбанен."
    )


async def cmd_mute_unmute(message: Message, command: CommandObject, muted: bool, action_name: str):
    if not is_admin(message.from_user.id):
        return
    args = command.args.split() if command.args else []
    if not args:
        await message.answer(f"Использование: /{action_name} <id или @ник>")
        return
    try:
        target = parse_ban_target(args[0])
    except Exception as e:
        await message.answer(_db_error_msg(e))
        return
    if not target:
        await message.answer("Пользователь не найден.")
        return
    try:
        db.set_mute(target, muted)
    except Exception as e:
        await message.answer(_db_error_msg(e))
        return
    _moderation_cache.pop(target, None)
    await message.answer(
        f"🔇 Пользователь {target} замьючен." if muted
        else f"✅ Пользователь {target} размьючен."
    )


@router.message(Command("ban", ignore_case=True))
async def cmd_ban(message: Message, command: CommandObject):
    await cmd_ban_unban(message, command, True, "ban")


@router.message(Command("unban", ignore_case=True))
async def cmd_unban(message: Message, command: CommandObject):
    await cmd_ban_unban(message, command, False, "unban")


@router.message(Command("mute", ignore_case=True))
async def cmd_mute(message: Message, command: CommandObject):
    await cmd_mute_unmute(message, command, True, "mute")


@router.message(Command("unmute", ignore_case=True))
async def cmd_unmute(message: Message, command: CommandObject):
    await cmd_mute_unmute(message, command, False, "unmute")


@router.message(AdminPanel.amount, F.text)
async def adm_panel_amount(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    action = data.get("ap_action")
    casino_param = data.get("casino_param")
    cur = db.get_setting("currency", config.CURRENCY)

    def parse_num(t):
        return float(t.replace(",", ".").strip())

    if casino_param:
        val = message.text.strip()
        if casino_param == "sticker":
            db.set_setting("slots_sticker", val)
            await state.clear()
            await message.answer(f"🖼 Стикер казино обновлён.")
            return
        try:
            num = float(val.replace(",", "."))
        except ValueError:
            await message.answer("Введите число.")
            return
        db.set_setting(casino_param, num)
        await state.clear()
        await message.answer(f"✅ Параметр «{casino_param}» установлен: {val}.")
        return

    if action == "task_del":
        num = message.text.strip()
        if not num.isdigit():
            await message.answer("Введите номер задания числом.")
            return
        task = db.get_task(int(num))
        if not task:
            await message.answer("Задание не найдено.")
            return
        db.deactivate_task(int(num))
        await state.clear()
        await message.answer(f"Задание #{num} отключено.")
        return

    if action == "rand_amount":
        r = parse_range(message.text)
        if not r:
            await message.answer("Введите диапазон в формате: <code>200-1000</code>")
            return
        await state.update_data(
            ap_action="rand_uses",
            rand_amount_lo=r[0],
            rand_amount_hi=r[1])
        await message.answer(
            f"🎲 Диапазон голды: {fmt_num(r[0])}-{fmt_num(r[1])}.\n"
            "Введите диапазон активаций, например: <code>1-5</code>"
        )
        return

    if action == "rand_uses":
        r = parse_range(message.text)
        if not r:
            await message.answer("Введите диапазон в формате: <code>1-5</code>")
            return
        try:
            amount = random.randint(int(data["rand_amount_lo"]), int(data["rand_amount_hi"]))
        except KeyError:
            await message.answer("Сначала введите диапазон голды.")
            return
        uses = random.randint(int(r[0]), int(r[1]))
        code = gen_promo_code()
        db.add_code(code, amount, max_uses=uses, owner_id=message.from_user.id)
        promo_info[code] = (amount, uses, message.from_user.id)
        cur = db.get_setting("currency", config.CURRENCY)
        await state.clear()
        await message.answer(
            promo_code_text(code, amount, uses, cur),
            reply_markup=promo_result_kb(code))
        return

    if action == "set_currency":
        db.set_setting("currency", message.text.strip())
        await state.clear()
        await message.answer(f"💱 Валюта: {message.text.strip()}.")
        return
    if action == "set_skin":
        db.set_setting("withdraw_skin", message.text.strip())
        await state.clear()
        await message.answer(f"🔫 Скин для вывода: {message.text.strip()}.")
        return

    try:
        value = parse_num(message.text)
    except ValueError:
        await message.answer("Введите число.")
        return

    if action == "bal_add":
        db.add_balance(data["ap_target"], value)
        await state.clear()
        await message.answer(f"➕ Начислено +{fmt_num(value)} {cur}.")
    elif action == "bal_sub":
        db.spend_balance(data["ap_target"], value)
        await state.clear()
        await message.answer(f"➖ Списано −{fmt_num(value)} {cur}.")
    elif action == "all_add":
        n = db.add_balance_all(value, config.ADMIN_IDS)
        await state.clear()
        await message.answer(f"🌐 Добавлено +{fmt_num(value)} {cur} {n} пользователям.")
    elif action == "all_sub":
        n = db.subtract_balance_all(value, config.ADMIN_IDS)
        await state.clear()
        await message.answer(f"🌐 Убрано −{fmt_num(value)} {cur} у {n} пользователей.")
    elif action == "code":
        await state.update_data(ap_code_amount=int(value))
        uses_kb = admin_sub_kb(
            [
                [
                    InlineKeyboardButton(text="1", callback_data="adm_code_uses:1"),
                    InlineKeyboardButton(text="3", callback_data="adm_code_uses:3"),
                    InlineKeyboardButton(text="5", callback_data="adm_code_uses:5"),
                ],
                [
                    InlineKeyboardButton(text="10", callback_data="adm_code_uses:10"),
                    InlineKeyboardButton(text="50", callback_data="adm_code_uses:50"),
                    InlineKeyboardButton(text="∞", callback_data="adm_code_uses:0"),
                ],
            ],
            "adm_codes")
        await message.answer(
            f"🎁 Промокод {data['ap_code']} на {int(value)} {cur}.\n"
            "Сколько активаций разрешить?",
            reply_markup=uses_kb)
    elif action == "task_rew":
        task_id = db.add_task(data["ap_task_chan"], int(value))
        await state.clear()
        await message.answer(
            f"📢 Задание #{task_id} создано: подписка на @{data['ap_task_chan']} = {int(value)} {cur}.\n"
            "Не забудь добавить бота в этот канал/чат."
        )
    elif action == "set_minwd":
        db.set_setting("min_withdraw", int(value))
        await state.clear()
        await message.answer(f"💵 Мин. вывод: {int(value)} {cur}.")
    elif action == "set_join":
        db.set_setting("reward_join", int(value))
        await state.clear()
        await message.answer(f"🎁 Награда за вступление: {int(value)} {cur}.")
    elif action == "set_invite":
        db.set_setting("invite_bonus", int(value))
        await state.clear()
        await message.answer(f"👥 Бонус за реферальную ссылку: {int(value)} {cur}.")
    elif action == "set_roulette":
        db.set_setting("roulette_chance", int(value))
        await state.clear()
        await message.answer(f"🎰 Шанс победы в рулетке: {int(value)}%.")
    elif action == "set_mult":
        db.set_setting("roulette_mult", value)
        await state.clear()
        await message.answer(f"🎲 Множитель рулетки: x{fmt_num(value)}.")
    elif action == "set_streak_base":
        db.set_setting("streak_base", int(value))
        await state.clear()
        await message.answer(f"🔥 Стрик: награда за 1-й день — {int(value)} {cur}.")
    elif action == "set_streak_step":
        db.set_setting("streak_step", int(value))
        await state.clear()
        await message.answer(f"🔥 Стрик: прирост за день — +{int(value)} {cur}.")
    elif action == "set_promo_min":
        db.set_setting("promo_min_amount", int(value))
        await state.clear()
        await message.answer(f"🎁 Мин. сумма одной активации промо: {int(value)} {cur}.")
    elif action == "set_promo_uses":
        db.set_setting("promo_min_uses", int(value))
        await state.clear()
        await message.answer(f"🎁 Мин. активаций промо: {int(value)}.")
    elif action == "set_promo_cd":
        cd = int(value)
        if cd < 1:
            cd = 1
        elif cd > 3600:
            cd = 3600
        db.set_setting("promo_cd", cd)
        await state.clear()
        await message.answer(f"🎁 КД создания промо: {cd} сек (1 секунда — 1 час).")
    elif action == "set_mines_3":
        db.set_setting("mines_3", int(value))
        await state.clear()
        await message.answer(f"💣 Мины 3×3: {int(value)} мин.")
    elif action == "set_mines_4":
        db.set_setting("mines_4", int(value))
        await state.clear()
        await message.answer(f"💣 Мины 4×4: {int(value)} мин.")
    elif action == "set_mines_5":
        db.set_setting("mines_5", int(value))
        await state.clear()
        await message.answer(f"💣 Мины 5×5: {int(value)} мин.")
    elif action == "set_mines_6":
        db.set_setting("mines_6", int(value))
        await state.clear()
        await message.answer(f"💣 Мины 6×6: {int(value)} мин.")
    else:
        await state.clear()
        await message.answer("Действие устарело. Откройте /a заново.")


def resolve_user(value):
    value = (value or "").strip().lstrip("@")
    if value.isdigit():
        return db.get_user(int(value))
    if value:
        return db.get_user_by_username("@" + value)
    return None


@router.message(Command("user"))
async def cmd_userbalance(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    user = resolve_user(command.args)
    if not user:
        await message.answer("Использование: /user <id или @юзернейм>")
        return
    cur = db.get_setting("currency", config.CURRENCY)
    referrals = db.count_referrals(user["id"])
    username = f"@{user['username']}" if user["username"] else "—"
    link = f"https://t.me/{config.BOT_USERNAME}?start={user['id']}"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="➕ Добавить баланс", callback_data=f"ub_add:{user['id']}"),
                InlineKeyboardButton(text="🔄 Обнулить", callback_data=f"ub_zero:{user['id']}"),
            ],
            [InlineKeyboardButton(text="💸 Активные выводы", callback_data=f"ub_wd:{user['id']}")],
        ]
    )
    await message.answer(
        f"👤 Информация о пользователе\n"
        f"ID: {user['id']}\n"
        f"Имя: {user['first_name']}\n"
        f"Ник: {username}\n"
        f"Баланс: {fmt_num(user['balance'])} {cur}\n"
        f"Пригласил: {referrals} чел.\n"
        f"Пришёл по реф.: {user['ref_id'] if user['ref_id'] else '—'}\n"
        f"🔗 {link}\n"
        f"Дата регистрации: {user['created_at'][:16]}",
        reply_markup=kb)


@router.callback_query(F.data.startswith("ub_add:"))
async def cq_ub_add(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        await cb.answer("Недоступно", show_alert=True)
        return
    user_id = int(cb.data.split(":", 1)[1])
    await state.set_state(AdminBalance.amount)
    await state.update_data(ub_target=user_id)
    await cb.answer()
    await cb.message.answer(f"Введите количество {db.get_setting('currency', config.CURRENCY)} для начисления:")


@router.message(AdminBalance.amount)
async def admin_balance_amount(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    target = data.get("ub_target")
    if not target:
        await state.clear()
        await message.answer("Действие устарело. Откройте /user заново.")
        return
    text = message.text.replace(",", ".") if message.text else ""
    if not text.isdigit():
        await message.answer("Введите число (количество голды).")
        return
    amount = int(float(text))
    user = db.get_user(target)
    db.add_balance(target, amount)
    await _send_admin_add(target, amount)
    cur = db.get_setting("currency", config.CURRENCY)
    await state.clear()
    await message.answer(
        f"➕ Начислено +{fmt_num(amount)} {cur} пользователю {user['first_name'] if user else target} "
        f"(ID {target}).\nТекущий баланс: {fmt_num(user['balance'] + amount)} {cur}."
    )


@router.callback_query(F.data.startswith("ub_zero:"))
async def cq_ub_zero(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("Недоступно", show_alert=True)
        return
    user_id = int(cb.data.split(":", 1)[1])
    user = db.get_user(user_id)
    if not user:
        await cb.answer("Пользователь не найден", show_alert=True)
        return
    db.set_balance(user_id, 0)
    await cb.answer("Баланс обнулён")
    await cb.message.edit_text(
        f"{cb.message.text}\n\n🔄 Баланс обнулён до 0 {db.get_setting('currency', config.CURRENCY)}."
    )


@router.callback_query(F.data.startswith("ub_wd:"))
async def cq_ub_wd(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("Недоступно", show_alert=True)
        return
    user_id = int(cb.data.split(":", 1)[1])
    rows = db.list_user_withdrawals(user_id, limit=10)
    if not rows:
        await cb.answer("Выводов нет", show_alert=True)
        await cb.message.answer("У пользователя нет выводов.")
        return
    cur = db.get_setting("currency", config.CURRENCY)
    label = {"pending": "⏳ ЖДЁТ", "paid": "✅ ВЫПЛАЧЕН", "rejected": "❌ ОТКЛОНЁН"}
    lines = []
    for r in rows:
        lines.append(
            f"#{r['id']} {label.get(r['status'], r['status'])} · {fmt_num(r['amount'])} {cur} · "
            f"🎮 {r.get('skin') or '—'} · {r['created_at'][:16]}\n"
            f"    📋 /withdraw {r['id']}"
        )
    await cb.answer()
    await cb.message.answer("💸 Выводы пользователя:\n\n" + "\n\n".join(lines))


@router.message(Command("userwithdrawals"))
async def cmd_userwithdrawals(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    target = resolve_user(command.args)
    if not target:
        await message.answer("Использование: /userwithdrawals <id или @юзернейм>")
        return
    rows = db.list_user_withdrawals(target["id"])
    if not rows:
        await message.answer("У этого пользователя нет выводов.")
        return
    cur = db.get_setting("currency", config.CURRENCY)
    label = {"pending": "⏳ ЖДЁТ", "paid": "✅ ВЫПЛАЧЕН", "rejected": "❌ ОТКЛОНЁН"}
    lines = []
    for r in rows:
        lines.append(
            f"#{r['id']} {label.get(r['status'], r['status'])} · {fmt_num(r['amount'])} {cur} · "
            f"🎮 {r.get('skin') or '—'} · {r['created_at'][:16]}\n"
            f"    📋 /withdraw {r['id']}"
        )
    await message.answer(f"💸 Выводы пользователя @{target['username'] or target['id']}:\n\n" + "\n\n".join(lines))


@router.message(Command("addbal"))
async def cmd_addbal(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    args = command.args.split() if command.args else []
    if len(args) != 2 or not args[1].isdigit():
        await message.answer("Использование: /addbal <id или @ник> <количество G>")
        return
    user = resolve_user(args[0])
    if not user:
        await message.answer("Пользователь не найден.")
        return
    db.add_balance(user["id"], int(args[1]))
    cur = db.get_setting("currency", config.CURRENCY)
    await _send_admin_add(user["id"], int(args[1]))
    await message.answer(
        f"Начислено +{fmt_num(args[1])} {cur} пользователю {user['first_name']}"
        f" (@{user['username'] or user['id']})."
    )


@router.message(Command("subbal"))
async def cmd_subbal(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    args = command.args.split() if command.args else []
    if len(args) != 2 or not args[1].isdigit():
        await message.answer("Использование: /subbal <id или @ник> <количество G>")
        return
    user = resolve_user(args[0])
    if not user:
        await message.answer("Пользователь не найден.")
        return
    db.spend_balance(user["id"], int(args[1]))
    cur = db.get_setting("currency", config.CURRENCY)
    await message.answer(
        f"Списано −{fmt_num(args[1])} {cur} у пользователя {user['first_name']}"
        f" (@{user['username'] or user['id']})."
    )


@router.message(Command("delbal"))
async def cmd_delbal(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    args = command.args.split() if command.args else []
    if len(args) != 2 or not args[1].isdigit():
        await message.answer("Использование: /delbal <id или @ник> <количество G>")
        return
    user = resolve_user(args[0])
    if not user:
        await message.answer("Пользователь не найден.")
        return
    amount = int(args[1])
    if not db.spend_balance(user["id"], amount):
        await message.answer("Не удалось списать: недостаточно голды на балансе пользователя.")
        return
    cur = db.get_setting("currency", config.CURRENCY)
    await message.answer(
        f"Списано −{fmt_num(args[1])} {cur} у пользователя {user['first_name']}"
        f" (@{user['username'] or user['id']})."
    )


@router.message(Command("userbal"))
async def cmd_userbal(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    user = resolve_user(command.args or "")
    if not user:
        await message.answer("Использование: /userbal <id или @юзернейм>")
        return
    cur = db.get_setting("currency", config.CURRENCY)
    username = f"@{user['username']}" if user["username"] else f"{user['id']}"
    await message.answer(
        f"👤 Пользователь: {user['first_name']} ({username})\n"
        f"💼 Баланс: {fmt_num(user['balance'])} {cur}"
    )


@router.message(Command("stickerid"))
async def cmd_stickerid(message: Message):
    await message.answer(
        "Отправьте стикер, и я верну его file_id. "
        "Если вы админ — стикер также сохранится как анимация казино-слота."
    )


@router.message(Command("setstick"))
async def cmd_setstick(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    sid = (command.args or "").strip()
    if not sid:
        await message.answer("Использование: /setstick <file_id>")
        return
    db.set_setting("slots_sticker", sid)
    db.set_setting("slots_custom_emoji", "")
    await message.answer("✅ Стикер казино-слота сохранён!")


@router.message(F.sticker)
async def on_sticker(message: Message):
    if message.chat.type != "private":
        return
    sid = message.sticker.file_id
    reply = f"🎴 file_id стикера:\n<code>{sid}</code>"
    if is_admin(message.from_user.id):
        db.set_setting("slots_sticker", sid)
        reply += "\n\n✅ Сохранён как стикер казино-слота!"
    await message.answer(reply, parse_mode=ParseMode.HTML)


@router.message(F.custom_emoji_id)
async def on_custom_emoji(message: Message):
    if message.chat.type != "private":
        return
    cid = message.custom_emoji_id
    text = message.text or ""
    reply = f"✨ Это премиум-эмодзи (custom_emoji_id):\n<code>{cid}</code>\nСимвол: {text}"
    if is_admin(message.from_user.id):
        db.set_setting("slots_custom_emoji", cid)
        db.set_setting("slots_sticker", "")
        reply += "\n\n✅ Сохранён как анимация казино-слота (премиум-эмодзи)!"
    await message.answer(reply, parse_mode=ParseMode.HTML)


@router.message(Command("withdrawals"))
async def cmd_withdrawals(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    status_map = {
        "pending": "pending",
        "paid": "paid",
        "rejected": "rejected",
    }
    status = None
    if command.args:
        status = status_map.get(command.args.strip().lower())
        if status is None:
            await message.answer(
                "Использование: /withdrawals [pending | paid | rejected]"
            )
            return
    rows = db.list_withdrawals(status=status, limit=15)
    if not rows:
        await message.answer("Заявок на вывод не найдено.")
        return
    cur = db.get_setting("currency", config.CURRENCY)
    label = {"pending": "⏳ В ОЖИДАНИИ", "paid": "✅ ВЫПЛАЧЕН", "rejected": "❌ ОТКЛОНЁН"}
    lines = []
    for r in rows:
        user = db.get_user(r["user_id"])
        uname = f"@{user['username']}" if user and user["username"] else f"ID {r['user_id']}"
        lines.append(
            f"#{r['id']} {label.get(r['status'], r['status'])} · {fmt_num(r['amount'])} {cur}\n"
            f"    🎮 {r.get('skin') or '—'} · {uname}\n"
            f"    🕒 {r['created_at'][:16]}"
        )
    await message.answer("💸 Выводы:\n\n" + "\n\n".join(lines))


@router.message(Command("withdraw"))
async def cmd_withdraw(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    if not command.args or not command.args.isdigit():
        await message.answer("Использование: /withdraw <номер вывода>")
        return
    wd = db.get_withdrawal(int(command.args))
    if not wd:
        await message.answer("Заявка не найдена.")
        return
    wd_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Выплатить", callback_data=f"wd_approve:{wd['id']}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"wd_reject:{wd['id']}"),
            ]
        ]
    )
    if wd.get("screenshot"):
        await message.answer_photo(
            photo=wd["screenshot"],
            caption=format_wd(wd),
            reply_markup=wd_kb)
    else:
        await message.answer(format_wd(wd), reply_markup=wd_kb)


@router.message(Command("addZad", ignore_case=True))
async def cmd_add_task(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    args = command.args.split() if command.args else []
    if len(args) != 2 or not args[1].isdigit():
        await message.answer("Использование: /addZad <@канал> <количество G>")
        return
    sponsor = args[0].lstrip("@")
    reward = int(args[1])
    task_id = db.add_task(sponsor, reward)
    cur = db.get_setting("currency", config.CURRENCY)
    await message.answer(
        f"Задание #{task_id} создано: подписка на @{sponsor} = {fmt_num(reward)} {cur}.\n"
        "Не забудь добавить бота в этот канал/чат."
    )


@router.message(Command("tasks"))
async def cmd_tasks(message: Message):
    if not is_admin(message.from_user.id):
        return
    rows = db.list_tasks(active=None)
    if not rows:
        await message.answer("Заданий нет. Создайте: /addZad <@канал> <G>")
        return
    cur = db.get_setting("currency", config.CURRENCY)
    lines = []
    for t in rows:
        state = "🟢 активно" if t["active"] else "🔴 отключено"
        lines.append(f"#{t['id']} @{t['sponsor']} — {fmt_num(t['reward'])} {cur} — {state}")
    await message.answer("📢 Задания:\n" + "\n".join(lines))


@router.message(Command("delZad", ignore_case=True))
async def cmd_del_task(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    if not command.args or not command.args.isdigit():
        await message.answer("Использование: /delZad <номер задания>")
        return
    task = db.get_task(int(command.args))
    if not task:
        await message.answer("Задание не найдено.")
        return
    db.deactivate_task(int(command.args))
    await message.answer(f"Задание #{command.args} отключено.")


@router.message(Command("set_invite"))
async def cmd_set_invite(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    if not command.args or not command.args.isdigit():
        await message.answer("Использование: /set_invite <количество G>")
        return
    db.set_setting("invite_bonus", int(command.args))
    cur = db.get_setting("currency", config.CURRENCY)
    await message.answer(f"Бонус за реферальную ссылку установлен: {command.args} {cur}.")


@router.callback_query(F.data == "earn_gold")
async def cq_earn_gold(cb: CallbackQuery):
    await cb.answer()
    tasks = db.list_tasks(active=True)
    if not tasks:
        await cb.message.answer(
            "Сейчас нет доступных заданий. Загляните позже!", reply_markup=main_menu()
        )
        return
    cur = db.get_setting("currency", config.CURRENCY)
    lines = ["💰 Задания. Подпишитесь на спонсоров и нажмите «Проверить»:\n"]
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"Задание #{t['id']}", callback_data=f"task_info:{t['id']}")]
            for t in tasks
        ]
    )
    await cb.message.answer(
        "💰 Заработать голду\nПодпишитесь на каналы спонсоров и получите голду за подписку:",
        reply_markup=kb)


@router.callback_query(F.data == "streak")
async def cq_streak(cb: CallbackQuery):
    await cb.answer()
    cur = db.get_setting("currency", config.CURRENCY)
    base = db.get_setting("streak_base", config.DEFAULT_STREAK_BASE)
    step = db.get_setting("streak_step", config.DEFAULT_STREAK_STEP)
    max_days = config.MAX_STREAK_DAYS

    def reward(d):
        d = min(d, max_days)
        return base + (d - 1) * step

    user = db.get_user(cb.from_user.id)
    day = int(user.get("streak") or 0)
    last_date = user.get("streak_date") or ""
    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()

    if last_date == today:
        await cb.message.answer(
            f"🔥 Вы уже получили награду за сегодня!\n"
            f"Стрик: {day} дн.\n"
            f"Завтра: +{fmt_num(reward(day + 1))} {cur}",
            reply_markup=main_menu())
        return

    if last_date == yesterday:
        day += 1
    else:
        day = 1

    got = reward(day)
    db.update_streak(cb.from_user.id, day, today)
    db.add_balance(cb.from_user.id, got)
    await cb.message.answer(
        f"🔥 Стрик: {day} день!\n"
        f"Награда: +{fmt_num(got)} {cur}\n\n"
        f"Заходите завтра — получите +{fmt_num(reward(day + 1))} {cur}.",
        reply_markup=main_menu())


@router.callback_query(F.data == "roulette")
async def cq_roulette(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🎰 Классическая", callback_data="casino_classic"),
                InlineKeyboardButton(text="💣 Сапёр", callback_data="casino_mines"),
            ],
            [
                InlineKeyboardButton(text="🎴 Казино-слот", callback_data="casino_slots"),
            ],
            [InlineKeyboardButton(text="↩️ В меню", callback_data="back_to_menu")],
        ]
    )
    await cb.message.answer("🎰 Выберите тип казино:", reply_markup=kb)


@router.callback_query(F.data == "casino_mines")
async def cq_casino_mines(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_state(Mines.bet)
    cur = db.get_setting("currency", config.CURRENCY)
    min_bet = db.get_setting("roulette_min", config.DEFAULT_ROULETTE_MIN)
    await cb.message.answer(
        f"💣 Казино «Сапёр»\n"
        f"Ставка: от {fmt_num(min_bet)} {cur}\n"
        f"Выигрыш: каждый безопасный ход ×{fmt_num(MINES_MULT)}\n\n"
        f"Введите сумму ставки:",
        reply_markup=casino_mines_back_kb(),
    )


def casino_mines_back_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💎 Поставить всё", callback_data="mines_all")],
            [InlineKeyboardButton(text="↩️ В меню", callback_data="back_to_menu")],
        ]
    )


@router.callback_query(F.data == "mines_all")
async def cq_mines_all(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    user = db.get_user(cb.from_user.id)
    if not user:
        await cb.message.answer("Сначала откройте меню командой /start.")
        await state.clear()
        return
    balance = user["balance"]
    min_bet = db.get_setting("roulette_min", config.DEFAULT_ROULETTE_MIN)
    if balance < min_bet:
        await cb.message.answer(
            f"Баланса не хватает даже на минимальную ставку "
            f"({fmt_num(min_bet)} {db.get_setting('currency', config.CURRENCY)})."
        )
        return
    await state.update_data(mines_bet=balance)
    await show_mines_grid_choice(cb.message, state)


@router.message(Mines.bet, F.text)
async def mines_bet(message: Message, state: FSMContext):
    try:
        amount = float(message.text.replace(",", "."))
    except ValueError:
        await message.answer("Введите число.")
        return
    cur = db.get_setting("currency", config.CURRENCY)
    min_bet = db.get_setting("roulette_min", config.DEFAULT_ROULETTE_MIN)
    if amount < min_bet:
        await message.answer(f"Минимальная ставка: {fmt_num(min_bet)} {cur}.")
        return
    user = db.get_user(message.from_user.id)
    if not user:
        db.add_user(message.from_user.id, message.from_user.username or "", message.from_user.first_name or "")
        user = db.get_user(message.from_user.id)
    if user["balance"] < amount:
        await message.answer("На балансе недостаточно голды.")
        return
    await state.update_data(mines_bet=amount)
    await show_mines_grid_choice(message, state)


async def show_mines_grid_choice(message, state: FSMContext):
    await state.set_state(Mines.grid)
    mines_3 = db.get_setting("mines_3", config.DEFAULT_MINES_3)
    mines_4 = db.get_setting("mines_4", config.DEFAULT_MINES_4)
    mines_5 = db.get_setting("mines_5", config.DEFAULT_MINES_5)
    mines_6 = db.get_setting("mines_6", config.DEFAULT_MINES_6)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=f"3×3 ({mines_3} мин)", callback_data="mines_grid:3"),
                InlineKeyboardButton(text=f"4×4 ({mines_4} мин)", callback_data="mines_grid:4"),
            ],
            [
                InlineKeyboardButton(text=f"5×5 ({mines_5} мин)", callback_data="mines_grid:5"),
                InlineKeyboardButton(text=f"6×6 ({mines_6} мин)", callback_data="mines_grid:6"),
            ],
            [InlineKeyboardButton(text="↩️ В меню", callback_data="back_to_menu")],
        ]
    )
    await message.answer("💣 Выберите размер поля:", reply_markup=kb)


@router.callback_query(F.data.startswith("mines_grid:"))
async def cq_mines_grid(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    size = cb.data.split(":")[1]
    if size not in MINES_GRIDS:
        return
    data = await state.get_data()
    amount = data.get("mines_bet")
    if not amount:
        await state.clear()
        await cb.message.answer("Действие устарело. Введите ставку заново.")
        return
    await state.clear()
    n = MINES_GRIDS[size]
    mines_key = f"mines_{size}"
    mines_count = int(db.get_setting(mines_key, getattr(config, f"DEFAULT_MINES_{size}")))
    if mines_count >= n * n - 1:
        mines_count = n * n - 2

    user = db.get_user(cb.from_user.id)
    if not user:
        await cb.message.answer("Сначала откройте меню командой /start.")
        return
    if user["balance"] < amount:
        await cb.message.answer("На балансе недостаточно голды.")
        return
    if not db.spend_balance(cb.from_user.id, amount):
        await cb.message.answer("На балансе недостаточно голды.")
        return

    cells = list(range(n * n))
    mines = set(random.sample(cells, mines_count))
    mines_games[cb.from_user.id] = {
        "n": n,
        "mines": mines,
        "revealed": set(),
        "bet": amount,
        "cur": amount,
        "steps": 0,
    }
    await cb.message.answer(
        f"💣 Сапёр {n}×{n} ({mines_count} мин)\n"
        f"Ставка: {fmt_num(amount)} {db.get_setting('currency', config.CURRENCY)}\n"
        f"Текущий выигрыш: {fmt_num(amount)} {db.get_setting('currency', config.CURRENCY)}\n"
        f"Нажмите на клетку. После ≥1 хода можно забрать.",
        reply_markup=render_mines_board(cb.from_user.id),
    )


def render_mines_board(uid):
    game = mines_games.get(uid)
    if not game:
        return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="↩️ В меню", callback_data="back_to_menu")]])
    n = game["n"]
    rows = []
    for r in range(n):
        row = []
        for c in range(n):
            idx = r * n + c
            if idx in game["revealed"]:
                row.append(InlineKeyboardButton(text="✅", callback_data="mines_noop"))
            else:
                row.append(InlineKeyboardButton(text="⬜", callback_data=f"mines_click:{idx}"))
        rows.append(row)
    rows.append([InlineKeyboardButton(text=f"💎 Забрать {fmt_num(game['cur'])}", callback_data="mines_cash")])
    rows.append([InlineKeyboardButton(text="↩️ В меню", callback_data="back_to_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("mines_click:"))
async def cq_mines_click(cb: CallbackQuery):
    await cb.answer()
    game = mines_games.get(cb.from_user.id)
    if not game:
        await cb.message.answer("Игра не найдена. Начните заново.", reply_markup=casino_mines_back_kb())
        return
    idx = int(cb.data.split(":")[1])
    if idx in game["revealed"]:
        return
    cur = db.get_setting("currency", config.CURRENCY)
    if idx in game["mines"]:
        mines_games.pop(cb.from_user.id, None)
        await cb.message.edit_text(
            f"💥 Бум! Вы нажали на мину.\n"
            f"Ставка {fmt_num(game['bet'])} {cur} потеряна.",
            reply_markup=casino_mines_back_kb(),
        )
        return
    game["revealed"].add(idx)
    game["steps"] += 1
    game["cur"] = round(game["cur"] * MINES_MULT, 2)
    # проверим, не открыты ли все безопасные клетки
    safe_total = game["n"] * game["n"] - len(game["mines"])
    if len(game["revealed"]) >= safe_total:
        win = game["cur"]
        mines_games.pop(cb.from_user.id, None)
        db.add_balance(cb.from_user.id, win)
        await cb.message.edit_text(
            f"🎉 Вы открыли все безопасные клетки!\n"
            f"+{fmt_num(win)} {cur} начислено.",
            reply_markup=casino_mines_back_kb(),
        )
        return
    await cb.message.edit_text(
        f"💣 Сапёр {game['n']}×{game['n']}\n"
        f"Текущий выигрыш: {fmt_num(game['cur'])} {cur}\n"
        f"Ход #{game['steps']}. Нажмите дальше или заберите.",
        reply_markup=render_mines_board(cb.from_user.id),
    )


@router.callback_query(F.data == "mines_cash")
async def cq_mines_cash(cb: CallbackQuery):
    await cb.answer()
    game = mines_games.get(cb.from_user.id)
    if not game:
        await cb.message.answer("Игра не найдена. Начните заново.", reply_markup=casino_mines_back_kb())
        return
    cur = db.get_setting("currency", config.CURRENCY)
    if game["steps"] < 1:
        await cb.answer("Сначала сделайте хотя бы 1 ход.", show_alert=True)
        return
    win = game["cur"]
    mines_games.pop(cb.from_user.id, None)
    db.add_balance(cb.from_user.id, win)
    await cb.message.edit_text(
        f"✅ Вы забрали выигрыш: +{fmt_num(win)} {cur}",
        reply_markup=casino_mines_back_kb(),
    )


@router.callback_query(F.data == "mines_noop")
async def cq_mines_noop(cb: CallbackQuery):
    await cb.answer()


@router.callback_query(F.data == "casino_classic")
async def cq_casino_classic(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    cur = db.get_setting("currency", config.CURRENCY)
    min_bet = db.get_setting("roulette_min", config.DEFAULT_ROULETTE_MIN)
    chance = db.get_setting("roulette_chance", config.DEFAULT_ROULETTE_CHANCE)
    mult = db.get_setting("roulette_mult", config.DEFAULT_ROULETTE_MULT)
    await state.set_state(Roulette.bet)
    await cb.message.answer(
        f"🎰 Рулетка\n"
        f"Ставка: от {fmt_num(min_bet)} {cur}\n"
        f"Шанс победы: {chance}%\n"
        f"Выигрыш: ставка ×{fmt_num(mult)}\n\n"
        f"Введите сумму ставки:",
        reply_markup=roulette_bet_kb())


@router.callback_query(F.data == "casino_slots")
async def cq_casino_slots(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_state(Slots.bet)
    cur = db.get_setting("currency", config.CURRENCY)
    min_bet = db.get_setting("roulette_min", config.DEFAULT_ROULETTE_MIN)
    slots_text = slots_rules_text()
    await cb.message.answer(
        f"🎴 Казино-слот\n"
        f"Ставка: от {fmt_num(min_bet)} {cur}\n"
        f"{slots_text}\n"
        f"Введите сумму ставки:",
        reply_markup=slots_bet_kb())


def slots_rules_text():
    cur = db.get_setting("currency", config.CURRENCY)
    mult_7 = db.get_setting("mult_7", "5")
    mult_lem = db.get_setting("mult_lemon", "2")
    mult_bar = db.get_setting("mult_bar", "1.5")
    mult_cherry = db.get_setting("mult_cherry", "1")
    l = [
        "🎴 Тройки:",
        f"  7️⃣7️⃣7️⃣ ×{mult_7}",
        f"  🍋🍋🍋 ×{mult_lem}",
        f"  🎰🎰🎰 ×{mult_bar}",
        f"  🍒🍒🍒 ×{mult_cherry}",
        f"Каша (разные) = минус ставка",
    ]
    return "\n".join(l)


def slots_bet_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💎 Поставить всё", callback_data="slots_all")],
            [InlineKeyboardButton(text="↩️ В меню", callback_data="back_to_menu")],
        ]
    )


def slots_again_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎰 Играть ещё", callback_data="slots_again")],
            [InlineKeyboardButton(text="↩️ В меню", callback_data="back_to_menu")],
        ]
    )


@router.callback_query(F.data == "slots_all")
async def cq_slots_all(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    user = db.get_user(cb.from_user.id)
    if not user:
        await cb.message.answer("Сначала откройте меню командой /start.")
        await state.clear()
        return
    balance = user["balance"]
    min_bet = db.get_setting("roulette_min", config.DEFAULT_ROULETTE_MIN)
    if balance < min_bet:
        await cb.message.answer(
            f"Баланса не хватает даже на минимальную ставку "
            f"({fmt_num(min_bet)} {db.get_setting('currency', config.CURRENCY)})."
        )
        return
    await run_slots(cb.from_user.id, balance, state, cb.message)


@router.message(Slots.bet, F.text)
async def slots_bet(message: Message, state: FSMContext):
    try:
        amount = float(message.text.replace(",", "."))
    except ValueError:
        await message.answer("Введите число.")
        return
    cur = db.get_setting("currency", config.CURRENCY)
    min_bet = db.get_setting("roulette_min", config.DEFAULT_ROULETTE_MIN)
    if amount < min_bet:
        await message.answer(f"Минимальная ставка: {fmt_num(min_bet)} {cur}.")
        return
    await run_slots(message.from_user.id, amount, state, message)


async def run_slots(uid, amount, state, msg):
    user = db.get_user(uid)
    if not user:
        db.add_user(uid, msg.from_user.username or "", msg.from_user.first_name or "")
        user = db.get_user(uid)
    cur = db.get_setting("currency", config.CURRENCY)
    if user["balance"] < amount:
        await msg.answer("На балансе недостаточно голды.")
        await state.clear()
        return
    if not db.spend_balance(uid, amount):
        await msg.answer("На балансе недостаточно голды.")
        await state.clear()
        return
    await state.clear()

    combo = random_slots_result()
    mult = slots_combo_mult(combo)

    sticker_id = db.get_setting("slots_sticker", "")
    custom_emoji_id = db.get_setting("slots_custom_emoji", "")
    animation_sent = False
    if sticker_id:
        try:
            await bot.send_sticker(uid, sticker_id)
            animation_sent = True
        except Exception:
            pass
    elif custom_emoji_id:
        try:
            await bot.send_message(
                uid,
                "🎲",
                entities=[MessageEntity(type="custom_emoji", offset=0, length=1, custom_emoji_id=custom_emoji_id)],
            )
            animation_sent = True
        except Exception:
            pass
    if not animation_sent:
        await msg.answer("🎰")

    if mult <= 0 or combo not in ("seven", "lemon", "bar", "cherry"):
        await msg.answer(
            f"🍀 Каша! Выпало: {combo_emoji(combo)}\n"
            f"Ставка {fmt_num(amount)} {cur} потеряна.",
            reply_markup=slots_again_kb(),
        )
        return

    win = round(amount * mult, 2)
    db.add_balance(uid, win)
    await msg.answer(
        f"{combo_emoji(combo)} Выпало: {combo_emoji(combo)}!\n"
        f"Ставка: {fmt_num(amount)} {cur}\n"
        f"Множитель: ×{fmt_num(mult)}\n"
        f"Выигрыш: +{fmt_num(win)} {cur} 💰",
        reply_markup=slots_again_kb(),
    )


COMMBO_EMOJI = {
    "seven": "7️⃣",
    "lemon": "🍋",
    "bar": "🎰",
    "cherry": "🍒",
    "mush": "❓",
}


def combo_emoji(combo):
    return COMMBO_EMOJI.get(combo, "❓")


def random_slots_result():
    mush_prob = float(db.get_setting("prob_mush", 40))
    seven_prob = float(db.get_setting("prob_7", 2))
    lemon_prob = float(db.get_setting("prob_lemon", 8))
    bar_prob = float(db.get_setting("prob_bar", 15))
    cherry_prob = float(db.get_setting("prob_cherry", 35))
    total = mush_prob + seven_prob + lemon_prob + bar_prob + cherry_prob
    r = random.random() * total
    acc = 0.0
    acc += mush_prob
    if r <= acc:
        return "mush"
    acc += seven_prob
    if r <= acc:
        return "seven"
    acc += lemon_prob
    if r <= acc:
        return "lemon"
    acc += bar_prob
    if r <= acc:
        return "bar"
    return "cherry"


def slots_combo_mult(combo):
    if combo == "seven":
        return float(db.get_setting("mult_7", "5"))
    if combo == "lemon":
        return float(db.get_setting("mult_lemon", "2"))
    if combo == "bar":
        return float(db.get_setting("mult_bar", "1.5"))
    if combo == "cherry":
        return float(db.get_setting("mult_cherry", "1"))
    return 0


@router.callback_query(F.data == "slots_again")
async def cq_slots_again(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_state(Slots.bet)
    cur = db.get_setting("currency", config.CURRENCY)
    min_bet = db.get_setting("roulette_min", config.DEFAULT_ROULETTE_MIN)
    await cb.message.answer(
        f"🎴 Казино-слот\n"
        f"Ставка: от {fmt_num(min_bet)} {cur}\n"
        f"{slots_rules_text()}\n"
        f"Введите сумму ставки:",
        reply_markup=slots_bet_kb(),
    )



def roulette_bet_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💎 Поставить всё", callback_data="roulette_all")],
            [InlineKeyboardButton(text="↩️ В меню", callback_data="back_to_menu")],
        ]
    )


@router.message(Roulette.bet, F.text)
async def roulette_bet(message: Message, state: FSMContext):
    try:
        amount = float(message.text.replace(",", "."))
    except ValueError:
        await message.answer("Введите число.")
        return
    cur = db.get_setting("currency", config.CURRENCY)
    min_bet = db.get_setting("roulette_min", config.DEFAULT_ROULETTE_MIN)
    if amount < min_bet:
        await message.answer(f"Минимальная ставка: {fmt_num(min_bet)} {cur}.")
        return
    user = db.get_user(message.from_user.id)
    if not user:
        db.add_user(message.from_user.id, message.from_user.username or "", message.from_user.first_name or "")
        user = db.get_user(message.from_user.id)
    if user["balance"] < amount:
        await message.answer("На балансе недостаточно голды.")
        return

    await state.clear()
    last_bet[message.from_user.id] = amount
    text = play_roulette(message.from_user.id, amount)
    await message.answer(text, reply_markup=roulette_result_kb())


@router.callback_query(F.data == "roulette_all")
async def roulette_all(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    user = db.get_user(cb.from_user.id)
    if not user:
        await cb.message.answer("Сначала откройте меню командой /start.")
        await state.clear()
        return
    balance = user["balance"]
    min_bet = db.get_setting("roulette_min", config.DEFAULT_ROULETTE_MIN)
    if balance < min_bet:
        await cb.message.answer(
            f"Баланса не хватает даже на минимальную ставку "
            f"({fmt_num(min_bet)} {db.get_setting('currency', config.CURRENCY)})."
        )
        return
    await state.clear()
    last_bet[cb.from_user.id] = balance
    text = play_roulette(cb.from_user.id, balance)
    await cb.message.answer(text, reply_markup=roulette_result_kb())


def roulette_result_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Играть ещё (та же ставка)", callback_data="roulette_again")],
            [InlineKeyboardButton(text="🎰 Другая ставка", callback_data="roulette_change")],
            [InlineKeyboardButton(text="↩️ В меню", callback_data="back_to_menu")],
        ]
    )


def play_roulette(user_id, amount):
    cur = db.get_setting("currency", config.CURRENCY)
    chance = db.get_setting("roulette_chance", config.DEFAULT_ROULETTE_CHANCE)
    mult = db.get_setting("roulette_mult", config.DEFAULT_ROULETTE_MULT)
    win = random.randint(1, 100) <= int(chance)
    if not db.spend_balance(user_id, amount):
        return "На балансе недостаточно голды."
    if win:
        payout = amount * mult
        db.add_balance(user_id, payout)
        db.add_roulette_win(user_id)
        return (
            f"🎉 Вы выиграли!\n"
            f"Ставка: {fmt_num(amount)} {cur}\n"
            f"Выплата: +{fmt_num(payout)} {cur} 💰"
        )
    return (
        f"💸 Вы проиграли −{fmt_num(amount)} {cur}.\n"
        f"Не расстраивайтесь, попробуйте ещё раз!"
    )


@router.callback_query(F.data == "roulette_again")
async def cq_roulette_again(cb: CallbackQuery):
    await cb.answer()
    amount = last_bet.get(cb.from_user.id)
    if not amount:
        await cb.message.answer("Ставка не найдена. Сделайте новую ставку.")
        return
    user = db.get_user(cb.from_user.id)
    if not user:
        db.add_user(cb.from_user.id, cb.from_user.username or "", cb.from_user.first_name or "")
        user = db.get_user(cb.from_user.id)
    if user["balance"] < amount:
        await cb.message.answer("На балансе недостаточно голды.")
        return
    text = play_roulette(cb.from_user.id, amount)
    await cb.message.answer(text, reply_markup=roulette_result_kb())


@router.callback_query(F.data == "roulette_change")
async def cq_roulette_change(cb: CallbackQuery, state: FSMContext):
    await cq_roulette(cb, state)


@router.callback_query(F.data == "back_to_menu")
async def cq_back_to_menu(cb: CallbackQuery):
    await cb.answer()
    try:
        await cb.message.edit_text("Главное меню:", reply_markup=main_menu())
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            return
        raise
    except Exception:
        pass


def top_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="💰 По балансу", callback_data="top_bal"),
                InlineKeyboardButton(text="🎰 Победы в рулетке", callback_data="top_roulette"),
            ],
            [InlineKeyboardButton(text="👥 По рефералам", callback_data="top_refs")],
            [InlineKeyboardButton(text="↩️ В меню", callback_data="back_to_menu")],
        ]
    )


def render_top(rows, item_label, value_key):
    medals = ["🥇", "🥈", "🥉"]
    lines = [f"🏆 Топ — {item_label}", ""]
    for i, r in enumerate(rows):
        medal = medals[i] if i < len(medals) else f"{i + 1}."
        name = r["username"] or r["first_name"] or f"ID {r['id']}"
        display = f"@{name}" if r["username"] else name
        value = fmt_short(r[value_key])
        lines.append(f"{medal} {display} — {value}")
    return "\n".join(lines)


@router.callback_query(F.data == "top")
async def cq_top(cb: CallbackQuery):
    await cb.answer()
    await cb.message.edit_text(
        "🏆 Топ пользователей. Выберите категорию:", reply_markup=top_kb()
    )


@router.callback_query(F.data == "top_bal")
async def cq_top_bal(cb: CallbackQuery):
    await cb.answer()
    cur = db.get_setting("currency", config.CURRENCY)
    rows = db.top_balance(10)
    if not rows:
        await cb.message.answer("Пока пусто — начните зарабатывать голду!")
        return
    await cb.message.answer(
        render_top(rows, f"по балансу · {cur}", "balance")
    )


@router.callback_query(F.data == "top_roulette")
async def cq_top_roulette(cb: CallbackQuery):
    await cb.answer()
    rows = db.top_roulette(10)
    if not rows:
        await cb.message.answer("Пока никто не выигрывал в рулетке.")
        return
    await cb.message.answer(
        render_top(rows, "победы в рулетке", "roulette_wins")
    )


@router.callback_query(F.data == "top_refs")
async def cq_top_refs(cb: CallbackQuery):
    await cb.answer()
    rows = db.top_referrals(10)
    if not rows:
        await cb.message.answer("Пока никого не пригласили.")
        return
    await cb.message.answer(
        render_top(rows, "по рефералам", "refs")
    )


@router.callback_query(F.data.startswith("task_info:"))
async def cq_task_info(cb: CallbackQuery):
    task_id = int(cb.data.split(":", 1)[1])
    task = db.get_task(task_id)
    if not task or not task["active"]:
        await cb.answer("Задание не активно", show_alert=True)
        return
    cur = db.get_setting("currency", config.CURRENCY)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔗 Перейти в канал", url=f"https://t.me/{task['sponsor']}"),
                InlineKeyboardButton(text="✅ Проверить подписку", callback_data=f"task_check:{task_id}"),
            ],
            [InlineKeyboardButton(text="↩️ Назад", callback_data="earn_gold")],
        ]
    )
    await cb.message.edit_text(
        f"Задание #{task['id']}\n"
        f"Подпишитесь на канал: @{task['sponsor']}\n"
        f"Награда: {fmt_num(task['reward'])} {cur}\n\n"
        "После подписки нажмите «Проверить подписку».",
        reply_markup=kb)
    await cb.answer()


async def resolve_chat_id(sponsor):
    try:
        chat = await bot.get_chat(f"@{sponsor}")
        return chat.id
    except Exception:
        return None


@router.callback_query(F.data.startswith("task_check:"))
async def cq_task_check(cb: CallbackQuery):
    await cb.answer()
    task_id = int(cb.data.split(":", 1)[1])
    task = db.get_task(task_id)
    if not task or not task["active"]:
        await cb.answer("Задание не активно", show_alert=True)
        return
    user_id = cb.from_user.id
    cur = db.get_setting("currency", config.CURRENCY)
    chat_id = await resolve_chat_id(task["sponsor"])
    if not chat_id:
        await cb.message.answer(
            f"Не удалось проверить — бот не в канале @{task['sponsor']}.\n"
            "Добавьте бота в канал (как админа) и попробуйте снова."
        )
        return
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        subscribed = member.status in ("member", "administrator", "creator")
    except Exception:
        subscribed = False

    completion = db.get_completion(task_id, user_id)

    if not subscribed:
        db.set_completion_rewarded(task_id, user_id, False)
        await cb.message.answer(
            f"Вы не подписаны на @{task['sponsor']}.\n"
            "Подпишитесь и нажмите «Проверить подписку» снова."
        )
        return

    if completion and completion["rewarded"]:
        await cb.message.answer(
            f"Вы уже получили награду за это задание.\n"
            "Продолжайте подписку, чтобы награда осталась у вас."
        )
        return

    db.get_user(user_id)
    db.add_balance(user_id, task["reward"])
    db.add_completion(task_id, user_id)
    db.set_completion_rewarded(task_id, user_id, True)
    await cb.message.answer(
        f"🎉 Подписка подтверждена! Начислено: +{fmt_num(task['reward'])} {cur}.",
        reply_markup=main_menu())


async def check_subscriptions_loop():
    await asyncio.sleep(30)
    while True:
        try:
            completions = db.list_completions_rewarded()
            for comp in completions:
                task = db.get_task(comp["task_id"])
                if not task or not task["active"]:
                    continue
                user = db.get_user(comp["user_id"])
                if not user:
                    continue
                chat_id = await resolve_chat_id(task["sponsor"])
                if not chat_id:
                    continue
                try:
                    member = await bot.get_chat_member(chat_id, comp["user_id"])
                    subscribed = member.status in ("member", "administrator", "creator")
                except Exception:
                    continue
                if not subscribed:
                    db.spend_balance(comp["user_id"], task["reward"])
                    db.set_completion_rewarded(comp["task_id"], comp["user_id"], False)
                    cur = db.get_setting("currency", config.CURRENCY)
                    try:
                        await bot.send_message(
                            comp["user_id"],
                            f"❌ Вы отписались от @{task['sponsor']}.\n"
                            f"Списано: −{task['reward']} {cur} с вашего баланса.")
                    except Exception:
                        pass
        except Exception:
            logging.exception("check_subscriptions_loop error")
        await asyncio.sleep(180)


@router.callback_query(F.data == "balance")
async def cq_balance(cb: CallbackQuery):
    await cb.answer()
    user = db.get_user(cb.from_user.id)
    cur = db.get_setting("currency", config.CURRENCY)
    link = f"https://t.me/{config.BOT_USERNAME}?start={cb.from_user.id}"
    await cb.message.answer(
        f"👛 Ваш баланс: {fmt_num(user['balance'])} {cur}\n\n"
        f"🔗 Ваша реферальная ссылка:\n{link}\n"
        "Приглашайте друзей — получайте голду за их вступление и задания!"
    )


@router.callback_query(F.data == "start_withdraw")
async def cq_start_withdraw(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    if not _withdraw_enabled():
        await cb.message.answer(
            "🔒 Выводы временно приостановлены. Попробуйте позже."
        )
        return
    min_wd = db.get_setting("min_withdraw", config.DEFAULT_MIN_WITHDRAW)
    cur = db.get_setting("currency", config.CURRENCY)
    skin = db.get_setting("withdraw_skin", config.DEFAULT_SKIN)
    await state.set_state(Withdraw.price)
    await cb.message.answer(
        f"Вывод средств.\n\n"
        f"1️⃣ Выставьте на продажу этот скин:\n"
        f"🔫 {skin}\n\n"
        f"2️⃣ Укажите цену в {cur}.\n"
        f"Минимальная сумма: {min_wd} {cur}."
    )


@router.message(Withdraw.price, F.text)
async def wd_price(message: Message, state: FSMContext):
    if not _withdraw_enabled():
        await state.clear()
        await message.answer("🔒 Выводы временно приостановлены. Попробуйте позже.")
        return
    try:
        amount = float(message.text.replace(",", "."))
    except ValueError:
        await message.answer("Введите число.")
        return
    min_wd = db.get_setting("min_withdraw", config.DEFAULT_MIN_WITHDRAW)
    if amount < min_wd:
        await message.answer(f"Минимальная сумма вывода: {min_wd}.")
        return
    user = db.get_user(message.from_user.id)
    if user["balance"] < amount:
        await message.answer("На балансе недостаточно голды.")
        return
    skin = db.get_setting("withdraw_skin", config.DEFAULT_SKIN)
    await state.update_data(amount=amount, skin=skin)
    await state.set_state(Withdraw.screenshot)
    await message.answer("📸 Отправьте скриншот выставленного скина (фото):")


@router.message(Withdraw.screenshot, F.photo)
async def wd_screenshot(message: Message, state: FSMContext):
    if not _withdraw_enabled():
        await state.clear()
        await message.answer("🔒 Выводы временно приостановлены. Попробуйте позже.")
        return
    data = await state.update_data(screenshot=message.photo[-1].file_id)
    await state.clear()

    wd_id = db.add_withdrawal(
        message.from_user.id,
        data["amount"],
        "",
        skin=data["skin"],
        screenshot=data["screenshot"])
    wd = db.get_withdrawal(wd_id)

    wd_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Выплатить", callback_data=f"wd_approve:{wd_id}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"wd_reject:{wd_id}"),
            ]
        ]
    )
    for admin_id in config.ADMIN_IDS:
        try:
            await bot.send_photo(
                admin_id,
                photo=data["screenshot"],
                caption=format_wd(wd),
                reply_markup=wd_kb)
        except Exception:
            logging.exception("Admin notify failed for %s", admin_id)
    cur = db.get_setting("currency", config.CURRENCY)
    await message.answer(
        f"✅ Заявка на вывод №{wd_id} отправлена!\n"
        f"Цена: {fmt_num(data['amount'])} {cur}\n"
        f"Скин: {data['skin']}\n"
        "Ожидайте, мы выплатим вам в ближайшее время.",
        reply_markup=main_menu())


@router.message(Withdraw.screenshot)
async def wd_screenshot_other(message: Message):
    await message.answer("Отправьте именно фото — скриншот скина (кнопка 📎 → отправка фото).")


@router.callback_query(F.data.startswith("wd_"))
async def cq_wd_action(cb: CallbackQuery):
    action, wd_id = cb.data.split(":")
    cur = db.get_setting("currency", config.CURRENCY)

    try:
        wd = db.get_withdrawal(int(wd_id))
    except Exception:
        logging.exception("Failed to load withdrawal")
        wd = None

    if not wd:
        await cb.answer("Вывод не найден", show_alert=True)
        return
    if wd["status"] != "pending":
        await cb.answer("Вывод уже обработан", show_alert=True)
        return

    await cb.answer()
    chat_id = cb.message.chat.id
    msg_id = cb.message.message_id

    try:
        if action == "wd_approve":
            if not db.spend_balance(wd["user_id"], wd["amount"]):
                await cb.message.answer(
                    f"⚠️ У пользователя недостаточно средств для выплаты №{wd_id}."
                )
                return
            db.set_withdrawal_status(int(wd_id), "paid")
            status_text = "✅ ВЫПЛАЧЕН"
            user_note = (
                f"💰 Ваш вывод №{wd_id} ({fmt_num(wd['amount'])} {cur}) одобрен и выплачен!"
            )
        elif action == "wd_reject":
            choose_kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="💾 Оставить баланс",
                            callback_data=f"wd_rejkeep:{wd_id}"),
                        InlineKeyboardButton(
                            text="🗑 Обнулить баланс",
                            callback_data=f"wd_rejzero:{wd_id}"),
                    ]
                ]
            )
            await cb.message.answer(
                f"Вывод №{wd_id}: выберите вариант отклонения.",
                reply_markup=choose_kb)
            return
        elif action == "wd_rejkeep":
            db.set_withdrawal_status(int(wd_id), "rejected")
            status_text = "❌ ОТКЛОНЁН (баланс сохранён)"
            user_note = (
                f"❌ Ваш вывод №{wd_id} отклонён.\n"
                "Средства остались на вашем балансе."
            )
        else:
            db.set_balance(wd["user_id"], 0)
            db.set_withdrawal_status(int(wd_id), "rejected")
            status_text = "❌ ОТКЛОНЁН (баланс обнулён)"
            user_note = (
                f"❌ Ваш вывод №{wd_id} отклонён.\n"
                "Баланс обнулён. Свяжитесь с нами для уточнения."
            )
    except Exception:
        logging.exception("Failed to process withdrawal %s", wd_id)
        await cb.message.answer(f"⚠️ Ошибка при обработке вывода №{wd_id}. Смотри логи.")
        return

    try:
        await bot.edit_message_caption(
            chat_id, msg_id, caption=format_wd(wd) + f"\n\nСтатус: {status_text}"
        )
    except Exception:
        pass
    try:
        await bot.send_message(wd["user_id"], user_note)
    except Exception:
        logging.exception("User notify failed for %s", wd["user_id"])

    await cb.message.answer(f"Вывод №{wd_id}: {status_text}.")


async def activate_promo(user_id, code_text):
    code = code_text.strip().upper()
    cur = db.get_setting("currency", config.CURRENCY)
    pc = db.get_code(code)
    if not pc:
        return None, f"Промокод {code} не найден."
    max_uses = pc.get("max_uses") if pc.get("max_uses") is not None else 1
    if max_uses != 0 and pc["used"] >= max_uses:
        return None, f"Промокод {code} уже использован."
    if db.use_code(code, user_id):
        db.add_balance(user_id, pc["amount"])
        return pc, f"🎉 Промокод {code} активирован!\nНачислено: +{fmt_num(pc['amount'])} {cur} на баланс."
    return None, f"Промокод {code} уже использован вами."


@router.callback_query(F.data == "promo_menu")
async def cq_promo_menu(cb: CallbackQuery):
    await cb.answer()
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🎟 Активировать промокод", callback_data="start_promo"),
                InlineKeyboardButton(text="🛠 Создать свой промокод", callback_data="create_promo"),
            ],
            [InlineKeyboardButton(text="↩️ В меню", callback_data="back_to_menu")],
        ]
    )
    await cb.message.edit_text("🎁 Промокоды. Выберите действие:", reply_markup=kb)


@router.callback_query(F.data == "create_promo")
async def cq_create_promo(cb: CallbackQuery, state: FSMContext):
    cd = int(db.get_setting("promo_cd", config.DEFAULT_PROMO_CD))
    user = db.get_user(cb.from_user.id)
    last = int(user.get("promo_last_created") or 0)
    remaining = cd - (int(time.time()) - last)
    if remaining > 0:
        await cb.answer(f"⏳ Подождите {remaining} сек перед созданием промо", show_alert=True)
        return
    await state.set_state(CreatePromo.code)
    min_amount = db.get_setting("promo_min_amount", config.DEFAULT_PROMO_MIN_AMOUNT)
    min_uses = db.get_setting("promo_min_uses", config.DEFAULT_PROMO_MIN_USES)
    cur = db.get_setting("currency", config.CURRENCY)
    await cb.answer()
    await cb.message.answer(
        "🛠 Создание своего промокода.\n"
        f"Мин. сумма одной активации: {fmt_num(min_amount)} {cur}\n"
        f"Мин. активаций: {min_uses}\n"
        "С вас спишется: награда × количество активаций.\n\n"
        "Введите код (латиницей и цифрами, например: <code>MYGIFT</code>):"
    )


@router.message(CreatePromo.code, F.text)
async def cp_code(message: Message, state: FSMContext):
    code = message.text.strip().upper()
    if not re.fullmatch(r"[A-Z0-9]+", code):
        await message.answer("Код может содержать только латинские буквы и цифры (без пробелов).")
        return
    existing = db.get_code(code)
    if existing:
        await message.answer("Такой промокод уже существует. Введите другой:")
        return
    await state.update_data(cp_code=code)
    await state.set_state(CreatePromo.amount)
    await message.answer("Введите количество голды за одну активацию:")
    return


@router.message(CreatePromo.amount, F.text)
async def cp_amount(message: Message, state: FSMContext):
    try:
        amount = float(message.text.strip().replace(",", "."))
    except ValueError:
        await message.answer("Введите число.")
        return
    min_amount = db.get_setting("promo_min_amount", config.DEFAULT_PROMO_MIN_AMOUNT)
    cur = db.get_setting("currency", config.CURRENCY)
    if amount < min_amount:
        await message.answer(f"Минимум: {fmt_num(min_amount)} {cur} за одну активацию.")
        return
    await state.update_data(cp_amount=amount)
    await state.set_state(CreatePromo.uses)
    min_uses = db.get_setting("promo_min_uses", config.DEFAULT_PROMO_MIN_USES)
    await message.answer(f"Введите количество активаций (минимум {min_uses}):")
    return


@router.message(CreatePromo.uses, F.text)
async def cp_uses(message: Message, state: FSMContext):
    text = message.text.strip()
    min_uses = db.get_setting("promo_min_uses", config.DEFAULT_PROMO_MIN_USES)
    if not text.isdigit() or int(text) < min_uses:
        await message.answer(f"Минимум активаций: {min_uses}. Введите число от {min_uses}:")
        return
    uses = int(text)
    data = await state.get_data()
    amount = data["cp_amount"]
    code = data["cp_code"]
    cost = amount * uses
    cur = db.get_setting("currency", config.CURRENCY)
    user = db.get_user(message.from_user.id)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Подтвердить", callback_data="create_promo_ok"),
                InlineKeyboardButton(text="❌ Отменить", callback_data="create_promo_no"),
            ],
        ]
    )
    await state.update_data(cp_uses=uses, cp_cost=cost)
    await message.answer(
        "🧾 Подтверждение покупки промокода:\n"
        f"Код: <code>{code}</code>\n"
        f"Награда: {fmt_num(amount)} {cur} × {uses} активаций\n"
        f"💸 Стоимость: {fmt_num(cost)} {cur}\n"
        f"💳 Ваш баланс: {fmt_num(user['balance'])} {cur}\n\n"
        f"Списать {fmt_num(cost)} {cur} с баланса?",
        reply_markup=kb)
    return


@router.callback_query(F.data == "create_promo_ok")
async def cq_create_promo_ok(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    cost = data.get("cp_cost")
    if not cost:
        await cb.answer("Заполните форму заново", show_alert=True)
        return
    cur = db.get_setting("currency", config.CURRENCY)
    user = db.get_user(cb.from_user.id)
    if user["balance"] < cost:
        await cb.answer("❌ Недостаточно голды на балансе!", show_alert=True)
        return
    db.spend_balance(cb.from_user.id, cost)
    db.add_code(data["cp_code"], data["cp_amount"], max_uses=int(data["cp_uses"]), owner_id=cb.from_user.id)
    db.set_promo_last_created(cb.from_user.id, int(time.time()))
    promo_info[data["cp_code"]] = (data["cp_amount"], int(data["cp_uses"]), cb.from_user.id)
    await cb.answer()
    if is_admin(cb.from_user.id):
        result_kb = promo_result_kb(data["cp_code"])
    else:
        result_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="📤 Отправить промокод юзерам",
                        callback_data=f"promo_send:{data['cp_code']}")
                ],
                [
                    InlineKeyboardButton(
                        text="📋 Показать для копирования",
                        callback_data=f"promo_copy:{data['cp_code']}")
                ],
                [InlineKeyboardButton(text="↩️ В меню", callback_data="back_to_menu")],
            ]
        )
    await cb.message.answer(
        "✅ Промокод создан и оплачен!\n"
        + promo_code_text(
            data["cp_code"], data["cp_amount"], int(data["cp_uses"]), cur
        ),
        reply_markup=result_kb)


@router.callback_query(F.data == "create_promo_no")
async def cq_create_promo_no(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer()
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🎟 Активировать промокод", callback_data="start_promo"),
                InlineKeyboardButton(text="🛠 Создать свой промокод", callback_data="create_promo"),
            ],
            [InlineKeyboardButton(text="↩️ В меню", callback_data="back_to_menu")],
        ]
    )
    await cb.message.edit_text("🎁 Промокоды. Выберите действие:", reply_markup=kb)


@router.callback_query(F.data == "start_promo")
async def cq_start_promo(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_state(Promo.code)
    await cb.message.answer("Введите промокод:")


@router.message(Command("code"))
async def cmd_code(message: Message, command: CommandObject, state: FSMContext):
    if command.args:
        await state.clear()
        pc, text = await activate_promo(message.from_user.id, command.args)
        await message.answer(text)
        return
    await state.set_state(Promo.code)
    await message.answer("Введите промокод:")


@router.message(Promo.code, F.text)
async def promo_code(message: Message, state: FSMContext):
    await state.clear()
    pc, text = await activate_promo(message.from_user.id, message.text)
    await message.answer(text)


async def health(request):
    return web.Response(text="ok")


async def run_http():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "8080"))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    logging.info("HTTP server on port %s", port)


@router.message()
async def fc_waiter(message: Message):
    global _active_fc
    fc = _active_fc
    if fc is None:
        return
    if message.chat.type not in ("group", "supergroup"):
        return
    if fc["group_id"] != message.chat.id:
        return
    if not (message.text or message.caption):
        return
    if not message.from_user or message.from_user.is_bot:
        return
    if message.from_user.id in (777000, 666000):
        return
    if is_admin(message.from_user.id):
        return

    if _active_fc is not fc:
        return
    _active_fc = None
    await _fc_cancel_task(fc.get("task"))

    winner = message.from_user
    logging.info(
        "FC win: user=%s chat=%s post=%s amount=%s",
        winner.id, message.chat.id, fc["post_id"], fc["amount"])
    db.add_user(
        winner.id,
        winner.username,
        winner.first_name or "",
        None)
    try:
        db.add_balance(winner.id, fc["amount"])
    except Exception:
        try:
            await bot.send_message(
                fc["group_id"],
                "⚠️ Не удалось начислить голду — напишите админу.")
        except Exception:
            pass
        return

    cur = db.get_setting("currency", config.CURRENCY)
    if winner.username:
        nick = f"@{winner.username}"
    elif winner.first_name and winner.first_name != "Telegram":
        nick = winner.first_name
    else:
        nick = f"id{winner.id}"
    try:
        await bot.send_message(
            fc["group_id"],
            f"🥇 {nick} написал первым и забирает "
            f"<b>{fmt_num(fc['amount'])} {cur}</b>!\n\n📣 Победитель — {nick}!")
    except Exception:
        pass
    try:
        await bot.send_message(
            fc["channel_id"],
            f"🏆 Победитель фаст-коммента: {nick}\n"
            f"Начислено: {fmt_num(fc['amount'])} {cur}")
    except Exception:
        pass
    try:
        await bot.send_message(
            winner.id,
            f"🏆 <b>Поздравляем, вы выиграли фаст-коммент!</b>\n\n"
            f"Вы написали первым и получаете "
            f"<b>+{fmt_num(fc['amount'])} {cur}</b>.\n"
            f"Начислено на ваш баланс. Удачного дня! ✨")
    except Exception:
        pass


async def main():
    global bot
    db.init()
    session = AiohttpSession(proxy=config.PROXY) if config.PROXY else None
    bot = Bot(
        token=config.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        session=session)
    dp = Dispatcher()
    dp.include_router(router)
    await asyncio.gather(
        dp.start_polling(bot),
        run_http(),
        check_subscriptions_loop())


if __name__ == "__main__":
    asyncio.run(main())