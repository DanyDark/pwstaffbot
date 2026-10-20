import sqlite3
import logging
import os
import json
import traceback
import requests
import base64
import asyncio
import sheet_layout as attendance
from contextlib import closing
from datetime import datetime, time
from io import BytesIO
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from telegram import ReplyKeyboardRemove
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# Google Sheets
import gspread
from google.oauth2.service_account import Credentials

# Нечёткое сравнение строк
from rapidfuzz import fuzz

# Классы стандартной версии Perfect World 1.3.6.
CHARACTER_CLASSES = ("ВАР", "МАГ", "ТАНК", "ДРУ", "ПРИСТ", "ЛУК")
CLASS_SHEET_SECTIONS = {
    "ВАР": "ВАРЫ",
    "МАГ": "МАГИ",
    "ДРУ": "ДРУЛИ",
    "ТАНК": "ТАНКИ",
    "ЛУК": "ЛУЧНИКИ",
    "ПРИСТ": "ПРИСТЫ",
}

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
# Telegram API URLs contain the bot token; do not log routine HTTP requests.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

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
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS external_responses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            poll_id INTEGER,
            external_nick TEXT,
            external_class TEXT,
            meeting TEXT,
            answer TEXT,
            admin_id INTEGER,
            responded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute("PRAGMA table_info(external_responses)")
    columns = [col[1] for col in cursor.fetchall()]
    if 'external_class' not in columns:
        cursor.execute("ALTER TABLE external_responses ADD COLUMN external_class TEXT")
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS activity_months (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            year INTEGER,
            month INTEGER,
            sheet_name TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
    return is_registered(user_id)

def approve_admin_registration(user_id):
    """Only configured administrators can approve their own pending profile."""
    if not is_admin(user_id):
        return
    with closing(sqlite3.connect(DB_FILE)) as conn, conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (user_id, nick, class) "
            "SELECT user_id, nick, class FROM pending_users WHERE user_id = ?",
            (user_id,)
        )
        conn.execute(
            "DELETE FROM pending_users WHERE user_id = ? "
            "AND EXISTS (SELECT 1 FROM users WHERE user_id = ?)",
            (user_id, user_id)
        )


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
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, nick, class FROM pending_users")
    pending = cursor.fetchall()
    confirmed_users = []
    for user_id, nick, user_class in pending:
        cursor.execute('''
            INSERT OR REPLACE INTO users (user_id, nick, class)
            VALUES (?, ?, ?)
        ''', (user_id, nick, user_class))
        confirmed_users.append((user_id, nick, user_class))
    cursor.execute("DELETE FROM pending_users")
    conn.commit()
    conn.close()
    return confirmed_users

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
    cursor.execute(
        "INSERT INTO polls (text, meetings_json, is_active) VALUES (?, ?, 1)",
        (text, meetings_json)
    )
    conn.commit()
    conn.close()

def get_active_poll():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT poll_id, text, meetings_json, created_at FROM polls WHERE is_active = 1"
    )
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
        cursor.execute(
            "INSERT OR REPLACE INTO poll_responses (user_id, poll_id, meeting, answer, responded_at) "
            "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)",
            (user_id, poll_id, meeting, answer)
        )
    conn.commit()
    conn.close()

def save_external_response(poll_id, external_nick, external_class, meeting, answer, admin_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO external_responses (poll_id, external_nick, external_class, meeting, answer, admin_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (poll_id, external_nick, external_class, meeting, answer, admin_id)
    )
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

def get_user_current_poll_answers(user_id):
    poll = get_active_poll()
    if not poll:
        return None
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT meeting, answer FROM poll_responses WHERE user_id = ? AND poll_id = ?",
        (user_id, poll['id'])
    )
    rows = cursor.fetchall()
    conn.close()
    if rows:
        return {meeting: answer for meeting, answer in rows}
    return {}

