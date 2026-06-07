import sqlite3
import logging
import os
import json
from datetime import datetime
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

# Google Sheets
import gspread
from google.oauth2.service_account import Credentials

# ================= НАСТРОЙКИ =================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("Не задана переменная окружения BOT_TOKEN")

ADMIN_IDS = os.environ.get("ADMIN_IDS", "")
ADMIN_LIST = [int(x.strip()) for x in ADMIN_IDS.split(",") if x.strip()]

WHITELIST_FILE = "whitelist.txt"
DATA_DIR = os.environ.get("DATA_DIR", "/data")
DB_FILE = os.path.join(DATA_DIR, "users.db")

# Google Sheets
GOOGLE_CREDS_JSON = os.environ.get("GOOGLE_CREDS")
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID")
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
    cursor.execute("SELECT poll_id, text, meetings_json, created_at FROM polls WHERE is_active = 1")
    row = cursor.fetchone()
    conn.close()
    if row:
        return {"id": row[0], "text": row[1], "meetings": json.loads(row[2]), "created_at": row[3]}
    return None

def deactivate_poll():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("UPDATE polls SET is_active = 0")
    conn.commit()
    conn.close()

def save_responses(user_id, poll_id, responses_dict):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    for meeting, answer in responses_dict.items():
        cursor.execute('''
            INSERT OR REPLACE INTO poll_responses (user_id, poll_id, meeting, answer, responded_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
        ''', (user_id, poll_id, meeting, answer))
    conn.commit()
    conn.close()

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

# ---------- GOOGLE SHEETS ----------
def get_google_sheet():
    if not GOOGLE_CREDS_JSON or not GOOGLE_SHEET_ID:
        logging.error("Переменные окружения для Google Sheets не заданы")
        return None
    try:
        creds_info = json.loads(GOOGLE_CREDS_JSON)
        scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
        creds = Credentials.from_service_account_info(creds_info, scopes=scopes)
        client = gspread.authorize(creds)
        sheet = client.open_by_key(GOOGLE_SHEET_ID).sheet1
        return sheet
    except Exception as e:
        logging.error(f"Ошибка подключения к Google Sheets: {e}")
        return None

async def export_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("Доступно только администратору.")
        return
    poll = get_active_poll()
    if not poll:
        await update.message.reply_text("Нет активного опроса для экспорта.")
        return
    sheet = get_google_sheet()
    if sheet is None:
        await update.message.reply_text("Не удалось подключиться к Google Sheets. Проверьте переменные GOOGLE_CREDS и GOOGLE_SHEET_ID.")
        return
    summary = get_response_summary(poll['id'], poll['meetings'])
    # Формируем данные: заголовки + строки
    headers = ["Название опроса", "Текст опроса", "Встреча", "Ник", "Ответ"]
    data = [headers]
    for meeting in poll['meetings']:
        responses = summary.get(meeting, {})
        if responses:
            for nick, answer in responses.items():
                data.append([f"Опрос {poll['id']}", poll['text'], meeting, nick, answer])
        else:
            data.append([f"Опрос {poll['id']}", poll['text'], meeting, "Нет ответов", "-"])
    try:
        sheet.clear()
        sheet.update(values=data, range_name='A1')
        await update.message.reply_text("✅ Результаты опроса успешно выгружены в Google Таблицу!")
    except Exception as e:
        logging.error(f"Ошибка записи: {e}")
        await update.message.reply_text("Произошла ошибка при записи в Google Sheets.")

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

# ---------- ОБРАБОТЧИКИ ----------
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

    if text == "👤 Мой профиль":
        nick = get_user_nick(user_id)
        await update.message.reply_text(f"Ваш ник: {nick}\nВаш Telegram ID: `{user_id}`", parse_mode="Markdown")
    elif text == "❓ Помощь":
        await update.message.reply_text("Используйте кнопки меню. /start — показать меню.")
    elif text == "📊 Админ-панель" and is_admin(user_id):
        await update.message.reply_text("Админ-панель:", reply_markup=get_admin_keyboard())
    elif text == "🔙 Назад" and is_admin(user_id):
        await update.message.reply_text("Главное меню:", reply_markup=get_main_keyboard(user_id))
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

