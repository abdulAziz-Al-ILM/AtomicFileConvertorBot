import os
import logging
import asyncio
import datetime
import subprocess
import pytesseract
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

# Tesseract yo'lini Linux uchun to'g'irlash (Docker ichida shart emas, lekin xavfsizlik uchun)
# pytesseract.pytesseract.tesseract_cmd = r'/usr/bin/tesseract' 

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
            total_paid_conversions INT DEFAULT 0,
            total_spent BIGINT DEFAULT 0
        )
    """)
    conn.commit()
    cur.close()
    conn.close()

# --- YORDAMCHI FUNKSIYALAR ---

# Haftalik limitni tekshirish va yangilash
def check_reset_weekly(user_id):
    conn = get_db_connection()
    cur = conn.cursor()
    today = datetime.date.today()
    # Haftaning dushanbasi
    start_of_week = today - datetime.timedelta(days=today.weekday())
    
    cur.execute("SELECT week_start_date FROM user_stats WHERE user_id = %s", (user_id,))
    result = cur.fetchone()
    
    if not result or result['week_start_date'] < start_of_week:
        # Yangi hafta, limitlarni tiklaymiz
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

def update_stat(user_id, file_type, is_paid, amount=0):
    conn = get_db_connection()
    cur = conn.cursor()
    if not is_paid:
        col_name = f"free_{file_type}" # free_docx, free_pptx...
        cur.execute(f"UPDATE user_stats SET {col_name} = FALSE WHERE user_id = %s", (user_id,))
    else:
        cur.execute("UPDATE user_stats SET total_paid_conversions = total_paid_conversions + 1, total_spent = total_spent + %s WHERE user_id = %s", (amount, user_id))
    conn.commit()
    cur.close()
    conn.close()

# Narx hisoblash
def calculate_price(size_mb):
    if size_mb <= 20:
        price = (size_mb * 300) + 1000
        if price < 1300: price = 1300
    else:
        # 20mb dan 30mb gacha
        price = (size_mb * 500) + 1000
    return int(price)

# OCR funksiyasi (Rasmdan matn o'qish)
def ocr_verify_payment(image_bytes, expected_amount):
    try:
        img = Image.open(BytesIO(image_bytes))
        text = pytesseract.image_to_string(img)
        # Oddiy tekshiruv: Matn ichida kutilgan summa bormi?
        # Bu juda sodda usul, haqiqiy hayotda murakkabroq bo'lishi kerak
        clean_text = text.replace(" ", "").replace(",", "").replace(".", "")
        if str(expected_amount) in clean_text:
            return True
        return False
    except Exception as e:
        print(f"OCR Xato: {e}")
        return False

# Konvertatsiya funksiyasi (LibreOffice)
async def convert_to_pdf(input_path, output_dir):
    try:
        # LibreOffice buyrug'i
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
    waiting_for_payment = State()

class BroadcastState(StatesGroup):
    waiting_for_message = State()

# --- KLAVIATURALAR ---
main_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🔄 Konvertatsiya")],
        [KeyboardButton(text="📢 Reklama Xizmati"), KeyboardButton(text="ℹ️ Yordam")]
    ], resize_keyboard=True
)

convert_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="DOCX ➡️ PDF"), KeyboardButton(text="PPTX ➡️ PDF")],
        [KeyboardButton(text="EXCEL ➡️ PDF"), KeyboardButton(text="TXT ➡️ PDF")],
        [KeyboardButton(text="🔙 Bosh menyu")]
    ], resize_keyboard=True
)

# --- HANDLERLAR (ADMIN) ---

@dp.message(Command("admin"))
async def admin_panel(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM users")
    count = cur.fetchone()['count']
    
    cur.execute("SELECT SUM(total_spent) as revenue FROM user_stats")
    revenue = cur.fetchone()['revenue'] or 0
    cur.close()
    conn.close()
    
    await message.answer(
        f"👑 <b>Admin Panel</b>\n\n"
        f"👥 Foydalanuvchilar: {count} ta\n"
        f"💰 Jami daromad: {revenue} UZS\n\n"
        f"Xabar yuborish uchun /broadcast buyrug'ini yuboring.",
        parse_mode="HTML"
    )

@dp.message(Command("broadcast"))
async def start_broadcast(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    await message.answer("Barcha foydalanuvchilarga yuboriladigan xabarni kiriting:")
    await state.set_state(BroadcastState.waiting_for_message)

@dp.message(BroadcastState.waiting_for_message)
async def process_broadcast(message: types.Message, state: FSMContext):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM users")
    users = cur.fetchall()
    cur.close()
    conn.close()
    
    count = 0
    for user in users:
        try:
            await bot.copy_message(user['user_id'], message.chat.id, message.message_id)
            count += 1
            await asyncio.sleep(0.05) # Telegram limitlariga tushmaslik uchun
        except:
            pass
            
    await message.answer(f"Xabar {count} ta foydalanuvchiga yuborildi.")
    await state.clear()

# --- HANDLERLAR (FOYDALANUVCHI) ---

@dp.message(CommandStart())
async def start_handler(message: types.Message):
    user = message.from_user
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("INSERT INTO users (user_id, full_name, username) VALUES (%s, %s, %s) ON CONFLICT (user_id) DO NOTHING",
                (user.id, user.full_name, user.username))
    conn.commit()
    cur.close()
    conn.close()
    
    check_reset_weekly(user.id)
    
    text = (f"Assalomu alaykum, {user.full_name}!\n\n"
            "Men fayllarni pdf formatga konvertatsiya qilib beruvchi botman. Menga istalgan (docx, pptx, excel va txt) hujjat fayl yuboring.\n\n"
            "Agar reklama bo'yicha savollaringiz bo'lsa, menyudagi tugmadan foydalaning.\n\n"
            "Foydalanish qoidalari (ToU) bilan tanishing: https://t.me/Online_Services_Atomic/5")
    
    await message.answer(text, reply_markup=main_menu)

@dp.message(F.text == "ℹ️ Yordam")
async def help_handler(message: types.Message):
    text = ("🤖 <b>Botdan foydalanish yo'riqnomasi:</b>\n\n"
            "1. 'Konvertatsiya' tugmasini bosing.\n"
            "2. Kerakli formatni tanlang (masalan, Word -> PDF).\n"
            "3. Faylni yuboring.\n"
            "4. Agar haftalik bepul limitingiz tugagan bo'lsa, bot narxni hisoblab beradi.\n"
            "5. To'lov chekini (skrinshot) yuboring. Bot avtomatik tekshiradi.\n\n"
            "❗️ Maksimal fayl hajmi: 30 MB.")
    await message.answer(text, parse_mode="HTML")

@dp.message(F.text == "📢 Reklama Xizmati")
async def ads_handler(message: types.Message):
    text = ("Ushbu bot orqali minglab faol foydalanuvchilarga o'z reklamangizni tarqatishingiz mumkin.\n\n"
            "Batafsil ma'lumot va narxlar uchun admin bilan bog'laning:")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Admin bilan bogʻlanish", url=f"tg://user?id={ADMIN_ID}")]
    ])
    await message.answer(text, reply_markup=kb)

@dp.message(F.text == "🔙 Bosh menyu")
async def back_menu(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Bosh menyuga qaytildi.", reply_markup=main_menu)

@dp.message(F.text == "🔄 Konvertatsiya")
async def convert_menu_handler(message: types.Message):
    await message.answer("Qaysi turdagi faylni konvertatsiya qilmoqchisiz?", reply_markup=convert_menu)

# Konvertatsiya tugmalarini ushlash
@dp.message(F.text.in_({"DOCX ➡️ PDF", "PPTX ➡️ PDF", "EXCEL ➡️ PDF", "TXT ➡️ PDF"}))
async def ask_for_file(message: types.Message, state: FSMContext):
    file_type_map = {
        "DOCX ➡️ PDF": "docx",
        "PPTX ➡️ PDF": "pptx",
        "EXCEL ➡️ PDF": "excel",
        "TXT ➡️ PDF": "txt"
    }
    selected_type = file_type_map[message.text]
    await state.update_data(file_type=selected_type)
    await state.set_state(ConvertState.waiting_for_file)
    await message.answer(f"Iltimos, {selected_type.upper()} formatdagi faylni yuboring.\n"
                         f"Maksimal hajm: 30 MB.", reply_markup=types.ReplyKeyboardRemove())

# Faylni qabul qilish va tekshirish
@dp.message(ConvertState.waiting_for_file, F.document)
async def process_file(message: types.Message, state: FSMContext):
    data = await state.get_data()
    expected_type = data.get('file_type')
    document = message.document
    
    # Formatni tekshirish
    valid_extensions = {
        "docx": [".docx", ".doc"],
        "pptx": [".pptx", ".ppt"],
        "excel": [".xlsx", ".xls"],
        "txt": [".txt"]
    }
    
    file_ext = os.path.splitext(document.file_name)[1].lower()
    if file_ext not in valid_extensions[expected_type]:
        await message.answer(f"❌ Noto'g'ri format! Iltimos, {expected_type.upper()} fayl yuboring.")
        return

    # Hajmni tekshirish
    file_size_mb = document.file_size / (1024 * 1024)
    if file_size_mb > 30:
        await message.answer("❌ Fayl hajmi 30 MB dan katta. Qabul qilinmaydi.")
        return

    # Limitni tekshirish
    user_stat = get_user_stat(message.from_user.id)
    is_free = False
    
    # Bepul limit bormi?
    limit_key = f"free_{expected_type}"
    if user_stat[limit_key] and file_size_mb <= 20:
        is_free = True
    
    # Narx hisoblash
    price = 0
    if not is_free:
        price = calculate_price(file_size_mb)
        await state.update_data(price=price, file_id=document.file_id, file_name=document.file_name, is_free=False)
        
        await message.answer(
            f"💰 <b>To'lov talab qilinadi</b>\n\n"
            f"Fayl hajmi: {file_size_mb:.2f} MB\n"
            f"Xizmat narxi: <b>{price} UZS</b>\n\n"
            f"Iltimos, quyidagi kartaga to'lov qiling va chek rasmini (skrinshot) shu yerga yuboring:\n"
            f"💳 <code>5614 6812 9088 6526</code> (Abdulaziz To'lqinov)\n", # Karta raqamini o'zgartiring
            parse_mode="HTML"
        )
        await state.set_state(ConvertState.waiting_for_payment)
    else:
        # Bepul bo'lsa darhol konvertatsiya
        await message.answer("🔄 Bepul limitdan foydalanilmoqda. Konvertatsiya boshlandi...")
        await execute_conversion(message, document.file_id, document.file_name, state, is_free=True)

# To'lov chekini tekshirish
@dp.message(ConvertState.waiting_for_payment, F.photo)
async def verify_payment(message: types.Message, state: FSMContext):
    data = await state.get_data()
    price = data['price']
    
    # Rasmni yuklab olish
    photo = message.photo[-1]
    file_info = await bot.get_file(photo.file_id)
    photo_bytes = await bot.download_file(file_info.file_path)
    
    await message.answer("⏳ Chek tekshirilmoqda...")
    
    # OCR tekshiruv
    is_valid = ocr_verify_payment(photo_bytes.read(), price)
    
    # DIQQAT: Bu yerda haqiqiy loyihada OCR ga 100% ishonib bo'lmaydi.
    # Hozircha "narx rasmda bormi" deb tekshiryapmiz.
    
    if is_valid:
        await message.answer("✅ To'lov tasdiqlandi! Konvertatsiya boshlandi...")
        file_id = data['file_id']
        file_name = data['file_name']
        await execute_conversion(message, file_id, file_name, state, is_free=False)
    else:
        await message.answer("❌ To'lov chekida summa topilmadi yoki xatolik.\n"
                             "Iltimos, chekni tiniqroq holda qayta yuboring yoki admin bilan bog'laning.")

# Konvertatsiya jarayoni (Common)
async def execute_conversion(message, file_id, file_name, state, is_free):
    data = await state.get_data()
    file_type = data.get('file_type')
    price = data.get('price', 0)
    
    # Faylni yuklash
    file_info = await bot.get_file(file_id)
    input_path = f"temp_{message.from_user.id}_{file_name}"
    await bot.download_file(file_info.file_path, input_path)
    
    # Konvertatsiya
    output_dir = os.getcwd()
    pdf_path = await convert_to_pdf(input_path, output_dir)
    
    if pdf_path and os.path.exists(pdf_path):
        # Yuborish
        file_to_send = FSInputFile(pdf_path)
        await message.answer_document(file_to_send, caption="✅ Marhamat, faylingiz tayyor!")
        
        # Statistikani yangilash
        update_stat(message.from_user.id, file_type, not is_free, price if not is_free else 0)
        
        # Tozalash
        os.remove(pdf_path)
    else:
        await message.answer("❌ Konvertatsiya vaqtida xatolik yuz berdi. Fayl buzilgan bo'lishi mumkin.")
    
    # Kiruvchi faylni o'chirish
    if os.path.exists(input_path):
        os.remove(input_path)
        
    await state.clear()
    await message.answer("Yana nimadir bajaramizmi?", reply_markup=main_menu)

# --- BOTNI ISHGA TUSHIRISH ---
async def main():
    # Bazani yaratish
    init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
