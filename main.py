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


class Form(StatesGroup):
    name = State()
    phone = State()
    comment = State()


class Withdraw(StatesGroup):
    price = State()
    skin = State()
    screenshot = State()


class Promo(StatesGroup):
    code = State()


def main_menu():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📋 Подать заявку", callback_data="start_form")],
            [
                InlineKeyboardButton(text="👛 Мой баланс", callback_data="balance"),
                InlineKeyboardButton(text="💸 Вывести", callback_data="start_withdraw"),
            ],
            [InlineKeyboardButton(text="🎁 Активировать промокод", callback_data="start_promo")],
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


def format_request(req, reply_kb=False):
    user = db.get_user(req["user_id"])
    name = user["first_name"] if user else "?"
    username = f"@{user['username']}" if user and user["username"] else f"ID {req['user_id']}"
    return (
        f"Заявка №{req['id']}\n"
        f"Имя: {req['name']}\n"
        f"Телефон: {req['phone']}\n"
        f"Комментарий: {req['comment']}\n"
        f"Пользователь: {name} ({username})"
    )


async def notify_admin(text, reply_markup=None):
    for admin_id in config.ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text, reply_markup=reply_markup)
        except Exception:
            logging.exception("Admin notify failed for %s", admin_id)


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

    await message.answer(
        f"Привет, {user.first_name}!\n"
        "Оставляйте заявки, зарабатывайте валюту за приглашённых и выводите её.",
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
        f"Заявок всего: {s['all_requests']} (новых: {s['new_requests']})\n"
        f"Выводов в ожидании: {s['pending_wds']}\n\n"
        f"Награда за вступление: {db.get_setting('reward_join', config.DEFAULT_REWARD_JOIN)} "
        f"{db.get_setting('currency', config.CURRENCY)}\n"
        f"Награда за заявку: {db.get_setting('reward_form', config.DEFAULT_REWARD_FORM)} "
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


@router.message(Command("set_form"))
async def cmd_set_form(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    if not command.args or not command.args.isdigit():
        await message.answer("Использование: /set_form <сумма>")
        return
    db.set_setting("reward_form", int(command.args))
    await message.answer("Награда за заявку обновлена.")


@router.message(Command("requests"))
async def cmd_requests(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    status_map = {
        "new": "new",
        "approved": "approved",
        "rejected": "rejected",
    }
    status = None
    if command.args:
        status = status_map.get(command.args.strip().lower())
        if status is None:
            await message.answer(
                "Использование: /requests [new | approved | rejected]\n"
                "Без параметра — все заявки."
            )
            return
    rows = db.list_requests(status=status, limit=15)
    if not rows:
        await message.answer("Заявок не найдено.")
        return
    label = {
        "new": "🆕 НОВАЯ",
        "approved": "✅ ПРИНЯТА",
        "rejected": "❌ ОТКЛОНЕНА",
    }
    lines = []
    for r in rows:
        user = db.get_user(r["user_id"])
        uname = f"@{user['username']}" if user and user["username"] else f"ID {r['user_id']}"
        lines.append(
            f"#{r['id']} {label.get(r['status'], r['status'])} · {uname}\n"
            f"    👤 {r['name']} · ☎️ {r['phone']}\n"
            f"    💬 {r['comment'][:80]}\n"
            f"    🕒 {r['created_at'][:16]}"
        )
    await message.answer("📋 Заявки:\n\n" + "\n\n".join(lines))


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


@router.message(Command("addbal"))
async def cmd_addbal(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    args = command.args.split() if command.args else []
    if len(args) != 2 or not args[0].isdigit() or not args[1].isdigit():
        await message.answer("Использование: /addbal <id пользователя> <количество G>")
        return
    user = db.get_user(int(args[0]))
    if not user:
        await message.answer("Пользователь не найден.")
        return
    db.add_balance(int(args[0]), int(args[1]))
    cur = db.get_setting("currency", config.CURRENCY)
    await message.answer(
        f"Начислено +{args[1]} {cur} пользователю {user['first_name']} (ID {args[0]})."
    )


@router.message(Command("subbal"))
async def cmd_subbal(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    args = command.args.split() if command.args else []
    if len(args) != 2 or not args[0].isdigit() or not args[1].isdigit():
        await message.answer("Использование: /subbal <id пользователя> <количество G>")
        return
    user = db.get_user(int(args[0]))
    if not user:
        await message.answer("Пользователь не найден.")
        return
    db.spend_balance(int(args[0]), int(args[1]))
    cur = db.get_setting("currency", config.CURRENCY)
    await message.answer(
        f"Списано −{args[1]} {cur} у пользователя {user['first_name']} (ID {args[0]})."
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
        await message.answer("Использование: /withdraw <номер заявки>")
        return
    wd = db.get_withdrawal(int(command.args))
    if not wd:
        await message.answer("Заявка не найдена.")
        return
    wd_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton("✅ Выплатить", callback_data=f"wd_approve:{wd['id']}"),
                InlineKeyboardButton("❌ Отклонить", callback_data=f"wd_reject:{wd['id']}"),
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


@router.callback_query(F.data == "start_form")
async def cq_start_form(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_state(Form.name)
    await cb.message.answer("Введите ваше имя:")


@router.message(Form.name, F.text)
async def form_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text)
    await state.set_state(Form.phone)
    await message.answer("Введите ваш телефон:")


@router.message(Form.phone, F.text)
async def form_phone(message: Message, state: FSMContext):
    await state.update_data(phone=message.text)
    await state.set_state(Form.comment)
    await message.answer("Опишите задачу или отправьте «-»:")


@router.message(Form.comment, F.text)
async def form_comment(message: Message, state: FSMContext):
    data = await state.update_data(comment=message.text)
    await state.clear()

    req_id = db.add_request(
        message.from_user.id, data["name"], data["phone"], data["comment"]
    )
    req = db.get_request(req_id)

    user = db.get_user(message.from_user.id)
    if user["ref_id"] and user["ref_id"] != message.from_user.id:
        referrer = db.get_user(user["ref_id"])
        if referrer:
            reward = db.get_setting("reward_form", config.DEFAULT_REWARD_FORM)
            db.add_balance(user["ref_id"], reward)
            await bot.send_message(
                user["ref_id"],
                f"🎉 Ваш приглашённый отправил заявку №{req_id}!\n"
                f"Начислено: +{reward} {db.get_setting('currency', config.CURRENCY)}",
            )

    req_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton("✅ Принять", callback_data=f"req_approve:{req_id}"),
                InlineKeyboardButton("❌ Отклонить", callback_data=f"req_reject:{req_id}"),
            ]
        ]
    )
    await notify_admin(format_request(req), req_kb)
    await message.answer("✅ Заявка принята! Мы свяжемся с вами.", reply_markup=main_menu())


@router.callback_query(F.data == "balance")
async def cq_balance(cb: CallbackQuery):
    await cb.answer()
    user = db.get_user(cb.from_user.id)
    cur = db.get_setting("currency", config.CURRENCY)
    link = f"https://t.me/{config.BOT_USERNAME}?start={cb.from_user.id}"
    await cb.message.answer(
        f"👛 Ваш баланс: {user['balance']} {cur}\n\n"
        f"🔗 Ваша реферальная ссылка:\n{link}\n"
        "Приглашайте друзей — получайте валюту за их вступление и заявки!"
    )


@router.callback_query(F.data == "start_withdraw")
async def cq_start_withdraw(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    min_wd = db.get_setting("min_withdraw", config.DEFAULT_MIN_WITHDRAW)
    cur = db.get_setting("currency", config.CURRENCY)
    await state.set_state(Withdraw.price)
    await cb.message.answer(
        f"Вывод средств.\n"
        f"1️⃣ Укажите цену скина в {cur}.\n"
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
    await state.update_data(amount=amount)
    await state.set_state(Withdraw.skin)
    await message.answer("2️⃣ Введите название скина / паттерна:")


@router.message(Withdraw.skin, F.text)
async def wd_skin(message: Message, state: FSMContext):
    await state.update_data(skin=message.text)
    await state.set_state(Withdraw.screenshot)
    await message.answer("3️⃣ Отправьте скриншот скина (фото):")


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
                InlineKeyboardButton("✅ Выплатить", callback_data=f"wd_approve:{wd_id}"),
                InlineKeyboardButton("❌ Отклонить", callback_data=f"wd_reject:{wd_id}"),
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


@router.callback_query(F.data.startswith("req_"))
async def cq_req_action(cb: CallbackQuery):
    action, req_id = cb.data.split(":")
    req = db.get_request(int(req_id))
    if not req:
        await cb.answer("Заявка не найдена", show_alert=True)
        return

    if action == "req_approve":
        db.set_request_status(int(req_id), "approved")
        await cb.message.edit_text(format_request(req) + "\n\nСтатус: ✅ ПРИНЯТА")
        await bot.send_message(
            req["user_id"],
            f"✅ Ваша заявка №{req['id']} принята! Мы с вами свяжемся.",
        )
    else:
        db.set_request_status(int(req_id), "rejected")
        await cb.message.edit_text(format_request(req) + "\n\nСтатус: ❌ ОТКЛОНЕНА")
        await bot.send_message(
            req["user_id"],
            f"❌ Ваша заявка №{req['id']} отклонена. Попробуйте оставить новую.",
        )
    await cb.answer()


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
    await asyncio.gather(dp.start_polling(bot), run_http())


if __name__ == "__main__":
    asyncio.run(main())