import sqlite3
import logging
import os
import json
import traceback
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
            class TEXT,
            registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute("PRAGMA table_info(users)")
    columns = [col[1] for col in cursor.fetchall()]
    if 'class' not in columns:
        cursor.execute("ALTER TABLE users ADD COLUMN class TEXT")
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

# ---------- БЕЛЫЙ СПИСОК ----------
def load_whitelist():
    whitelist = set()
    try:
        with open(WHITELIST_FILE, 'r', encoding='utf-8') as f:
            for line in f:
                nick = line.strip()
                if nick:
                    whitelist.add(nick.lower())
    except FileNotFoundError:
        logging.error(f"Файл {WHITELIST_FILE} не найден!")
    return whitelist

def is_nick_in_whitelist(nick):
    return nick.lower() in load_whitelist()

# ---------- ПОЛЬЗОВАТЕЛИ ----------
def is_registered(user_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    conn.close()
    return result is not None

def get_user_nick(user_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT nick FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None

def get_user_class(user_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT class FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None

def get_user_id_by_nick(nick):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM users WHERE nick = ?", (nick,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None

def update_user_class(user_id, new_class):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET class = ? WHERE user_id = ?", (new_class, user_id))
    conn.commit()
    conn.close()

def delete_user(user_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
    cursor.execute("DELETE FROM poll_responses WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def register_user(user_id, nick, user_class):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO users (user_id, nick, class) VALUES (?, ?, ?)", (user_id, nick, user_class))
    conn.commit()
    conn.close()

def get_all_users():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, nick, class, registered_at FROM users ORDER BY registered_at")
    users = cursor.fetchall()
    conn.close()
    return users

def is_admin(user_id):
    return user_id in ADMIN_LIST

def is_user_valid(user_id):
    if not is_registered(user_id):
        return False
    nick = get_user_nick(user_id)
    if not nick:
        return False
    if not is_nick_in_whitelist(nick):
        delete_user(user_id)
        return False
    return True

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

def get_responses_for_export(poll_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT u.nick, u.class, pr.meeting, pr.answer
        FROM poll_responses pr
        JOIN users u ON pr.user_id = u.user_id
        WHERE pr.poll_id = ?
        ORDER BY pr.meeting, u.nick
    ''', (poll_id,))
    rows = cursor.fetchall()
    conn.close()
    return rows

# ---------- GOOGLE SHEETS (каждый опрос – новый лист) ----------
def get_google_sheet():
    if not GOOGLE_CREDS_JSON or not GOOGLE_SHEET_ID:
        logging.error("Переменные окружения для Google Sheets не заданы")
        return None
    try:
        creds_info = json.loads(GOOGLE_CREDS_JSON)
        scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
        creds = Credentials.from_service_account_info(creds_info, scopes=scopes)
        client = gspread.authorize(creds)
        spreadsheet = client.open_by_key(GOOGLE_SHEET_ID)
        return spreadsheet
    except Exception as e:
        logging.error(f"Ошибка подключения: {e}")
        return None

async def export_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("Доступно только администратору.")
        return

    if not GOOGLE_CREDS_JSON or not GOOGLE_SHEET_ID:
        await update.message.reply_text("❌ Не заданы переменные GOOGLE_CREDS или GOOGLE_SHEET_ID.")
        return

    poll = get_active_poll()
    if not poll:
        await update.message.reply_text("Нет активного опроса для экспорта.")
        return

    spreadsheet = get_google_sheet()
    if spreadsheet is None:
        await update.message.reply_text("❌ Не удалось подключиться к Google Sheets.")
        return

    try:
        rows = get_responses_for_export(poll['id'])
        headers = ["Название босса", "Ник", "Класс", "Ответ пользователя"]
        data = [headers]
        for nick, user_class, meeting, answer in rows:
            data.append([meeting, nick, user_class if user_class else "Не указан", answer])
        if len(data) == 1:
            data.append(["Нет ответов", "", "", ""])

        # Создаём новый лист с именем "Опрос_ID_дата"
        sheet_name = f"Опрос_{poll['id']}_{datetime.now().strftime('%Y-%m-%d_%H-%M')}"
        # Проверяем, нет ли листа с таким именем (на всякий случай)
        existing_sheets = [ws.title for ws in spreadsheet.worksheets()]
        if sheet_name in existing_sheets:
            sheet_name = sheet_name + "_new"
        worksheet = spreadsheet.add_worksheet(title=sheet_name, rows="100", cols="20")
        worksheet.update(values=data, range_name='A1')
        await update.message.reply_text(f"✅ Результаты опроса выгружены на новый лист **{sheet_name}** в Google Таблице!")
    except Exception as e:
        logging.error(f"Ошибка при экспорте: {e}\n{traceback.format_exc()}")
        await update.message.reply_text("❌ Ошибка при экспорте в Google Sheets. Проверьте логи.")

# ---------- РЕДАКТИРОВАНИЕ КЛАССА ПО НИКУ ----------
async def edit_class_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("Доступно только администратору.")
        return
    context.user_data['edit_class_mode'] = True
    await update.message.reply_text("Введите ник пользователя, чей класс нужно изменить:")

async def handle_edit_class(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('edit_class_mode'):
        return
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return
    text = update.message.text.strip()
    if text.lower() == '/cancel':
        context.user_data.pop('edit_class_mode', None)
        await update.message.reply_text("Редактирование отменено.")
        return
    # Первый шаг – получили ник
    if 'edit_class_nick' not in context.user_data:
        target_nick = text
        target_user_id = get_user_id_by_nick(target_nick)
        if not target_user_id:
            await update.message.reply_text(f"Пользователь с ником {target_nick} не найден в БД. Попробуйте ещё раз или /cancel.")
            return
        context.user_data['edit_class_nick'] = target_nick
        context.user_data['edit_class_user_id'] = target_user_id
        # Предлагаем выбрать новый класс
        classes = ["ВАР", "МАГ", "ТАНК", "ДРУ", "ПРИСТ", "ЛУК", "СИН", "ШАМ", "СИК", "МИСТИК"]
        keyboard = [[KeyboardButton(cls) for cls in classes[i:i+3]] for i in range(0, len(classes), 3)]
        await update.message.reply_text(
            f"Найден пользователь: {target_nick}. Текущий класс: {get_user_class(target_user_id)}.\n"
            "Выберите новый класс:",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)
        )
        return
    # Второй шаг – получили класс
    new_class = text.upper()
    valid_classes = ["ВАР", "МАГ", "ТАНК", "ДРУ", "ПРИСТ", "ЛУК", "СИН", "ШАМ", "СИК", "МИСТИК"]
    if new_class not in valid_classes:
        await update.message.reply_text("Неверный класс. Выберите из предложенных кнопок.")
        return
    target_user_id = context.user_data['edit_class_user_id']
    update_user_class(target_user_id, new_class)
    await update.message.reply_text(f"Класс пользователя {context.user_data['edit_class_nick']} изменён на {new_class}.")
    # Очищаем состояние
    context.user_data.pop('edit_class_mode', None)
    context.user_data.pop('edit_class_nick', None)
    context.user_data.pop('edit_class_user_id', None)

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
        [KeyboardButton("🔢 Количество"), KeyboardButton("✏️ Изменить класс"), KeyboardButton("🔙 Назад")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_class_keyboard():
    classes = ["ВАР", "МАГ", "ТАНК", "ДРУ", "ПРИСТ", "ЛУК", "СИН", "ШАМ", "СИК", "МИСТИК"]
    keyboard = [[KeyboardButton(cls) for cls in classes[i:i+3]] for i in range(0, len(classes), 3)]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)

# ---------- ОБРАБОТЧИКИ ПОЛЬЗОВАТЕЛЯ ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if is_registered(user_id):
        nick = get_user_nick(user_id)
        user_class = get_user_class(user_id)
        await update.message.reply_text(f"С возвращением, {nick} (класс: {user_class})!", reply_markup=get_main_keyboard(user_id))
    else:
        # Предупреждение перед регистрацией
        await update.message.reply_text(
            "⚠️ *Внимание!*\n"
            "Бот будет использовать ваш игровой ник (из списка) и ваш Telegram ID для идентификации.\n"
            "Никакие другие персональные данные не собираются.\n\n"
            "Для продолжения регистрации введите свой игровой ник из списка.",
            parse_mode="Markdown"
        )
        context.user_data['awaiting_nick'] = True

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text

    # Обработка редактирования класса (админ)
    if context.user_data.get('edit_class_mode'):
        await handle_edit_class(update, context)
        return

    # Регистрация: шаг 1 – ник
    if context.user_data.get('awaiting_nick'):
        nick = text.strip()
        if is_nick_in_whitelist(nick):
            context.user_data['temp_nick'] = nick
            context.user_data['awaiting_nick'] = False
            context.user_data['awaiting_class'] = True
            await update.message.reply_text(
                "Отлично! Теперь выберите класс вашего персонажа:",
                reply_markup=get_class_keyboard()
            )
        else:
            await update.message.reply_text("Ник не найден в белом списке. Попробуйте ещё раз.")
        return

    # Регистрация: шаг 2 – класс
    if context.user_data.get('awaiting_class'):
        valid_classes = ["ВАР", "МАГ", "ТАНК", "ДРУ", "ПРИСТ", "ЛУК", "СИН", "ШАМ", "СИК", "МИСТИК"]
        if text in valid_classes:
            nick = context.user_data.pop('temp_nick')
            user_class = text
            register_user(user_id, nick, user_class)
            context.user_data.pop('awaiting_class', None)
            await update.message.reply_text(
                f"Регистрация завершена!\nНик: {nick}\nКласс: {user_class}",
                reply_markup=get_main_keyboard(user_id)
            )
        else:
            await update.message.reply_text("Пожалуйста, выберите класс из предложенных кнопок.")
        return

    # Основное меню (проверяем валидность)
    if not is_user_valid(user_id):
        await update.message.reply_text(
            "❌ Ваш ник был удалён из списка доступа. Обратитесь к администратору.\n"
            "Для повторной регистрации нажмите /start."
        )
        return

    if text == "👤 Мой профиль":
        nick = get_user_nick(user_id)
        user_class = get_user_class(user_id)
        await update.message.reply_text(f"Ваш ник: {nick}\nВаш класс: {user_class}\nВаш Telegram ID: `{user_id}`", parse_mode="Markdown")
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
        await export_command(update, context)
    elif text == "📋 Текущий опрос" and is_admin(user_id):
        poll = get_active_poll()
        if poll:
            await update.message.reply_text(f"*Текущий опрос:*\n\n{poll['text']}\n\nВстречи: {', '.join(poll['meetings'])}", parse_mode="Markdown")
        else:
            await update.message.reply_text("Нет активного опроса.")
    elif text == "🚫 Завершить опрос" and is_admin(user_id):
        deactivate_poll()
        await update.message.reply_text("Текущий опрос завершён.")
    elif text == "✏️ Изменить класс" and is_admin(user_id):
        await edit_class_command(update, context)
    elif text == "👥 Список пользователей" and is_admin(user_id):
        users = get_all_users()
        if not users:
            await update.message.reply_text("Нет пользователей.")
            return
        msg = "📋 *Список пользователей:*\n"
        for uid, nick, user_class, reg_date in users:
            msg += f"• {nick} (класс: {user_class}) (ID: `{uid}`) — {reg_date}\n"
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

# ---------- СОЗДАНИЕ ОПРОСА (админ) ----------
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

# ---------- РАССЫЛКА ОПРОСА (последовательная) ----------
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
    for uid, nick, _, _ in users:
        if not is_user_valid(uid):
            logging.info(f"Пользователь {uid} ({nick}) пропущен: ник не в белом списке")
            continue
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
    user_id = query.from_user.id

    if not is_user_valid(user_id):
        await query.edit_message_text(
            "❌ Ваш ник был удалён из списка доступа. Обратитесь к администратору.\n"
            "Для повторной регистрации нажмите /start."
        )
        return

    parts = data.split('_')
    if len(parts) < 5 or parts[0] != 'poll':
        await query.edit_message_text("Ошибка: некорректные данные.")
        return
    poll_id = int(parts[1])
    answer_str = None
    for i, p in enumerate(parts):
        if p in ('да', 'нет', 'не знаю'):
            answer_str = p
            answer_idx = i
            break
    if answer_str is None:
        await query.edit_message_text("Ошибка: не распознан ответ.")
        return
    meeting = '_'.join(parts[2:answer_idx])
    next_index = int(parts[-1])

    poll = get_active_poll()
    if not poll or poll['id'] != poll_id:
        await query.edit_message_text("Этот опрос уже не активен.")
        return

    meetings = poll['meetings']
    if 'poll_answers' not in context.user_data:
        context.user_data['poll_answers'] = {}
    context.user_data['poll_answers'][meeting] = answer_str

    if next_index >= len(meetings):
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
    user_id = query.from_user.id
    if not is_user_valid(user_id):
        await query.edit_message_text(
            "❌ Ваш ник был удалён из списка доступа. Обратитесь к администратору.\n"
            "Для повторной регистрации нажмите /start."
        )
        return
    data = query.data
    poll_id = int(data.split('_')[1])
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
    user_id = query.from_user.id
    if not is_user_valid(user_id):
        await query.edit_message_text(
            "❌ Ваш ник был удалён из списка доступа. Обратитесь к администратору.\n"
            "Для повторной регистрации нажмите /start."
        )
        return
    data = query.data
    poll_id = int(data.split('_')[1])
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
    for uid, nick, user_class, reg_date in users:
        msg += f"• {nick} (класс: {user_class}) (ID: `{uid}`) — {reg_date}\n"
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
    if not is_user_valid(user_id):
        await update.message.reply_text(
            "❌ Ваш ник был удалён из списка доступа. Обратитесь к администратору.\n"
            "Для повторной регистрации нажмите /start."
        )
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

async def sync_whitelist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("Доступно только администратору.")
        return
    whitelist = load_whitelist()
    users = get_all_users()
    deleted = 0
    for uid, nick, _, _ in users:
        if nick.lower() not in whitelist:
            delete_user(uid)
            deleted += 1
    await update.message.reply_text(f"Синхронизация завершена. Удалено пользователей: {deleted}")

# ---------- ЗАПУСК ----------
def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(CommandHandler("users", users_command))
    app.add_handler(CommandHandler("count", count_command))
    app.add_handler(CommandHandler("send_poll", send_poll_to_all))
    app.add_handler(CommandHandler("export", export_command))
    app.add_handler(CommandHandler("end_poll", lambda u,c: deactivate_poll() or u.message.reply_text("Опрос завершён.")))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("sync_whitelist", sync_whitelist_command))
    app.add_handler(CommandHandler("edit_class", edit_class_command))

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
