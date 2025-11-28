import os
import logging
import asyncio
import datetime
import subprocess
import json
import re 
import time

from io import BytesIO
import psycopg2
from psycopg2.extras import RealDictCursor
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile, LabeledPrice, PreCheckoutQuery, SuccessfulPayment
from aiogram.dispatcher.middlewares.base import BaseMiddleware # Middleware uchun
from typing import Callable, Awaitable, Any, Dict # Middleware uchun

# Veb-server uchun kutubxonalar
from aiohttp import web
from aiogram.types import Update

# --- SOZLAMALAR ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
DATABASE_URL = os.getenv("DATABASE_URL")
WEB_SERVER_HOST = '0.0.0.0'
WEB_SERVER_PORT = int(os.environ.get("PORT", 8080))
BASE_WEBHOOK_URL = os.getenv("BASE_WEBHOOK_URL") 
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL = f"{BASE_WEBHOOK_URL}{WEBHOOK_PATH}"

PAYMENT_TOKEN = os.getenv("PAYMENT_TOKEN")

MIN_DEPOSIT_UZS = 5000 
MIN_WITHDRAWAL_UZS = 5000 
REFERRAL_BONUS_UZS = 500 
CONVERSION_PRICE_PER_MB = 1300

# --- XAVFSIZLIK SOZLAMALARI ---
MAX_FILE_SIZE_MB = 100  # Maksimal fayl hajmi 100 MB
FLOOD_CONTROL_RATE = 1.0  # Bir foydalanuvchidan xabarni qabul qilish oralig'i (sekundda)

# Agar asosiy o'zgaruvchilar yo'q bo'lsa, xato berish
if not all([BOT_TOKEN, ADMIN_ID, DATABASE_URL, BASE_WEBHOOK_URL, PAYMENT_TOKEN]):
    logging.error("Muhit o'zgaruvchilari to'liq kiritilmagan! Bot ishga tushirilmaydi.")
    exit(1)


bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
logging.basicConfig(level=logging.INFO)

# --- XAVFSIZLIK: FLOOD CONTROL MIDDLEWARE ---
class AntiFloodMiddleware(BaseMiddleware):
    def __init__(self, rate_limit: float = FLOOD_CONTROL_RATE):
        self.rate_limit = rate_limit
        self.users = {}

    async def __call__(
        self,
        handler: Callable[[types.Message, Dict[str, Any]], Awaitable[Any]],
        event: types.Message,
        data: Dict[str, Any],
    ) -> Any:
        user_id = event.from_user.id
        current_time = time.time()
        
        # Agar foydalanuvchi birinchi marta yozayotgan bo'lsa
        if user_id not in self.users:
            self.users[user_id] = current_time
            return await handler(event, data)
        
        # Oxirgi xabar bilan joriy xabar o'rtasidagi farq
        time_since_last_message = current_time - self.users[user_id]

        if time_since_last_message < self.rate_limit:
            # Cheklovni buzdi
            await event.answer("⚠️ Iltimos, sekinroq yozing. Bot serverini himoya qilyapmiz.")
            return # Handler ishga tushmaydi
        
        # Vaqtni yangilash
        self.users[user_id] = current_time
        return await handler(event, data)

dp.message.middleware(AntiFloodMiddleware())

# --- BAZA BILAN ISHLASH (O'zgarishsiz qoldi) ---
# ... get_db_connection, init_db, check_reset_weekly, get_user_stat, update_stat_and_balance funksiyalari ...
def get_db_connection():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
# ... [Qolgan barcha funksiyalar (init_db, check_reset_weekly, get_user_stat, update_stat_and_balance, deposit_balance, calculate_price, convert_to_pdf) avvalgi kod bilan bir xil joylashadi] ...
# Bu yerga avvalgi kodning barcha funksiyalarini joylashtiring.