def get_non_responders(poll_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, nick FROM users")
    all_users = cursor.fetchall()
    cursor.execute(
        "SELECT DISTINCT user_id FROM poll_responses WHERE poll_id = ?",
        (poll_id,)
    )
    responders = set(row[0] for row in cursor.fetchall())
    conn.close()
    non_responders = [(uid, nick) for uid, nick in all_users if uid not in responders]
    return non_responders

# ---------- OCR.space ----------
def extract_nicks_from_image(image_bytes):
    if not OCR_SPACE_API_KEY:
        raise ValueError("На Bothost не задан OCR_SPACE_API_KEY. Добавьте ключ OCR.space в переменные окружения.")
    try:
        image_base64 = base64.b64encode(image_bytes).decode('utf-8')
        payload = {
            'apikey': OCR_SPACE_API_KEY,
            'language': 'rus',
            'isOverlayRequired': False,
            'base64Image': f'data:image/{"jpeg" if image_bytes.startswith(bytes.fromhex("ffd8ff")) else "png"};base64,{image_base64}',
            'OCREngine': 2,
        }
        response = requests.post('https://api.ocr.space/parse/image', data=payload, timeout=30)
        response.raise_for_status()
        result = response.json()
        if result.get('IsErroredOnProcessing'):
            logging.error(f"OCR.space ошибка: {result.get('ErrorMessage')}")
            return []
        parsed_text = '\n'.join(item.get('ParsedText', '') for item in (result.get('ParsedResults') or []))
        lines = [line.strip() for line in parsed_text.splitlines() if line.strip()]
        nicks = []
        for line in lines:
            # Keep the first OCR line even if it looks invalid: dropping it
            # would silently promote the next player to party leader.
            nicks.append(line)
        return nicks
    except Exception as e:
        logging.error(f"Ошибка OCR.space: {e}")
        return []

# ---------- НЕЧЁТКОЕ СРАВНЕНИЕ ----------
def fuzzy_match_nicks(recognized_nicks, known_nicks, threshold=70):
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

# ---------- GOOGLE SHEETS (Активность через шаблон) ----------
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

def get_or_create_monthly_activity_sheet(spreadsheet):
    year, month = attendance.moscow_month()
    return attendance.ensure_month(spreadsheet, year, month)


def get_current_activity_sheet():
    spreadsheet = get_google_spreadsheet()
    if not spreadsheet:
        raise ValueError("Проверьте GOOGLE_CREDS, GOOGLE_SHEET_ID и доступ редактора к таблице.")
    return get_or_create_monthly_activity_sheet(spreadsheet)


def find_column_by_header(ws, header_name):
    return attendance.layout(ws.get_all_values())['headers'].get(attendance.label(header_name))


def find_nick_column(ws):
    return find_column_by_header(ws, 'НИК')


def get_available_activities(ws):
    return list(attendance.EVENTS)


def get_user_activity_count(nick):
    rows = get_current_activity_sheet().get_all_values()
    spec = attendance.layout(rows)
    for row, name in attendance.players(rows, spec):
        if name.casefold() == nick.casefold():
            return sum(str(attendance.cell(rows[row - 1], col)).strip() in ('1', '2')
                       for cols in spec['events'].values() for col in cols)
    return 0


def salary_summary(nick=None):
    ws = get_current_activity_sheet()
    rows = ws.get_all_values()
    spec = attendance.layout(rows)
    first = spec['headers'].get('ЗП 1/2')
    second = spec['headers'].get('ЗП 2/2')
    if not first or not second:
        raise ValueError('В таблице нет столбцов «зп 1/2» и «зп 2/2».')
    result = []
    for row, name in attendance.players(rows, spec):
        if nick is None or nick.casefold() == name.casefold():
            result.append(f"{name}: 1/2 — {attendance.cell(rows[row - 1], first) or '0'}; "
                          f"2/2 — {attendance.cell(rows[row - 1], second) or '0'}")
    return ws.title, result


async def calculate_salaries(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Доступно только администратору.")
        return
    try:
        title, result = await asyncio.to_thread(salary_summary)
        await update.message.reply_text(f"💰 {title}. Зарплата по формулам таблицы:")
        # Keep messages within Telegram's limit without changing the formulas.
        chunk = ''
        for line in result:
            if len(chunk) + len(line) > 3500:
                await update.message.reply_text(chunk)
                chunk = ''
            chunk += line + '\n'
        await update.message.reply_text(chunk or 'Нет игроков в таблице.')
    except Exception as exc:
        await update.message.reply_text(f"❌ Не удалось прочитать зарплату: {exc}")


def sync_pa_internal():
    return attendance.sync_players(get_current_activity_sheet(), get_all_users())


async def sync_pa_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Доступно только администратору.")
        return
    try:
        count = await asyncio.to_thread(sync_pa_internal)
        await update.message.reply_text(f"✅ Добавлено игроков в текущий месяц: {count}.")
    except Exception as exc:
        await update.message.reply_text(f"❌ Синхронизация не выполнена: {exc}")


async def sync_registered_profile(update, user_id):
    if not globals().get('GOOGLE_CREDS_JSON') or not globals().get('GOOGLE_SHEET_ID'):
        return
    try:
        users = [user for user in get_all_users() if user[0] == user_id]
        ws = await asyncio.to_thread(get_current_activity_sheet)
        await asyncio.to_thread(attendance.sync_players, ws, users)
    except Exception as exc:
        await update.message.reply_text(f"Профиль сохранён, но не добавлен в таблицу: {exc}")


# ---------- ЭКСПОРТ РЕЗУЛЬТАТОВ ОПРОСА ----------
def get_responses_grouped_by_meeting(poll_id):
    grouped = {}
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT u.nick, u.class, pr.meeting, pr.answer "
        "FROM poll_responses pr JOIN users u ON pr.user_id = u.user_id "
        "WHERE pr.poll_id = ?",
        (poll_id,)
    )
    rows = cursor.fetchall()
    for nick, user_class, meeting, answer in rows:
        if meeting not in grouped:
            grouped[meeting] = []
        grouped[meeting].append((nick, user_class if user_class else "Не указан", answer))
    cursor.execute(
        "SELECT external_nick, external_class, meeting, answer "
        "FROM external_responses WHERE poll_id = ?",
        (poll_id,)
    )
    ext_rows = cursor.fetchall()
    for ext_nick, ext_class, meeting, answer in ext_rows:
        if meeting not in grouped:
            grouped[meeting] = []
        grouped[meeting].append((ext_nick, ext_class if ext_class else "Внешний", answer))
    conn.close()
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
            responses_sorted = sorted(responses, key=lambda x: (x[1], x[0]))
            sheet_name = sanitize_sheet_name(meeting)
            try:
                worksheet = spreadsheet.worksheet(sheet_name)
                worksheet.clear()
            except gspread.WorksheetNotFound:
                worksheet = spreadsheet.add_worksheet(title=sheet_name, rows="1000", cols="20")
            data = [headers]
            for nick, user_class, answer in responses_sorted:
                data.append([nick, user_class, answer])
            worksheet.update(values=data, range_name='A1')
        await update.message.reply_text(f"✅ Результаты опроса выгружены на листы: {', '.join(grouped.keys())}")
    except Exception as e:
        logging.error(f"Ошибка при экспорте: {e}\n{traceback.format_exc()}")
        await update.message.reply_text("❌ Ошибка при экспорте в Google Sheets.")

# ---------- КЕШ-ЗАЯВКИ ----------
def create_cash_order(user_id, nick, photo_file_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO cash_orders (user_id, nick, photo_file_id, description, status) "
        "VALUES (?, ?, ?, ?, 'pending')",
        (user_id, nick, photo_file_id, "Золотые яйца")
    )
    conn.commit()
    conn.close()

def get_pending_orders():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT order_id, user_id, nick, photo_file_id, description "
        "FROM cash_orders WHERE status = 'pending' ORDER BY created_at"
    )
    rows = cursor.fetchall()
    conn.close()
    return rows

def update_order_status(order_id, status):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE cash_orders SET status = ?, reviewed_at = CURRENT_TIMESTAMP WHERE order_id = ?",
        (status, order_id)
    )
    conn.commit()
    conn.close()

def get_next_pending_order():
    orders = get_pending_orders()
    return orders[0] if orders else None

async def cash_order_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_user_valid(user_id):
        await update.message.reply_text("Вы не зарегистрированы. Нажмите /start.")
        return
    context.user_data['cash_order'] = {'step': 'photo'}
    keyboard = ReplyKeyboardMarkup([[KeyboardButton("❌ Отмена")]], resize_keyboard=True)
    await update.message.reply_text(
        "📸 Отправьте скриншот из личного кабинета с донатом.\n\nПосле этого кешбек будет выдан золотыми яйцами.",
        reply_markup=keyboard
    )

