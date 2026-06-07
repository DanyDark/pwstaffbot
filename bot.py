import sqlite3
import logging
import os
import json
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

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

# ---------- БАЗА ДАННЫХ ----------
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
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS polls (
            poll_id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL,
            meetings_json TEXT NOT NULL,
            is_active INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS poll_responses (
            user_id INTEGER,
            poll_id INTEGER,
            meeting TEXT,
            answer TEXT,
            responded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, poll_id, meeting)
        )
    ''')
    conn.commit()
    conn.close()

# ---------- ПОЛЬЗОВАТЕЛИ ----------
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

# ---------- ОПРОСЫ ----------
def create_poll(text, meetings):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("UPDATE polls SET is_active = 0")
    meetings_json = json.dumps(meetings)
    cursor.execute("INSERT INTO polls (text, meetings_json, is_active) VALUES (?, ?, 1)", (text, meetings_json))
    conn.commit()
    conn.close()

def get_active_poll():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT poll_id, text, meetings_json FROM polls WHERE is_active = 1")
    row = cursor.fetchone()
    conn.close()
    if row:
        return {"id": row[0], "text": row[1], "meetings": json.loads(row[2])}
    return None

def deactivate_poll():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("UPDATE polls SET is_active = 0")
    conn.commit()
    conn.close()

def save_responses(user_id, poll_id, responses_dict):
    """responses_dict = {meeting: answer}"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    for meeting, answer in responses_dict.items():
        cursor.execute('''
            INSERT OR REPLACE INTO poll_responses (user_id, poll_id, meeting, answer, responded_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
        ''', (user_id, poll_id, meeting, answer))
    conn.commit()
    conn.close()

def get_user_responses_for_poll(user_id, poll_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT meeting, answer FROM poll_responses WHERE user_id = ? AND poll_id = ?", (user_id, poll_id))
    rows = cursor.fetchall()
    conn.close()
    return {meeting: answer for meeting, answer in rows}

def get_response_summary(poll_id, meetings):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT u.nick, pr.meeting, pr.answer
        FROM poll_responses pr
        JOIN users u ON pr.user_id = u.user_id
        WHERE pr.poll_id = ?
        ORDER BY pr.meeting, u.nick
    ''', (poll_id,))
    rows = cursor.fetchall()
    conn.close()
    summary = {m: {} for m in meetings}
    for nick, meeting, answer in rows:
        if meeting in summary:
            summary[meeting][nick] = answer
    return summary

