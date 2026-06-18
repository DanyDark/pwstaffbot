import sqlite3
import logging
import os
import json
import traceback
import requests
import base64
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
    # Таблица для внешних ответов (админ за другого)
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
    # Добавляем поле external_class, если его нет (миграция)
    cursor.execute("PRAGMA table_info(external_responses)")
    columns = [col[1] for col in cursor.fetchall()]
    if 'external_class' not in columns:
        cursor.execute("ALTER TABLE external_responses ADD COLUMN external_class TEXT")
    # Таблица для учёта созданных месячных листов активности
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

def save_external_response(poll_id, external_nick, external_class, meeting, answer, admin_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO external_responses (poll_id, external_nick, external_class, meeting, answer, admin_id)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (poll_id, external_nick, external_class, meeting, answer, admin_id))
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
    cursor.execute('''
        SELECT meeting, answer FROM poll_responses
        WHERE user_id = ? AND poll_id = ?
    ''', (user_id, poll['id']))
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
    cursor.execute("SELECT DISTINCT user_id FROM poll_responses WHERE poll_id = ?", (poll_id,))
    responders = set(row[0] for row in cursor.fetchall())
    conn.close()
    non_responders = [(uid, nick) for uid, nick in all_users if uid not in responders]
    return non_responders

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
    try:
        template = spreadsheet.worksheet("ШАБЛОН АКТИВНОСТИ")
    except gspread.WorksheetNotFound:
        logging.error("Лист 'ШАБЛОН АКТИВНОСТИ' не найден в Google Sheets. Создайте его вручную.")
        raise Exception("Отсутствует шаблон активности")

    now = datetime.now()
    year = now.year
    month = now.month
    sheet_name = f"Активность {month:02d}.{year}"

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT sheet_name FROM activity_months WHERE year = ? AND month = ?", (year, month))
    row = cursor.fetchone()
    if row:
        try:
            ws = spreadsheet.worksheet(row[0])
            conn.close()
            return ws
        except gspread.WorksheetNotFound:
            logging.warning(f"Лист {row[0]} не найден, пересоздаём")

    new_ws = template.duplicate(insert_sheet_index=0, new_sheet_name=sheet_name)
    cursor.execute("INSERT INTO activity_months (year, month, sheet_name) VALUES (?, ?, ?)", (year, month, sheet_name))
    conn.commit()
    conn.close()
    return new_ws

def get_current_activity_sheet():
    spreadsheet = get_google_spreadsheet()
    if not spreadsheet:
        raise Exception("Не удалось подключиться к Google Sheets")
    return get_or_create_monthly_activity_sheet(spreadsheet)

def find_column_by_header(ws, header_name):
    """Возвращает номер столбца (1-базированный) по названию заголовка в первой строке"""
    headers = ws.row_values(1)
    for idx, h in enumerate(headers):
        if h.strip().lower() == header_name.strip().lower():
            return idx + 1
    return None

# Функция для поиска столбца "НИК" (он в столбце C, но для надёжности ищем по заголовку)
def find_nick_column(ws):
    headers = ws.row_values(1)
    for idx, h in enumerate(headers):
        if h.strip().lower() == "ник":
            return idx + 1
    return None

# Конфигурация активностей: название -> (start_col, end_col) 1-базированные
ACTIVITY_COLUMNS = {
    "Комендант": (3, 7),   # C-G
    "Баньши": (8, 11),     # H-K
    "ГВГ": (12, 15)        # L-O
}

def get_available_activities(ws):
    """Возвращает список активностей, которые есть в конфигурации (Комендант, Баньши, ГВГ)"""
    return list(ACTIVITY_COLUMNS.keys())

def mark_activity_in_sheet(ws, activity_name, nicks):
    """Ставит 'БЫЛ' в первую свободную ячейку в диапазоне активности для каждого найденного ника"""
    nick_col = find_nick_column(ws)
    if nick_col is None:
        raise Exception("В листе не найден столбец 'НИК'")
    
    if activity_name not in ACTIVITY_COLUMNS:
        raise Exception(f"Активность '{activity_name}' не поддерживается шаблоном")
    
    start_col, end_col = ACTIVITY_COLUMNS[activity_name]
    all_values = ws.get_all_values()
    updated = 0
    for row_idx, row in enumerate(all_values[1:], start=2):
        if len(row) < nick_col:
            continue
        nick_in_sheet = row[nick_col-1].strip()
        if not nick_in_sheet:
            continue
        if nick_in_sheet.lower() not in [n.lower() for n in nicks]:
            continue
        # Ищем первую пустую ячейку в диапазоне активности для этой строки
        for col in range(start_col, end_col+1):
            if len(row) >= col and row[col-1] and row[col-1].strip():
                continue
            # Пустая ячейка найдена – ставим "БЫЛ"
            ws.update_cell(row_idx, col, "БЫЛ")
            updated += 1
            break
    return updated

