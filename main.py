import os
import logging
import asyncio
import datetime
import subprocess
import json
import hashlib

from io import BytesIO
import psycopg2
from psycopg2.extras import RealDictCursor
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile

# Veb-server uchun kutubxonalar
from aiohttp import web
from aiogram.types import Update

# --- SOZLAMALAR ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
DATABASE_URL = os.getenv("DATABASE_URL")
WEB_SERVER_HOST = '0.0.0.0'
WEB_SERVER_PORT = int(os.environ.get("PORT", 8080))
BASE_WEBHOOK_URL = os.getenv("BASE_WEBHOOK_URL") # https://loyihangiz-xxxx.railway.app
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL = f"{BASE_WEBHOOK_URL}{WEBHOOK_PATH}"

# Click Sozlamalari
CLICK_SERVICE_ID = os.getenv("CLICK_SERVICE_ID")
CLICK_SECRET_KEY = os.getenv("CLICK_SECRET_KEY")

MIN_DEPOSIT_UZS = 5000 
MIN_WITHDRAWAL_UZS = 5000 
REFERRAL_BONUS_UZS = 500 

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
logging.basicConfig(level=logging.INFO)

# --- BAZA BILAN ISHLASH (O'zgarishsiz qoldi) ---
def get_db_connection():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def init_db():
    # ... (init_db funksiyasi o'zgarishsiz qoldi) ...
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
    cur.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            payment_id SERIAL PRIMARY KEY,
            user_id BIGINT,
            amount BIGINT,
            click_transaction_id BIGINT UNIQUE,
            status TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    conn.commit()
    cur.close()
    conn.close()

def check_reset_weekly(user_id):
    # ... (check_reset_weekly funksiyasi o'zgarishsiz qoldi) ...
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
    # ... (get_user_stat funksiyasi o'zgarishsiz qoldi) ...
    check_reset_weekly(user_id)
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM user_stats WHERE user_id = %s", (user_id,))
    stat = cur.fetchone()
    cur.close()
    conn.close()
    return stat

def update_stat_and_balance(user_id, file_type, is_paid, amount=0):
    # ... (update_stat_and_balance funksiyasi o'zgarishsiz qoldi) ...
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

# Balansni to'ldirish funksiyasi (Endi Click'dan kelganda ishlaydi)
def deposit_balance(user_id, amount):
    conn = get_db_connection()
    cur = conn.cursor()
    
    # 1. Asosiy balansga pul qo'shish
    cur.execute("UPDATE user_stats SET balance = balance + %s WHERE user_id = %s", (amount, user_id))
    
    # 2. Referal bonus shartini tekshirish
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
    # ... (calculate_price funksiyasi o'zgarishsiz qoldi) ...
    if size_mb <= 20:
        price = (size_mb * 300) + 1000
        if price < 1300: price = 1300
    else:
        price = (size_mb * 500) + 1000
    return int(price)

async def convert_to_pdf(input_path, output_dir):
    # ... (convert_to_pdf funksiyasi o'zgarishsiz qoldi) ...
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


# --- CLICK API FUNKSIYALARI ---

# Click Hash Check
def check_click_hash(data: dict) -> bool:
    sign_string = f"{data.get('click_trans_id', 0)}{CLICK_SERVICE_ID}{CLICK_SECRET_KEY}{data.get('merchant_trans_id', 0)}{data.get('param1', '')}{data.get('param2', '')}{data.get('action', 0)}{data.get('sign_time', '')}"
    
    # Agar Request tayyorlash (0) yoki Tasdiqlash (1) bo'lsa
    if data.get('action', 0) in [0, 1]:
        sign_string += str(data.get('amount', 0))
    
    generated_sign = hashlib.sha1(sign_string.encode('utf-8')).hexdigest()
    return generated_sign == data.get('sign_string', '')

