import asyncio
import csv
import html
import logging
import os
import re
from datetime import date, datetime, timedelta

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))  # Telegram ID владельца, куда приходят заявки
CSV_FILE = "bookings.csv"

# ---- Настройки, которые меняются под клиента ----
SERVICES = ["Консультация", "Стандартная услуга", "Расширенная услуга"]
TIME_SLOTS = ["10:00", "11:00", "12:00", "13:00", "14:00", "15:00", "16:00", "17:00", "18:00"]
DAYS_AHEAD = 7  # на сколько дней вперёд можно записаться (начиная с завтра)
# --------------------------------------------------

WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
MONTHS = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]

router = Router()


class Booking(StatesGroup):
    name = State()
    service = State()
    day = State()
    time = State()
    phone = State()
    confirm = State()


# ---------- Вспомогательные функции ----------

def available_days() -> list[date]:
    today = datetime.now().date()
    return [today + timedelta(days=i) for i in range(1, DAYS_AHEAD + 1)]


def day_label(d: date) -> str:
    return f"{WEEKDAYS[d.weekday()]}, {d.day} {MONTHS[d.month - 1]}"


def get_booked_times(day_iso: str) -> set[str]:
    """Какое время на этот день уже занято (читаем из CSV)."""
    if not os.path.exists(CSV_FILE):
        return set()
    booked = set()
    with open(CSV_FILE, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if row.get("date") == day_iso and row.get("time"):
                booked.add(row["time"])
    return booked


def free_slots(day_iso: str) -> list[str]:
    booked = get_booked_times(day_iso)
    return [t for t in TIME_SLOTS if t not in booked]


def normalize_phone(raw: str, from_contact: bool = False) -> str | None:
    """Возвращает номер в виде +7 999 123-45-67 или None, если номер неверный."""
    digits = re.sub(r"\D", "", raw)
    if not from_contact and not re.fullmatch(r"\+?[\d\s\-()]+", raw):
        return None
    if len(digits) == 10 and digits[0] == "9":
        digits = "7" + digits
    if len(digits) == 11 and digits[0] in "78":
        d = "7" + digits[1:]
        return f"+7 {d[1:4]} {d[4:7]}-{d[7:9]}-{d[9:11]}"
    if from_contact and 10 <= len(digits) <= 15:
        return f"+{digits}"  # контакт из Telegram, номер не из РФ
    return None


def save_booking(data: dict, user_id: int) -> None:
    new_file = not os.path.exists(CSV_FILE)
    with open(CSV_FILE, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        if new_file:
            writer.writerow(["created", "user_id", "name", "service", "date", "time", "phone"])
        writer.writerow(
            [
                datetime.now().isoformat(timespec="seconds"),
                user_id,
                data["name"],
                data["service"],
                data["day"],
                data["time"],
                data["phone"],
            ]
        )


def booking_text(data: dict) -> str:
    return (
        f"Имя: {html.escape(data['name'])}\n"
        f"Услуга: {html.escape(data['service'])}\n"
        f"Дата и время: {html.escape(data['when'])}\n"
        f"Телефон: {html.escape(data['phone'])}"
    )


# ---------- Клавиатуры ----------

def start_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Записаться", callback_data="book")]]
    )


def services_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=name, callback_data=f"svc:{i}")]
        for i, name in enumerate(SERVICES)
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def days_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(text=day_label(d), callback_data=f"day:{d.isoformat()}")
        for d in available_days()
    ]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def times_keyboard(slots: list[str]) -> InlineKeyboardMarkup:
    buttons = [InlineKeyboardButton(text=t, callback_data=f"time:{t}") for t in slots]
    rows = [buttons[i : i + 3] for i in range(0, len(buttons), 3)]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def phone_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Отправить мой номер", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Подтвердить", callback_data="ok"),
                InlineKeyboardButton(text="Отменить", callback_data="no"),
            ]
        ]
    )


# ---------- Обработчики ----------

async def begin_booking(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(Booking.name)
    await message.answer("Как вас зовут?")


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "Запись отменена. Чтобы начать заново, нажмите /start",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "Здравствуйте! Здесь можно записаться на приём.",
        reply_markup=start_keyboard(),
    )


@router.message(Command("book"))
async def book_command(message: Message, state: FSMContext) -> None:
    await begin_booking(message, state)


