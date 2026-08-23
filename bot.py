import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    FSInputFile
)
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage

import database as db
from downloader import download, remove_file, MAX_MB
from music import search_music

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "8464597898"))
CURRENCY = os.getenv("CURRENCY", "₽")
MIN_WITHDRAW = float(os.getenv("MIN_WITHDRAW", "500"))

if not BOT_TOKEN or BOT_TOKEN.startswith("PASTE_"):
    raise RuntimeError("Укажите BOT_TOKEN в .env")

bot = Bot(BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

class UserStates(StatesGroup):
    waiting_url = State()
    waiting_music = State()
    waiting_wallet = State()
    waiting_withdraw_amount = State()

class AdminStates(StatesGroup):
    salary_user = State()
    salary_amount = State()
    salary_reason = State()
    search_user = State()
    min_withdraw = State()

def is_admin(tg_id: int) -> bool:
    return tg_id == OWNER_ID

def main_kb(admin=False):
    rows = [
        [InlineKeyboardButton(text="⬇️ Скачать видео", callback_data="download")],
        [InlineKeyboardButton(text="🎵 Поиск музыки", callback_data="music")],
        [InlineKeyboardButton(text="👤 Мой профиль", callback_data="profile")],
        [InlineKeyboardButton(text="💸 Вывести", callback_data="withdraw")],
    ]
    if admin:
        rows.append([InlineKeyboardButton(text="🛠 Админ-панель", callback_data="admin")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def profile_text(u):
    return (
        "👤 <b>Мой профиль</b>\n\n"
        f"💰 Баланс: <b>{u['balance']:.2f} {CURRENCY}</b>\n"
        f"💵 Получено зарплатой: <b>{u['total_salary']:.2f} {CURRENCY}</b>\n"
        f"👛 Кошелёк: <code>{u['wallet'] or 'не указан'}</code>\n\n"
        f"Минимальный вывод: <b>{MIN_WITHDRAW:.2f} {CURRENCY}</b>"
    )

def admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Пользователи", callback_data="users")],
        [InlineKeyboardButton(text="💰 Выдать зарплату", callback_data="salary")],
        [InlineKeyboardButton(text="💸 Заявки на вывод", callback_data="withdrawals")],
        [InlineKeyboardButton(text="🔎 Найти пользователя", callback_data="find_user")],
        [InlineKeyboardButton(text="⚙️ Мин. вывод", callback_data="set_min")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="stats")],
        [InlineKeyboardButton(text="◀️ Главное меню", callback_data="home")],
    ])

@dp.message(CommandStart())
async def start(message: Message):
    db.upsert_user(message.from_user)
    await message.answer(
        "👋 <b>Добро пожаловать!</b>\n\n"
        "Отправляй ссылку на видео — бот попробует скачать его.\n"
        "Также здесь есть музыка, профиль и вывод средств.",
        reply_markup=main_kb(is_admin(message.from_user.id))
    )

@dp.message(Command("id"))
async def my_id(message: Message):
    await message.answer(f"Ваш Telegram ID: <code>{message.from_user.id}</code>")

@dp.callback_query(F.data == "home")
async def home(call: CallbackQuery):
    await call.message.edit_text("🏠 <b>Главное меню</b>", reply_markup=main_kb(is_admin(call.from_user.id)))
    await call.answer()

@dp.callback_query(F.data == "profile")
async def profile(call: CallbackQuery):
    db.upsert_user(call.from_user)
    await call.message.edit_text(profile_text(db.get_user(call.from_user.id)), reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👛 Изменить кошелёк", callback_data="wallet")],
            [InlineKeyboardButton(text="💸 Вывести", callback_data="withdraw")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="home")]
        ]))
    await call.answer()

@dp.callback_query(F.data == "download")
async def download_start(call: CallbackQuery, state: FSMContext):
    await state.set_state(UserStates.waiting_url)
    await call.message.answer("🔗 Отправь ссылку на видео.\n\nНапример: TikTok / YouTube / Instagram.")
    await call.answer()

