import sqlite3
import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# ------------------ НАСТРОЙКИ ------------------
BOT_TOKEN = '8909227571:AAHRgxTIK1QIiJpCX-QuG2u8jB0mLqtFMAc'   # ВСТАВЬ СЮДА ТОКЕН!
WHITELIST_FILE = "whitelist.txt"
DB_FILE = "/data/users.db"
# ----------------------------------------------

logging.basicConfig(level=logging.INFO)

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            nick TEXT NOT NULL,
            registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

def load_whitelist():
    whitelist = set()
    try:
        with open(WHITELIST_FILE, 'r', encoding='utf-8') as f:
            for line in f:
                nick = line.strip()
                if nick:
                    whitelist.add(nick)
    except FileNotFoundError:
        logging.error(f"Файл {WHITELIST_FILE} не найден!")
    return whitelist

def is_registered(user_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    conn.close()
    return result is not None

def register_user(user_id, nick):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO users (user_id, nick) VALUES (?, ?)", (user_id, nick))
    conn.commit()
    conn.close()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if is_registered(user_id):
        await update.message.reply_text("Вы уже зарегистрированы! Добро пожаловать.")
    else:
        context.user_data['awaiting_nick'] = True
        await update.message.reply_text(
            "Привет! Вы не зарегистрированы.\n"
            "Пожалуйста, введите свой ник из списка."
        )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if context.user_data.get('awaiting_nick'):
        nick = update.message.text.strip()
        whitelist = load_whitelist()
        # Регистрозависимая проверка
        if nick in whitelist:
            register_user(user_id, nick)
            context.user_data['awaiting_nick'] = False
            await update.message.reply_text(f"Отлично, {nick}! Вы успешно зарегистрированы.")
        else:
            await update.message.reply_text(
                "Ник не найден в белом списке. Попробуйте ещё раз или обратитесь к администратору."
            )
    else:
        await update.message.reply_text("Вы уже зарегистрированы. Если нужна помощь, напишите /start")

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    
    print("Бот запущен. Нажмите Ctrl+C для остановки.")
    app.run_polling()

if __name__ == "__main__":
    main()