@router.callback_query(F.data == "book")
async def book_button(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await begin_booking(callback.message, state)


@router.message(StateFilter(Booking.name), F.text)
async def get_name(message: Message, state: FSMContext) -> None:
    name = message.text.strip()
    if not 2 <= len(name) <= 60:
        await message.answer("Введите имя от 2 до 60 символов.")
        return
    await state.update_data(name=name)
    await state.set_state(Booking.service)
    await message.answer("Выберите услугу:", reply_markup=services_keyboard())


@router.callback_query(StateFilter(Booking.service), F.data.startswith("svc:"))
async def get_service(callback: CallbackQuery, state: FSMContext) -> None:
    index = int(callback.data.split(":", 1)[1])
    if not 0 <= index < len(SERVICES):
        await callback.answer("Выберите услугу из списка", show_alert=True)
        return
    await callback.answer()
    await state.update_data(service=SERVICES[index])
    await state.set_state(Booking.day)
    await callback.message.answer("Выберите день:", reply_markup=days_keyboard())


@router.callback_query(StateFilter(Booking.day), F.data.startswith("day:"))
async def get_day(callback: CallbackQuery, state: FSMContext) -> None:
    day_iso = callback.data.split(":", 1)[1]
    if day_iso not in {d.isoformat() for d in available_days()}:
        await callback.answer("Выберите день из списка", show_alert=True)
        return
    slots = free_slots(day_iso)
    if not slots:
        await callback.answer("На этот день всё занято, выберите другой", show_alert=True)
        return
    await callback.answer()
    await state.update_data(day=day_iso)
    await state.set_state(Booking.time)
    await callback.message.answer("Выберите время:", reply_markup=times_keyboard(slots))


@router.callback_query(StateFilter(Booking.time), F.data.startswith("time:"))
async def get_time(callback: CallbackQuery, state: FSMContext) -> None:
    time_value = callback.data.split(":", 1)[1]
    data = await state.get_data()
    slots = free_slots(data["day"])
    if time_value not in slots:
        await callback.answer("Это время уже занято", show_alert=True)
        if slots:
            await callback.message.answer(
                "Выберите другое время:", reply_markup=times_keyboard(slots)
            )
        else:
            await state.set_state(Booking.day)
            await callback.message.answer(
                "На этот день всё занято. Выберите другой день:",
                reply_markup=days_keyboard(),
            )
        return
    await callback.answer()
    when = f"{day_label(date.fromisoformat(data['day']))}, {time_value}"
    await state.update_data(time=time_value, when=when)
    await state.set_state(Booking.phone)
    await callback.message.answer(
        "Отправьте номер телефона кнопкой ниже или напишите его, например: +7 999 123-45-67",
        reply_markup=phone_keyboard(),
    )


@router.message(StateFilter(Booking.phone), F.contact | F.text)
async def get_phone(message: Message, state: FSMContext) -> None:
    if message.contact:
        phone = normalize_phone(message.contact.phone_number, from_contact=True)
    else:
        phone = normalize_phone(message.text.strip())
    if phone is None:
        await message.answer(
            "Не похоже на номер телефона. Напишите в формате +7 999 123-45-67 "
            "или нажмите кнопку «Отправить мой номер»."
        )
        return
    await state.update_data(phone=phone)
    await state.set_state(Booking.confirm)
    data = await state.get_data()
    await message.answer("Почти готово.", reply_markup=ReplyKeyboardRemove())
    await message.answer(
        "Проверьте заявку:\n\n" + booking_text(data), reply_markup=confirm_keyboard()
    )


@router.callback_query(StateFilter(Booking.confirm), F.data == "ok")
async def confirm_booking(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()

    # Пока человек думал, это время мог занять кто-то другой
    if data["time"] not in free_slots(data["day"]):
        await callback.answer("Это время только что заняли", show_alert=True)
        await state.set_state(Booking.day)
        await callback.message.answer(
            "Выберите другой день и время:", reply_markup=days_keyboard()
        )
        return

    await callback.answer()
    await state.clear()
    save_booking(data, callback.from_user.id)

    if OWNER_ID:
        try:
            await bot.send_message(OWNER_ID, "Новая заявка\n\n" + booking_text(data))
        except Exception:
            logging.exception("Не удалось отправить заявку владельцу")

    await callback.message.answer("Заявка принята. Мы свяжемся с вами для подтверждения.")


@router.callback_query(StateFilter(Booking.confirm), F.data == "no")
async def decline_booking(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    await callback.message.answer("Заявка отменена. Чтобы начать заново, нажмите /start")


@router.message(StateFilter(Booking.service, Booking.day, Booking.time, Booking.confirm))
async def use_buttons(message: Message) -> None:
    await message.answer("Пожалуйста, выберите вариант кнопкой выше или нажмите /cancel.")


@router.message()
async def fallback(message: Message) -> None:
    await message.answer("Нажмите /start, чтобы записаться.")


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    if not BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN. Создайте файл .env по образцу .env.example")
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