async def handle_cash_order_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.user_data.get('cash_order') or context.user_data['cash_order'].get('step') != 'photo':
        return
    if not update.message.photo:
        await update.message.reply_text("Пожалуйста, отправьте фото (скриншот).")
        return
    photo_file = await update.message.photo[-1].get_file()
    photo_file_id = photo_file.file_id
    nick = get_user_nick(user_id)
    create_cash_order(user_id, nick, photo_file_id)
    del context.user_data['cash_order']
    await update.message.reply_text(
        "✅ Заявка принята! Кешбек будет выдан золотыми яйцами.",
        reply_markup=get_main_keyboard(user_id)
    )
    for admin_id in ADMIN_LIST:
        try:
            await context.bot.send_message(
                admin_id,
                f"📦 Новая заявка на кеш от {nick} (ID: {user_id})\nСтатус: ожидает выдачи."
            )
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
        await context.bot.send_photo(
            chat_id=user_id,
            photo=photo_file_id,
            caption=f"👤 Ник: {nick}\n🎭 Класс: {get_user_class(uid)}\n📦 Заявка на кеш (кешбек золотыми яйцами)"
        )
    except Exception as e:
        await update.message.reply_text(f"Не удалось отправить фото. Ошибка: {e}")
        await update.message.reply_text(f"👤 Ник: {nick}\n🎭 Класс: {get_user_class(uid)}")
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
                await context.bot.send_message(uid, "✅ Кешбек выдан золотыми яйцами! Спасибо за активность.")
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
        await update.message.reply_text("Редактирование отменено.", reply_markup=get_admin_keyboard())
        return
    if 'edit_class_nick' not in context.user_data:
        target_nick = text
        target_user_id = get_user_id_by_nick(target_nick)
        if not target_user_id:
            await update.message.reply_text(f"Пользователь с ником {target_nick} не найден. Попробуйте ещё раз или нажмите «❌ Отмена».")
            return
        context.user_data['edit_class_nick'] = target_nick
        context.user_data['edit_class_user_id'] = target_user_id
        classes = CHARACTER_CLASSES
        keyboard = [[KeyboardButton(cls) for cls in classes[i:i+3]] for i in range(0, len(classes), 3)]
        keyboard.append([KeyboardButton("❌ Отмена")])
        await update.message.reply_text(
            f"Найден пользователь: {target_nick}. Текущий класс: {get_user_class(target_user_id)}.\n"
            "Выберите новый класс:",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)
        )
        return
    new_class = text.upper()
    valid_classes = CHARACTER_CLASSES
    if new_class not in valid_classes:
        await update.message.reply_text("Неверный класс. Выберите из кнопок или нажмите «❌ Отмена».")
        return
    target_user_id = context.user_data['edit_class_user_id']
    update_user_class(target_user_id, new_class)
    await update.message.reply_text(f"Класс пользователя {context.user_data['edit_class_nick']} изменён на {new_class}.")
    context.user_data.pop('edit_class_mode', None)
    context.user_data.pop('edit_class_nick', None)
    context.user_data.pop('edit_class_user_id', None)
    await update.message.reply_text("Админ-панель:", reply_markup=get_admin_keyboard())

async def edit_nick_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("Доступно только администратору.")
        return
    context.user_data['edit_nick_mode'] = True
    keyboard = ReplyKeyboardMarkup([[KeyboardButton("❌ Отмена")]], resize_keyboard=True)
    await update.message.reply_text("Введите старый ник пользователя, чей ник нужно изменить:", reply_markup=keyboard)

async def handle_edit_nick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('edit_nick_mode'):
        return
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return
    text = update.message.text.strip()
    if text == "❌ Отмена":
        context.user_data.pop('edit_nick_mode', None)
        context.user_data.pop('edit_old_nick', None)
        context.user_data.pop('edit_target_user_id', None)
        await update.message.reply_text("Редактирование отменено.", reply_markup=get_admin_keyboard())
        return
    if 'edit_old_nick' not in context.user_data:
        old_nick = text
        target_user_id = get_user_id_by_nick(old_nick)
        if not target_user_id:
            await update.message.reply_text(f"Пользователь с ником {old_nick} не найден. Попробуйте ещё раз или нажмите «❌ Отмена».")
            return
        context.user_data['edit_old_nick'] = old_nick
        context.user_data['edit_target_user_id'] = target_user_id
        await update.message.reply_text("Введите новый ник для этого пользователя:")
        return
    new_nick = text
    if is_nick_taken(new_nick):
        await update.message.reply_text("❌ Этот ник уже занят другим пользователем. Введите другой.")
        return
    target_user_id = context.user_data['edit_target_user_id']
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET nick = ? WHERE user_id = ?", (new_nick, target_user_id))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"Ник пользователя {context.user_data['edit_old_nick']} изменён на {new_nick}.")
    context.user_data.pop('edit_nick_mode', None)
    context.user_data.pop('edit_old_nick', None)
    context.user_data.pop('edit_target_user_id', None)
    await update.message.reply_text("Админ-панель:", reply_markup=get_admin_keyboard())

# ---------- АКТИВНОСТЬ ИГРОКОВ ----------
async def activity_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text('Доступно только администратору.')
        return
    clear_input_state(context)
    context.user_data.update(activity_mode=True, activity_step='select_activity')
    await update.message.reply_text('Выберите мероприятие, затем дату и загрузите скриншоты.',
        reply_markup=ReplyKeyboardMarkup([[KeyboardButton(e)] for e in attendance.EVENTS]
            + [[KeyboardButton('❌ Отмена')]], resize_keyboard=True))


def upload_keyboard():
    return ReplyKeyboardMarkup([[KeyboardButton('ЗАВЕРШИТЬ ЗАГРУЗКУ СКРИНОВ')],
                               [KeyboardButton('❌ Отмена')]], resize_keyboard=True)


async def send_activity_report(update, text, reply_markup=None):
    # Multiple screenshots can produce a report longer than one Telegram message.
    while len(text) > 3500:
        await update.message.reply_text(text[:3500])
        text = text[3500:]
    await update.message.reply_text(text, reply_markup=reply_markup)


def accumulate_screenshot(state, raw, known):
    # Only exact normalized names are accepted automatically. OCR guesses must
    # never award attendance or leadership to a different player.
    canonical = {}
    for nick in known:
        canonical.setdefault(nick.strip().casefold(), []).append(nick)
    recognized, unknown = [], []
    mapped = {}
    for value in raw:
        matches = list(dict.fromkeys(canonical.get(value.strip().casefold(), [])))
        if len(matches) == 1:
            mapped[value] = matches[0]
            recognized.append(matches[0])
        else:
            unknown.append(value)
    nicks = state.setdefault('activity_nicks', [])
    leaders = state.setdefault('activity_leaders', [])
    missing = state.setdefault('activity_unknown', [])
    for nick in recognized:
        if nick not in nicks:
            nicks.append(nick)
    for nick in unknown:
        if nick not in missing:
            missing.append(nick)
    top = raw[0] if raw else None
    leader = mapped.get(top)
    if leader and leader not in leaders:
        leaders.append(leader)
    return list(dict.fromkeys(recognized)), unknown, leader


