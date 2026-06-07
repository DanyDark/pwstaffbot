import sqlite3
import logging
import os
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# ================= НАСТРОЙКИ =================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("Не задана переменная окружения BOT_TOKEN")

ADMIN_IDS = os.environ.get("ADMIN_IDS", "")
ADMIN_LIST = [int(x.strip()) for x in ADMIN_IDS.split(",") if x.strip()]

WHITELIST_FILE = "whitelist.txt"
DATA_DIR = os.environ.get("DATA_DIR", "/data")
DB_FILE = os.path.join(DATA_DIR, "users.db")
# =============================================

logging.basicConfig(level=logging.INFO)

# ---------- Работа с БД и белым списком (без изменений) ----------
def init_db():
    db_dir = os.path.dirname(DB_FILE)
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)
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

def get_user_nick(user_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT nick FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None

def get_all_users():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, nick, registered_at FROM users ORDER BY registered_at")
    users = cursor.fetchall()
    conn.close()
    return users

def is_admin(user_id):
    return user_id in ADMIN_LIST

# ---------- Клавиатуры ----------
def get_main_keyboard(user_id):
    """Возвращает ReplyKeyboardMarkup в зависимости от прав пользователя"""
    keyboard = [
        [KeyboardButton("👤 Мой профиль"), KeyboardButton("❓ Помощь")]
    ]
    if is_admin(user_id):
        keyboard.append([KeyboardButton("👥 Список пользователей"), KeyboardButton("🔢 Количество")])
        # Позже добавим: KeyboardButton("📤 Экспорт Excel")
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# ---------- Обработчики ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if is_registered(user_id):
        nick = get_user_nick(user_id)
        await update.message.reply_text(
            f"С возвращением, {nick}!",
            reply_markup=get_main_keyboard(user_id)
        )
    else:
        context.user_data['awaiting_nick'] = True
        await update.message.reply_text(
            "Привет! Вы не зарегистрированы.\n"
            "Пожалуйста, введите свой ник из списка (или отправьте картинку с ником — позже)."
        )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text

    # Если пользователь в процессе ввода ника
    if context.user_data.get('awaiting_nick'):
        nick = text.strip()
        whitelist = load_whitelist()
        if nick in whitelist:
            register_user(user_id, nick)
            context.user_data['awaiting_nick'] = False
            await update.message.reply_text(
                f"Отлично, {nick}! Вы успешно зарегистрированы.",
                reply_markup=get_main_keyboard(user_id)
            )
        else:
            await update.message.reply_text(
                "Ник не найден в белом списке. Попробуйте ещё раз или обратитесь к администратору."
            )
        return

    # Обработка кнопок меню (только для зарегистрированных пользователей)
    if not is_registered(user_id):
        await update.message.reply_text("Пожалуйста, начните с /start для регистрации.")
        return

    if text == "👤 Мой профиль":
        nick = get_user_nick(user_id)
        await update.message.reply_text(f"Ваш ник: {nick}\nВаш Telegram ID: `{user_id}`", parse_mode="Markdown")
    elif text == "❓ Помощь":
        await update.message.reply_text(
            "Доступные команды:\n"
            "/start — показать меню\n"
            "/menu — показать это меню\n"
            "/hide — скрыть клавиатуру\n\n"
            "Кнопки:\n"
            "👤 Мой профиль — посмотреть свой ник и ID\n"
            "❓ Помощь — это сообщение\n" +
            ("👥 Список пользователей — показать всех (админ)\n"
             "🔢 Количество — число зарегистрированных (админ)\n" if is_admin(user_id) else "")
        )
    elif text == "👥 Список пользователей" and is_admin(user_id):
        users = get_all_users()
        if not users:
            await update.message.reply_text("Список зарегистрированных пользователей пуст.")
            return
        msg = "📋 *Список пользователей:*\n\n"
        for uid, nick, reg_date in users:
            msg += f"• {nick} (ID: `{uid}`) — {reg_date}\n"
            if len(msg) > 3800:
                await update.message.reply_text(msg, parse_mode="Markdown")
                msg = ""
        if msg:
            await update.message.reply_text(msg, parse_mode="Markdown")
    elif text == "🔢 Количество" and is_admin(user_id):
        count = len(get_all_users())
        await update.message.reply_text(f"👥 Зарегистрировано пользователей: {count}")
    else:
        await update.message.reply_text(
            "Используйте кнопки меню или команды /start, /menu, /hide",
            reply_markup=get_main_keyboard(user_id)
        )

async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать главное меню с клавиатурой"""
    user_id = update.effective_user.id
    if not is_registered(user_id):
        await update.message.reply_text("Вы не зарегистрированы. Напишите /start для регистрации.")
        return
    await update.message.reply_text("Главное меню:", reply_markup=get_main_keyboard(user_id))

async def hide_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Скрыть клавиатуру"""
    await update.message.reply_text("Клавиатура скрыта. Напишите /menu чтобы показать снова.", reply_markup=None)

# ---------- Старые админ-команды (оставляем для обратной совместимости) ----------
async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("Доступно только администратору.")
        return
    users = get_all_users()
    if not users:
        await update.message.reply_text("Список пуст.")
        return
    msg = "📋 *Список пользователей:*\n\n"
    for uid, nick, reg_date in users:
        msg += f"• {nick} (ID: `{uid}`) — {reg_date}\n"
        if len(msg) > 3800:
            await update.message.reply_text(msg, parse_mode="Markdown")
            msg = ""
    if msg:
        await update.message.reply_text(msg, parse_mode="Markdown")

async def count_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("Доступно только администратору.")
        return
    count = len(get_all_users())
    await update.message.reply_text(f"👥 Зарегистрировано: {count}")

# ---------- Запуск ----------
def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    # Команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(CommandHandler("hide", hide_command))
    app.add_handler(CommandHandler("users", users_command))
    app.add_handler(CommandHandler("count", count_command))

    # Обработчик текстовых сообщений (кнопки, ввод ника)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print("Бот запущен. Нажмите Ctrl+C для остановки.")
    app.run_polling()

if __name__ == "__main__":
    main()