def get_user_activity_count(nick):
    """Возвращает количество 'БЫЛ' для данного ника в текущем листе активности (по всем столбцам, кроме столбца 'НИК')"""
    try:
        ws = get_current_activity_sheet()
        nick_col = find_nick_column(ws)
        if nick_col is None:
            return 0
        all_values = ws.get_all_values()
        count = 0
        for row in all_values[1:]:
            if len(row) >= nick_col and row[nick_col-1].strip().lower() == nick.lower():
                # Считаем "БЫЛ" во всех столбцах, кроме столбца с ником
                for col_idx, val in enumerate(row):
                    if col_idx != nick_col-1 and val == "БЫЛ":
                        count += 1
                break
        return count
    except Exception as e:
        logging.error(f"Ошибка подсчёта активности для {nick}: {e}")
        return 0
        
async def calculate_salaries(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("Доступно только администратору.")
        return

    try:
        ws = get_current_activity_sheet()
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка доступа к Google Sheets: {e}")
        return

    nick_col = find_column_by_header(ws, "НИК")
    salary_col = find_column_by_header(ws, "Общая ЗП")
    if nick_col is None or salary_col is None:
        await update.message.reply_text("❌ В листе не найдены столбцы 'НИК' или 'Общая ЗП'.")
        return

    all_values = ws.get_all_values()
    if len(all_values) < 2:
        await update.message.reply_text("Нет данных для расчета.")
        return

    updated_rows = 0
    for row_idx, row in enumerate(all_values[1:], start=2):
        nick = row[nick_col-1].strip() if len(row) >= nick_col else ""
        if not nick:
            continue

        total_salary = 0
        for activity_name, (start_col, end_col) in ACTIVITY_COLUMNS.items():
            for col in range(start_col, end_col+1):
                if len(row) >= col:
                    cell_value = row[col-1].strip()
                    if cell_value == "БЫЛ":
                        total_salary += 10_000_000
                    elif cell_value == "БЫЛ ПЛ":
                        total_salary += 20_000_000

        ws.update_cell(row_idx, salary_col, total_salary)
        updated_rows += 1

    await update.message.reply_text(f"✅ Расчет ЗП завершен.\nОбновлено строк: {updated_rows}")

async def sync_pa_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("Доступно только администратору.")
        return

    # 1. Получаем всех пользователей из БД
    users = get_all_users()
    if not users:
        await update.message.reply_text("Нет зарегистрированных пользователей.")
        return

    # 2. Маппинг классов из БД в названия разделов в шаблоне
    class_mapping = {
        "ВАР": "ВАРЫ",
        "МАГ": "МАГИ",
        "ДРУ": "ДРУЛИ",
        "ТАНК": "ТАНКИ",
        "ЛУК": "ЛУЧНИКИ",
        "ПРИСТ": "ПРИСТЫ",
        "СИН": "СИНЫ",
        "ШАМ": "ШАМЫ",
        "СИК": "СТРАЖИ",
        "МИСТИК": "МИСТИКИ"
    }
    # Порядок разделов в шаблоне (как они идут сверху вниз)
    section_order = ["ВАРЫ", "МАГИ", "ДРУЛИ", "ТАНКИ", "ЛУЧНИКИ", "ПРИСТЫ", "СИНЫ", "ШАМЫ", "СТРАЖИ", "МИСТИКИ"]

    # Группируем ники по разделам
    grouped = {section: [] for section in section_order}
    for uid, nick, user_class, _ in users:
        section = class_mapping.get(user_class.upper())
        if section:
            grouped[section].append(nick)
        else:
            logging.warning(f"Неизвестный класс: {user_class}")

    # Сортируем ники внутри каждой группы по алфавиту
    for section in grouped:
        grouped[section].sort()

    # 3. Открываем лист "ШАБЛОН АКТИВНОСТИ"
    spreadsheet = get_google_spreadsheet()
    if not spreadsheet:
        await update.message.reply_text("❌ Не удалось подключиться к Google Sheets.")
        return
    try:
        ws = spreadsheet.worksheet("ШАБЛОН АКТИВНОСТИ")
    except gspread.WorksheetNotFound:
        await update.message.reply_text("❌ Лист 'ШАБЛОН АКТИВНОСТИ' не найден. Создайте его вручную.")
        return

    # 4. Находим строки с заголовками разделов (столбец B)
    all_values = ws.get_all_values()
    header_rows = {}  # section_name -> row_index (1-базированный)
    for idx, row in enumerate(all_values, start=1):
        if len(row) >= 2:
            cell_value = row[1].strip()
            if cell_value in section_order:
                header_rows[cell_value] = idx

    # Проверяем, что все разделы найдены
    missing_sections = [s for s in section_order if s not in header_rows]
    if missing_sections:
        await update.message.reply_text(f"⚠️ В шаблоне не найдены разделы: {', '.join(missing_sections)}. Добавьте их вручную.")
        return

    # 5. Синхронизация по каждому разделу
    total_updated = 0
    for idx, section in enumerate(section_order):
        header_row = header_rows[section]
        next_header_row = header_rows.get(section_order[idx+1]) if idx+1 < len(section_order) else None

        # Определяем количество доступных строк под разделом
        if next_header_row:
            available_rows = next_header_row - header_row - 1
        else:
            # последний раздел – до конца листа
            available_rows = len(all_values) - header_row

        nicks = grouped.get(section, [])
        needed_rows = len(nicks)

        # Если ников больше, чем доступных строк – вставляем недостающие
        if needed_rows > available_rows:
            rows_to_insert = needed_rows - available_rows
            if next_header_row:
                insert_index = next_header_row  # вставляем перед следующим заголовком
            else:
                insert_index = len(all_values) + 1  # в конец
            # Вставляем строки (одну за раз, начиная с конца, чтобы не сбить индексы)
            for _ in range(rows_to_insert):
                ws.insert_rows(insert_index, amount=1)
                # После вставки индексы сдвигаются, поэтому увеличиваем insert_index (если вставляем перед одним и тем же местом)
                # Но так как мы вставляем перед next_header_row, при каждой вставке next_header_row смещается на 1,
                # поэтому мы должны вставлять всегда на одну и ту же позицию (insert_index не меняется, но после вставки следующая вставка
                # должна быть на ту же позицию, потому что строка сдвинулась). Проще вставить все строки за один раз.
                # Однако gspread не поддерживает массовую вставку с указанием количества, поэтому будем вставлять по одной, но сохраняя индекс.
                # После вставки одной строки, следующая вставка должна быть на тот же индекс (т.к. строка сдвинулась).
                pass
            # Альтернатива: использовать batch_update, но это сложнее. Для простоты вставим по одной, но каждый раз на тот же индекс.
            # Для этого нужно после каждой вставки не увеличивать insert_index, а оставлять прежним, потому что строка, перед которой вставляем, смещается вниз.
            # Т.е. вставляем rows_to_insert раз на позицию insert_index.
            for _ in range(rows_to_insert):
                ws.insert_rows(insert_index, amount=1)
                # insert_index не меняем, так как следующая вставка должна быть перед той же строкой (которая теперь сдвинулась)
            # После вставки обновим all_values (перечитаем лист) – но проще потом обновить значения.
            # Перечитываем лист, чтобы получить актуальные индексы
            all_values = ws.get_all_values()
            # Корректируем header_rows для следующих разделов (их индексы сдвинулись)
            for s in section_order[idx+1:]:
                header_rows[s] += rows_to_insert
            # Также сдвигаем текущий next_header_row для следующего цикла, но мы его не используем дальше в этом цикле

        # Теперь записываем ники в столбец B, начиная с header_row+1
        start_row = header_row + 1
        for i, nick in enumerate(nicks):
            ws.update_cell(start_row + i, 2, nick)  # столбец B = номер 2
            total_updated += 1

        # Очищаем оставшиеся ячейки в этом блоке (если ников меньше, чем было строк)
        # Это необязательно, но для чистоты можно очистить лишние строки (которые остались пустыми)
        # Однако мы не удаляем строки, поэтому оставляем как есть – они будут пустыми.

    await update.message.reply_text(f"✅ Синхронизация завершена.\nОбновлено ников: {total_updated}")
    
def sync_pa_internal():
    """Внутренняя синхронизация шаблона (без отправки сообщений в чат)"""
    users = get_all_users()
    if not users:
        return

    class_mapping = {
        "ВАР": "ВАРЫ",
        "МАГ": "МАГИ",
        "ДРУ": "ДРУЛИ",
        "ТАНК": "ТАНКИ",
        "ЛУК": "ЛУЧНИКИ",
        "ПРИСТ": "ПРИСТЫ",
        "СИН": "СИНЫ",
        "ШАМ": "ШАМЫ",
        "СИК": "СТРАЖИ",
        "МИСТИК": "МИСТИКИ"
    }
    section_order = ["ВАРЫ", "МАГИ", "ДРУЛИ", "ТАНКИ", "ЛУЧНИКИ", "ПРИСТЫ", "СИНЫ", "ШАМЫ", "СТРАЖИ", "МИСТИКИ"]

    grouped = {section: [] for section in section_order}
    for uid, nick, user_class, _ in users:
        section = class_mapping.get(user_class.upper())
        if section:
            grouped[section].append(nick)
        else:
            logging.warning(f"Неизвестный класс: {user_class}")

    for section in grouped:
        grouped[section].sort()

    spreadsheet = get_google_spreadsheet()
    if not spreadsheet:
        logging.error("Не удалось подключиться к Google Sheets для синхронизации шаблона")
        return
    try:
        ws = spreadsheet.worksheet("ШАБЛОН АКТИВНОСТИ")
    except gspread.WorksheetNotFound:
        logging.error("Лист 'ШАБЛОН АКТИВНОСТИ' не найден")
        return

    all_values = ws.get_all_values()
    header_rows = {}
    for idx, row in enumerate(all_values, start=1):
        if len(row) >= 2:
            cell_value = row[1].strip()
            if cell_value in section_order:
                header_rows[cell_value] = idx

    missing_sections = [s for s in section_order if s not in header_rows]
    if missing_sections:
        logging.warning(f"В шаблоне не найдены разделы: {', '.join(missing_sections)}")
        return

    total_updated = 0
    for idx, section in enumerate(section_order):
        header_row = header_rows[section]
        next_header_row = header_rows.get(section_order[idx+1]) if idx+1 < len(section_order) else None

        if next_header_row:
            available_rows = next_header_row - header_row - 1
        else:
            available_rows = len(all_values) - header_row

        nicks = grouped.get(section, [])
        needed_rows = len(nicks)

        if needed_rows > available_rows:
            rows_to_insert = needed_rows - available_rows
            if next_header_row:
                insert_index = next_header_row
            else:
                insert_index = len(all_values) + 1
            for _ in range(rows_to_insert):
                ws.insert_rows(insert_index, amount=1)
            # Перечитываем лист, чтобы обновить индексы для следующих разделов
            all_values = ws.get_all_values()
            for s in section_order[idx+1:]:
                if s in header_rows:
                    header_rows[s] += rows_to_insert

        start_row = header_row + 1
        for i, nick in enumerate(nicks):
            ws.update_cell(start_row + i, 2, nick)
            total_updated += 1

    logging.info(f"Синхронизация шаблона завершена. Обновлено ников: {total_updated}")
    return total_updated

# ---------- ЭКСПОРТ РЕЗУЛЬТАТОВ ОПРОСА ----------
def get_responses_grouped_by_meeting(poll_id):
    grouped = {}
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    # Обычные ответы пользователей
    cursor.execute('''
        SELECT u.nick, u.class, pr.meeting, pr.answer
        FROM poll_responses pr
        JOIN users u ON pr.user_id = u.user_id
        WHERE pr.poll_id = ?
    ''', (poll_id,))
    rows = cursor.fetchall()
    for nick, user_class, meeting, answer in rows:
        if meeting not in grouped:
            grouped[meeting] = []
        grouped[meeting].append((nick, user_class if user_class else "Не указан", answer))
    # Внешние ответы (админ за другого)
    cursor.execute('''
        SELECT external_nick, external_class, meeting, answer
        FROM external_responses
        WHERE poll_id = ?
    ''', (poll_id,))
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
            # Сортировка по классу (второй элемент) и по нику (первый)
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
    cursor.execute('''
        INSERT INTO cash_orders (user_id, nick, photo_file_id, description, status)
        VALUES (?, ?, ?, ?, 'pending')
    ''', (user_id, nick, photo_file_id, "Золотые яйца"))
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
    # Возвращаем в админ-панель
    await update.message.reply_text("Админ-панель:", reply_markup=get_admin_keyboard())
# ---------- ИСПРАВЛЕНИЕ НИКА -------------
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
    # Обновляем ник в базе
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET nick = ? WHERE user_id = ?", (new_nick, target_user_id))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"Ник пользователя {context.user_data['edit_old_nick']} изменён на {new_nick}.")
    context.user_data.pop('edit_nick_mode', None)
    context.user_data.pop('edit_old_nick', None)
    context.user_data.pop('edit_target_user_id', None)
    # Возвращаем в админ-панель
    await update.message.reply_text("Админ-панель:", reply_markup=get_admin_keyboard())

# ---------- АКТИВНОСТЬ ИГРОКОВ (НОВАЯ ВЕРСИЯ) ----------
async def activity_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("Доступно только администратору.")
        return
    context.user_data['activity_mode'] = True
    keyboard = ReplyKeyboardMarkup([[KeyboardButton("❌ Отмена")]], resize_keyboard=True)
    await update.message.reply_text(
        "📸 Отправьте скриншот (фото) со списком ников игроков.\nПосле распознавания вы сможете выбрать активность.\n\nДоступные активности: Комендант, Баньши, ГВГ.\n\nДля отмены нажмите «❌ Отмена».",
        reply_markup=keyboard
    )
    
def sort_sheet_by_class(worksheet):
    """Сортирует лист по столбцу 'Класс' (столбец B) от А до Я, сохраняя заголовок"""
    all_values = worksheet.get_all_values()
    if len(all_values) < 2:
        return

    headers = all_values[0]
    data = all_values[1:]
    # Проверяем, что столбец B есть
    if len(headers) < 2:
        return
    # Сортируем по столбцу B (класс)
    data_sorted = sorted(data, key=lambda row: row[1].strip() if len(row) > 1 else "")
    # Объединяем заголовки и отсортированные данные
    new_values = [headers] + data_sorted
    # Очищаем лист и записываем отсортированные данные
    worksheet.clear()
    if new_values:
        worksheet.update(values=new_values, range_name='A1')

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

    users = get_all_users()
    known_nicks = [nick for _, nick, _, _ in users]

    # Для каждого распознанного ника ищем лучшее совпадение (порог 70)
    final_ordered_nicks = []
    matched_count = 0
    unmatched_nicks = []
    for rn in raw_nicks:
        best_match = None
        best_ratio = 0
        for kn in known_nicks:
            ratio = fuzz.ratio(rn.lower(), kn.lower())
            if ratio > best_ratio:
                best_ratio = ratio
                best_match = kn
        if best_ratio >= 70 and best_match:
            final_ordered_nicks.append(best_match)
            matched_count += 1
        else:
            final_ordered_nicks.append(rn)
            unmatched_nicks.append(rn)

    context.user_data['activity_nicks'] = final_ordered_nicks
    context.user_data['activity_raw'] = raw_nicks
    context.user_data['activity_matched_count'] = matched_count
    context.user_data['activity_unmatched'] = unmatched_nicks

    if not final_ordered_nicks:
        await update.message.reply_text("Не удалось распознать ни одного ника после сопоставления.")
        return

    stats = f"✅ Распознано: {len(raw_nicks)} ников.\n" \
            f"🎯 Совпало с зарегистрированными: {matched_count}.\n" \
            f"❓ Не распознано (будут записаны как есть): {len(unmatched_nicks)}.\n\n"
    if unmatched_nicks:
        stats += f"Неопознанные: {', '.join(unmatched_nicks)}\n\n"

    activities = ["Комендант", "Баньши", "ГВГ"]
    keyboard = []
    for act in activities:
        keyboard.append([KeyboardButton(act)])
    keyboard.append([KeyboardButton("❌ Отмена")])
    await update.message.reply_text(
        stats + f"Список для записи (в порядке скриншота): {', '.join(final_ordered_nicks)}\n\nВыберите активность:",
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

    try:
        ws = get_current_activity_sheet()
        updated = mark_activity_in_sheet_with_pl(ws, activity, nicks, threshold=70)
        nick_col = find_column_by_header(ws, "НИК")
        all_nicks_in_sheet = []
        if nick_col:
            all_vals = ws.get_all_values()
            all_nicks_in_sheet = [row[nick_col-1].strip() for row in all_vals[1:] if row and len(row) >= nick_col and row[nick_col-1].strip()]
        
        # Нечёткое сравнение для формирования списка не найденных (порог 70)
        not_found = []
        for recognized_nick in nicks:
            best_ratio = 0
            for table_nick in all_nicks_in_sheet:
                ratio = fuzz.ratio(recognized_nick.lower(), table_nick.lower())
                if ratio > best_ratio:
                    best_ratio = ratio
            if best_ratio < 70:
                not_found.append(recognized_nick)
        
        await update.message.reply_text(
            f"✅ Готово!\nАктивность: {activity}\nПоставлено отметок: {updated}\nРаспознано ников: {len(nicks)}"
            + (f"\n⚠️ Не найдены в таблице: {', '.join(not_found)}" if not_found else ""),
            reply_markup=get_main_keyboard(user_id)
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка при записи: {e}")
    finally:
        context.user_data.pop('activity_mode', None)
        context.user_data.pop('activity_step', None)
        context.user_data.pop('activity_nicks', None)

def mark_activity_in_sheet_with_pl(ws, activity_name, ordered_nicks, threshold=70):
    """Ставит 'БЫЛ ПЛ' для первого распознанного ника, 'БЫЛ' для остальных.
       Для каждого распознанного ника ищет наиболее похожий ник в столбце 'НИК' (нечёткое сравнение)."""
    nick_col = find_column_by_header(ws, "НИК")
    if nick_col is None:
        raise Exception("В листе не найден столбец 'НИК'")
    
    if activity_name not in ACTIVITY_COLUMNS:
        raise Exception(f"Активность '{activity_name}' не поддерживается шаблоном")
    
    start_col, end_col = ACTIVITY_COLUMNS[activity_name]
    all_values = ws.get_all_values()
    table_nicks = []
    for row_idx, row in enumerate(all_values[1:], start=2):
        if len(row) >= nick_col:
            nick_val = row[nick_col-1].strip()
            if nick_val:
                table_nicks.append((row_idx, nick_val))
    
    updated = 0
    for idx, recognized_nick in enumerate(ordered_nicks):
        is_first = (idx == 0)
        best_match_row = None
        best_ratio = 0
        for row_idx, table_nick in table_nicks:
            ratio = fuzz.ratio(recognized_nick.lower(), table_nick.lower())
            if ratio > best_ratio:
                best_ratio = ratio
                best_match_row = row_idx
                best_match_nick = table_nick
        if best_ratio >= threshold:
            row_values = ws.row_values(best_match_row)
            for col in range(start_col, end_col+1):
                if len(row_values) >= col and row_values[col-1] and row_values[col-1].strip():
                    continue
                mark = "БЫЛ ПЛ" if is_first else "БЫЛ"
                ws.update_cell(best_match_row, col, mark)
                updated += 1
                table_nicks = [(r, n) for r, n in table_nicks if r != best_match_row]
                break
        else:
            logging.warning(f"Не найдено соответствие для распознанного ника '{recognized_nick}' (лучшее совпадение {best_ratio}%)")
    return updated

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
    # Формируем кнопки в 2 столбца
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
        # Удаляем заявку из pending_users
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM pending_users WHERE nick = ?", (nick_to_reject,))
        conn.commit()
        conn.close()
        await update.message.reply_text(f"❌ Заявка для {nick_to_reject} отклонена.")
        # Показываем обновлённый список для отказа
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

    # Автоматическая синхронизация шаблона
    try:
        sync_pa_internal()
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
        "Пример: `Qudas \\ СИН \\ Да \\ Нет \\ Не знаю`\n\n"
        "Для отмены нажмите «❌ Отмена».",
        parse_mode="Markdown",
        reply_markup=keyboard
    )
    
async def handle_admin_poll_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id) or context.user_data.get('admin_poll_step') != 'awaiting_input':
        return
    text = update.message.text.strip()
    
    # ОБРАБОТКА ОТМЕНЫ
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
    user_class = parts[1]
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
    # НЕ удаляем состояние, чтобы можно было ввести следующую строку
    await update.message.reply_text(
        f"✅ Ответы для {nick} (класс: {user_class}) сохранены!\n"
        f"Встречи и ответы:\n" + "\n".join([f"• {m}: {a}" for m, a in zip(meetings, answers)]),
        reply_markup=ReplyKeyboardMarkup([[KeyboardButton("❌ Отмена")]], resize_keyboard=True)
    )

async def admin_poll_nick_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id) or 'admin_poll' not in context.user_data:
        return
    data = context.user_data['admin_poll']
    if data.get('step') != 'awaiting_nick':
        return
    nick = update.message.text.strip()
    if nick == "❌ Отмена":
        del context.user_data['admin_poll']
        await update.message.reply_text("Операция отменена.", reply_markup=get_main_keyboard(user_id))
        return
    if not nick:
        await update.message.reply_text("Ник не может быть пустым. Попробуйте ещё раз.")
        return
    data['external_nick'] = nick
    poll = get_active_poll()
    if not poll:
        del context.user_data['admin_poll']
        await update.message.reply_text("Активный опрос исчез. Попробуйте позже.")
        return
    data['poll_id'] = poll['id']
    data['poll_text'] = poll['text']
    data['meetings'] = poll['meetings']
    data['current_index'] = 0
    data['answers'] = {}
    data['step'] = 'polling'
    await send_admin_poll_question(update, context, user_id)