async def handle_activity_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data
    if not is_admin(update.effective_user.id) or not state.get('activity_mode'):
        return
    if state.get('activity_step') != 'upload':
        await update.message.reply_text('Сначала выберите мероприятие и дату. Для новой загрузки откройте «Активность игроков».')
        return
    try:
        photo_file = await update.message.photo[-1].get_file()
        image = await photo_file.download_as_bytearray()
        await update.message.reply_text('🔍 Распознаю скриншот...')
        raw = await asyncio.to_thread(extract_nicks_from_image, bytes(image))
        if not raw:
            await update.message.reply_text('⚠️ На скриншоте не удалось прочитать ники. '
                'Проверьте изображение и отметьте игроков вручную в таблице либо пришлите более чёткий скриншот.',
                reply_markup=upload_keyboard())
            return
        recognized, unknown, leader = accumulate_screenshot(state, raw, state['activity_known'])
        state['activity_screenshots'] += 1
        await send_activity_report(update,
            f"Скриншот №{state['activity_screenshots']}. Определены: " + (', '.join(recognized) or 'нет')
            + ('\nПЛ: ' + leader if leader else '\n⚠️ Верхний ник «' + raw[0]
               + '» не определён. ПЛ не назначен; отметьте его вручную в таблице значением 2.')
            + ('\n⚠️ Не определены: ' + ', '.join(unknown)
               + '. Проверьте эти ники на скриншоте и отметьте их вручную в таблице.' if unknown else '')
            + f"\nВсего уникальных игроков: {len(state['activity_nicks'])}."
            + '\nЗагрузите следующий скриншот или завершите загрузку.',
            reply_markup=upload_keyboard())
    except Exception as exc:
        await update.message.reply_text(f'❌ Скриншот не обработан: {exc}. Можно повторить отправку или отметить игроков вручную.')


async def handle_activity_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    state = context.user_data
    text = update.message.text.strip()
    try:
        year, month = attendance.moscow_month()
        step = state.get('activity_step')
        if step == 'select_activity':
            if text not in attendance.EVENTS:
                await update.message.reply_text('Выберите событие из кнопок.')
                return
            ws = await asyncio.to_thread(get_current_activity_sheet)
            state['activity_sheet'] = ws.title
            rows = await asyncio.to_thread(ws.get_all_values)
            dates = attendance.event_dates(rows, text, year, month)
            if not dates:
                raise ValueError('В заголовках события нет дат. Заполните даты в месячном листе.')
            state.update(activity_event=text, activity_dates=dates, activity_step='select_date')
            await update.message.reply_text('Выберите дату события:', reply_markup=ReplyKeyboardMarkup(
                [[KeyboardButton(d)] for d in dates] + [[KeyboardButton('❌ Отмена')]], resize_keyboard=True))
        elif step == 'select_date':
            if text not in state['activity_dates']:
                await update.message.reply_text('Выберите дату из кнопок.')
                return
            ws = await asyncio.to_thread(get_current_activity_sheet)
            if ws.title != state['activity_sheet']:
                raise ValueError('Месяц сменился. Начните загрузку заново.')
            rows = await asyncio.to_thread(ws.get_all_values)
            state.update(activity_date=text, activity_step='upload', activity_nicks=[],
                         activity_leaders=[], activity_unknown=[], activity_screenshots=0,
                         activity_known=[nick for _, nick in attendance.players(rows)])
            await update.message.reply_text(
                f"{state['activity_event']} — {text}.\nЗагружайте скриншоты. Верхний игрок каждого скриншота — ПЛ."
                + '\nКогда все скриншоты отправлены, нажмите «ЗАВЕРШИТЬ ЗАГРУЗКУ СКРИНОВ».',
                reply_markup=upload_keyboard())
        elif step == 'upload':
            if text != 'ЗАВЕРШИТЬ ЗАГРУЗКУ СКРИНОВ':
                await update.message.reply_text('Отправьте скриншот либо нажмите «ЗАВЕРШИТЬ ЗАГРУЗКУ СКРИНОВ».')
                return
            if not state['activity_screenshots'] or not state['activity_nicks']:
                await update.message.reply_text('Пока нет определённых игроков. Загрузите скриншот или отмените операцию.')
                return
            state['activity_step'] = 'confirm'
            await send_activity_report(update,
                f"{state['activity_event']} — {state['activity_date']}\n"
                + f"Скриншотов: {state['activity_screenshots']}. Игроков: {len(state['activity_nicks'])}.\n"
                + 'ПЛ (2): ' + (', '.join(state['activity_leaders']) or 'не определены')
                + '\nВсе игроки: ' + ', '.join(state['activity_nicks'])
                + ('\n⚠️ Не определены: ' + ', '.join(state['activity_unknown'])
                   + '. Заполните их вручную в таблице.' if state['activity_unknown'] else ''),
                reply_markup=ReplyKeyboardMarkup([[KeyboardButton('✅ Записать')],
                                                  [KeyboardButton('❌ Отмена')]], resize_keyboard=True))
        elif step == 'confirm':
            if text != '✅ Записать':
                await update.message.reply_text('Подтвердите запись кнопкой или отмените.')
                return
            ws = await asyncio.to_thread(get_current_activity_sheet)
            if ws.title != state['activity_sheet']:
                raise ValueError('Месяц сменился. Начните заново.')
            rows = await asyncio.to_thread(ws.get_all_values)
            dates = attendance.event_dates(rows, state['activity_event'], year, month)
            col = dates.get(state['activity_date'])
            if col is None:
                raise ValueError('Дата удалена из таблицы. Начните заново.')
            count, missing = await asyncio.to_thread(attendance.mark_attendance, ws, col,
                state['activity_nicks'], state['activity_leaders'])
            clear_input_state(context)
            await update.message.reply_text(f"✅ Обновлено отметок: {count}."
                + (" Не найдены: " + ', '.join(missing) + '. Отметьте их вручную в таблице.' if missing else ''), reply_markup=get_main_keyboard(update.effective_user.id))
    except Exception as exc:
        await update.message.reply_text(f"❌ Запись не выполнена: {exc}\nМожно отменить операцию и начать заново.")


