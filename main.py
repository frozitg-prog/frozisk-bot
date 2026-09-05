import asyncio
import logging
import os

from aiohttp import web
from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import config
import database as db

logging.basicConfig(level=logging.INFO)

router = Router()
bot = None


class Withdraw(StatesGroup):
    price = State()
    screenshot = State()


class Promo(StatesGroup):
    code = State()


class AdminBalance(StatesGroup):
    amount = State()


def main_menu():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👛 Мой баланс", callback_data="balance"),
                InlineKeyboardButton(text="💸 Вывести", callback_data="start_withdraw"),
            ],
            [
                InlineKeyboardButton(text="🎁 Активировать промокод", callback_data="start_promo"),
                InlineKeyboardButton(text="💰 Заработать голду", callback_data="earn_gold"),
            ],
        ]
    )


def is_admin(user_id):
    return user_id in config.ADMIN_IDS


def format_wd(wd):
    user = db.get_user(wd["user_id"])
    name = user["first_name"] if user else "?"
    username = f"@{user['username']}" if user and user["username"] else f"ID {wd['user_id']}"
    cur = db.get_setting("currency", config.CURRENCY)
    return (
        f"Заявка на вывод №{wd['id']}\n"
        f"Цена: {wd['amount']} {cur}\n"
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
                f"Начислено: +{reward} {db.get_setting('currency', config.CURRENCY)}",
            )

    bonus = ""
    if is_new and ref_id:
        invite_bonus = db.get_setting("invite_bonus", 777)
        if invite_bonus and invite_bonus > 0:
            db.add_balance(user.id, invite_bonus)
            bonus = (
                f"\n🎁 Бонус за переход по реферальной ссылке: "
                f"+{invite_bonus} {db.get_setting('currency', config.CURRENCY)} зачислены на баланс!"
            )

    await message.answer(
        f"Привет, {user.first_name}!\n"
        "Зарабатывайте голду за приглашённых, выполняйте задания и выводите её."
        f"{bonus}",
        reply_markup=main_menu(),
    )


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    if not is_admin(message.from_user.id):
        return
    s = db.stats()
    await message.answer(
        f"📊 Статистика\n"
        f"Пользователей: {s['users']}\n"
        f"Выводов в ожидании: {s['pending_wds']}\n\n"
        f"Награда за вступление: {db.get_setting('reward_join', config.DEFAULT_REWARD_JOIN)} "
        f"{db.get_setting('currency', config.CURRENCY)}\n"
        f"Мин. вывод: {db.get_setting('min_withdraw', config.DEFAULT_MIN_WITHDRAW)} "
        f"{db.get_setting('currency', config.CURRENCY)}"
    )


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
    if len(args) != 2 or not args[1].isdigit():
        await message.answer("Использование: /add_code <КОД> <количество голды>")
        return
    code = args[0].upper()
    amount = int(args[1])
    db.add_code(code, amount)
    cur = db.get_setting("currency", config.CURRENCY)
    await message.answer(f"Промокод {code} создан на {amount} {cur}.")


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
        state = "✅ использован (x" + str(r["used_by"]) + ")" if r["used"] else "🆕 активен"
        lines.append(f"{r['code']} — {r['amount']} {cur} — {state}")
    await message.answer("🎁 Промокоды:\n" + "\n".join(lines))


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
        f"Баланс: {user['balance']} {cur}\n"
        f"Пригласил: {referrals} чел.\n"
        f"Пришёл по реф.: {user['ref_id'] if user['ref_id'] else '—'}\n"
        f"🔗 {link}\n"
        f"Дата регистрации: {user['created_at'][:16]}",
        reply_markup=kb,
    )


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
    cur = db.get_setting("currency", config.CURRENCY)
    await state.clear()
    await message.answer(
        f"➕ Начислено +{amount} {cur} пользователю {user['first_name'] if user else target} "
        f"(ID {target}).\nТекущий баланс: {user['balance'] + amount} {cur}."
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
            f"#{r['id']} {label.get(r['status'], r['status'])} · {r['amount']} {cur} · "
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
            f"#{r['id']} {label.get(r['status'], r['status'])} · {r['amount']} {cur} · "
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
    await message.answer(
        f"Начислено +{args[1]} {cur} пользователю {user['first_name']}"
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
        f"Списано −{args[1]} {cur} у пользователя {user['first_name']}"
        f" (@{user['username'] or user['id']})."
    )


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
            f"#{r['id']} {label.get(r['status'], r['status'])} · {r['amount']} {cur}\n"
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
            reply_markup=wd_kb,
        )
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
        f"Задание #{task_id} создано: подписка на @{sponsor} = {reward} {cur}.\n"
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
        lines.append(f"#{t['id']} @{t['sponsor']} — {t['reward']} {cur} — {state}")
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
        reply_markup=kb,
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
                InlineKeyboardButton(text="Перейти в канал", url=f"https://t.me/{task['sponsor']}"),
                InlineKeyboardButton(text="✅ Проверить подписку", callback_data=f"task_check:{task_id}"),
            ],
            [InlineKeyboardButton(text="↩️ Назад", callback_data="earn_gold")],
        ]
    )
    await cb.message.edit_text(
        f"Задание #{task['id']}\n"
        f"Подпишитесь на канал: @{task['sponsor']}\n"
        f"Награда: {task['reward']} {cur}\n\n"
        "После подписки нажмите «Проверить подписку».",
        reply_markup=kb,
    )
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
        f"🎉 Подписка подтверждена! Начислено: +{task['reward']} {cur}.",
        reply_markup=main_menu(),
    )


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
                            f"Списано: −{task['reward']} {cur} с вашего баланса.",
                        )
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
        f"👛 Ваш баланс: {user['balance']} {cur}\n\n"
        f"🔗 Ваша реферальная ссылка:\n{link}\n"
        "Приглашайте друзей — получайте голду за их вступление и задания!"
    )