async def send_admin_poll_question(update, context, user_id):
    data = context.user_data.get('admin_poll')
        # Ручной опрос админом (ввод данных)
    if context.user_data.get('admin_poll'):
        await handle_admin_poll_text(update, context)
        return
    idx = data['current_index']
    meetings = data['meetings']
    poll_text = data['poll_text']
    if idx >= len(meetings):
        answers = data['answers']
        summary = "✅ *Ваши ответы:*\n\n"
        for m in meetings:
            ans = answers.get(m, "❌ Не отвечен")
            summary += f"• {m} → {ans}\n"
        summary += "\nСохранить ответы?"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Да, сохранить", callback_data="admin_poll_save")],
            [InlineKeyboardButton("❌ Нет, отменить", callback_data="admin_poll_cancel")]
        ])
        await context.bot.send_message(chat_id=user_id, text=summary, parse_mode="Markdown", reply_markup=keyboard)
        return
    meeting = meetings[idx]
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Да", callback_data=f"admin_poll_ans_да"),
            InlineKeyboardButton("❌ Нет", callback_data=f"admin_poll_ans_нет"),
            InlineKeyboardButton("❓ Не знаю", callback_data=f"admin_poll_ans_не знаю")
        ]
    ])
    text = f"📢 *Опрос за другого*\n\n{poll_text}\n\nВопрос {idx+1} из {len(meetings)}:\n{meeting}\n\nВаш ответ:"
    await context.bot.send_message(chat_id=user_id, text=text, parse_mode="Markdown", reply_markup=keyboard)