@dp.message(UserStates.waiting_url)
async def download_video(message: Message, state: FSMContext):
    url = (message.text or "").strip()
    await state.clear()
    if not url.startswith(("http://", "https://")):
        await message.answer("❌ Это не похоже на ссылку.")
        return
    status = await message.answer("⏳ Скачиваю видео...")
    path = None
    try:
        path, info = await download(url)
        if not path.exists():
            raise RuntimeError("Файл не найден после загрузки.")
        size_mb = path.stat().st_size / 1024 / 1024
        if size_mb > MAX_MB:
            raise RuntimeError(f"Видео весит {size_mb:.1f} МБ, лимит бота — {MAX_MB} МБ.")
        await status.edit_text("📤 Отправляю видео...")
        caption = f"🎬 {info.get('title', 'Видео')[:700]}"
        await message.answer_video(FSInputFile(path), caption=caption)
        await status.delete()
    except Exception as e:
        await status.edit_text(f"❌ Не удалось скачать видео.\n\n<code>{str(e)[:1000]}</code>")
    finally:
        if path:
            remove_file(path)

@dp.callback_query(F.data == "music")
async def music_start(call: CallbackQuery, state: FSMContext):
    await state.set_state(UserStates.waiting_music)
    await call.message.answer("🎵 Напиши название трека или исполнителя.")
    await call.answer()

@dp.message(UserStates.waiting_music)
async def music_search(message: Message, state: FSMContext):
    await state.clear()
    q = (message.text or "").strip()
    if not q:
        return
    try:
        results = await search_music(q)
    except Exception:
        await message.answer("❌ Сервис поиска музыки временно недоступен.")
        return
    if not results:
        await message.answer("Ничего не найдено.")
        return
    chunks = ["🎵 <b>Результаты поиска</b>\n"]
    for i, x in enumerate(results, 1):
        name = x.get("trackName", "Без названия")
        artist = x.get("artistName", "Неизвестный исполнитель")
        link = x.get("trackViewUrl", "")
        chunks.append(f"{i}. <b>{artist}</b> — {name}\n{link}")
    await message.answer("\n\n".join(chunks))

@dp.callback_query(F.data == "wallet")
async def wallet_start(call: CallbackQuery, state: FSMContext):
    await state.set_state(UserStates.waiting_wallet)
    await call.message.answer("👛 Отправь реквизит кошелька, на который хочешь получать выплаты.")
    await call.answer()

@dp.message(UserStates.waiting_wallet)
async def wallet_save(message: Message, state: FSMContext):
    await state.clear()
    wallet = (message.text or "").strip()[:255]
    db.set_wallet(message.from_user.id, wallet)
    await message.answer("✅ Кошелёк сохранён.", reply_markup=main_kb(is_admin(message.from_user.id)))

@dp.callback_query(F.data == "withdraw")
async def withdraw_start(call: CallbackQuery, state: FSMContext):
    u = db.get_user(call.from_user.id)
    if not u:
        db.upsert_user(call.from_user)
        u = db.get_user(call.from_user.id)
    if not u["wallet"]:
        await call.message.answer("Сначала укажи кошелёк в профиле.")
        await call.answer()
        return
    if u["balance"] < MIN_WITHDRAW:
        await call.message.answer(f"❌ Для вывода нужно минимум {MIN_WITHDRAW:.2f} {CURRENCY}.")
        await call.answer()
        return
    await state.set_state(UserStates.waiting_withdraw_amount)
    await call.message.answer(f"💸 Сколько вывести? Доступно: {u['balance']:.2f} {CURRENCY}")
    await call.answer()