# ---------- УПРАВЛЕНИЕ РЕГИСТРАЦИЕЙ (АДМИН) ----------
async def registration_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("Доступно только администратору.")
        return
    keyboard = [
        [KeyboardButton("📋 Список ожидания")],
        [KeyboardButton("✅ Подтвердить всех")],
        [KeyboardButton("❌ Отказать")],
        [KeyboardButton("🔙 Назад")]
    ]
    await update.message.reply_text("Управление регистрацией:", reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))

def get_reject_keyboard():
    pending = get_pending_users()
    if not pending:
        return None
    keyboard = []
    row = []
    for uid, nick, user_class, _ in pending:
        row.append(KeyboardButton(f"❌ {nick}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([KeyboardButton("🔙 Назад")])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

async def reject_pending_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("Доступно только администратору.")
        return
    keyboard = get_reject_keyboard()
    if not keyboard:
        await update.message.reply_text("Нет ожидающих регистрации.")
        return
    await update.message.reply_text("Выберите пользователя для отмены заявки:", reply_markup=keyboard)
    context.user_data['reject_mode'] = True

async def handle_reject_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('reject_mode'):
        return
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return
    text = update.message.text
    if text == "🔙 Назад":
        context.user_data.pop('reject_mode', None)
        await registration_menu(update, context)
        return
    if text.startswith("❌ "):
        nick_to_reject = text[2:]
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM pending_users WHERE nick = ?", (nick_to_reject,))
        conn.commit()
        conn.close()
        await update.message.reply_text(f"❌ Заявка для {nick_to_reject} отклонена.")
        await reject_pending_menu(update, context)
        return

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
    confirmed = confirm_all_pending()
    count = len(confirmed)
    active_poll = get_active_poll()
    if active_poll:
        for uid, nick, _ in confirmed:
            try:
                await send_first_question(uid, active_poll, context)
                logging.info(f"Отправлен опрос новому пользователю {nick} (ID: {uid})")
            except Exception as e:
                logging.error(f"Не удалось отправить опрос пользователю {uid}: {e}")
    try:
        await asyncio.to_thread(sync_pa_internal)
    except Exception as e:
        logging.error(f"Ошибка при синхронизации шаблона: {e}")
    await update.message.reply_text(f"✅ Подтверждено {count} пользователей. Они теперь зарегистрированы.")

# ---------- ПЛАНИРОВЩИК ОБЪЯВЛЕНИЙ (БОССЫ) ----------
async def send_announcement_to_all(text):
    users = get_all_users()
    for uid, _, _, _ in users:
        try:
            await app.bot.send_message(chat_id=uid, text=text)
        except Exception as e:
            logging.error(f"Не удалось отправить объявление {uid}: {e}")

async def create_boss_announcement(boss_name, day_of_week, time_str):
    text = f"📢 *{boss_name} сегодня!*\nСбор в {time_str}.\n\nУчаствуйте!"
    await send_announcement_to_all(text)

def schedule_boss_announcements(scheduler):
    scheduler.add_job(
        create_boss_announcement,
        'cron',
        day_of_week='wed',
        hour=11,
        minute=0,
        args=("Комендант", "среду", "20:45"),
        id="komendant_announce",
        timezone='Europe/Moscow'
    )
    scheduler.add_job(
        create_boss_announcement,
        'cron',
        day_of_week='sun',
        hour=11,
        minute=0,
        args=("Баньши", "воскресенье", "15:40"),
        id="banyshi_announce",
        timezone='Europe/Moscow'
    )
    logging.info("Планировщик объявлений о боссах запущен")

# ---------- РУЧНОЙ ОПРОС АДМИНОМ ЗА ДРУГОГО ----------
async def admin_poll_for_other(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("Доступно только администратору.")
        return
    poll = get_active_poll()
    if not poll:
        await update.message.reply_text("Нет активного опроса.")
        return
    context.user_data['admin_poll_step'] = 'awaiting_input'
    context.user_data['admin_poll_data'] = {'poll_id': poll['id'], 'meetings': poll['meetings']}
    keyboard = ReplyKeyboardMarkup([[KeyboardButton("❌ Отмена")]], resize_keyboard=True)
    await update.message.reply_text(
        "Введите данные для прохождения опроса за другого игрока в формате:\n\n"
        "`НИК \\ КЛАСС \\ ОТВЕТ_1 \\ ОТВЕТ_2 \\ ОТВЕТ_3 ...`\n\n"
        f"Количество ответов должно быть равно количеству встреч в опросе ({len(poll['meetings'])}):\n"
        f"{', '.join(poll['meetings'])}\n\n"
        "Пример: `Qudas \\ ВАР \\ Буду \\ Не буду \\ Не знаю`\n\n"
        "Для отмены нажмите «❌ Отмена».",
        parse_mode="Markdown",
        reply_markup=keyboard
    )

async def handle_admin_poll_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id) or context.user_data.get('admin_poll_step') != 'awaiting_input':
        return
    text = update.message.text.strip()
    if text == "❌ Отмена":
        context.user_data.pop('admin_poll_step', None)
        context.user_data.pop('admin_poll_data', None)
        await update.message.reply_text("Операция отменена.", reply_markup=get_main_keyboard(user_id))
        return
    parts = [part.strip() for part in text.split('\\')]
    if len(parts) < 3:
        await update.message.reply_text("❌ Неверный формат. Ожидается: НИК \\ КЛАСС \\ ОТВЕТ_1 \\ ОТВЕТ_2 ...")
        return
    nick = parts[0]
    user_class = parts[1].upper()
    if user_class not in CHARACTER_CLASSES:
        await update.message.reply_text(
            "❌ Неверный класс для PW 1.3.6. Допустимые классы: "
            + ", ".join(CHARACTER_CLASSES)
        )
        return
    answers = parts[2:]
    poll_data = context.user_data.get('admin_poll_data')
    if not poll_data:
        await update.message.reply_text("❌ Нет данных об опросе. Начните заново.")
        return
    meetings = poll_data['meetings']
    if len(answers) != len(meetings):
        await update.message.reply_text(
            f"❌ Количество ответов ({len(answers)}) не совпадает с количеством встреч ({len(meetings)}).\n"
            f"Ожидается: {len(meetings)} ответов."
        )
        return
    poll_id = poll_data['poll_id']
    for meeting, answer in zip(meetings, answers):
        save_external_response(poll_id, nick, user_class, meeting, answer, user_id)
    await update.message.reply_text(
        f"✅ Ответы для {nick} (класс: {user_class}) сохранены!\n"
        f"Встречи и ответы:\n" + "\n".join([f"• {m}: {a}" for m, a in zip(meetings, answers)]),
        reply_markup=ReplyKeyboardMarkup([[KeyboardButton("❌ Отмена")]], resize_keyboard=True)
    )

# ---------- НОВЫЙ СЦЕНАРИЙ СОЗДАНИЯ ГВГ-ОПРОСА ----------
async def start_gvg_poll_creation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("Доступно только администратору.")
        return
    context.user_data['gvg_poll'] = {'step': 'datetime'}
    keyboard = ReplyKeyboardMarkup([[KeyboardButton("❌ Отмена")]], resize_keyboard=True)
    await update.message.reply_text(
        "Введите дату и время ГВГ в формате:\n"
        "`ДД.ММ.ГГГГ ЧЧ:ММ` (например, 25.06.2026 19:00)\n\n"
        "Для отмены нажмите «❌ Отмена».",
        parse_mode="Markdown",
        reply_markup=keyboard
    )

async def handle_gvg_poll_creation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if 'gvg_poll' not in context.user_data:
        return
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return
    text = update.message.text.strip()
    data = context.user_data['gvg_poll']
    if text == "❌ Отмена":
        context.user_data.pop('gvg_poll', None)
        await update.message.reply_text("Создание ГВГ-опроса отменено.", reply_markup=get_admin_keyboard())
        return
    if data['step'] == 'datetime':
        try:
            datetime.strptime(text, "%d.%m.%Y %H:%M")
        except ValueError:
            await update.message.reply_text("❌ Неверный формат. Используйте `ДД.ММ.ГГГГ ЧЧ:ММ`")
            return
        data['datetime'] = text
        data['step'] = 'opponents'
        await update.message.reply_text("Введите название противников (или «-», если нет):")
        return
    elif data['step'] == 'opponents':
        data['opponents'] = text
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Продолжить (добавить ещё ГВГ)", callback_data="gvg_add_more")],
            [InlineKeyboardButton("✅ Закончить и создать опрос", callback_data="gvg_finish")]
        ])
        await update.message.reply_text(
            f"ГВГ добавлен:\n📅 {data['datetime']}\n⚔️ Противники: {data['opponents']}\n\n"
            "Выберите действие:",
            reply_markup=keyboard
        )
        data['step'] = 'waiting_choice'