async def admin_poll_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id
    if not is_admin(user_id) or 'admin_poll' not in context.user_data:
        await query.edit_message_text("Сессия опроса не найдена. Начните заново.")
        return
    poll_data = context.user_data['admin_poll']
    if data.startswith("admin_poll_ans_"):
        answer = data.split('_')[3]
        meeting = poll_data['meetings'][poll_data['current_index']]
        poll_data['answers'][meeting] = answer
        poll_data['current_index'] += 1
        await query.edit_message_text(f"✅ Ответ на '{meeting}' сохранён. Следующий вопрос...")
        await send_admin_poll_question(update, context, user_id)
    elif data == "admin_poll_save":
        poll_id = poll_data['poll_id']
        external_nick = poll_data['external_nick']
        admin_id = user_id
        answers = poll_data['answers']
        for meeting, ans in answers.items():
            save_external_response(poll_id, external_nick, meeting, ans, admin_id)
        del context.user_data['admin_poll']
        await query.edit_message_text(f"✅ Ответы для пользователя {external_nick} сохранены! Они будут учтены в результатах опроса.")
        await context.bot.send_message(chat_id=user_id, text="Управление опросами:", reply_markup=get_polls_management_keyboard())
    elif data == "admin_poll_cancel":
        del context.user_data['admin_poll']
        await query.edit_message_text("Опрос отменён. Ответы не сохранены.")
        await context.bot.send_message(chat_id=user_id, text="Управление опросами:", reply_markup=get_polls_management_keyboard())

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
        await update.message.reply_text("Вы не зарегистрированы. Нажмите /start.")
        return

    nick = get_user_nick(user_id)
    if not nick:
        await update.message.reply_text("Не удалось определить ваш ник.")
        return

    try:
        ws = get_current_activity_sheet()
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка доступа к Google Sheets: {e}")
        return

    nick_col = find_column_by_header(ws, "НИК")
    if nick_col is None:
        await update.message.reply_text("❌ В листе не найден столбец 'НИК'.")
        return

    all_values = ws.get_all_values()
    total_salary = 0

    for row_idx, row in enumerate(all_values[1:], start=2):
        current_nick = row[nick_col-1].strip() if len(row) >= nick_col else ""
        if current_nick.lower() != nick.lower():
            continue

        # Считаем ЗП по активностям (только в диапазонах ACTIVITY_COLUMNS)
        for activity_name, (start_col, end_col) in ACTIVITY_COLUMNS.items():
            for col in range(start_col, end_col+1):
                if len(row) >= col:
                    cell_value = row[col-1].strip()
                    if cell_value == "БЫЛ":
                        total_salary += 10_000_000
                    elif cell_value == "БЫЛ ПЛ":
                        total_salary += 20_000_000
        break  # нашли пользователя, дальше не ищем

    # Переводим в чеки (1 чек = 10_000_000)
    checks = total_salary // 10_000_000
    await update.message.reply_text(
        f"💰 *Ваша зарплата за текущий месяц*\n\n"
        f"Сумма: {total_salary:,} (в игровой валюте)\n"
        f"Это составляет: *{checks} чеков*\n\n"
        f"1 чек = 10 000 000",
        parse_mode="Markdown"
    )