@dp.message(UserStates.waiting_withdraw_amount)
async def withdraw_amount(message: Message, state: FSMContext):
    await state.clear()
    try:
        amount = float((message.text or "").replace(",", "."))
    except ValueError:
        await message.answer("❌ Введи сумму числом.")
        return
    u = db.get_user(message.from_user.id)
    if amount < MIN_WITHDRAW or amount > u["balance"]:
        await message.answer("❌ Некорректная сумма.")
        return
    wid = db.create_withdrawal(message.from_user.id, amount, u["wallet"])
    if not wid:
        await message.answer("❌ Не удалось создать заявку.")
        return
    await message.answer(f"✅ Заявка #{wid} создана. После проверки владелец обработает выплату.")
    await bot.send_message(
        OWNER_ID,
        f"💸 <b>Новая заявка на вывод #{wid}</b>\n\n"
        f"👤 ID: <code>{message.from_user.id}</code>\n"
        f"Username: @{message.from_user.username or 'нет'}\n"
        f"💰 Сумма: <b>{amount:.2f} {CURRENCY}</b>\n"
        f"👛 Кошелёк: <code>{u['wallet']}</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Одобрить", callback_data=f"approve:{wid}"),
             InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{wid}")]
        ])
    )

@dp.callback_query(F.data.startswith(("approve:", "reject:")))
async def process_withdrawal(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа.", show_alert=True)
        return
    action, wid_s = call.data.split(":")
    wid = int(wid_s)
    row = db.get_withdrawal(wid)
    if not row or row["status"] != "pending":
        await call.answer("Заявка уже обработана.", show_alert=True)
        return
    status = "approved" if action == "approve" else "rejected"
    db.process_withdrawal(wid, status, call.from_user.id)
    text = "✅ Выплата одобрена." if status == "approved" else "❌ Выплата отклонена, сумма возвращена на баланс."
    await call.message.edit_text(call.message.text + f"\n\n<b>{text}</b>")
    await bot.send_message(row["telegram_id"], text)
    await call.answer("Готово.")

@dp.callback_query(F.data == "admin")
async def admin(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа.", show_alert=True); return
    await call.message.edit_text("🛠 <b>Админ-панель</b>", reply_markup=admin_kb())
    await call.answer()

@dp.callback_query(F.data == "users")
async def users(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    rows = db.list_users(50, 0)
    if not rows:
        text = "Пользователей пока нет."
    else:
        text = f"👥 <b>Пользователи</b> — всего {db.count_users()}\n\n"
        for i, u in enumerate(rows, 1):
            name = u["first_name"] or "Без имени"
            username = f"@{u['username']}" if u["username"] else "без username"
            text += f"{i}. {name} {username}\nID: <code>{u['telegram_id']}</code> | {u['balance']:.2f} {CURRENCY}\n\n"
    await call.message.edit_text(text[:3900], reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="◀️ Админ-панель", callback_data="admin")]]))
    await call.answer()

@dp.callback_query(F.data == "stats")
async def stats(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    s = db.stats()
    await call.message.edit_text(
        f"📊 <b>Статистика</b>\n\n"
        f"👥 Пользователей: <b>{s['users']}</b>\n"
        f"💰 Балансы пользователей: <b>{s['balances']:.2f} {CURRENCY}</b>\n"
        f"💵 Всего выдано зарплат: <b>{s['salaries']:.2f} {CURRENCY}</b>",
        reply_markup=admin_kb())
    await call.answer()

@dp.callback_query(F.data == "withdrawals")
async def withdrawals(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    rows = db.list_pending_withdrawals()
    if not rows:
        await call.message.edit_text("💸 Нет ожидающих заявок.", reply_markup=admin_kb())
        await call.answer(); return
    for row in rows:
        await call.message.answer(
            f"💸 <b>Заявка #{row['id']}</b>\n"
            f"ID: <code>{row['telegram_id']}</code>\n"
            f"Сумма: <b>{row['amount']:.2f} {CURRENCY}</b>\n"
            f"Кошелёк: <code>{row['wallet']}</code>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Одобрить", callback_data=f"approve:{row['id']}"),
                 InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{row['id']}")]
            ])
        )
    await call.answer()