# ---------- СОЗДАНИЕ ОПРОСА ----------
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
        await update.message.reply_text(
            f"➕ Добавлена встреча: {text}. Введите следующую или нажмите кнопку ниже для завершения.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Завершить создание", callback_data="finish_poll_creation")]])
        )

async def finish_poll_creation_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if not is_admin(user_id) or 'poll_creation' not in context.user_data:
        await query.edit_message_text("Нет активного процесса создания опроса.")
        return
    data = context.user_data['poll_creation']
    if data.get('step') != 'meeting' or not data.get('meetings'):
        await query.edit_message_text("Вы не добавили ни одной встречи. Опрос не создан.")
        return
    create_poll(data['text'], data['meetings'])
    await query.edit_message_text(f"✅ Опрос создан!\n\nТекст: {data['text']}\nВстречи: {', '.join(data['meetings'])}")
    del context.user_data['poll_creation']

# ---------- РАССЫЛКА ОПРОСА (последовательная, без глобального хранилища) ----------
async def send_poll_to_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("Доступно только администратору.")
        return
    poll = get_active_poll()
    if not poll:
        await update.message.reply_text("Нет активного опроса. Сначала создайте опрос.")
        return
    users = get_all_users()
    if not users:
        await update.message.reply_text("Нет зарегистрированных пользователей.")
        return

    await update.message.reply_text(f"Начинаю рассылку опроса {len(users)} пользователям...")
    success = 0
    for uid, nick, _ in users:
        try:
            await send_first_question(uid, poll, context)
            success += 1
        except Exception as e:
            logging.error(f"Не удалось начать опрос для {uid}: {e}")
    await update.message.reply_text(f"Рассылка инициирована. Первый вопрос отправлен {success} из {len(users)} пользователям.")

async def send_first_question(chat_id: int, poll: dict, context: ContextTypes.DEFAULT_TYPE):
    meetings = poll['meetings']
    if not meetings:
        return
    first_meeting = meetings[0]
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Да", callback_data=f"poll_{poll['id']}_{first_meeting}_да_1"),
            InlineKeyboardButton("❌ Нет", callback_data=f"poll_{poll['id']}_{first_meeting}_нет_1"),
            InlineKeyboardButton("❓ Не знаю", callback_data=f"poll_{poll['id']}_{first_meeting}_не знаю_1")
        ]
    ])
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"📢 *Опрос*\n\n{poll['text']}\n\nВопрос 1 из {len(meetings)}:\n{first_meeting}",
        parse_mode="Markdown",
        reply_markup=keyboard
    )