async def gvg_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if not is_admin(user_id) or 'gvg_poll' not in context.user_data:
        await query.edit_message_text("Сессия создания опроса не найдена.")
        return
    data = context.user_data['gvg_poll']
    if data.get('step') != 'waiting_choice':
        await query.message.reply_text("Сначала введите дату и противников текущего ГВГ.")
        return
    if query.data == "gvg_add_more":
        if 'gvg_list' not in data:
            data['gvg_list'] = []
        data['gvg_list'].append({
            'datetime': data['datetime'],
            'opponents': data['opponents']
        })
        data['step'] = 'datetime'
        keyboard = ReplyKeyboardMarkup([[KeyboardButton("❌ Отмена")]], resize_keyboard=True)
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(
            "Введите дату и время следующего ГВГ в формате:\n"
            "`ДД.ММ.ГГГГ ЧЧ:ММ`\n\n"
            "Для отмены создания опроса нажмите «❌ Отмена».",
            parse_mode="Markdown",
            reply_markup=keyboard
        )
    elif query.data == "gvg_finish":
        if 'gvg_list' not in data:
            data['gvg_list'] = []
        if 'datetime' in data and 'opponents' in data:
            data['gvg_list'].append({
                'datetime': data['datetime'],
                'opponents': data['opponents']
            })
        meetings = [f"{g['datetime']} - {g['opponents']}" for g in data['gvg_list']]
        poll_text = f"ГВГ на {datetime.now().strftime('%d.%m.%Y')}"
        create_poll(poll_text, meetings)
        await query.edit_message_text(
            f"✅ Опрос ГВГ создан!\n\nКоличество ГВГ: {len(meetings)}\nВстречи:\n" + "\n".join(meetings)
        )
        context.user_data.pop('gvg_poll', None)
        await query.message.reply_text("Админ-панель:", reply_markup=get_admin_keyboard())

# ---------- КЛАВИАТУРЫ ----------
def get_main_keyboard(user_id):
    keyboard = [
        [KeyboardButton("👤 Мой профиль"), KeyboardButton("💰 Моя ЗП")],
        [KeyboardButton("📊 Моя активность"), KeyboardButton("📝 Мои ответы")],
        [KeyboardButton("❓ Помощь"), KeyboardButton("💰 Заказ кеша")]
    ]
    if is_admin(user_id):
        keyboard.append([KeyboardButton("📊 Админ-панель")])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

async def my_salary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_user_valid(user_id):
        await update.message.reply_text('Вы не зарегистрированы. Нажмите /start.')
        return
    try:
        title, result = await asyncio.to_thread(salary_summary, get_user_nick(user_id))
        await update.message.reply_text(f"💰 {title}\n" + ('\n'.join(result) or 'Ваш ник пока не найден в месячной таблице.'))
    except Exception as exc:
        await update.message.reply_text(f"❌ Не удалось прочитать зарплату: {exc}")


