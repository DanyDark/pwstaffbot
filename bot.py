import sqlite3
import logging
import os
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# ------------------ НАСТРОЙКИ ------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("Не задана переменная окружения BOT_TOKEN")

ADMIN_IDS = os.environ.get("ADMIN_IDS", "")
ADMIN_LIST = [int(x.strip()) for x in ADMIN_IDS.split(",") if x.strip()]

WHITELIST_FILE = "whitelist.txt"

# Используем DATA_DIR из переменных окружения (по умолчанию /data)
DATA_DIR = os.environ.get("DATA_DIR", "/data")
DB_FILE = os.path.join(DATA_DIR, "users.db")
# ----------------------------------------------

logging.basicConfig(level=logging.INFO)

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