# ---------- КЛАВИАТУРЫ ----------
def get_main_keyboard(user_id):
    keyboard = [[KeyboardButton("👤 Мой профиль"), KeyboardButton("❓ Помощь")]]
    if is_admin(user_id):
        keyboard.append([KeyboardButton("📊 Админ-панель")])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_admin_keyboard():
    keyboard = [
        [KeyboardButton("📝 Создать опрос"), KeyboardButton("📤 Разослать опрос")],
        [KeyboardButton("📈 Результаты опроса"), KeyboardButton("📋 Текущий опрос")],
        [KeyboardButton("🚫 Завершить опрос"), KeyboardButton("👥 Список пользователей")],
        [KeyboardButton("🔢 Количество"), KeyboardButton("🔙 Назад")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_meeting_keyboard(poll_id, meeting, index, total):
    """Inline-клавиатура для ответа на одну встречу"""
    keyboard = [
        [
            InlineKeyboardButton("✅ Да", callback_data=f"poll_{poll_id}_{meeting}_да"),
            InlineKeyboardButton("❌ Нет", callback_data=f"poll_{poll_id}_{meeting}_нет"),
            InlineKeyboardButton("❓ Не знаю", callback_data=f"poll_{poll_id}_{meeting}_не знаю")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_confirm_keyboard():
    """Клавиатура для подтверждения ответов"""
    keyboard = [
        [InlineKeyboardButton("✅ Да, всё верно", callback_data="confirm_yes")],
        [InlineKeyboardButton("❌ Нет, пройти заново", callback_data="confirm_no")]
    ]
    return InlineKeyboardMarkup(keyboard)

# ---------- ОБРАБОТЧИКИ ПОЛЬЗОВАТЕЛЯ ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if is_registered(user_id):
        nick = get_user_nick(user_id)
        await update.message.reply_text(f"С возвращением, {nick}!", reply_markup=get_main_keyboard(user_id))
    else:
        context.user_data['awaiting_nick'] = True
        await update.message.reply_text(
            "Привет! Вы не зарегистрированы.\nПожалуйста, введите свой ник из списка."
        )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text

    # Регистрация
    if context.user_data.get('awaiting_nick'):
        nick = text.strip()
        whitelist = load_whitelist()
        if nick in whitelist:
            register_user(user_id, nick)
            context.user_data['awaiting_nick'] = False
            await update.message.reply_text(f"Отлично, {nick}! Вы зарегистрированы.", reply_markup=get_main_keyboard(user_id))
        else:
            await update.message.reply_text("Ник не найден в белом списке. Попробуйте ещё раз.")
        return

    if not is_registered(user_id):
        await update.message.reply_text("Пожалуйста, начните с /start для регистрации.")
        return

    # Кнопки главного меню
    if text == "👤 Мой профиль":
        nick = get_user_nick(user_id)
        await update.message.reply_text(f"Ваш ник: {nick}\nВаш Telegram ID: `{user_id}`", parse_mode="Markdown")
    elif text == "❓ Помощь":
        await update.message.reply_text("Используйте кнопки меню. /start — показать меню.")
    elif text == "📊 Админ-панель" and is_admin(user_id):
        await update.message.reply_text("Админ-панель:", reply_markup=get_admin_keyboard())
    elif text == "🔙 Назад" and is_admin(user_id):
        await update.message.reply_text("Главное меню:", reply_markup=get_main_keyboard(user_id))

    # Админ-команды (через кнопки)
    elif text == "📝 Создать опрос" and is_admin(user_id):
        context.user_data['poll_creation'] = {'step': 'text'}
        await update.message.reply_text(
            "Введите текст объявления для опроса.\n"
            "После этого вводите встречи по одной строке.\n"
            "В конце нажмите кнопку «✅ Завершить создание»."
        )
    elif text == "📤 Разослать опрос" and is_admin(user_id):
        await send_poll_to_all(update, context)
    elif text == "📈 Результаты опроса" and is_admin(user_id):
        await show_results(update, context)
    elif text == "📋 Текущий опрос" and is_admin(user_id):
        poll = get_active_poll()
        if poll:
            await update.message.reply_text(f"*Текущий опрос:*\n\n{poll['text']}\n\nВстречи: {', '.join(poll['meetings'])}", parse_mode="Markdown")
        else:
            await update.message.reply_text("Нет активного опроса.")
    elif text == "🚫 Завершить опрос" and is_admin(user_id):
        deactivate_poll()
        await update.message.reply_text("Текущий опрос завершён.")
    elif text == "👥 Список пользователей" and is_admin(user_id):
        users = get_all_users()
        if not users:
            await update.message.reply_text("Нет пользователей.")
            return
        msg = "📋 *Список пользователей:*\n"
        for uid, nick, reg_date in users:
            msg += f"• {nick} (ID: `{uid}`) — {reg_date}\n"
            if len(msg) > 3800:
                await update.message.reply_text(msg, parse_mode="Markdown")
                msg = ""
        if msg:
            await update.message.reply_text(msg, parse_mode="Markdown")
    elif text == "🔢 Количество" and is_admin(user_id):
        count = len(get_all_users())
        await update.message.reply_text(f"👥 Зарегистрировано: {count}")
    else:
        await update.message.reply_text("Неизвестная команда. Используйте кнопки меню.")

# ---------- СОЗДАНИЕ ОПРОСА (АДМИН) ----------
async def handle_poll_creation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id) or 'poll_creation' not in context.user_data:
        return
    data = context.user_data['poll_creation']
    text = update.message.text

    if data['step'] == 'text':
        data['text'] = text
        data['meetings'] = []
        data['step'] = 'meeting'
        await update.message.reply_text(
            "Теперь вводите встречи по одной строке.\n"
            "После добавления всех встреч нажмите кнопку «✅ Завершить создание»."
        )
    elif data['step'] == 'meeting':
        data['meetings'].append(text.strip())
        await update.message.reply_text(f"➕ Добавлена встреча: {text}. Введите следующую или нажмите кнопку ниже для завершения.",
                                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Завершить создание", callback_data="finish_poll_creation")]]))

async def finish_poll_creation_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if not is_admin(user_id) or 'poll_creation' not in context.user_data:
        await query.edit_message_text("Нет активного процесса создания опроса.")
        return
    data = context.user_data['poll_creation']
    if data.get('step') != 'meeting' or not data.get('meetings'):
        await query.edit_message_text("Вы не добавили ни одной встречи. Опрос не создан. Используйте /cancel для отмены.")
        return
    create_poll(data['text'], data['meetings'])
    await query.edit_message_text(
        f"✅ Опрос создан!\n\nТекст: {data['text']}\nВстречи: {', '.join(data['meetings'])}"
    )
    del context.user_data['poll_creation']

# ---------- ПОСЛЕДОВАТЕЛЬНАЯ РАССЫЛКА ОПРОСА ----------
async def send_poll_to_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("Доступно только администратору.")
        return
    poll = get_active_poll()
    if not poll:
        await update.message.reply_text("Нет активного опроса. Сначала создайте опрос через '📝 Создать опрос'.")
        return
    users = get_all_users()
    if not users:
        await update.message.reply_text("Нет зарегистрированных пользователей.")
        return

    await update.message.reply_text(f"Начинаю последовательную рассылку опроса {len(users)} пользователям...")
    success = 0
    for uid, nick, _ in users:
        # Инициализируем сессию опроса для пользователя
        context.application.chat_data[uid] = {
            'poll_id': poll['id'],
            'meetings': poll['meetings'],
            'current_index': 0,
            'temp_answers': {}
        }
        try:
            # Отправляем первый вопрос
            await send_next_question(uid, context.application.bot, context)
            success += 1
        except Exception as e:
            logging.error(f"Не удалось начать опрос для {uid}: {e}")
    await update.message.reply_text(f"Рассылка инициирована. Отправлено первое сообщение {success} из {len(users)} пользователям.")

async def send_next_question(chat_id, bot, context):
    """Отправляет следующий вопрос пользователю, если есть неотвеченные встречи"""
    user_data = context.application.chat_data.get(chat_id)
    if not user_data:
        return
    idx = user_data['current_index']
    meetings = user_data['meetings']
    if idx >= len(meetings):
        # Все встречи пройдены — показываем сводку
        await show_confirmation(chat_id, bot, context)
        return
    meeting = meetings[idx]
    poll_id = user_data['poll_id']
    keyboard = get_meeting_keyboard(poll_id, meeting, idx+1, len(meetings))
    await bot.send_message(
        chat_id=chat_id,
        text=f"📋 *Вопрос {idx+1} из {len(meetings)}*\n\n{meeting}\n\nВаш ответ:",
        parse_mode="Markdown",
        reply_markup=keyboard
    )

async def show_confirmation(chat_id, bot, context):
    """Показывает сводку ответов и кнопки подтверждения"""
    user_data = context.application.chat_data.get(chat_id)
    if not user_data:
        return
    answers = user_data['temp_answers']
    meetings = user_data['meetings']
    text = "✅ *Ваши ответы:*\n\n"
    for m in meetings:
        ans = answers.get(m, "❌ Не отвечен")
        text += f"• {m} → {ans}\n"
    text += "\nВсё верно?"
    keyboard = get_confirm_keyboard()
    await bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown", reply_markup=keyboard)

async def poll_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка ответа на конкретную встречу"""
    query = update.callback_query
    await query.answer()
    data = query.data
    parts = data.split('_', 3)
    if len(parts) != 4 or parts[0] != 'poll':
        await query.edit_message_text("Ошибка: некорректные данные.")
        return
    _, poll_id_str, meeting, answer = parts
    poll_id = int(poll_id_str)
    user_id = query.from_user.id
    if not is_registered(user_id):
        await query.edit_message_text("Вы не зарегистрированы. Напишите /start")
        return

    user_data = context.application.chat_data.get(user_id)
    if not user_data or user_data['poll_id'] != poll_id:
        await query.edit_message_text("Этот опрос уже не активен или не найден. Начните заново с /start.")
        return

    # Сохраняем временный ответ
    user_data['temp_answers'][meeting] = answer
    user_data['current_index'] += 1

    # Удаляем клавиатуру у текущего сообщения
    await query.edit_message_text(f"✅ Ваш ответ на '{meeting}': {answer}\n\nСпасибо!")

    # Отправляем следующий вопрос
    await send_next_question(user_id, context.bot, context)

async def confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Подтверждение или отказ от ответов"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user_data = context.application.chat_data.get(user_id)
    if not user_data:
        await query.edit_message_text("Нет активного опроса.")
        return

    if query.data == "confirm_yes":
        # Сохраняем ответы в БД
        save_responses(user_id, user_data['poll_id'], user_data['temp_answers'])
        await query.edit_message_text("✅ Спасибо! Ваши ответы сохранены. Опрос завершён.")
        # Очищаем сессию
        del context.application.chat_data[user_id]
    elif query.data == "confirm_no":
        # Сбрасываем и начинаем заново
        user_data['temp_answers'] = {}
        user_data['current_index'] = 0
        await query.edit_message_text("🔄 Начинаем опрос заново. Пожалуйста, ответьте на вопросы.")
        await send_next_question(user_id, context.bot, context)

# ---------- РЕЗУЛЬТАТЫ ДЛЯ АДМИНА ----------
async def show_results(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("Доступно только администратору.")
        return
    poll = get_active_poll()
    if not poll:
        await update.message.reply_text("Нет активного опроса.")
        return
    summary = get_response_summary(poll['id'], poll['meetings'])
    result_text = f"📊 *Результаты опроса (ID {poll['id']})*\n\n{poll['text']}\n\n"
    for meeting in poll['meetings']:
        result_text += f"*{meeting}*:\n"
        responses = summary[meeting]
        if not responses:
            result_text += "  Нет ответов.\n"
        else:
            for nick, ans in responses.items():
                result_text += f"  • {nick} → {ans}\n"
        result_text += "\n"
        if len(result_text) > 3800:
            await update.message.reply_text(result_text, parse_mode="Markdown")
            result_text = ""
    if result_text:
        await update.message.reply_text(result_text, parse_mode="Markdown")

# ---------- ОБЫЧНЫЕ АДМИН-КОМАНДЫ ----------
async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("Доступно только администратору.")
        return
    users = get_all_users()
    if not users:
        await update.message.reply_text("Нет пользователей.")
        return
    msg = "📋 *Список пользователей:*\n"
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
    await update.message.reply_text(f"👥 Всего зарегистрировано: {count}")

async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_registered(user_id):
        await update.message.reply_text("Вы не зарегистрированы. Напишите /start.")
        return
    await update.message.reply_text("Главное меню:", reply_markup=get_main_keyboard(user_id))

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("Доступно только администратору.")
        return
    if 'poll_creation' in context.user_data:
        del context.user_data['poll_creation']
        await update.message.reply_text("Создание опроса отменено.")
    else:
        await update.message.reply_text("Нет активного процесса создания опроса.")

# ---------- ЗАПУСК ----------
def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    # Команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(CommandHandler("users", users_command))
    app.add_handler(CommandHandler("count", count_command))
    app.add_handler(CommandHandler("send_poll", send_poll_to_all))
    app.add_handler(CommandHandler("results", show_results))
    app.add_handler(CommandHandler("end_poll", lambda u,c: deactivate_poll() or u.message.reply_text("Опрос завершён.")))
    app.add_handler(CommandHandler("cancel", cancel_command))

    # Обработчики
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(poll_callback, pattern="^poll_"))
    app.add_handler(CallbackQueryHandler(confirm_callback, pattern="^confirm_"))
    app.add_handler(CallbackQueryHandler(finish_poll_creation_callback, pattern="^finish_poll_creation"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_poll_creation), group=1)

    print("Бот запущен. Нажмите Ctrl+C для остановки.")
    app.run_polling()

if __name__ == "__main__":
    main()