@router.callback_query(F.data == "start_withdraw")
async def cq_start_withdraw(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
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
    data = await state.update_data(screenshot=message.photo[-1].file_id)
    await state.clear()

    wd_id = db.add_withdrawal(
        message.from_user.id,
        data["amount"],
        "",
        skin=data["skin"],
        screenshot=data["screenshot"],
    )
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
                reply_markup=wd_kb,
            )
        except Exception:
            logging.exception("Admin notify failed for %s", admin_id)
    cur = db.get_setting("currency", config.CURRENCY)
    await message.answer(
        f"✅ Заявка на вывод №{wd_id} отправлена!\n"
        f"Цена: {data['amount']} {cur}\n"
        f"Скин: {data['skin']}\n"
        "Ожидайте, мы выплатим вам в ближайшее время.",
        reply_markup=main_menu(),
    )


@router.message(Withdraw.screenshot)
async def wd_screenshot_other(message: Message):
    await message.answer("Отправьте именно фото — скриншот скина (кнопка 📎 → отправка фото).")


@router.callback_query(F.data.startswith("wd_"))
async def cq_wd_action(cb: CallbackQuery):
    action, wd_id = cb.data.split(":")
    wd = db.get_withdrawal(int(wd_id))
    if not wd:
        await cb.answer("Заявка не найдена", show_alert=True)
        return

    chat_id = cb.message.chat.id
    msg_id = cb.message.message_id

    if action == "wd_approve":
        if not db.spend_balance(wd["user_id"], wd["amount"]):
            await cb.answer("Недостаточно средств у пользователя", show_alert=True)
            return
        db.set_withdrawal_status(int(wd_id), "paid")
        caption = format_wd(wd) + "\n\nСтатус: ✅ ВЫПЛАЧЕН"
        try:
            await bot.edit_message_caption(chat_id, msg_id, caption=caption)
        except Exception:
            await cb.message.edit_text(caption)
        cur = db.get_setting("currency", config.CURRENCY)
        await bot.send_message(
            wd["user_id"],
            f"💰 Ваш вывод №{wd_id} ({wd['amount']} {cur}) одобрен и выплачен!",
        )
    else:
        db.set_withdrawal_status(int(wd_id), "rejected")
        caption = format_wd(wd) + "\n\nСтатус: ❌ ОТКЛОНЁН"
        try:
            await bot.edit_message_caption(chat_id, msg_id, caption=caption)
        except Exception:
            await cb.message.edit_text(caption)
        await bot.send_message(
            wd["user_id"],
            f"❌ Ваш вывод №{wd_id} отклонён. Свяжитесь с нами для уточнения.",
        )
    await cb.answer()


async def activate_promo(user_id, code_text):
    code = code_text.strip().upper()
    cur = db.get_setting("currency", config.CURRENCY)
    pc = db.get_code(code)
    if not pc or pc["used"]:
        return None, f"Промокод {code} не найден или уже использован."
    if db.use_code(code, user_id):
        db.add_balance(user_id, pc["amount"])
        return pc, f"🎉 Промокод {code} активирован!\nНачислено: +{pc['amount']} {cur} на баланс."
    return None, f"Промокод {code} не найден или уже использован."


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


async def main():
    global bot
    db.init()
    session = AiohttpSession(proxy=config.PROXY) if config.PROXY else None
    bot = Bot(
        token=config.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        session=session,
    )
    dp = Dispatcher()
    dp.include_router(router)
    await asyncio.gather(
        dp.start_polling(bot),
        run_http(),
        check_subscriptions_loop(),
    )


if __name__ == "__main__":
    asyncio.run(main())