def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            full_name TEXT,
            username TEXT,
            referrer_id BIGINT,
            joined_at TIMESTAMP DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_stats (
            user_id BIGINT PRIMARY KEY REFERENCES users(user_id),
            week_start_date DATE,
            free_docx BOOLEAN DEFAULT TRUE,
            free_pptx BOOLEAN DEFAULT TRUE,
            free_excel BOOLEAN DEFAULT TRUE,
            free_txt BOOLEAN DEFAULT TRUE,
            balance BIGINT DEFAULT 0,
            referral_balance BIGINT DEFAULT 0,
            total_paid_conversions INT DEFAULT 0,
            total_spent BIGINT DEFAULT 0
        )
    """)
    conn.commit()
    cur.close()
    conn.close()

def check_reset_weekly(user_id):
    conn = get_db_connection()
    cur = conn.cursor()
    today = datetime.date.today()
    start_of_week = today - datetime.timedelta(days=today.weekday())
    
    cur.execute("SELECT week_start_date FROM user_stats WHERE user_id = %s", (user_id,))
    result = cur.fetchone()
    
    if not result or result['week_start_date'] < start_of_week:
        cur.execute("""
            INSERT INTO user_stats (user_id, week_start_date, free_docx, free_pptx, free_excel, free_txt)
            VALUES (%s, %s, TRUE, TRUE, TRUE, TRUE)
            ON CONFLICT (user_id) DO UPDATE 
            SET week_start_date = %s, free_docx=TRUE, free_pptx=TRUE, free_excel=TRUE, free_txt=TRUE
        """, (user_id, start_of_week, start_of_week))
        conn.commit()
    
    cur.close()
    conn.close()

def get_user_stat(user_id):
    check_reset_weekly(user_id)
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM user_stats WHERE user_id = %s", (user_id,))
    stat = cur.fetchone()
    cur.close()
    conn.close()
    return stat

def update_stat_and_balance(user_id, file_type, is_paid, amount=0):
    conn = get_db_connection()
    cur = conn.cursor()
    if not is_paid:
        col_name = f"free_{file_type}" 
        cur.execute(f"UPDATE user_stats SET {col_name} = FALSE WHERE user_id = %s", (user_id,))
    else:
        cur.execute("""
            UPDATE user_stats 
            SET total_paid_conversions = total_paid_conversions + 1, 
                total_spent = total_spent + %s,
                balance = balance - %s
            WHERE user_id = %s
        """, (amount, amount, user_id))
    conn.commit()
    cur.close()
    conn.close()

def deposit_balance(user_id, amount):
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute("UPDATE user_stats SET balance = balance + %s WHERE user_id = %s", (amount, user_id))
    
    if amount >= MIN_DEPOSIT_UZS:
        cur.execute("SELECT referrer_id FROM users WHERE user_id = %s", (user_id,))
        referrer_row = cur.fetchone()
        
        if referrer_row and referrer_row['referrer_id']:
            referrer_id = referrer_row['referrer_id']
            cur.execute("UPDATE user_stats SET referral_balance = referral_balance + %s WHERE user_id = %s", 
                        (REFERRAL_BONUS_UZS, referrer_id))
            
    conn.commit()
    cur.close()
    conn.close()
    
def calculate_price(size_mb):
    if size_mb <= 20:
        price = (size_mb * 300) + 1000
        if price < CONVERSION_PRICE_PER_MB: 
            price = CONVERSION_PRICE_PER_MB 
    else:
        price = (size_mb * 500) + 1000
    return int(price)

async def convert_to_pdf(input_path, output_dir):
    try:
        process = subprocess.run(
            ['soffice', '--headless', '--convert-to', 'pdf', '--outdir', output_dir, input_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        if process.returncode == 0:
            filename = os.path.basename(input_path)
            pdf_filename = filename.rsplit('.', 1)[0] + '.pdf'
            return os.path.join(output_dir, pdf_filename)
        return None
    except Exception as e:
        print(f"Konvertatsiya xatosi: {e}")
        return None
# --- [Avvalgi kodning qolgan qismi (States, Keyboards, Handlers) bu yerga o'zgarishsiz keladi] ---
# Faqat 'process_file_handler' ga fayl hajmi cheklovi qo'shiladi

# --- STATE LAR (O'zgarishsiz qoldi) ---
class ConvertState(StatesGroup):
    waiting_for_file = State()

class PayState(StatesGroup):
    waiting_for_deposit_amount = State()

class WithdrawState(StatesGroup):
    waiting_for_card = State()
    
class BroadcastState(StatesGroup):
    waiting_for_message = State()

# --- KLAVIATURALAR (O'zgarishsiz qoldi) ---
main_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🔄 Konvertatsiya"), KeyboardButton(text="💰 Balansim")],
        [KeyboardButton(text="🤝 Referal"), KeyboardButton(text="📢 Reklama Xizmati"), KeyboardButton(text="ℹ️ Yordam")]
    ], resize_keyboard=True
)

convert_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="DOCX ➡️ PDF"), KeyboardButton(text="PPTX ➡️ PDF")],
        [KeyboardButton(text="EXCEL ➡️ PDF"), KeyboardButton(text="TXT ➡️ PDF")],
        [KeyboardButton(text="🔙 Bosh menyu")]
    ], resize_keyboard=True
)

deposit_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="5000 UZS"), KeyboardButton(text="10000 UZS")],
        [KeyboardButton(text="Boshqa summa"), KeyboardButton(text="🔙 Bosh menyu")]
    ], resize_keyboard=True
)


# --- HANDLERLAR (ADMIN) ---
# ... (Admin buyruqlari avvalgi koddan o'zgarishsiz) ...
@dp.message(Command("admin"))
async def admin_menu(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(user_id) FROM users")
    user_count = cur.fetchone()['count']
    cur.close()
    conn.close()

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 E'lon Yuborish", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="📊 Statistikani ko'rish", callback_data="admin_stats")]
    ])
    await message.answer(f"🤖 **Admin Panel**\n\nJami foydalanuvchilar soni: **{user_count}**", reply_markup=kb, parse_mode="Markdown")

@dp.callback_query(F.data == "admin_stats")
async def admin_stats_callback(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID: return
    
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT SUM(total_spent) as total_spent, SUM(total_paid_conversions) as total_conversions FROM user_stats")
    stats = cur.fetchone()
    cur.close()
    conn.close()

    text = (f"📊 **Umumiy Statistika**\n"
            f"Jami sarflangan: **{stats['total_spent'] if stats['total_spent'] else 0} UZS**\n"
            f"Jami pullik konvertatsiyalar: **{stats['total_conversions'] if stats['total_conversions'] else 0} ta**")
    await call.message.answer(text, parse_mode="Markdown")
    await call.answer()

@dp.callback_query(F.data == "admin_broadcast")
async def admin_start_broadcast(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID: return
    await call.message.answer("E'lon uchun matn yoki rasmni yuboring:")
    await state.set_state(BroadcastState.waiting_for_message)
    await call.answer()

@dp.message(BroadcastState.waiting_for_message)
async def admin_send_broadcast(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    
    await state.clear()
    
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM users")
    users = [row['user_id'] for row in cur.fetchall()]
    cur.close()
    conn.close()
    
    sent_count = 0
    for user_id in users:
        try:
            if message.text:
                await bot.send_message(user_id, message.html_text, parse_mode="HTML")
            elif message.photo:
                await bot.send_photo(user_id, message.photo[-1].file_id, caption=message.caption, parse_mode="HTML")
            sent_count += 1
            await asyncio.sleep(0.05) 
        except Exception:
            pass 
            
    await message.answer(f"✅ E'lon {sent_count} nafar foydalanuvchiga muvaffaqiyatli yuborildi.")


@dp.message(F.reply_to_message.is_attribute('text'))
async def admin_withdraw_check(message: types.Message):
    if message.from_user.id != ADMIN_ID or not message.photo:
        return

    reply_text = message.reply_to_message.text
    if "pul yechishni so'radi" in reply_text and "USER_ID:" in reply_text:
        
        try:
            user_id = int(reply_text.split("USER_ID:")[1].split("\n")[0].strip())
            amount = int(reply_text.split("SUMMA:")[1].split(" UZS")[0].strip())
            
            photo = message.photo[-1]
            
            await bot.send_photo(
                chat_id=user_id,
                photo=photo.file_id,
                caption=f"✅ **{amount} UZS** miqdoridagi pul mablag'ingiz siz kiritgan karta raqamiga o'tkazildi.\n"
                        f"Iltimos, chekni tekshiring. Referal balansingiz 0 ga tushirildi.",
                parse_mode="Markdown"
            )
            
            # Referal balansni 0 ga tushirish
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("UPDATE user_stats SET referral_balance = 0 WHERE user_id = %s", (user_id,))
            conn.commit()
            cur.close()
            conn.close()
            
            await message.answer(f"✅ User {user_id} ga {amount} UZS o'tkazilgani tasdiqlandi. Balansi 0 ga tushirildi.")
            
        except Exception as e:
            await message.answer(f"❌ Xatolik yuz berdi: {e}")

# --- HANDLERLAR (TELEGRAM PAYMENT) ---
@dp.pre_checkout_query(lambda query: True)
async def pre_checkout_handler(pre_checkout_query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@dp.message(F.successful_payment)
async def successful_payment_handler(message: types.Message):
    payment: SuccessfulPayment = message.successful_payment
    amount_uzs = payment.total_amount / 100 
    user_id = message.from_user.id
    deposit_balance(user_id, int(amount_uzs))
    
    await message.answer(
        f"🎉 To'lov muvaffaqiyatli yakunlandi!\n"
        f"💵 Balansingizga **{int(amount_uzs)} UZS** qo'shildi.",
        parse_mode="Markdown",
        reply_markup=main_menu
    )

# --- HANDLERLAR (ASOSIY & CONVERSION) ---
@dp.message(CommandStart())
async def start_handler(message: types.Message):
    user_id = message.from_user.id
    full_name = message.from_user.full_name
    username = message.from_user.username
    referrer_id = None
    
    if message.text and len(message.text.split()) > 1:
        param = message.text.split()[1]
        if param.startswith("ref_") and param[4:].isdigit():
            referrer_id = int(param[4:])
            if referrer_id == user_id:
                referrer_id = None
    
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute("SELECT user_id FROM users WHERE user_id = %s", (user_id,))
    if not cur.fetchone():
        cur.execute("""
            INSERT INTO users (user_id, full_name, username, referrer_id) 
            VALUES (%s, %s, %s, %s)
        """, (user_id, full_name, username, referrer_id))
        
        cur.execute("INSERT INTO user_stats (user_id) VALUES (%s)", (user_id,))
        conn.commit()

    cur.close()
    conn.close()
    
    text = (f"Assalomu alaykum, {full_name}!\n\n"
            "Men hujjatlaringizni DOCX, PPTX, EXCEL, TXT formatlaridan PDF formatiga o'tkazib beruvchi botman.\n"
            "Haftada har bir turdagi faylni bir martadan **BEPUL** konvertatsiya qilishingiz mumkin.")
    
    await message.answer(text, reply_markup=main_menu)

@dp.message(F.text == "🔄 Konvertatsiya")
async def conversion_menu_handler(message: types.Message):
    await message.answer("Konvertatsiya turini tanlang:", reply_markup=convert_menu)

@dp.message(F.text.in_(["DOCX ➡️ PDF", "PPTX ➡️ PDF", "EXCEL ➡️ PDF", "TXT ➡️ PDF"]))
async def ask_for_file_handler(message: types.Message, state: FSMContext):
    file_type_map = {
        "DOCX ➡️ PDF": ("docx", "DOCX"),
        "PPTX ➡️ PDF": ("pptx", "PPTX"),
        "EXCEL ➡️ PDF": ("excel", "EXCEL"),
        "TXT ➡️ PDF": ("txt", "TXT"),
    }
    
    file_key, file_name = file_type_map[message.text]
    user_id = message.from_user.id
    user_stat = get_user_stat(user_id)
    
    if user_stat[f'free_{file_key}']:
        await message.answer(f"Haftalik **{file_name}** fayl uchun bepul konvertatsiya limiti mavjud.\nIltimos, faylni yuboring.")
    else:
        await message.answer(f"Haftalik **{file_name}** fayl uchun bepul limit tugagan.\nIltimos, konvertatsiya qilinishi kerak bo'lgan faylni yuboring. Konvertatsiya pulga amalga oshiriladi (narx hajmdan kelib chiqib hisoblanadi).")
    
    await state.set_state(ConvertState.waiting_for_file)
    await state.update_data(target_file_type=file_key)


@dp.message(ConvertState.waiting_for_file, F.document)
async def process_file_handler(message: types.Message, state: FSMContext):
    data = await state.get_data()
    file_type = data['target_file_type']
    await state.clear() 
    
    doc = message.document
    file_extension = doc.file_name.split('.')[-1].lower()
    
    # 🚨 XAVFSIZLIK: Fayl kengaytmasini tekshirish
    if file_extension not in [file_type]:
        await message.answer(f"❌ Noto'g'ri fayl turi. Iltimos, **.{file_type}** fayl yuboring.", reply_markup=convert_menu)
        return
        
    # 🚨 XAVFSIZLIK: Fayl hajmi cheklovi
    file_size_mb = doc.file_size / (1024 * 1024)
    if file_size_mb > MAX_FILE_SIZE_MB:
        await message.answer(f"❌ Fayl hajmi juda katta ({file_size_mb:.2f} MB). Maksimal ruxsat etilgan hajm: {MAX_FILE_SIZE_MB} MB.", reply_markup=convert_menu)
        return

    price = calculate_price(file_size_mb)
    user_id = message.from_user.id
    user_stat = get_user_stat(user_id)

    if user_stat[f'free_{file_type}']:
        is_paid = False
        status_text = "Bepul"
    else:
        is_paid = True
        if user_stat['balance'] < price:
            await message.answer(f"❌ Konvertatsiya uchun balansingizda yetarli mablag' yo'q. Bu fayl uchun **{price} UZS** kerak. Iltimos, balansni to'ldiring.", reply_markup=main_menu)
            return
        status_text = f"Pullik ({price} UZS balansingizdan yechiladi)"


    await message.answer(f"⏳ Faylingizni (Hajmi: {file_size_mb:.2f} MB, Konvertatsiya: {status_text}) qayta ishlayapman...")

    try:
        file_info = await bot.get_file(doc.file_id)
        downloaded_file = await bot.download_file(file_info.file_path)

        temp_dir = 'temp'
        os.makedirs(temp_dir, exist_ok=True)
        input_path = os.path.join(temp_dir, doc.file_name)
        
        with open(input_path, 'wb') as f:
            f.write(downloaded_file.read())

        output_path = await convert_to_pdf(input_path, temp_dir)

        if output_path:
            pdf_file = FSInputFile(output_path)
            await message.answer_document(pdf_file, caption="✅ Konvertatsiya muvaffaqiyatli yakunlandi!")
            
            update_stat_and_balance(user_id, file_type, is_paid, price)
        else:
            await message.answer("❌ Konvertatsiya amalga oshmadi. Fayl shikastlangan bo'lishi mumkin yoki ichida ma'lumot yo'q.")

    except Exception as e:
        logging.error(f"Konvertatsiya jarayonida kutilmagan xato: {e}")
        await message.answer("❌ Serverda kutilmagan xato ro'y berdi. Keyinroq urinib ko'ring.")
    finally:
        if 'input_path' in locals() and os.path.exists(input_path):
            os.remove(input_path)
        if 'output_path' in locals() and output_path and os.path.exists(output_path):
            os.remove(output_path)

# --- HANDLERLAR (PUL YECHISH/REFERAL) ---
# ... (Qolgan referal va pul yechish handlerlari avvalgi koddan o'zgarishsiz) ...
@dp.message(F.text == "💰 Balansim")
async def balance_handler(message: types.Message):
    user_stat = get_user_stat(message.from_user.id)
    balance = user_stat['balance']
    
    text = (f"💵 <b>Balansingiz: {balance} UZS</b>\n\n"
            "Bu mablag'ni pullik konvertatsiyalar uchun ishlatishingiz mumkin.")
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Balansni to'ldirish", callback_data="deposit_start")]
    ])
    await message.answer(text, reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data == "deposit_start")
async def start_deposit(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.delete()
    await callback.message.answer("💳 Qancha pul to'ldirmoqchisiz? Minimal to'lov summasi: 5000 UZS", reply_markup=deposit_keyboard)
    await state.set_state(PayState.waiting_for_deposit_amount)
    await callback.answer()

@dp.message(PayState.waiting_for_deposit_amount)
async def get_deposit_amount(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    
    if message.text == "🔙 Bosh menyu":
        await state.clear()
        await message.answer("Bosh menyuga qaytildi.", reply_markup=main_menu)
        return

    try:
        if message.text in ["5000 UZS", "10000 UZS"]:
            amount = int(message.text.split()[0])
        else:
            amount = int(message.text)

        if amount < MIN_DEPOSIT_UZS:
            await message.answer(f"❌ Minimal to'lov summasi {MIN_DEPOSIT_UZS} UZS bo'lishi kerak. Iltimos, kattaroq summa kiriting.")
            return

        amount_cents = amount * 100 

        await bot.send_invoice(
            chat_id=user_id,
            title=f"Balansni to'ldirish",
            description=f"Konvertatsiya xizmatlari uchun {amount} UZS to'ldirish.",
            payload=f"deposit_{user_id}_{amount}", 
            provider_token=PAYMENT_TOKEN,
            currency="UZS",
            prices=[
                LabeledPrice(label=f"Balansni to'ldirish", amount=amount_cents)
            ],
            start_parameter=f"deposit_{user_id}",
            need_name=False,
            need_email=False,
            is_flexible=False
        )
        await state.clear() 

    except ValueError:
        await message.answer("❌ Noto'g'ri qiymat. Iltimos, faqat raqam kiriting.")
    except Exception as e:
        await message.answer(f"❌ Xatolik: To'lov tizimida muammo yuz berdi. Iltimos, tekshirib ko'ring. {e}")


@dp.message(F.text == "🤝 Referal")
async def referral_handler(message: types.Message):
    user_id = message.from_user.id
    user_stat = get_user_stat(user_id)
    referral_balance = user_stat['referral_balance']
    
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(user_id) FROM users WHERE referrer_id = %s", (user_id,))
    referral_count = cur.fetchone()['count']
    cur.close()
    conn.close()
    
    referral_link = f"https://t.me/{message.bot.me.username}?start=ref_{user_id}"
    
    text = (f"🎁 **Referal Tizim**\n\n"
            f"Siz chaqirgan har bir do'stingiz {MIN_DEPOSIT_UZS} UZS dan ortiq to'lov qilsa, sizning referal balansingizga **{REFERRAL_BONUS_UZS} UZS** qo'shiladi.\n\n"
            f"👤 Sizning do'stlaringiz: **{referral_count} ta**\n"
            f"💰 Referal balansingiz: **{referral_balance} UZS**\n\n"
            f"🔗 **Sizning referal havolangiz:**\n"
            f"<code>{referral_link}</code>")
            
    kb = None
    if referral_balance >= MIN_WITHDRAWAL_UZS:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💸 Pul Yechish", callback_data="start_withdrawal")]
        ])

    await message.answer(text, reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data == "start_withdrawal")
async def start_withdrawal_callback(call: types.CallbackQuery, state: FSMContext):
    user_id = call.from_user.id
    user_stat = get_user_stat(user_id)
    referral_balance = user_stat['referral_balance']

    if referral_balance < MIN_WITHDRAWAL_UZS:
        await call.answer(f"Minimal yechish summasi {MIN_WITHDRAWAL_UZS} UZS.", show_alert=True)
        return

    await call.message.edit_text(
        f"💳 Yechib olish summasi: **{referral_balance} UZS**.\n\n"
        "Iltimos, pul o'tkazilishi kerak bo'lgan **UZCARD/HUMO karta raqamingizni (16 xona)** yuboring:", 
        parse_mode="Markdown"
    )
    await state.set_state(WithdrawState.waiting_for_card)
    await call.answer()

@dp.message(WithdrawState.waiting_for_card)
async def process_withdrawal_card(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    card_number = message.text.replace(' ', '')
    
    if not re.match(r'^\d{16}$', card_number):
        await message.answer("❌ Karta raqami noto'g'ri formatda. Iltimos, 16 xonali raqamni to'g'ri kiriting:")
        return
        
    user_stat = get_user_stat(user_id)
    amount = user_stat['referral_balance']
    
    if amount < MIN_WITHDRAWAL_UZS:
        await message.answer("Pul yechish so'rovingiz eskirgan yoki balansingiz kamaygan. Bosh menyuga qayting.", reply_markup=main_menu)
        await state.clear()
        return

    admin_message = (f"💸 **Yangi Pul Yechish So'rovi!**\n\n"
                     f"USER_ID: {user_id}\n"
                     f"Foydalanuvchi: {message.from_user.full_name}\n"
                     f"SUMMA: {amount} UZS\n"
                     f"KARTA: <code>{card_number}</code>\n\n"
                     f"Iltimos, pulni o'tkazing va chek rasmi bilan javob bering.")
    
    await bot.send_message(ADMIN_ID, admin_message, parse_mode="HTML")
    
    await message.answer("✅ Pul yechish so'rovingiz adminlarga yuborildi. Tez orada pulingiz o'tkaziladi. Bosh menyuga qaytildi.", reply_markup=main_menu)
    await state.clear()
# --- BOSHQA HANDLERLAR ---
@dp.message(F.text == "ℹ️ Yordam")
async def help_handler(message: types.Message):
    text = ("❓ **Yordam Bo'limi**\n\n"
            "1. **Konvertatsiya:** Konvertatsiya menyusidan kerakli turini tanlang va faylni yuboring.\n"
            "2. **Bepul Limit:** Har bir turdan haftada bir marta bepul foydalanishingiz mumkin.\n"
            f"3. **Pullik:** Bepul limit tugaganda, konvertatsiya **{CONVERSION_PRICE_PER_MB} UZS** dan boshlanadigan narxda amalga oshiriladi.\n"
            "4. **Balans:** Balansingizni to'ldirib, pullik konvertatsiyalardan foydalaning.\n"
            "5. **Referal:** Do'stlaringizni chaqirib pul ishlang, pulni kartaga yechib olishingiz mumkin.")
    await message.answer(text, parse_mode="Markdown")

@dp.message(F.text == "📢 Reklama Xizmati")
async def ads_handler(message: types.Message):
    await message.answer("Reklama xizmatlari bo'yicha admin (@Sizning_adminingiz) bilan bog'laning.")

# --- BOTNI ISHGA TUSHIRISH (WEBHOOK) ---
async def on_startup(dispatcher):
    await bot.set_webhook(WEBHOOK_URL)
    logging.info(f"Webhook o'rnatildi: {WEBHOOK_URL}")

async def on_shutdown(dispatcher):
    await bot.delete_webhook()

def create_app():
    init_db()

    app = web.Application()
    
    app.router.add_post(WEBHOOK_PATH, lambda request: telegram_webhook(request, dp))
    
    app.on_startup.append(lambda app: on_startup(dp))
    app.on_shutdown.append(lambda app: on_shutdown(dp))
    
    return app

async def telegram_webhook(request, dispatcher):
    update = Update.model_validate(await request.json(), context={'bot': dispatcher.bot})
    await dispatcher.feed_update(update)
    return web.Response()

# Gunicorn shu 'app' ni chaqiradi
app = create_app()

if __name__ == '__main__':
    logging.warning("Starting bot in local polling mode...")
    async def start_polling():
        init_db()
        await dp.start_polling(bot)
    asyncio.run(start_polling())
