import sqlite3
import logging
import os
import json
import traceback
import requests
import base64
from datetime import datetime
from io import BytesIO
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from telegram import ReplyKeyboardRemove

# Google Sheets
import gspread
from google.oauth2.service_account import Credentials

# Нечёткое сравнение строк
from rapidfuzz import fuzz

# ================= НАСТРОЙКИ =================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("Не задана переменная окружения BOT_TOKEN")

ADMIN_IDS = os.environ.get("ADMIN_IDS", "")
ADMIN_LIST = [int(x.strip()) for x in ADMIN_IDS.split(",") if x.strip()]

DATA_DIR = os.environ.get("DATA_DIR", "/data")
DB_FILE = os.path.join(DATA_DIR, "users.db")

GOOGLE_CREDS_JSON = os.environ.get("GOOGLE_CREDS")
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID")

OCR_SPACE_API_KEY = os.environ.get("OCR_SPACE_API_KEY")
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
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS cash_orders (
            order_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            nick TEXT,
            photo_file_id TEXT,
            description TEXT,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            reviewed_at TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS pending_users (
            user_id INTEGER PRIMARY KEY,
            nick TEXT NOT NULL,
            class TEXT NOT NULL,
            requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

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

def is_nick_taken(nick):
    """Проверяет, занят ли ник в users или pending_users"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM users WHERE nick = ?", (nick,))
    if cursor.fetchone():
        conn.close()
        return True
    cursor.execute("SELECT 1 FROM pending_users WHERE nick = ?", (nick,))
    result = cursor.fetchone()
    conn.close()
    return result is not None

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
    cursor.execute("DELETE FROM cash_orders WHERE user_id = ?", (user_id,))
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
    """Пользователь считается валидным, если он есть в таблице users"""
    return is_registered(user_id)

# ---------- ЗАЯВКИ НА РЕГИСТРАЦИЮ ----------
def add_pending_user(user_id, nick, user_class):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT OR REPLACE INTO pending_users (user_id, nick, class)
        VALUES (?, ?, ?)
    ''', (user_id, nick, user_class))
    conn.commit()
    conn.close()

def get_pending_users():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, nick, class, requested_at FROM pending_users ORDER BY requested_at")
    rows = cursor.fetchall()
    conn.close()
    return rows

def confirm_all_pending():
    """Переносит всех ожидающих в таблицу users, очищает pending, возвращает количество"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, nick, class FROM pending_users")
    pending = cursor.fetchall()
    for user_id, nick, user_class in pending:
        cursor.execute('''
            INSERT OR REPLACE INTO users (user_id, nick, class)
            VALUES (?, ?, ?)
        ''', (user_id, nick, user_class))
    cursor.execute("DELETE FROM pending_users")
    conn.commit()
    conn.close()
    return len(pending)

def is_pending(user_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM pending_users WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    conn.close()
    return result is not None

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

def get_all_polls_meetings():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT meetings_json FROM polls")
    rows = cursor.fetchall()
    conn.close()
    meetings_set = set()
    for row in rows:
        meetings = json.loads(row[0])
        for m in meetings:
            meetings_set.add(m)
    return sorted(meetings_set)

# ---------- OCR.space ----------
def extract_nicks_from_image(image_bytes):
    if not OCR_SPACE_API_KEY:
        logging.error("OCR_SPACE_API_KEY не задан")
        return []
    try:
        image_base64 = base64.b64encode(image_bytes).decode('utf-8')
        payload = {
            'apikey': OCR_SPACE_API_KEY,
            'language': 'rus',
            'isOverlayRequired': False,
            'base64Image': f'data:image/png;base64,{image_base64}',
            'OCREngine': 2,
        }
        response = requests.post('https://api.ocr.space/parse/image', data=payload, timeout=30)
        result = response.json()
        if result.get('IsErroredOnProcessing'):
            logging.error(f"OCR.space ошибка: {result.get('ErrorMessage')}")
            return []
        parsed_text = result.get('ParsedResults', [{}])[0].get('ParsedText', '')
        lines = [line.strip() for line in parsed_text.splitlines() if line.strip()]
        nicks = []
        for line in lines:
            if len(line) >= 2 and not line.isdigit():
                nicks.append(line)
        return nicks
    except Exception as e:
        logging.error(f"Ошибка OCR.space: {e}")
        return []

# ---------- НЕЧЁТКОЕ СРАВНЕНИЕ ----------
def fuzzy_match_nicks(recognized_nicks, known_nicks, threshold=85):
    matched = {}
    unmatched = []
    for rn in recognized_nicks:
        best_match = None
        best_ratio = 0
        for kn in known_nicks:
            ratio = fuzz.ratio(rn.lower(), kn.lower())
            if ratio > best_ratio:
                best_ratio = ratio
                best_match = kn
        if best_ratio >= threshold:
            matched[rn] = best_match
        else:
            unmatched.append(rn)
    return matched, unmatched

async def remove_all_keyboards(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда для массового удаления клавиатур у всех пользователей."""
    if update.effective_user.id not in ADMIN_LIST:
        await update.message.reply_text("У вас нет прав на эту команду.")
        return
    report_msg = await update.message.reply_text("🚀 Начинаю удаление клавиатур у всех пользователей...")
    users = get_all_users()
    if not users:
        await report_msg.edit_text("В базе данных нет пользователей.")
        return
    success = 0
    failed = 0
    for user_id, nick, _, _ in users:
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text="🔄 Интерфейс бота обновлён. Ваша клавиатура скрыта.",
                reply_markup=ReplyKeyboardRemove()
            )
            success += 1
        except Exception as e:
            logging.error(f"Не удалось удалить клавиатуру у {nick} (ID: {user_id}): {e}")
            failed += 1
    await report_msg.edit_text(
        f"✅ **Отчёт об удалении клавиатур**\n"
        f"▸ Успешно: {success}\n"
        f"▸ С ошибками: {failed}\n"
        f"▸ Всего: {success + failed}",
        parse_mode="Markdown"
    )

# ---------- GOOGLE SHEETS (Активность игроков) ----------
def get_google_spreadsheet():
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

def get_or_create_activity_sheet(spreadsheet):
    sheet_name = "Активность игроков"
    try:
        ws = spreadsheet.worksheet(sheet_name)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=sheet_name, rows="1000", cols="100")
        ws.update_cell(1, 1, "Ник")
    return ws

def add_activity_column(ws, activity_name):
    headers = ws.row_values(1)
    for i, h in enumerate(headers):
        if i == 0:
            continue
        if h.strip() == activity_name:
            return i + 1
    col_num = len(headers) + 1
    if col_num == 1:
        col_num = 2
    ws.update_cell(1, col_num, activity_name)
    return col_num

def mark_activity_for_nicks(ws, activity_name, nicks):
    all_values = ws.get_all_values()
    if not all_values:
        return 0
    headers = all_values[0]
    col_idx = None
    for i, h in enumerate(headers):
        if i == 0:
            continue
        if h.strip() == activity_name:
            col_idx = i + 1
            break
    if col_idx is None:
        col_idx = add_activity_column(ws, activity_name)
    updated = 0
    for row_idx, row in enumerate(all_values[1:], start=2):
        nick_in_sheet = row[0].strip()
        if nick_in_sheet.lower() in [n.lower() for n in nicks]:
            ws.update_cell(row_idx, col_idx, "БЫЛ")
            updated += 1
    return updated

def get_responses_grouped_by_meeting(poll_id):
    grouped = {}
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT u.nick, u.class, pr.meeting, pr.answer
        FROM poll_responses pr
        JOIN users u ON pr.user_id = u.user_id
        WHERE pr.poll_id = ?
    ''', (poll_id,))
    rows = cursor.fetchall()
    conn.close()
    for nick, user_class, meeting, answer in rows:
        if meeting not in grouped:
            grouped[meeting] = []
        grouped[meeting].append((nick, user_class if user_class else "Не указан", answer))
    return grouped

def sanitize_sheet_name(name):
    forbidden = r'[]:*?/\\'
    for ch in forbidden:
        name = name.replace(ch, '')
    if len(name) > 100:
        name = name[:100]
    name = name.strip()
    if not name:
        name = "Лист"
    return name

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
    spreadsheet = get_google_spreadsheet()
    if spreadsheet is None:
        await update.message.reply_text("❌ Не удалось подключиться к Google Sheets.")
        return
    grouped = get_responses_grouped_by_meeting(poll['id'])
    if not grouped:
        await update.message.reply_text("Нет ответов на опрос. Экспорт не выполнен.")
        return
    headers = ["Ник", "Класс", "Ответ пользователя"]
    try:
        for meeting, responses in grouped.items():
            sheet_name = sanitize_sheet_name(meeting)
            try:
                worksheet = spreadsheet.worksheet(sheet_name)
                worksheet.clear()
            except gspread.WorksheetNotFound:
                worksheet = spreadsheet.add_worksheet(title=sheet_name, rows="1000", cols="20")
            data = [headers]
            for nick, user_class, answer in responses:
                data.append([nick, user_class, answer])
            worksheet.update(values=data, range_name='A1')
        await update.message.reply_text(f"✅ Результаты опроса выгружены на листы: {', '.join(grouped.keys())}")
    except Exception as e:
        logging.error(f"Ошибка при экспорте: {e}\n{traceback.format_exc()}")
        await update.message.reply_text("❌ Ошибка при экспорте в Google Sheets.")

# ---------- КЕШ-ЗАЯВКИ ----------
def create_cash_order(user_id, nick, photo_file_id, description):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO cash_orders (user_id, nick, photo_file_id, description, status)
        VALUES (?, ?, ?, ?, 'pending')
    ''', (user_id, nick, photo_file_id, description))
    conn.commit()
    conn.close()

def get_pending_orders():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT order_id, user_id, nick, photo_file_id, description FROM cash_orders WHERE status = 'pending' ORDER BY created_at")
    rows = cursor.fetchall()
    conn.close()
    return rows

def update_order_status(order_id, status):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("UPDATE cash_orders SET status = ?, reviewed_at = CURRENT_TIMESTAMP WHERE order_id = ?", (status, order_id))
    conn.commit()
    conn.close()

def get_next_pending_order():
    orders = get_pending_orders()
    return orders[0] if orders else None

async def cash_order_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_user_valid(user_id):
        await update.message.reply_text("Вы не зарегистрированы или ваш ник удалён. Нажмите /start.")
        return
    context.user_data['cash_order'] = {'step': 'photo'}
    keyboard = ReplyKeyboardMarkup([[KeyboardButton("❌ Отмена")]], resize_keyboard=True)
    await update.message.reply_text("Пожалуйста, отправьте фото из личного кабинета с донатом.", reply_markup=keyboard)

async def handle_cash_order_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.user_data.get('cash_order') or context.user_data['cash_order'].get('step') != 'photo':
        return
    if not update.message.photo:
        await update.message.reply_text("Пожалуйста, отправьте фото (скриншот).")
        return
    photo_file = await update.message.photo[-1].get_file()
    photo_file_id = photo_file.file_id
    context.user_data['cash_order']['photo_file_id'] = photo_file_id
    context.user_data['cash_order']['step'] = 'description'
    keyboard = ReplyKeyboardMarkup([[KeyboardButton("❌ Отмена")]], resize_keyboard=True)
    await update.message.reply_text("Что хотите получить? (опишите)", reply_markup=keyboard)

async def handle_cash_order_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.user_data.get('cash_order') or context.user_data['cash_order'].get('step') != 'description':
        return
    description = update.message.text
    nick = get_user_nick(user_id)
    photo_file_id = context.user_data['cash_order']['photo_file_id']
    create_cash_order(user_id, nick, photo_file_id, description)
    del context.user_data['cash_order']
    await update.message.reply_text("✅ Заявка отправлена. Ожидайте получения.", reply_markup=get_main_keyboard(user_id))
    for admin_id in ADMIN_LIST:
        try:
            await context.bot.send_message(admin_id, f"📦 Новая заявка на кеш от {nick} (ID: {user_id})\nОписание: {description}")
        except Exception as e:
            logging.error(f"Не удалось уведомить админа {admin_id}: {e}")

async def leave_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_LIST:
        await update.message.reply_text("У вас нет прав.")
        return
    chat_id = update.effective_chat.id
    await context.bot.send_message(chat_id, "Клавиатура скрыта.", reply_markup=ReplyKeyboardRemove())
    await context.bot.leave_chat(chat_id)

async def process_cash_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("Доступно только администратору.")
        return
    order = get_next_pending_order()
    if not order:
        await update.message.reply_text("Нет новых заявок на кеш.")
        return
    order_id, uid, nick, photo_file_id, description = order
    context.user_data['current_cash_order'] = {'order_id': order_id, 'user_id': uid, 'nick': nick}
    try:
        await context.bot.send_photo(chat_id=user_id, photo=photo_file_id, caption=f"👤 Ник: {nick}\n📝 Что хочет: {description}")
    except Exception as e:
        await update.message.reply_text(f"Не удалось отправить фото. Ошибка: {e}\nТекст заявки: {nick} - {description}")
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Отправлено", callback_data=f"cash_done_{order_id}")],
        [InlineKeyboardButton("❌ Отклонено", callback_data=f"cash_reject_{order_id}")]
    ])
    await update.message.reply_text("Действие по заявке:", reply_markup=keyboard)

async def cash_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id
    if not is_admin(user_id):
        await query.edit_message_text("Доступно только администратору.")
        return
    if data.startswith("cash_done_"):
        order_id = int(data.split('_')[2])
        update_order_status(order_id, 'done')
        await query.edit_message_text("✅ Заявка отмечена как выполненная.")
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM cash_orders WHERE order_id = ?", (order_id,))
        row = cursor.fetchone()
        conn.close()
        if row:
            uid = row[0]
            try:
                await context.bot.send_message(uid, "✅ Ваша заявка на кеш выполнена! Приятной игры!")
            except Exception as e:
                logging.error(f"Не удалось уведомить {uid}: {e}")
    elif data.startswith("cash_reject_"):
        order_id = int(data.split('_')[2])
        update_order_status(order_id, 'rejected')
        await query.edit_message_text("❌ Заявка отклонена.")
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM cash_orders WHERE order_id = ?", (order_id,))
        row = cursor.fetchone()
        conn.close()
        if row:
            uid = row[0]
            try:
                await context.bot.send_message(uid, "❌ Ваша заявка на кеш отклонена. Свяжитесь с администратором.")
            except Exception as e:
                logging.error(f"Не удалось уведомить {uid}: {e}")
    await process_cash_orders(update, context)

# ---------- РЕДАКТИРОВАНИЕ КЛАССА ----------
async def edit_class_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("Доступно только администратору.")
        return
    context.user_data['edit_class_mode'] = True
    keyboard = ReplyKeyboardMarkup([[KeyboardButton("❌ Отмена")]], resize_keyboard=True)
    await update.message.reply_text("Введите ник пользователя, чей класс нужно изменить:", reply_markup=keyboard)

async def handle_edit_class(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('edit_class_mode'):
        return
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return
    text = update.message.text.strip()
    if text == "❌ Отмена":
        context.user_data.pop('edit_class_mode', None)
        context.user_data.pop('edit_class_nick', None)
        context.user_data.pop('edit_class_user_id', None)
        await update.message.reply_text("Редактирование отменено.", reply_markup=get_main_keyboard(user_id))
        return
    if 'edit_class_nick' not in context.user_data:
        target_nick = text
        target_user_id = get_user_id_by_nick(target_nick)
        if not target_user_id:
            await update.message.reply_text(f"Пользователь с ником {target_nick} не найден. Попробуйте ещё раз или нажмите «❌ Отмена».")
            return
        context.user_data['edit_class_nick'] = target_nick
        context.user_data['edit_class_user_id'] = target_user_id
        classes = ["ВАР", "МАГ", "ТАНК", "ДРУ", "ПРИСТ", "ЛУК", "СИН", "ШАМ", "СИК", "МИСТИК"]
        keyboard = [[KeyboardButton(cls) for cls in classes[i:i+3]] for i in range(0, len(classes), 3)]
        keyboard.append([KeyboardButton("❌ Отмена")])
        await update.message.reply_text(
            f"Найден пользователь: {target_nick}. Текущий класс: {get_user_class(target_user_id)}.\n"
            "Выберите новый класс:",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)
        )
        return
    new_class = text.upper()
    valid_classes = ["ВАР", "МАГ", "ТАНК", "ДРУ", "ПРИСТ", "ЛУК", "СИН", "ШАМ", "СИК", "МИСТИК"]
    if new_class not in valid_classes:
        await update.message.reply_text("Неверный класс. Выберите из кнопок или нажмите «❌ Отмена».")
        return
    target_user_id = context.user_data['edit_class_user_id']
    update_user_class(target_user_id, new_class)
    await update.message.reply_text(f"Класс пользователя {context.user_data['edit_class_nick']} изменён на {new_class}.")
    context.user_data.pop('edit_class_mode', None)
    context.user_data.pop('edit_class_nick', None)
    context.user_data.pop('edit_class_user_id', None)

# ---------- АКТИВНОСТЬ ИГРОКОВ ----------
async def activity_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("Доступно только администратору.")
        return
    context.user_data['activity_mode'] = True
    keyboard = ReplyKeyboardMarkup([[KeyboardButton("❌ Отмена")]], resize_keyboard=True)
    await update.message.reply_text(
        "📸 Отправьте скриншот (фото) со списком ников игроков.\nПосле распознавания вы сможете выбрать активность (ГВГ).\n\nДля отмены нажмите «❌ Отмена».",
        reply_markup=keyboard
    )

async def handle_activity_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('activity_mode'):
        return
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return
    if not update.message.photo:
        await update.message.reply_text("Пожалуйста, отправьте фото (скриншот).")
        return
    photo_file = await update.message.photo[-1].get_file()
    image_bytes = await photo_file.download_as_bytearray()
    await update.message.reply_text("🔍 Распознаю ники на изображении...")
    raw_nicks = extract_nicks_from_image(bytes(image_bytes))
    if not raw_nicks:
        await update.message.reply_text("Не удалось распознать ни одного ника.")
        return

    # Получаем все ники из зарегистрированных пользователей
    users = get_all_users()
    known_nicks = [nick for _, nick, _, _ in users]

    matched, unmatched = fuzzy_match_nicks(raw_nicks, known_nicks, threshold=85)
    final_nicks = list(matched.values()) + unmatched

    context.user_data['activity_nicks'] = final_nicks
    context.user_data['activity_raw'] = raw_nicks
    context.user_data['activity_matched'] = matched

    if not final_nicks:
        await update.message.reply_text("Не удалось распознать ни одного ника после сопоставления.")
        return

    stats = f"✅ Распознано: {len(raw_nicks)} ников.\n" \
            f"🎯 Совпало с зарегистрированными: {len(matched)}.\n" \
            f"❓ Не распознано (будут записаны как есть): {len(unmatched)}.\n\n"
    if unmatched:
        stats += f"Неопознанные: {', '.join(unmatched)}\n\n"

    meetings = get_all_polls_meetings()
    if not meetings:
        await update.message.reply_text("Нет созданных опросов (ГВГ). Сначала создайте опрос с встречами.")
        context.user_data.pop('activity_mode', None)
        return
    keyboard = []
    for m in meetings:
        keyboard.append([KeyboardButton(m)])
    keyboard.append([KeyboardButton("❌ Отмена")])
    await update.message.reply_text(
        stats + f"Список для записи: {', '.join(final_nicks)}\n\nВыберите активность (ГВГ):",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    )
    context.user_data['activity_step'] = 'select_activity'

async def handle_activity_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('activity_mode') or context.user_data.get('activity_step') != 'select_activity':
        return
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return
    activity = update.message.text
    if activity == "❌ Отмена":
        context.user_data.pop('activity_mode', None)
        context.user_data.pop('activity_step', None)
        context.user_data.pop('activity_nicks', None)
        await update.message.reply_text("Операция отменена.", reply_markup=get_main_keyboard(user_id))
        return
    nicks = context.user_data.get('activity_nicks', [])
    if not nicks:
        await update.message.reply_text("Нет распознанных ников. Попробуйте заново.")
        context.user_data.pop('activity_mode', None)
        context.user_data.pop('activity_step', None)
        return
    spreadsheet = get_google_spreadsheet()
    if spreadsheet is None:
        await update.message.reply_text("❌ Не удалось подключиться к Google Sheets. Проверьте настройки.")
        return
    ws = get_or_create_activity_sheet(spreadsheet)
    updated = mark_activity_for_nicks(ws, activity, nicks)
    all_nicks_in_sheet = [row[0].strip() for row in ws.get_all_values()[1:] if row]
    all_nicks_in_sheet_lower = [n.lower() for n in all_nicks_in_sheet]
    not_found = [nick for nick in nicks if nick.lower() not in all_nicks_in_sheet_lower]
    await update.message.reply_text(
        f"✅ Готово!\nАктивность: {activity}\nПоставлено плюсов: {updated}\nРаспознано ников: {len(nicks)}"
        + (f"\n⚠️ Не найдены в таблице: {', '.join(not_found)}" if not_found else ""),
        reply_markup=get_main_keyboard(user_id)
    )
    context.user_data.pop('activity_mode', None)
    context.user_data.pop('activity_step', None)
    context.user_data.pop('activity_nicks', None)

# ---------- УПРАВЛЕНИЕ РЕГИСТРАЦИЕЙ (АДМИН) ----------
async def registration_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("Доступно только администратору.")
        return
    keyboard = [
        [KeyboardButton("📋 Список ожидания")],
        [KeyboardButton("✅ Подтвердить всех")],
        [KeyboardButton("🔙 Назад")]
    ]
    await update.message.reply_text("Управление регистрацией:", reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))

async def list_pending_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("Доступно только администратору.")
        return
    pending = get_pending_users()
    if not pending:
        await update.message.reply_text("Нет ожидающих регистрации.")
        return
    msg = "📝 *Список ожидающих регистрации:*\n\n"
    for uid, nick, user_class, req_date in pending:
        msg += f"• {nick} (класс: {user_class}) – ID: `{uid}` – заявка от {req_date}\n"
        if len(msg) > 3800:
            await update.message.reply_text(msg, parse_mode="Markdown")
            msg = ""
    if msg:
        await update.message.reply_text(msg, parse_mode="Markdown")

async def confirm_all_pending_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("Доступно только администратору.")
        return
    count = confirm_all_pending()
    # Добавляем подтверждённых ников в Google Таблицу (лист Активность игроков)
    spreadsheet = get_google_spreadsheet()
    if spreadsheet:
        ws = get_or_create_activity_sheet(spreadsheet)
        # Получаем текущие ники из первого столбца
        current_nicks = ws.col_values(1)[1:]  # пропускаем заголовок
        current_nicks_lower = [n.strip().lower() for n in current_nicks if n.strip()]
        added = 0
        # Все только что подтверждённые пользователи уже в users, получаем их
        users = get_all_users()
        for uid, nick, _, _ in users:
            if nick.lower() not in current_nicks_lower:
                # Добавляем в конец
                row_num = len(current_nicks) + 2 + added
                ws.update_cell(row_num, 1, nick)
                added += 1
        if added:
            logging.info(f"Добавлено {added} новых ников в лист активности")
    await update.message.reply_text(f"✅ Подтверждено {count} пользователей. Они теперь зарегистрированы.")

# ---------- КЛАВИАТУРЫ ----------
def get_main_keyboard(user_id):
    keyboard = [[KeyboardButton("👤 Мой профиль"), KeyboardButton("❓ Помощь")]]
    if is_admin(user_id):
        keyboard.append([KeyboardButton("📊 Админ-панель")])
    keyboard.append([KeyboardButton("💰 Заказ кеша")])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_admin_keyboard():
    keyboard = [
        [KeyboardButton("📝 Создать опрос"), KeyboardButton("📤 Разослать опрос")],
        [KeyboardButton("📈 Результаты опроса"), KeyboardButton("📋 Текущий опрос")],
        [KeyboardButton("🚫 Завершить опрос"), KeyboardButton("🏰 Управление кланом")],
        [KeyboardButton("💸 Выдача кеша"), KeyboardButton("🔙 Назад")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_clan_management_keyboard():
    keyboard = [
        [KeyboardButton("👥 Список пользователей")],
        [KeyboardButton("✏️ Исправить класс"), KeyboardButton("📊 Активность игроков")],
        [KeyboardButton("📝 Регистрация"), KeyboardButton("🔙 Назад")]
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
    elif is_pending(user_id):
        await update.message.reply_text("Ваша заявка на регистрацию уже отправлена администратору. Пожалуйста, ожидайте подтверждения.")
    else:
        await update.message.reply_text(
            "⚠️ *Внимание!*\n"
            "Бот будет использовать ваш игровой ник и ваш Telegram ID для идентификации.\n"
            "Никакие другие персональные данные не собираются.\n\n"
            "Для регистрации введите свой игровой ник.",
            parse_mode="Markdown"
        )
        context.user_data['awaiting_nick'] = True

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text

    # Отмена
    if text == "❌ Отмена":
        if context.user_data.get('cash_order'):
            del context.user_data['cash_order']
            await update.message.reply_text("Заказ кеша отменён.", reply_markup=get_main_keyboard(user_id))
            return
        if context.user_data.get('edit_class_mode'):
            context.user_data.pop('edit_class_mode', None)
            context.user_data.pop('edit_class_nick', None)
            context.user_data.pop('edit_class_user_id', None)
            await update.message.reply_text("Редактирование класса отменено.", reply_markup=get_main_keyboard(user_id))
            return
        if context.user_data.get('poll_creation'):
            del context.user_data['poll_creation']
            await update.message.reply_text("Создание опроса отменено.", reply_markup=get_admin_keyboard())
            return
        if context.user_data.get('activity_mode'):
            context.user_data.pop('activity_mode', None)
            context.user_data.pop('activity_step', None)
            context.user_data.pop('activity_nicks', None)
            await update.message.reply_text("Операция с активностью отменена.", reply_markup=get_main_keyboard(user_id))
            return
        if context.user_data.get('awaiting_nick') or context.user_data.get('awaiting_class'):
            context.user_data.clear()
            await update.message.reply_text("Регистрация отменена.", reply_markup=get_main_keyboard(user_id))
            return

    # Редактирование класса
    if context.user_data.get('edit_class_mode'):
        await handle_edit_class(update, context)
        return

    # Выбор активности после распознавания
    if context.user_data.get('activity_mode') and context.user_data.get('activity_step') == 'select_activity':
        await handle_activity_choice(update, context)
        return

    # Заказ кеша: описание
    if context.user_data.get('cash_order') and context.user_data['cash_order'].get('step') == 'description':
        await handle_cash_order_description(update, context)
        return

    # Регистрация: ник
    if context.user_data.get('awaiting_nick'):
        nick = text.strip()
        if is_nick_taken(nick):
            await update.message.reply_text("❌ Этот ник уже зарегистрирован или ожидает подтверждения. Введите другой ник.")
            return
        context.user_data['temp_nick'] = nick
        context.user_data['awaiting_nick'] = False
        context.user_data['awaiting_class'] = True
        await update.message.reply_text("Отлично! Теперь выберите класс вашего персонажа:", reply_markup=get_class_keyboard())
        return

    # Регистрация: класс (отправляем заявку)
    if context.user_data.get('awaiting_class'):
        valid_classes = ["ВАР", "МАГ", "ТАНК", "ДРУ", "ПРИСТ", "ЛУК", "СИН", "ШАМ", "СИК", "МИСТИК"]
        if text in valid_classes:
            nick = context.user_data.pop('temp_nick')
            user_class = text
            add_pending_user(user_id, nick, user_class)
            context.user_data.pop('awaiting_class', None)
            await update.message.reply_text(
                f"✅ Заявка на регистрацию отправлена!\nНик: {nick}\nКласс: {user_class}\n\n"
                "Дождитесь подтверждения администратора.",
                reply_markup=get_main_keyboard(user_id)
            )
            for admin_id in ADMIN_LIST:
                try:
                    await context.bot.send_message(
                        admin_id,
                        f"📝 Новая заявка на регистрацию!\nНик: {nick}\nКласс: {user_class}\nID: {user_id}"
                    )
                except Exception as e:
                    logging.error(f"Не удалось уведомить админа {admin_id}: {e}")
        else:
            await update.message.reply_text("Пожалуйста, выберите класс из предложенных кнопок.")
        return

    # Проверка валидности
    if not is_user_valid(user_id):
        await update.message.reply_text(
            "❌ Вы не зарегистрированы. Нажмите /start для регистрации."
        )
        return

    # Основное меню
    if text == "👤 Мой профиль":
        nick = get_user_nick(user_id)
        user_class = get_user_class(user_id)
        await update.message.reply_text(f"Ваш ник: {nick}\nВаш класс: {user_class}\nВаш Telegram ID: `{user_id}`", parse_mode="Markdown")
    elif text == "❓ Помощь":
        await update.message.reply_text("Используйте кнопки меню. /start — показать меню.")
    elif text == "💰 Заказ кеша":
        await cash_order_start(update, context)
    elif text == "📊 Админ-панель" and is_admin(user_id):
        await update.message.reply_text("Админ-панель:", reply_markup=get_admin_keyboard())
    elif text == "🔙 Назад" and is_admin(user_id):
        await update.message.reply_text("Главное меню:", reply_markup=get_main_keyboard(user_id))
    elif text == "📝 Создать опрос" and is_admin(user_id):
        await start_poll_creation(update, context)
        return
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
    elif text == "🏰 Управление кланом" and is_admin(user_id):
        await update.message.reply_text("Управление кланом:", reply_markup=get_clan_management_keyboard())
    elif text == "💸 Выдача кеша" and is_admin(user_id):
        await process_cash_orders(update, context)
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
    elif text == "✏️ Исправить класс" and is_admin(user_id):
        await edit_class_command(update, context)
    elif text == "📊 Активность игроков" and is_admin(user_id):
        await activity_menu(update, context)
    elif text == "📝 Регистрация" and is_admin(user_id):
        await registration_menu(update, context)
    elif text == "📋 Список ожидания" and is_admin(user_id):
        await list_pending_command(update, context)
    elif text == "✅ Подтвердить всех" and is_admin(user_id):
        await confirm_all_pending_command(update, context)
    else:
        await update.message.reply_text("Неизвестная команда. Используйте кнопки меню.")

# ---------- СОЗДАНИЕ ОПРОСА ----------
async def start_poll_creation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("Доступно только администратору.")
        return
    context.user_data['poll_creation'] = {'step': 'text'}
    keyboard = ReplyKeyboardMarkup([[KeyboardButton("❌ Отмена")]], resize_keyboard=True)
    await update.message.reply_text("Введите текст объявления для опроса.\n\nДля отмены нажмите «❌ Отмена».", reply_markup=keyboard)

async def handle_poll_creation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id) or 'poll_creation' not in context.user_data:
        return
    data = context.user_data['poll_creation']
    text = update.message.text
    if text == "❌ Отмена":
        del context.user_data['poll_creation']
        await update.message.reply_text("Создание опроса отменено.", reply_markup=get_admin_keyboard())
        return
    if data['step'] == 'text':
        data['text'] = text
        data['meetings'] = []
        data['step'] = 'meeting'
        await update.message.reply_text("Теперь вводите встречи по одной строке.\nКогда закончите, нажмите кнопку «✅ Завершить создание» под сообщением.\n\nДля отмены отправьте «❌ Отмена».")
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
    meetings_list = ", ".join([f'"{m}"' for m in data['meetings']])
    await query.edit_message_text(f"✅ Опрос создан!\n\nГВГ: {meetings_list}")
    del context.user_data['poll_creation']
    await query.message.reply_text("Админ-панель:", reply_markup=get_admin_keyboard())

# ---------- РАССЫЛКА ОПРОСА ----------
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
            logging.info(f"Пользователь {uid} ({nick}) пропущен: не зарегистрирован")
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
        await query.edit_message_text("❌ Вы не зарегистрированы. Нажмите /start.")
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
        await query.edit_message_text("❌ Вы не зарегистрированы. Нажмите /start.")
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
        await query.edit_message_text("❌ Вы не зарегистрированы. Нажмите /start.")
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
async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_user_valid(user_id):
        await update.message.reply_text("❌ Вы не зарегистрированы. Нажмите /start.")
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

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('activity_mode') and not context.user_data.get('activity_step'):
        await handle_activity_photo(update, context)
        return
    if context.user_data.get('cash_order') and context.user_data['cash_order'].get('step') == 'photo':
        await handle_cash_order_photo(update, context)
        return
    await update.message.reply_text("Если хотите заказать кеш, нажмите кнопку «💰 Заказ кеща». Для активности используйте пункт «📊 Активность игроков».")

# ---------- ЗАПУСК ----------
def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(CommandHandler("users", users_command))
    app.add_handler(CommandHandler("send_poll", send_poll_to_all))
    app.add_handler(CommandHandler("export", export_command))
    app.add_handler(CommandHandler("end_poll", lambda u,c: deactivate_poll() or u.message.reply_text("Опрос завершён.")))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("edit_class", edit_class_command))

    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(poll_callback, pattern="^poll_"))
    app.add_handler(CallbackQueryHandler(confirm_callback, pattern="^confirm_"))
    app.add_handler(CallbackQueryHandler(restart_callback, pattern="^restart_"))
    app.add_handler(CallbackQueryHandler(finish_poll_creation_callback, pattern="^finish_poll_creation"))
    app.add_handler(CallbackQueryHandler(cash_callback, pattern="^(cash_done_|cash_reject_)"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_poll_creation), group=1)
    app.add_handler(CommandHandler("leave", leave_chat))
    app.add_handler(CommandHandler("remove_all_keyboards", remove_all_keyboards))

    print("Бот запущен. Нажмите Ctrl+C для остановки.")
    app.run_polling()

if __name__ == "__main__":
    main()
