#!/usr/bin/env python3
# bot.py
import os
import logging
import sqlite3
import requests
import tempfile
import difflib
import asyncio
import html
from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

# ----- Load env -----
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")  # for Whisper transcription
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
DB_PATH = os.getenv("DB_PATH", "bot.db")
LESSONS_PER_LEVEL = int(os.getenv("LESSONS_PER_LEVEL", "25"))

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN kerak — .env ga qo'shing.")

# ----- Logging -----
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ----- DB helpers -----
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            chat_id INTEGER,
            level TEXT,
            idx INTEGER,
            awaiting INTEGER,
            correct_count INTEGER,
            expected_text TEXT
        )
        """
    )
    conn.commit()
    conn.close()

def get_user(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT user_id, chat_id, level, idx, awaiting, correct_count, expected_text FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return {
            "user_id": row[0],
            "chat_id": row[1],
            "level": row[2],
            "idx": row[3],
            "awaiting": bool(row[4]),
            "correct_count": row[5],
            "expected_text": row[6],
        }
    return None

def create_or_update_user(user_id, chat_id, level=None, idx=None, awaiting=None, correct_count=None, expected_text=None):
    u = get_user(user_id)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if u is None:
        c.execute(
            "INSERT INTO users(user_id, chat_id, level, idx, awaiting, correct_count, expected_text) VALUES (?,?,?,?,?,?,?)",
            (user_id, chat_id, level or "", idx or 1, int(bool(awaiting)), correct_count or 0, expected_text or "")
        )
    else:
        updates = []
        params = []
        if level is not None:
            updates.append("level = ?"); params.append(level)
        if idx is not None:
            updates.append("idx = ?"); params.append(idx)
        if awaiting is not None:
            updates.append("awaiting = ?"); params.append(int(bool(awaiting)))
        if correct_count is not None:
            updates.append("correct_count = ?"); params.append(correct_count)
        if expected_text is not None:
            updates.append("expected_text = ?"); params.append(expected_text)
        if updates:
            params.append(user_id)
            c.execute(f"UPDATE users SET {', '.join(updates)} WHERE user_id = ?", params)
    conn.commit()
    conn.close()

# ----- Utility helpers -----
def normalize_text(t: str) -> str:
    if not t:
        return ""
    t = t.lower().strip()
    # remove simple punctuation
    for ch in ".,!?;:\"'()[]{}«»—–-":
        t = t.replace(ch, "")
    t = " ".join(t.split())
    return t

def similarity(a: str, b: str) -> float:
    a_n = normalize_text(a)
    b_n = normalize_text(b)
    if not a_n or not b_n:
        return 0.0
    return difflib.SequenceMatcher(None, a_n, b_n).ratio()

def map_level_label_to_folder(label: str) -> str:
    # labels in Uzbek UI -> folder names
    mapping = {
        "oson": "easy",
        "o'rtacha": "medium",
        "ortacha": "medium",
        "qiyin": "hard",
    }
    key = label.lower()
    return mapping.get(key, "easy")

async def fetch_text_from_base(level_folder: str, idx: int) -> str:
    """
    Tries to fetch {BASE_URL}/{level_folder}/{idx}.txt
    If fails, returns empty string.
    """
    if not BASE_URL:
        return ""
    url = f"{BASE_URL}/{level_folder}/{idx}.txt"
    try:
        r = await asyncio.to_thread(requests.get, url, {"timeout": 10})
        if r.status_code == 200:
            return r.text.strip()
    except Exception as e:
        logger.warning("fetch_text_from_base error: %s", e)
    return ""

async def send_lesson_for_user(app, user_id):
    """
    Sends current lesson audio+text to the user's chat using stored state.
    """
    user = get_user(user_id)
    if not user:
        return
    chat_id = user["chat_id"]
    level = user["level"] or "easy"
    idx = user["idx"] or 1

    if idx > LESSONS_PER_LEVEL:
        # completed
        await app.bot.send_message(chat_id, "🎉 Tabriklaymiz! Siz darajani yakunladingiz. /start orqali qayta boshlash mumkin.")
        create_or_update_user(user_id, chat_id, idx=1, awaiting=0, correct_count=0, expected_text="")
        return

    level_folder = level
    audio_url = f"{BASE_URL}/{level_folder}/{idx}.mp3" if BASE_URL else None
    text_content = await fetch_text_from_base(level_folder, idx)
    if not text_content:
        # fallback sample text if remote missing (to keep bot usable)
        text_content = f"[{level_folder} - {idx}] (matn mavjud emas. Admin faylni joylang.)"

    caption = f"Daraja: {level_folder.upper()} — so'z #{idx}\n\nMatn:\n{html.escape(text_content)}\n\nIltimos, audio eshiting va so'zni takrorlab, ovozli xabar yuboring."
    # send audio (URL if available), otherwise notify
    try:
        if audio_url:
            # send as audio by URL
            await app.bot.send_message(chat_id, "Audio yuborilyapti...")
            await app.bot.send_audio(chat_id, audio_url, caption=caption)
        else:
            await app.bot.send_message(chat_id, caption)
    except Exception as e:
        logger.exception("xato audio yuborishda: %s", e)
        # still send text
        await app.bot.send_message(chat_id, caption)

    # set expected_text in DB for quicker access
    create_or_update_user(user_id, chat_id, expected_text=text_content, awaiting=1)

    # Send inline keyboard with skip & menu
    kb = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("🔄 Qayta urinib ko'rish", callback_data=f"action:retry"),
            InlineKeyboardButton("⏭ Tashlab ketish", callback_data=f"action:skip")
        ],
        [
            InlineKeyboardButton("🏠 Menyu", callback_data="menu:main")
        ]]
    )
    await app.bot.send_message(chat_id, "Agar talaffuzni tekshashni xohlasangiz, ovozli xabar yuboring. Yoki biror tugmani bosing.", reply_markup=kb)

# ----- OpenAI Whisper transcription (sync) -----
def transcribe_with_openai(file_path: str) -> str:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY o'rnatilmagan.")
    url = "https://api.openai.com/v1/audio/transcriptions"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
    # use model whisper-1
    files = {
        "file": open(file_path, "rb")
    }
    data = {
        "model": "whisper-1",
        # optionally you can set "language": "ru" to bias recognition to russian
        "language": "ru"
    }
    try:
        r = requests.post(url, headers=headers, files=files, data=data, timeout=120)
    finally:
        files["file"].close()
    if r.status_code == 200:
        j = r.json()
        return j.get("text", "").strip()
    else:
        logger.error("OpenAI transcription failed: %s - %s", r.status_code, r.text)
        raise RuntimeError(f"OpenAI transcription error: {r.status_code}")

# ----- Handlers -----
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    # initialize db user row
    create_or_update_user(user.id, chat_id, level=None, idx=1, awaiting=0, correct_count=0, expected_text="")
    # main menu
    text = "Salom! Men rus tilini o'rganishda yordam beradigan AI botman.\n\nQuyidagi bo'limlardan birini tanlang:"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Darslarni boshlash", callback_data="menu:lessons")],
        [InlineKeyboardButton("📖 Lug'at", callback_data="menu:vocab"), InlineKeyboardButton("⚙️ Sozlamalar", callback_data="menu:settings")],
        [InlineKeyboardButton("💬 Fikr qoldirish", callback_data="menu:feedback"), InlineKeyboardButton("🤝 Do'stlarga ulashish", callback_data="menu:share")],
    ])
    await update.message.reply_text(text, reply_markup=kb)

async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    user_id = query.from_user.id
    chat_id = query.message.chat_id

    if data == "menu:main":
        # show main menu again
        text = "Asosiy menyu:"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📝 Darslarni boshlash", callback_data="menu:lessons")],
            [InlineKeyboardButton("📖 Lug'at", callback_data="menu:vocab"), InlineKeyboardButton("⚙️ Sozlamalar", callback_data="menu:settings")],
            [InlineKeyboardButton("💬 Fikr qoldirish", callback_data="menu:feedback"), InlineKeyboardButton("🤝 Do'stlarga ulashish", callback_data="menu:share")],
        ])
        await query.edit_message_text(text, reply_markup=kb)
        return

    if data == "menu:lessons":
        # show level choices
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🟢 Oson", callback_data="level:oson")],
            [InlineKeyboardButton("🟡 O'rtacha", callback_data="level:o'rtacha")],
            [InlineKeyboardButton("🔴 Qiyin", callback_data="level:qiyin")],
            [InlineKeyboardButton("🏠 Menyu", callback_data="menu:main")],
        ])
        await query.edit_message_text("Darajani tanlang:", reply_markup=kb)
        return

    if data.startswith("level:"):
        lvl_label = data.split(":",1)[1]
        folder = map_level_label_to_folder(lvl_label)
        # set user state
        create_or_update_user(user_id, chat_id, level=folder, idx=1, awaiting=0, correct_count=0, expected_text="")
        await query.edit_message_text(f"Tanlandi: {folder.upper()}. Dars boshlanmoqda...")
        # send first lesson
        await send_lesson_for_user(context.application, user_id)
        return

    if data == "menu:vocab":
        # try to fetch a vocab file, else show sample 10 words
        sample = [
            "привет — salom",
            "спасибо — rahmat",
            "пожалуйста — iltimos",
            "до свидания — xayr",
            "да — ha",
            "нет — yo'q",
            "извините — kechirasiz",
            "я — men",
            "ты — sen",
            "хорошо — yaxshi"
        ]
        text = "Lug'at: (10 ta misol)\n\n" + "\n".join(sample)
        await query.edit_message_text(text)
        return

    if data == "menu:settings":
        await query.edit_message_text("Sozlamalar:\n— Hozircha sozlamalar mavjud emas. Keyinchalik qo'shiladi.")
        return

    if data == "menu:feedback":
        await query.edit_message_text("Fikr bildirish: Iltimos, fikr va takliflaringizni yozing. Keyinchalik biz ularni qayta ishlaymiz.")
        return

    if data == "menu:share":
        bot_user = await context.bot.get_me()
        username = bot_user.username or "your_bot"
        ref_link = f"https://t.me/{username}"
        await query.edit_message_text(f"Do'stlaringizga ulashish uchun havola:\n{ref_link}")
        return

    # actions: retry, skip, next
    if data == "action:retry":
        # simply prompt user to send voice again
        create_or_update_user(user_id, chat_id, awaiting=1)
        await context.bot.send_message(chat_id, "Iltimos, ovozli xabar yuboring (so'zni takrorlang).")
        await query.answer("Qayta yuborish uchun ovozli xabar yuboring.", show_alert=False)
        return

    if data == "action:skip":
        # increment index and send next
        user = get_user(user_id)
        if not user:
            await query.answer("Sizning holatingiz topilmadi.", show_alert=True)
            return
        new_idx = (user["idx"] or 1) + 1
        if new_idx > LESSONS_PER_LEVEL:
            await context.bot.send_message(chat_id, "Siz bu darajani tugatdingiz!")
            create_or_update_user(user_id, chat_id, awaiting=0, idx=1, expected_text="")
            await query.answer()
            return
        create_or_update_user(user_id, chat_id, idx=new_idx, awaiting=0, expected_text="")
        await query.edit_message_text("So'z tashlab ketildi. Keyingi so'z yuborilyapti...")
        await send_lesson_for_user(context.application, user_id)
        return

    if data == "action:next":
        user = get_user(user_id)
        if not user:
            await query.answer("Sizning holatingiz topilmadi.", show_alert=True)
            return
        new_idx = (user["idx"] or 1) + 1
        if new_idx > LESSONS_PER_LEVEL:
            await context.bot.send_message(chat_id, "Siz bu darajani tugatdingiz! 🎉")
            create_or_update_user(user_id, chat_id, awaiting=0, idx=1, expected_text="")
            await query.answer()
            return
        create_or_update_user(user_id, chat_id, idx=new_idx, awaiting=0, expected_text="")
        await query.edit_message_text("Keyingi so'zga o'tilyapti...")
        await send_lesson_for_user(context.application, user_id)
        return

    # default fallback
    await query.answer()

async def voice_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles incoming voice messages or audio messages when user is awaiting answer.
    """
    user = update.effective_user
    user_row = get_user(user.id)
    if not user_row or not user_row.get("awaiting"):
        await update.message.reply_text("Hozir topshiriq yo'q. /start orqali darsni boshlang.")
        return

    chat_id = update.effective_chat.id
    # determine message contains voice or audio
    voice = update.message.voice or update.message.audio
    if not voice:
        await update.message.reply_text("Iltimos, ovozli xabar yuboring (telegram voice message).")
        return

    # download the file to temp
    tmpf = None
    try:
        file = await context.bot.get_file(voice.file_id)
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".ogg")
        tmpf = tmp.name
        tmp.close()
        await file.download_to_drive(tmpf)
    except Exception as e:
        logger.exception("fayl yuklashda xato: %s", e)
        await update.message.reply_text("Ovoz yuklanmadi, qaytadan urinib ko'ring.")
        if tmpf and os.path.exists(tmpf):
            os.remove(tmpf)
        return

    # transcribe (use OpenAI Whisper if available)
    transcribed = ""
    try:
        if OPENAI_API_KEY:
            # run blocking network call in thread
            transcribed = await asyncio.to_thread(transcribe_with_openai, tmpf)
        else:
            # fallback: we cannot transcribe without API key
            await update.message.reply_text("Afsuski, serverda OpenAI API kaliti o'rnatilmagan. Iltimos matnni to'g'ridan-to'g'ri yozing yoki admindan kalit qo'yishni so'rang.")
            if tmpf and os.path.exists(tmpf):
                os.remove(tmpf)
            return
    except Exception as e:
        logger.exception("transcribe error: %s", e)
        await update.message.reply_text("Transkripsiya xatosi yuz berdi. Keyinroq qayta urinib ko'ring.")
        if tmpf and os.path.exists(tmpf):
            os.remove(tmpf)
        return
    finally:
        if tmpf and os.path.exists(tmpf):
            os.remove(tmpf)

    logger.info("Transcribed text: %s", transcribed)
    expected = (user_row.get("expected_text") or "").strip()
    if not expected:
        # try to fetch from base (in case not stored)
        expected = await fetch_text_from_base(user_row["level"], user_row["idx"])

    # compare
    sim = similarity(expected, transcribed)
    logger.info("Expected: %s ; Got: %s ; sim=%.3f", expected, transcribed, sim)

    THRESHOLD = 0.75  # you can tune this
    if sim >= THRESHOLD:
        # correct
        create_or_update_user(user.id, chat_id, awaiting=0, correct_count=(user_row.get("correct_count") or 0) + 1)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("➡️ Keyingi so'z", callback_data="action:next")],
            [InlineKeyboardButton("🏠 Menyu", callback_data="menu:main")]
        ])
        await update.message.reply_text("✅ Siz to'g'ri talaffuz qildingiz!", reply_markup=kb)
    else:
        # incorrect
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Qayta urinib ko'rish", callback_data="action:retry"),
             InlineKeyboardButton("⏭ Tashlab ketish", callback_data="action:skip")],
            [InlineKeyboardButton("🏠 Menyu", callback_data="menu:main")]
        ])
        await update.message.reply_text("❌ Talaffuzingiz yaxshi emas, iltimos qayta urinib ko'ring yoki tashlab ketish tugmasini bosing.", reply_markup=kb)
        # keep awaiting = 1 (user can retry)

