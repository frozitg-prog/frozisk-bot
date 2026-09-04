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
    amount = State()
    details = State()


def main_menu():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📋 Подать заявку", callback_data="start_form")],
            [
                InlineKeyboardButton(text="👛 Мой баланс", callback_data="balance"),
                InlineKeyboardButton(text="💸 Вывести", callback_data="start_withdraw"),
            ],
        ]
    )


def is_admin(user_id):
    return user_id in config.ADMIN_IDS


def format_wd(wd):
    user = db.get_user(wd["user_id"])
    name = user["first_name"] if user else "?"
    username = f"@{user['username']}" if user and user["username"] else f"ID {wd['user_id']}"
    return (
        f"Заявка на вывод №{wd['id']}\n"
        f"Сумма: {wd['amount']} {db.get_setting('currency', config.CURRENCY)}\n"
        f"Реквизиты: {wd['details']}\n"
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
    await state.set_state(Withdraw.amount)
    await cb.message.answer(
        f"Сколько {cur} вы хотите вывести?\nМинимальная сумма: {min_wd} {cur}."
    )


@router.message(Withdraw.amount, F.text)
async def wd_amount(message: Message, state: FSMContext):
    try:
        amount = float(message.text.replace(",", "."))
    except ValueError:
        await message.answer("Введите число.")
        return
    min_wd = db.get_setting("min_withdraw", config.DEFAULT_MIN_WITHDRAW)
    if amount < min_wd:
        await message.answer(f"Минимальная сумма вывода: {min_wd}. Введите сумму ещё раз.")
        return
    user = db.get_user(message.from_user.id)
    if user["balance"] < amount:
        await message.answer("На балансе недостаточно средств.")
        return
    await state.update_data(amount=amount)
    await state.set_state(Withdraw.details)
    await message.answer("Укажите реквизиты для вывода (номер карты / кошелька):")


@router.message(Withdraw.details, F.text)
async def wd_details(message: Message, state: FSMContext):
    data = await state.update_data(details=message.text)
    await state.clear()

    wd_id = db.add_withdrawal(message.from_user.id, data["amount"], data["details"])
    wd = db.get_withdrawal(wd_id)

    wd_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton("✅ Выплатить", callback_data=f"wd_approve:{wd_id}"),
                InlineKeyboardButton("❌ Отклонить", callback_data=f"wd_reject:{wd_id}"),
            ]
        ]
    )
    await notify_admin(format_wd(wd), wd_kb)
    cur = db.get_setting("currency", config.CURRENCY)
    await message.answer(
        f"✅ Заявка на вывод {data['amount']} {cur} отправлена!\n"
        "Ожидайте, мы выплатим вам в ближайшее время.",
        reply_markup=main_menu(),
    )


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

    if action == "wd_approve":
        if not db.spend_balance(wd["user_id"], wd["amount"]):
            await cb.answer("Недостаточно средств у пользователя", show_alert=True)
            return
        db.set_withdrawal_status(int(wd_id), "paid")
        await cb.message.edit_text(format_wd(wd) + "\n\nСтатус: ✅ ВЫПЛАЧЕН")
        cur = db.get_setting("currency", config.CURRENCY)
        await bot.send_message(
            wd["user_id"],
            f"💰 Вывод {wd['amount']} {cur} одобрен и выплачен!\n"
            f"Реквизиты: {wd['details']}",
        )
    else:
        db.set_withdrawal_status(int(wd_id), "rejected")
        await cb.message.edit_text(format_wd(wd) + "\n\nСтатус: ❌ ОТКЛОНЁН")
        await bot.send_message(
            wd["user_id"],
            f"❌ Ваш вывод {wd['amount']} отклонён. Свяжитесь с нами для уточнения.",
        )
    await cb.answer()


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