async def poll_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    parts = data.split('_')
    if len(parts) < 5 or parts[0] != 'poll':
        await query.edit_message_text("Ошибка: некорректные данные.")
        return
    poll_id = int(parts[1])
    # meeting может содержать подчёркивания, поэтому собираем всё между poll_id и answer
    answer_idx = -2
    next_idx_str = parts[-1]
    # Ищем, где начинается answer: один из "да","нет","не знаю"
    answer_str = None
    for i, p in enumerate(parts):
        if p in ('да', 'нет', 'не знаю'):
            answer_str = p
            answer_idx = i
            break
    if answer_str is None:
        await query.edit_message_text("Ошибка: не распознан ответ.")
        return
    meeting = '_'.join(parts[2:answer_idx])  # всё, что между poll_id и ответом
    next_index = int(next_idx_str)
    user_id = query.from_user.id
    if not is_registered(user_id):
        await query.edit_message_text("Вы не зарегистрированы. Напишите /start")
        return

    poll = get_active_poll()
    if not poll or poll['id'] != poll_id:
        await query.edit_message_text("Этот опрос уже не активен.")
        return

    meetings = poll['meetings']
    if 'poll_answers' not in context.user_data:
        context.user_data['poll_answers'] = {}
    context.user_data['poll_answers'][meeting] = answer_str

    if next_index >= len(meetings):
        # Показать сводку
        summary_text = "✅ *Ваши ответы:*\n\n"
        for m in meetings:
            ans = context.user_data['poll_answers'].get(m, "❌ Не отвечен")
            summary_text += f"• {m} → {ans}\n"
        summary_text += "\nВсё верно?"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Да, всё верно", callback_data=f"confirm_{poll_id}")],
            [InlineKeyboardButton("❌ Нет, пройти заново", callback_data=f"restart_{poll_id}")]
        ])
        await query.edit_message_text(summary_text, parse_mode="Markdown", reply_markup=keyboard)
        return

    next_meeting = meetings[next_index]
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Да", callback_data=f"poll_{poll_id}_{next_meeting}_да_{next_index+1}"),
            InlineKeyboardButton("❌ Нет", callback_data=f"poll_{poll_id}_{next_meeting}_нет_{next_index+1}"),
            InlineKeyboardButton("❓ Не знаю", callback_data=f"poll_{poll_id}_{next_meeting}_не знаю_{next_index+1}")
        ]
    ])
    await query.edit_message_text(
        f"📢 *Опрос*\n\n{poll['text']}\n\nВопрос {next_index+1} из {len(meetings)}:\n{next_meeting}",
        parse_mode="Markdown",
        reply_markup=keyboard
    )

async def confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    poll_id = int(data.split('_')[1])
    user_id = query.from_user.id
    answers = context.user_data.get('poll_answers', {})
    if not answers:
        await query.edit_message_text("Нет данных для сохранения.")
        return
    save_responses(user_id, poll_id, answers)
    del context.user_data['poll_answers']
    await query.edit_message_text("✅ Спасибо! Ваши ответы сохранены. Опрос завершён.")

async def restart_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    poll_id = int(data.split('_')[1])
    user_id = query.from_user.id
    context.user_data['poll_answers'] = {}
    poll = get_active_poll()
    if not poll or poll['id'] != poll_id:
        await query.edit_message_text("Опрос более не активен.")
        return
    meetings = poll['meetings']
    if not meetings:
        return
    first_meeting = meetings[0]
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Да", callback_data=f"poll_{poll_id}_{first_meeting}_да_1"),
            InlineKeyboardButton("❌ Нет", callback_data=f"poll_{poll_id}_{first_meeting}_нет_1"),
            InlineKeyboardButton("❓ Не знаю", callback_data=f"poll_{poll_id}_{first_meeting}_не знаю_1")
        ]
    ])
    await query.edit_message_text(
        f"📢 *Опрос заново*\n\n{poll['text']}\n\nВопрос 1 из {len(meetings)}:\n{first_meeting}",
        parse_mode="Markdown",
        reply_markup=keyboard
    )

# ---------- РЕЗУЛЬТАТЫ (текстовые) ----------
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

# ---------- ОБЩИЕ КОМАНДЫ ----------
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

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(CommandHandler("users", users_command))
    app.add_handler(CommandHandler("count", count_command))
    app.add_handler(CommandHandler("send_poll", send_poll_to_all))
    app.add_handler(CommandHandler("results", show_results))
    app.add_handler(CommandHandler("export", export_command))
    app.add_handler(CommandHandler("end_poll", lambda u,c: deactivate_poll() or u.message.reply_text("Опрос завершён.")))
    app.add_handler(CommandHandler("cancel", cancel_command))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(poll_callback, pattern="^poll_"))
    app.add_handler(CallbackQueryHandler(confirm_callback, pattern="^confirm_"))
    app.add_handler(CallbackQueryHandler(restart_callback, pattern="^restart_"))
    app.add_handler(CallbackQueryHandler(finish_poll_creation_callback, pattern="^finish_poll_creation"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_poll_creation), group=1)

    print("Бот запущен. Нажмите Ctrl+C для остановки.")
    app.run_polling()

if __name__ == "__main__":
    main()