async def text_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    If user cannot send voice (or OpenAI not configured), allow text answer fallback.
    """
    user = update.effective_user
    user_row = get_user(user.id)
    if not user_row or not user_row.get("awaiting"):
        # no specific lesson waiting
        return
    chat_id = update.effective_chat.id
    user_answer = update.message.text.strip()
    expected = (user_row.get("expected_text") or "").strip()
    if not expected:
        expected = await fetch_text_from_base(user_row["level"], user_row["idx"])
    sim = similarity(expected, user_answer)
    THRESHOLD = 0.75
    if sim >= THRESHOLD:
        create_or_update_user(user.id, chat_id, awaiting=0, correct_count=(user_row.get("correct_count") or 0) + 1)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("➡️ Keyingi so'z", callback_data="action:next")]])
        await update.message.reply_text("✅ Matn to'g'ri! (text-fallback).", reply_markup=kb)
    else:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Qayta urinib ko'rish", callback_data="action:retry"),
             InlineKeyboardButton("⏭ Tashlab ketish", callback_data="action:skip")]
        ])
        await update.message.reply_text("❌ Matn noaniq. Iltimos yana urinib ko'ring yoki tashlab keting.", reply_markup=kb)


# ----- Main -----
def main():
    init_db()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(menu_callback))
    # voice & audio messages
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, voice_message_handler))
    # text messages used for fallback answers
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), text_message_handler))

    logger.info("Bot ishga tushmoqda...")
    app.run_polling()

if __name__ == "__main__":
    main()