@dp.callback_query(F.data == "salary")
async def salary_start(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id): return
    await state.set_state(AdminStates.salary_user)
    await call.message.answer("💰 Введи Telegram ID пользователя, которому выдать зарплату.")
    await call.answer()

@dp.message(AdminStates.salary_user)
async def salary_user(message: Message, state: FSMContext):
    try:
        tg_id = int(message.text.strip())
    except ValueError:
        await message.answer("❌ ID должен быть числом."); return
    if not db.get_user(tg_id):
        await message.answer("❌ Пользователь не найден."); return
    await state.update_data(tg_id=tg_id)
    await state.set_state(AdminStates.salary_amount)
    await message.answer("Сколько выдать?")

@dp.message(AdminStates.salary_amount)
async def salary_amount(message: Message, state: FSMContext):
    try:
        amount = float(message.text.replace(",", "."))
    except ValueError:
        await message.answer("❌ Сумма должна быть числом."); return
    if amount <= 0:
        await message.answer("❌ Сумма должна быть больше нуля."); return
    await state.update_data(amount=amount)
    await state.set_state(AdminStates.salary_reason)
    await message.answer("За что выдана зарплата? Например: <i>Работа над проектом</i>.")

@dp.message(AdminStates.salary_reason)
async def salary_reason(message: Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    reason = (message.text or "").strip()[:500]
    db.add_salary(data["tg_id"], data["amount"], reason, message.from_user.id)
    await message.answer(
        f"✅ Выдано <b>{data['amount']:.2f} {CURRENCY}</b>\nПричина: {reason}"
    )
    await bot.send_message(
        data["tg_id"],
        f"💰 <b>Вам начислена зарплата</b>\n\n"
        f"Сумма: <b>{data['amount']:.2f} {CURRENCY}</b>\n"
        f"Причина: {reason}\n\n"
        f"Текущий баланс: <b>{db.get_user(data['tg_id'])['balance']:.2f} {CURRENCY}</b>"
    )

@dp.callback_query(F.data == "find_user")
async def find_user_start(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id): return
    await state.set_state(AdminStates.search_user)
    await call.message.answer("🔎 Введи Telegram ID пользователя.")
    await call.answer()

@dp.message(AdminStates.search_user)
async def find_user(message: Message, state: FSMContext):
    await state.clear()
    try:
        tg_id = int(message.text.strip())
    except ValueError:
        await message.answer("❌ ID должен быть числом."); return
    u = db.get_user(tg_id)
    if not u:
        await message.answer("❌ Пользователь не найден."); return
    await message.answer(
        f"👤 <b>Пользователь</b>\n\n"
        f"ID: <code>{u['telegram_id']}</code>\n"
        f"Имя: {u['first_name']}\n"
        f"Username: @{u['username'] or 'нет'}\n"
        f"Баланс: <b>{u['balance']:.2f} {CURRENCY}</b>\n"
        f"Зарплатами: <b>{u['total_salary']:.2f} {CURRENCY}</b>\n"
        f"Кошелёк: <code>{u['wallet'] or 'не указан'}</code>"
    )

@dp.callback_query(F.data == "set_min")
async def set_min(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id): return
    await state.set_state(AdminStates.min_withdraw)
    await call.message.answer(f"⚙️ Текущий минимум: {MIN_WITHDRAW:.2f} {CURRENCY}\n\nВведи новую сумму.\n\nИзменение действует после перезапуска бота.")
    await call.answer()

@dp.message(AdminStates.min_withdraw)
async def set_min_value(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "⚠️ В текущей версии минимум задаётся в `.env` через `MIN_WITHDRAW`.\n"
        "Измени значение и перезапусти бота."
    )

async def main():
    db.init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