def get_admin_keyboard():
    keyboard = [
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

    # ---------- ОТМЕНА (только для остальных состояний) ----------
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
            await update.message.reply_text("Создание опроса отменено.", reply_markup=get_polls_management_keyboard())
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
        if context.user_data.get('reject_mode'):
            context.user_data.pop('reject_mode', None)
            await update.message.reply_text("Режим отмены заявок завершён.", reply_markup=get_main_keyboard(user_id))
            return
        # Если ничего не подошло – просто игнорируем, не делаем return!
        # Это позволит дойти до проверки admin_poll_step

    # ---------- РУЧНОЙ ОПРОС АДМИНОМ (включая отмену) ----------
    if context.user_data.get('admin_poll_step') == 'awaiting_input':
        await handle_admin_poll_text(update, context)
        return

    # ---------- РЕДАКТИРОВАНИЕ КЛАССА ----------
    if context.user_data.get('edit_class_mode'):
        await handle_edit_class(update, context)
        return

    # ---------- ВЫБОР АКТИВНОСТИ ----------
    if context.user_data.get('activity_mode') and context.user_data.get('activity_step') == 'select_activity':
        await handle_activity_choice(update, context)
        return

    # ---------- РЕГИСТРАЦИЯ: НИК ----------
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

    # ---------- РЕГИСТРАЦИЯ: КЛАСС ----------
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

    # ---------- ПРОВЕРКА ВАЛИДНОСТИ ----------
    if not is_user_valid(user_id):
        await update.message.reply_text(
            "❌ Вы не зарегистрированы. Нажмите /start для регистрации."
        )
        return

    # ---------- ОСНОВНОЕ МЕНЮ ----------
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
        activity_count = get_user_activity_count(nick)
        await update.message.reply_text(
            f"📊 *Ваша активность*\n\n"
            f"┌ Всего отметок «БЫЛ»: *{activity_count}*\n"
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
    elif text == "📊 Админ-панель" and is_admin(user_id):
        await update.message.reply_text("Админ-панель:", reply_markup=get_admin_keyboard())
    elif text == "🔙 Назад" and is_admin(user_id):
        await update.message.reply_text("Главное меню:", reply_markup=get_main_keyboard(user_id))
    elif text == "📊 Управление опросами" and is_admin(user_id):
        await update.message.reply_text("Управление опросами:", reply_markup=get_polls_management_keyboard())
    elif text == "📝 Создать опрос (ГВГ)" and is_admin(user_id):
        context.user_data['poll_creation'] = {'step': 'text'}
        keyboard = ReplyKeyboardMarkup([[KeyboardButton("❌ Отмена")]], resize_keyboard=True)
        await update.message.reply_text(
            "Введите текст объявления для ГВГ-опроса.\n\nДля отмены нажмите «❌ Отмена».",
            reply_markup=keyboard
        )
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
    elif text == "🏰 Управление кланом" and is_admin(user_id):
        await update.message.reply_text("Управление кланом:", reply_markup=get_clan_management_keyboard())
    elif text == "💸 Выдача кеша" and is_admin(user_id):
        await process_cash_orders(update, context)
    elif text == "👥 Список пользователей" and is_admin(user_id):
        await users_command(update, context)
    elif text == "✏️ Исправить профиль" and is_admin(user_id):
        await edit_profile_menu(update, context)
    elif text == "✏️ Исправить класс" and is_admin(user_id):
        await edit_class_command(update, context)
    elif text == "🔄 Исправить ник" and is_admin(user_id):
        await edit_nick_command(update, context)
    elif text == "📊 Активность игроков" and is_admin(user_id):
        await activity_menu(update, context)
    elif text == "📝 Регистрация" and is_admin(user_id):
        await registration_menu(update, context)
    elif text == "📋 Список ожидания" and is_admin(user_id):
        await list_pending_command(update, context)
    elif text == "✅ Подтвердить всех" and is_admin(user_id):
        await confirm_all_pending_command(update, context)
    elif text == "❌ Отказать" and is_admin(user_id):
        await reject_pending_menu(update, context)
    elif text == "💰 Расчет ЗП" and is_admin(user_id):   # <-- ДОБАВЛЕНА ОБРАБОТКА КНОПКИ
        await calculate_salaries(update, context)
        return
    else:
        await update.message.reply_text("Неизвестная команда. Используйте кнопки меню.")
        
# ---------- СОЗДАНИЕ ОПРОСА (ГВГ) ----------
async def handle_poll_creation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id) or 'poll_creation' not in context.user_data:
        return
    data = context.user_data['poll_creation']
    text = update.message.text
    if text == "❌ Отмена":
        del context.user_data['poll_creation']
        await update.message.reply_text("Создание опроса отменено.", reply_markup=get_polls_management_keyboard())
        return
    if data['step'] == 'text':
        data['text'] = text
        data['meetings'] = []
        data['step'] = 'meeting'
        await update.message.reply_text(
            "Теперь вводите названия встреч (ГВГ) по одной строке.\nКогда закончите, нажмите кнопку «✅ Завершить создание» под сообщением.\n\nДля отмены отправьте «❌ Отмена»."
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
    meetings_list = ", ".join([f'"{m}"' for m in data['meetings']])
    await query.edit_message_text(f"✅ Опрос (ГВГ) создан!\n\nГВГ: {meetings_list}")
    del context.user_data['poll_creation']
    await query.message.reply_text("Управление опросами:", reply_markup=get_polls_management_keyboard())

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

    message_text = "📋 *Список пользователей:*\n\n"
    for uid, nick, user_class, _ in users:
        message_text += f"• {nick} - {user_class}\n"
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
        text += f"• {nick} - {user_class} - `{uid}`\n"
        if len(text) > 3800:
            await query.edit_message_text(text, parse_mode="Markdown")
            return
    await query.edit_message_text(text, parse_mode="Markdown")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('activity_mode') and not context.user_data.get('activity_step'):
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
    app.add_handler(CommandHandler("end_poll", lambda u,c: deactivate_poll() or u.message.reply_text("Опрос завершён.")))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("edit_class", edit_class_command))
    app.add_handler(CallbackQueryHandler(show_all_ids_callback, pattern="^show_all_ids$"))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CommandHandler("mysalary", my_salary))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(poll_callback, pattern="^poll_"))
    app.add_handler(CallbackQueryHandler(confirm_callback, pattern="^confirm_"))
    app.add_handler(CallbackQueryHandler(restart_callback, pattern="^restart_"))
    app.add_handler(CallbackQueryHandler(finish_poll_creation_callback, pattern="^finish_poll_creation"))
    app.add_handler(CallbackQueryHandler(cash_callback, pattern="^(cash_done_|cash_reject_)"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_poll_creation), group=1)
    app.add_handler(CommandHandler("leave", leave_chat))
    app.add_handler(CommandHandler("remove_all_keyboards", remove_all_keyboards))
    app.add_handler(CommandHandler("calc_salary", calculate_salaries))
    app.add_handler(CommandHandler("sync_pa", sync_pa_command))
    print("Бот запущен. Нажмите Ctrl+C для остановки.")
    app.run_polling()

if __name__ == "__main__":
    main()