# Click Webhook Handler
async def click_webhook(request):
    data = await request.post()
    
    # 1. Hashni tekshirish
    if not check_click_hash(data):
        return web.json_response({'click_trans_id': data.get('click_trans_id', 0), 'merchant_trans_id': data.get('merchant_trans_id', 0), 'error': -1, 'error_note': 'SIGN CHECK FAILED!'})
    
    # Ma'lumotlarni olish
    action = int(data.get('action', 0))
    merchant_trans_id = int(data.get('merchant_trans_id', 0)) # Bu bizning user_id miz
    click_trans_id = int(data.get('click_trans_id', 0))
    amount = int(float(data.get('amount', 0)))
    error = int(data.get('error', 0))
    user_id = merchant_trans_id

    conn = get_db_connection()
    cur = conn.cursor()

    if action == 0: # Prepare Request
        # Bizda hamma to'lovlar yangi bo'ladi, shuning uchun faqat kiritish
        cur.execute("INSERT INTO payments (user_id, amount, click_transaction_id, status) VALUES (%s, %s, %s, %s)",
                    (user_id, amount, click_trans_id, 'pending'))
        conn.commit()
        cur.close()
        conn.close()
        return web.json_response({'click_trans_id': click_trans_id, 'merchant_trans_id': merchant_trans_id, 'error': 0, 'error_note': 'Success', 'merchant_prepare_id': user_id})

    elif action == 1: # Complete Request
        
        # 1. To'lovni bazadan topish
        cur.execute("SELECT * FROM payments WHERE click_transaction_id = %s", (click_trans_id,))
        payment = cur.fetchone()

        if error < 0:
            # Xato yuz berdi
            cur.execute("UPDATE payments SET status = %s WHERE click_transaction_id = %s", ('failed', click_trans_id))
            conn.commit()
            cur.close()
            conn.close()
            return web.json_response({'click_trans_id': click_trans_id, 'merchant_trans_id': merchant_trans_id, 'error': -4, 'error_note': 'Payment failed on Click side'})

        if not payment:
            # Prepare bosqichini o'tkazib yuborgan
            cur.close()
            conn.close()
            return web.json_response({'click_trans_id': click_trans_id, 'merchant_trans_id': merchant_trans_id, 'error': -6, 'error_note': 'Transaction not found in prepare step'})

        if payment['status'] == 'completed':
            # Avval tasdiqlangan
            cur.close()
            conn.close()
            return web.json_response({'click_trans_id': click_trans_id, 'merchant_trans_id': merchant_trans_id, 'error': 0, 'error_note': 'Already paid'})

        if payment['amount'] != amount:
            # Summalar mos kelmadi
            cur.execute("UPDATE payments SET status = %s WHERE click_transaction_id = %s", ('mismatch', click_trans_id))
            conn.commit()
            cur.close()
            conn.close()
            return web.json_response({'click_trans_id': click_trans_id, 'merchant_trans_id': merchant_trans_id, 'error': -2, 'error_note': 'Incorrect amount'})
        
        # 2. Balansni to'ldirish
        deposit_balance(user_id, amount)
        
        # 3. Baza statusini yangilash
        cur.execute("UPDATE payments SET status = %s WHERE click_transaction_id = %s", ('completed', click_trans_id))
        conn.commit()

        # 4. Foydalanuvchiga xabar berish
        try:
            await bot.send_message(user_id, f"✅ Balansingizga **{amount} UZS** avtomatik tarzda qo'shildi! Endi konvertatsiya xizmatidan foydalanishingiz mumkin.", parse_mode="Markdown", reply_markup=main_menu)
        except Exception as e:
            logging.error(f"Xabar yuborish xatosi: {e}")

        cur.close()
        conn.close()
        return web.json_response({'click_trans_id': click_trans_id, 'merchant_trans_id': merchant_trans_id, 'error': 0, 'error_note': 'Success'})

    return web.json_response({'click_trans_id': data.get('click_trans_id', 0), 'merchant_trans_id': data.get('merchant_trans_id', 0), 'error': -3, 'error_note': 'Action not supported'})

# --- STATE LAR ---
class ConvertState(StatesGroup):
    waiting_for_file = State()

class PayState(StatesGroup):
    waiting_for_deposit_amount = State()

class WithdrawState(StatesGroup):
    waiting_for_card = State()
    
class BroadcastState(StatesGroup):
    waiting_for_message = State()

