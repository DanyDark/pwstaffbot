import sqlite3
import logging
import os
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# ------------------ НАСТРОЙКИ ------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("Не задана переменная окружения BOT_TOKEN")

# Список админов (user_id через запятую)
ADMIN_IDS = os.environ.get("ADMIN_IDS", "")
ADMIN_LIST = [int(x.strip()) for x in ADMIN_IDS.split(",") if x.strip()]

WHITELIST_FILE = "whitelist.txt"
DB_FILE = "/data/users.db"   # постоянное хранилище на Bothost
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

def is_admin(user_id):
    return user_id in ADMIN_LIST

def get_all_users():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, nick, registered_at FROM users ORDER BY registered_at")
    users = cursor.fetchall()
    conn.close()
    return users

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

# --- АДМИН-КОМАНДЫ ---
async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("Эта команда доступна только администратору.")
        return
    
    users = get_all_users()
    if not users:
        await update.message.reply_text("Список зарегистрированных пользователей пуст.")
        return
    
    text = "📋 *Список зарегистрированных пользователей:*\n\n"
    for uid, nick, reg_date in users:
        text += f"• {nick} (ID: `{uid}`) — зарегистрирован {reg_date}\n"
        if len(text) > 3800:  # Telegram ограничение ~4096 символов
            await update.message.reply_text(text, parse_mode="Markdown")
            text = ""
    if text:
        await update.message.reply_text(text, parse_mode="Markdown")

async def count_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("Эта команда доступна только администратору.")
        return
    
    users = get_all_users()
    count = len(users)
    await update.message.reply_text(f"👥 Всего зарегистрировано пользователей: {count}")

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("users", users_command))
    app.add_handler(CommandHandler("count", count_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    
    print("Бот запущен. Нажмите Ctrl+C для остановки.")
    app.run_polling()

if __name__ == "__main__":
    main()
