import os
import logging
import asyncio
import datetime
import subprocess
# import pytesseract # OCR o'chirildi
from PIL import Image
from io import BytesIO
import psycopg2
from psycopg2.extras import RealDictCursor
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile

# --- SOZLAMALAR ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
DATABASE_URL = os.getenv("DATABASE_URL")
MIN_DEPOSIT_UZS = 5000 
MIN_WITHDRAWAL_UZS = 5000 
REFERRAL_BONUS_UZS = 500 

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
logging.basicConfig(level=logging.INFO)

# --- BAZA BILAN ISHLASH ---
def get_db_connection():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    # Foydalanuvchilar jadvali
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            full_name TEXT,
            username TEXT,
            referrer_id BIGINT,
            joined_at TIMESTAMP DEFAULT NOW()
        )
    """)
    # Limitlar va statistika jadvali
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

# --- YORDAMCHI FUNKSIYALAR ---
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

# Balansga pul qo'shish (Tasdiqlashdan keyin ishlaydi)
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
            # Bonusni referal balansga qo'shish
            cur.execute("UPDATE user_stats SET referral_balance = referral_balance + %s WHERE user_id = %s", 
                        (REFERRAL_BONUS_UZS, referrer_id))
            
    conn.commit()
    cur.close()
    conn.close()

def calculate_price(size_mb):
    if size_mb <= 20:
        price = (size_mb * 300) + 1000
        if price < 1300: price = 1300
    else:
        price = (size_mb * 500) + 1000
    return int(price)

# OCR funksiyasi o'chirildi!

# Konvertatsiya funksiyasi
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


# --- STATE LAR ---
class ConvertState(StatesGroup):
    waiting_for_file = State()

class PayState(StatesGroup):
    waiting_for_deposit_amount = State()
    waiting_for_deposit_check = State() # Chek rasmi kutiladi

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

# Adminning pul yechish chekini qabul qilish
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

# Adminning Balans to'ldirish chekini qabul qilish (Yangi funksiya!)
@dp.callback_query(F.data.startswith("deposit_confirm_"))
async def admin_confirm_deposit(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Siz admin emassiz.", show_alert=True)
        return
        
    try:
        data = callback.data.split('_')
        user_id = int(data[2])
        amount = int(data[3])
        
        # 1. Balansni to'ldirish (deposit_balance ichida referal bonusi ham bor)
        deposit_balance(user_id, amount)
        
        # 2. Foydalanuvchiga tasdiqlash xabarini yuborish
        await bot.send_message(user_id, f"✅ To'lov tasdiqlandi! Balansingizga <b>{amount} UZS</b> qo'shildi.", parse_mode="HTML", reply_markup=main_menu)
        
        # 3. Admin xabarini o'zgartirish
        await callback.message.edit_caption(
            caption=callback.message.caption + f"\n\n✅ ADMIN TOMONIDAN TASDIQLANDI ({datetime.datetime.now().strftime('%H:%M')})",
            reply_markup=None,
            parse_mode="HTML"
        )
        
        await callback.answer(f"{amount} UZS balansga qo'shildi.", show_alert=True)
        
    except Exception as e:
        await callback.answer(f"Xatolik: {e}", show_alert=True)


# --- HANDLERLAR (FOYDALANUVCHI) ---
# ... (start_handler, help_handler, ads_handler o'zgarishsiz qoladi) ...

# To'lov chekini yuborish (qo'lda tekshiruvga o'tdi)
@dp.message(PayState.waiting_for_deposit_check, F.photo)
async def send_check_to_admin(message: types.Message, state: FSMContext):
    data = await state.get_data()
    amount = data.get('deposit_amount')
    
    if not amount:
        await message.answer("Kutilmagan xato. Iltimos, Balansim tugmasini qayta bosing.")
        await state.clear()
        return

    # Admin xabarini tayyorlash
    admin_caption = (f"💰 **Yangi Balans To'ldirish So'rovi!**\n\n"
                     f"USER_ID: {message.from_user.id}\n"
                     f"Foydalanuvchi: {message.from_user.full_name}\n"
                     f"SUMMA: {amount} UZS\n\n"
                     f"Iltimos, tekshirib, quyidagi tugma orqali tasdiqlang.")
    
    # Tasdiqlash tugmasi
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"✅ {amount} UZS ni tasdiqlash", callback_data=f"deposit_confirm_{message.from_user.id}_{amount}")]
    ])
    
    # Chekni Admin ID ga yuborish
    await bot.send_photo(
        ADMIN_ID, 
        photo=message.photo[-1].file_id, 
        caption=admin_caption, 
        reply_markup=kb,
        parse_mode="HTML"
    )

    await message.answer("✅ Chek muvaffaqiyatli yuborildi. Admin tekshiruvidan so'ng balansingizga pul tushadi.", reply_markup=main_menu)
    await state.clear()

# --- BOTNI ISHGA TUSHIRISH ---
async def main():
    init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