# --- KLAVIATURALAR ---
main_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🔄 Konvertatsiya"), KeyboardButton(text="💰 Balansim")],
        [KeyboardButton(text="🤝 Referal"), KeyboardButton(text="📢 Reklama Xizmati"), KeyboardButton(text="ℹ️ Yordam")]
    ], resize_keyboard=True
)

deposit_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="5000 UZS"), KeyboardButton(text="10000 UZS")],
        [KeyboardButton(text="Boshqa summa"), KeyboardButton(text="🔙 Bosh menyu")]
    ], resize_keyboard=True
)

# --- HANDLERLAR (ADMIN) ---
# ... (Admin withdraw check handleri o'zgarishsiz qoldi) ...
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

# --- HANDLERLAR (FOYDALANUVCHI) ---

# Balans to'ldirish
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

# To'lov summasini kiritish va Click linkini generatsiya qilish
@dp.message(PayState.waiting_for_deposit_amount)
async def get_deposit_amount(message: types.Message, state: FSMContext):
    await state.update_data(deposit_amount=None)
    user_id = message.from_user.id
    
    try:
        if message.text in ["5000 UZS", "10000 UZS"]:
            amount = int(message.text.split()[0])
        elif message.text == "🔙 Bosh menyu":
            await state.clear()
            await message.answer("Bosh menyuga qaytildi.", reply_markup=main_menu)
            return
        else:
            amount = int(message.text)

        if amount < MIN_DEPOSIT_UZS:
            await message.answer(f"❌ Minimal to'lov summasi {MIN_DEPOSIT_UZS} UZS bo'lishi kerak. Iltimos, kattaroq summa kiriting.")
            return

        # Click to'lov linkini generatsiya qilish
        # 'merchant_trans_id' - bu bizning user_id miz
        click_link = f"https://my.click.uz/services/pay?service_id={CLICK_SERVICE_ID}&merchant_id={CLICK_SERVICE_ID}&amount={amount}&transaction_param={user_id}"

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"Click orqali {amount} UZS to'lash", url=click_link)]
        ])
        
        await message.answer(
            f"💰 Balansingizni avtomatik to'ldirish uchun **{amount} UZS** miqdorini Click orqali to'lang.\n\n"
            f"To'lovni amalga oshirishingiz bilan pul balansingizga avtomatik qo'shiladi.",
            parse_mode="HTML",
            reply_markup=kb
        )
        await state.clear() # Endi to'lov jarayoni Click tomonidan boshqariladi

    except ValueError:
        await message.answer("❌ Noto'g'ri qiymat. Iltimos, faqat raqam kiriting.")

# ... (Qolgan handlerlar o'zgarishsiz qoldi) ...


# --- BOTNI ISHGA TUSHIRISH ---
# Webhook va Webserver ishlatish uchun 'app' kerak
async def on_startup(dispatcher, webhook_url):
    await bot.set_webhook(webhook_url)
    
async def on_shutdown(dispatcher):
    await bot.delete_webhook()

def create_app():
    # Bazani yaratish/yangilash
    init_db()

    app = web.Application()
    
    # Click Webhook endpoint
    app.router.add_post('/click_webhook', click_webhook)
    
    # Telegram Webhook endpoint
    app.router.add_post(WEBHOOK_PATH, lambda request: telegram_webhook(request, dp))
    
    # Startup/Shutdown funksiyalarini sozlash
    app.on_startup.append(lambda app: on_startup(dp, WEBHOOK_URL))
    app.on_shutdown.append(lambda app: on_shutdown(dp))
    
    return app

async def telegram_webhook(request, dispatcher):
    update = Update.model_validate(await request.json(), context={'bot': dispatcher.bot})
    await dispatcher.feed_update(update)
    return web.Response()

# Gunicorn shu 'app' ni chaqiradi
app = create_app()

if __name__ == '__main__':
    # Lokal ishlatish uchun (Deploymentda Gunicorn ishlaydi)
    logging.warning("Starting bot in local polling mode...")
    async def start_polling():
        init_db()
        await dp.start_polling(bot)
    asyncio.run(start_polling())