def get_admin_keyboard():
    keyboard = [
        [KeyboardButton("📝 Регистрация")],
        [KeyboardButton("📊 Управление опросами")],
        [KeyboardButton("🏰 Управление кланом")],
        [KeyboardButton("💸 Выдача кеша"), KeyboardButton("🔙 Назад")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_polls_management_keyboard():
    keyboard = [
        [KeyboardButton("📝 Создать опрос (ГВГ)"), KeyboardButton("👥 Пройти за другого")],
        [KeyboardButton("📤 Разослать опрос"), KeyboardButton("📈 Результаты опроса")],
        [KeyboardButton("❌ Не ответившие"), KeyboardButton("🔙 Назад")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_clan_management_keyboard():
    keyboard = [
        [KeyboardButton("👥 Список пользователей")],
        [KeyboardButton("✏️ Исправить профиль"), KeyboardButton("📊 Активность игроков")],
        [KeyboardButton("💰 Расчет ЗП"), KeyboardButton("📝 Регистрация")],
        [KeyboardButton("🔙 Назад")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

async def edit_profile_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("Доступно только администратору.")
        return
    keyboard = [
        [KeyboardButton("✏️ Исправить класс")],
        [KeyboardButton("🔄 Исправить ник")],
        [KeyboardButton("🔙 Назад")]
    ]
    await update.message.reply_text("Выберите, что хотите исправить:", reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))

def get_class_keyboard():
    classes = CHARACTER_CLASSES
    keyboard = [[KeyboardButton(cls) for cls in classes[i:i+3]] for i in range(0, len(classes), 3)]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)

MENU_PARENTS = {
    "admin": "main", "polls": "admin", "clan": "admin",
    "registration": "admin", "profile": "clan",
}


def clear_input_state(context):
    # Keep the menu location, but discard all unfinished input scenarios.
    menu = context.user_data.get('menu', 'main')
    context.user_data.clear()
    context.user_data['menu'] = menu


async def show_menu(update, context, menu='main'):
    user_id = update.effective_user.id
    if menu != 'main' and not is_admin(user_id):
        await update.message.reply_text("Доступно только администратору.")
        return
    context.user_data['menu'] = menu
    if menu == 'registration':
        await registration_menu(update, context)
    elif menu == 'profile':
        await edit_profile_menu(update, context)
    else:
        menus = {
            'main': ("Главное меню:", lambda: get_main_keyboard(user_id)),
            'admin': ("Админ-панель:", get_admin_keyboard),
            'polls': ("Управление опросами:", get_polls_management_keyboard),
            'clan': ("Управление кланом:", get_clan_management_keyboard),
        }
        title, keyboard = menus[menu]
        await update.message.reply_text(title, reply_markup=keyboard())


async def route_navigation(update, context):
    text = update.message.text
    destinations = {
        "📊 Админ-панель": 'admin', "📊 Управление опросами": 'polls',
        "🏰 Управление кланом": 'clan', "📝 Регистрация": 'registration',
        "✏️ Исправить профиль": 'profile',
    }
    if text in ("🔙 Назад", "❌ Отмена"):
        current = context.user_data.get('menu', 'main')
        # Back/cancel from a form returns to its menu, not its parent.
        in_form = any(k != 'menu' for k in context.user_data)
        destination = current if in_form else MENU_PARENTS.get(current, 'main')
        clear_input_state(context)
        await show_menu(update, context, destination)
        return True
    if text in destinations:
        if not is_admin(update.effective_user.id):
            await update.message.reply_text("Доступно только администратору.")
            return True
        clear_input_state(context)
        await show_menu(update, context, destinations[text])
        return True
    return False


# ---------- ОБРАБОТЧИКИ ПОЛЬЗОВАТЕЛЯ ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    clear_input_state(context)
    context.user_data['menu'] = 'main'
    approve_admin_registration(user_id)
    if is_registered(user_id):
        nick = get_user_nick(user_id)
        user_class = get_user_class(user_id)
        await update.message.reply_text(f"С возвращением, {nick} (класс: {user_class})!", reply_markup=get_main_keyboard(user_id))
        await sync_registered_profile(update, user_id)
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

def escape_md(text):
    chars = r'_*[]()~`>#+-=|{}.!'
    for ch in chars:
        text = text.replace(ch, f'\\{ch}')
    return text

async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("Доступно только администратору.")
        return
    users = get_all_users()
    if not users:
        await update.message.reply_text("Нет пользователей.")
        return
    message_text = "📋 *Список пользователей:*\n\n"
    for uid, nick, user_class, _ in users:
        safe_nick = escape_md(nick)
        safe_class = escape_md(user_class if user_class else "Не указан")
        message_text += f"• {safe_nick} - {safe_class}\n"
        if len(message_text) > 3800:
            await update.message.reply_text(message_text, parse_mode="Markdown")
            message_text = ""
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🆘 Показать ID всех пользователей", callback_data="show_all_ids")]
    ])
    await update.message.reply_text(message_text, parse_mode="Markdown", reply_markup=keyboard)

async def show_all_ids_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    users = get_all_users()
    if not users:
        await query.edit_message_text("Нет пользователей.")
        return
    text = "🆔 *ID всех пользователей:*\n\n"
    for uid, nick, user_class, _ in users:
        safe_nick = escape_md(nick)
        safe_class = escape_md(user_class if user_class else "Не указан")
        text += f"• {safe_nick} - {safe_class} - `{uid}`\n"
        if len(text) > 3800:
            await query.edit_message_text(text, parse_mode="Markdown")
            return
    await query.edit_message_text(text, parse_mode="Markdown")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text

    approve_admin_registration(user_id)
    if await route_navigation(update, context):
        return
    if context.user_data.get('reject_mode'):
        await handle_reject_pending(update, context)
        return
    if context.user_data.get('gvg_poll'):
        await handle_gvg_poll_creation(update, context)
        return

    # Ручной опрос админом
    if context.user_data.get('admin_poll_step') == 'awaiting_input':
        await handle_admin_poll_text(update, context)
        return

    # Редактирование класса
    if context.user_data.get('edit_class_mode'):
        await handle_edit_class(update, context)
        return

    # Редактирование ника (ДОБАВЛЕНО)
    if context.user_data.get('edit_nick_mode'):
        await handle_edit_nick(update, context)
        return

    # Выбор активности
    if context.user_data.get('activity_mode') and context.user_data.get('activity_step'):
        await handle_activity_choice(update, context)
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

    # Регистрация: класс
    if context.user_data.get('awaiting_class'):
        valid_classes = CHARACTER_CLASSES
        if text in valid_classes:
            nick = context.user_data.pop('temp_nick')
            user_class = text
            add_pending_user(user_id, nick, user_class)
            context.user_data.pop('awaiting_class', None)
            if is_admin(user_id):
                approve_admin_registration(user_id)
                await update.message.reply_text(
                    f"✅ Администратор зарегистрирован: {nick} ({user_class}).",
                    reply_markup=get_main_keyboard(user_id)
                )
                await sync_registered_profile(update, user_id)
                return
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
        await update.message.reply_text(
            f"👤 *Ваш профиль*\n\nНик: {nick}\nКласс: {user_class}",
            parse_mode="Markdown"
        )
    elif text == "📊 Моя активность":
        nick = get_user_nick(user_id)
        if not nick:
            await update.message.reply_text("❌ Не удалось определить ваш ник.")
            return
        try:
            activity_count = await asyncio.to_thread(get_user_activity_count, nick)
        except Exception as exc:
            await update.message.reply_text(f"❌ Не удалось прочитать активность: {exc}")
            return
        await update.message.reply_text(
            f"📊 *Ваша активность*\n\n"
            f"┌ Посещено событий: *{activity_count}*\n"
            f"└ (за текущий месяц)\n\n"
            f"💡 *Совет:* больше участвуйте в ГВГ и боссах!",
            parse_mode="Markdown"
        )
    elif text == "📝 Мои ответы":
        answers = get_user_current_poll_answers(user_id)
        poll = get_active_poll()
        if not poll:
            await update.message.reply_text("📭 *Нет активного опроса*\nСейчас нет опросов для ответа.", parse_mode="Markdown")
        elif not answers:
            await update.message.reply_text("❓ *Вы ещё не ответили* на текущий опрос.\nНажмите кнопки под сообщением опроса.", parse_mode="Markdown")
        else:
            msg_text = "📝 *Ваши ответы на текущий опрос*\n\n"
            for meeting, ans in answers.items():
                emoji = "✅" if ans == "да" else "❌" if ans == "нет" else "❓"
                msg_text += f"{emoji} *{meeting}*: {ans}\n"
            await update.message.reply_text(msg_text, parse_mode="Markdown")
    elif text == "💰 Моя ЗП":
        await my_salary(update, context)
        return
    elif text == "❓ Помощь":
        await update.message.reply_text(
            "По всем вопросам и предложениям обращаться к @Dark_Dany_M и в клановый чат https://t.me/c/2254350662/44735"
        )
    elif text == "💰 Заказ кеша":
        await cash_order_start(update, context)
    elif text == "📝 Создать опрос (ГВГ)" and is_admin(user_id):
        await start_gvg_poll_creation(update, context)
        return
    elif text == "👥 Пройти за другого" and is_admin(user_id):
        await admin_poll_for_other(update, context)
    elif text == "📤 Разослать опрос" and is_admin(user_id):
        await send_poll_to_all(update, context)
    elif text == "📈 Результаты опроса" and is_admin(user_id):
        await export_command(update, context)
    elif text == "❌ Не ответившие" and is_admin(user_id):
        poll = get_active_poll()
        if not poll:
            await update.message.reply_text("Нет активного опроса.")
            return
        non_responders = get_non_responders(poll['id'])
        if not non_responders:
            await update.message.reply_text("✅ Все пользователи ответили на текущий опрос!")
            return
        msg = "❌ *Не ответили на опрос:*\n\n"
        for uid, nick in non_responders:
            msg += f"• {nick} (ID: `{uid}`)\n"
        await update.message.reply_text(msg, parse_mode="Markdown")
    elif text == "💸 Выдача кеша" and is_admin(user_id):
        await process_cash_orders(update, context)
    elif text == "👥 Список пользователей" and is_admin(user_id):
        await users_command(update, context)
    elif text == "✏️ Исправить класс" and is_admin(user_id):
        await edit_class_command(update, context)
    elif text == "🔄 Исправить ник" and is_admin(user_id):
        await edit_nick_command(update, context)
    elif text == "📊 Активность игроков" and is_admin(user_id):
        await activity_menu(update, context)
    elif text == "📋 Список ожидания" and is_admin(user_id):
        await list_pending_command(update, context)
    elif text == "✅ Подтвердить всех" and is_admin(user_id):
        await confirm_all_pending_command(update, context)
    elif text == "❌ Отказать" and is_admin(user_id):
        await reject_pending_menu(update, context)
    elif text == "💰 Расчет ЗП" and is_admin(user_id):
        await calculate_salaries(update, context)
        return
    else:
        await update.message.reply_text("Неизвестная команда. Используйте кнопки меню.")

async def handle_poll_creation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return

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
            InlineKeyboardButton("✅ Буду", callback_data=f"poll_{poll['id']}_{first_meeting}_да_1"),
            InlineKeyboardButton("❌ Не буду", callback_data=f"poll_{poll['id']}_{first_meeting}_нет_1"),
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

async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_input_state(context)
    await show_menu(update, context, context.user_data.get('menu', 'main'))


async def end_poll_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Доступно только администратору.")
        return
    deactivate_poll()
    await update.message.reply_text("Опрос завершён.")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('activity_mode'):
        await handle_activity_photo(update, context)
        return
    if context.user_data.get('cash_order') and context.user_data['cash_order'].get('step') == 'photo':
        await handle_cash_order_photo(update, context)
        return
    await update.message.reply_text("Если хотите заказать кеш, нажмите кнопку «💰 Заказ кеша». Для активности используйте пункт «📊 Активность игроков».")

# ---------- ЗАПУСК ----------
async def post_init(app: Application):
    scheduler = AsyncIOScheduler(timezone='Europe/Moscow')
    schedule_boss_announcements(scheduler)
    scheduler.start()
    app.bot_data['scheduler'] = scheduler
    logging.info("Планировщик объявлений запущен")

def main():
    global app
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.post_init = post_init

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(CommandHandler("users", users_command))
    app.add_handler(CommandHandler("send_poll", send_poll_to_all))
    app.add_handler(CommandHandler("export", export_command))
    app.add_handler(CommandHandler("end_poll", end_poll_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("edit_class", edit_class_command))
    app.add_handler(CallbackQueryHandler(show_all_ids_callback, pattern="^show_all_ids$"))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CommandHandler("mysalary", my_salary))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(poll_callback, pattern="^poll_"))
    app.add_handler(CallbackQueryHandler(confirm_callback, pattern="^confirm_"))
    app.add_handler(CallbackQueryHandler(restart_callback, pattern="^restart_"))
    app.add_handler(CallbackQueryHandler(cash_callback, pattern="^(cash_done_|cash_reject_)"))
    app.add_handler(CallbackQueryHandler(gvg_callback, pattern="^(gvg_add_more|gvg_finish)"))
    app.add_handler(CommandHandler("leave", leave_chat))
    app.add_handler(CommandHandler("remove_all_keyboards", remove_all_keyboards))
    app.add_handler(CommandHandler("calc_salary", calculate_salaries))
    app.add_handler(CommandHandler("sync_pa", sync_pa_command))

    print("Бот запущен. Нажмите Ctrl+C для остановки.")
    app.run_polling()

if __name__ == "__main__":
    main()
