import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from flask import Flask, request

app = Flask(__name__)

DB_PATH = os.environ.get('DB_PATH', 'stats.db')
PORT = int(os.environ.get('PORT', 5000))
MOSCOW_TZ = ZoneInfo("Europe/Moscow")

def migrate_db():
    """Убираем UNIQUE constraint с conversation_id без потери данных"""
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        # Проверяем, существует ли таблица closed_chats
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='closed_chats'")
        if cursor.fetchone():
            # Проверяем, есть ли ограничение UNIQUE (попробуем создать новую таблицу)
            conn.execute("PRAGMA foreign_keys = OFF")
            # Создаём новую таблицу без UNIQUE
            conn.execute('''
                CREATE TABLE IF NOT EXISTS closed_chats_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    operator_name TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    closed_at_utc TEXT NOT NULL
                )
            ''')
            # Копируем все старые данные
            conn.execute('''
                INSERT INTO closed_chats_new (id, operator_name, conversation_id, closed_at_utc)
                SELECT id, operator_name, conversation_id, closed_at_utc FROM closed_chats
            ''')
            # Удаляем старую таблицу
            conn.execute("DROP TABLE closed_chats")
            # Переименовываем новую
            conn.execute("ALTER TABLE closed_chats_new RENAME TO closed_chats")
            # Создаём обычный индекс для скорости
            conn.execute("CREATE INDEX IF NOT EXISTS idx_conversation_id ON closed_chats(conversation_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_closed_at ON closed_chats(closed_at_utc)")
            conn.execute("PRAGMA foreign_keys = ON")
            print("✅ Миграция выполнена: ограничение UNIQUE удалено")
        else:
            # Таблицы нет, создаём без UNIQUE
            conn.execute('''
                CREATE TABLE IF NOT EXISTS closed_chats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    operator_name TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    closed_at_utc TEXT NOT NULL
                )
            ''')
            conn.execute("CREATE INDEX IF NOT EXISTS idx_conversation_id ON closed_chats(conversation_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_closed_at ON closed_chats(closed_at_utc)")
            print("✅ Таблица создана без UNIQUE")

def init_db():
    # Включаем WAL для производительности
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        conn.execute('PRAGMA journal_mode = WAL')
    migrate_db()

init_db()

def moscow_date_from_utc(utc_iso_str: str):
    utc_iso_str = utc_iso_str.replace('Z', '+00:00')
    if '+' not in utc_iso_str:
        utc_iso_str += '+00:00'
    dt_utc = datetime.fromisoformat(utc_iso_str).replace(tzinfo=timezone.utc)
    return dt_utc.astimezone(MOSCOW_TZ).date()

def delete_old_records():
    today = datetime.now(MOSCOW_TZ).date()
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        rows = conn.execute("SELECT id, closed_at_utc FROM closed_chats").fetchall()
        to_delete = []
        for row_id, closed_utc in rows:
            try:
                if moscow_date_from_utc(closed_utc) != today:
                    to_delete.append(row_id)
            except Exception:
                to_delete.append(row_id)
        if to_delete:
            placeholders = ','.join('?' for _ in to_delete)
            conn.execute(f"DELETE FROM closed_chats WHERE id IN ({placeholders})", to_delete)
            print(f"🧹 Удалено {len(to_delete)} старых записей")

def cleaner_worker():
    last_cleanup = None
    while True:
        now = datetime.now(MOSCOW_TZ)
        if now.hour == 0 and now.minute < 5 and last_cleanup != now.date():
            print(f"🕛 Очистка в {now}")
            delete_old_records()
            last_cleanup = now.date()
        time.sleep(60)

threading.Thread(target=cleaner_worker, daemon=True).start()

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        payload = request.get_json(silent=True)
        if not payload:
            return ("", 200)

        event = payload.get('event')
        if event != 'chat.closed':
            return ("", 200)

        data = payload.get('data')
        if not data:
            return ("", 200)

        operator = data.get('operator') or {}
        operator_name = operator.get('name')
        conversation = data.get('conversation') or {}
        conv_id = conversation.get('id')
        closed_at = conversation.get('closed_at')

        if not operator_name or not conv_id or not closed_at:
            print(f"⚠️ Пропущен: name={operator_name}, id={conv_id}, closed_at={closed_at}")
            return ("", 200)

        # Обычная вставка без игнорирования дублей
        with sqlite3.connect(DB_PATH, timeout=10) as conn:
            conn.execute('''
                INSERT INTO closed_chats (operator_name, conversation_id, closed_at_utc)
                VALUES (?, ?, ?)
            ''', (operator_name, conv_id, closed_at))
            print(f"✅ Сохранён {operator_name} - {conv_id} (дубли разрешены)")
        return ("", 200)
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        return ("", 200)

@app.route('/stats')
def stats():
    today = datetime.now(MOSCOW_TZ).date()
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("SELECT closed_at_utc, operator_name FROM closed_chats").fetchall()
    counter = {}
    for closed_utc, op_name in rows:
        try:
            if moscow_date_from_utc(closed_utc) == today:
                counter[op_name] = counter.get(op_name, 0) + 1
        except:
            pass
    sorted_ops = sorted(counter.items(), key=lambda x: x[1], reverse=True)

    html = f'''
    <!DOCTYPE html>
    <html>
    <head><meta charset="utf-8"><title>Статистика за сегодня</title>
    <style>
        body {{ font-family: sans-serif; margin: 40px; }}
        table {{ border-collapse: collapse; width: 50%%; }}
        th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
        th {{ background-color: #f2f2f2; }}
    </style>
    </head>
    <body>
    <h2>📊 Закрытые чаты за {today.isoformat()}</h2>
    <table>
    <tr><th>#</th><th>Оператор</th><th>Кол-во</th></tr>
    '''
    if not sorted_ops:
        html += '<tr><td colspan="3">Нет данных за сегодня</td></tr>'
    else:
        for i, (name, count) in enumerate(sorted_ops, 1):
            html += f'<tr><td>{i}</td><td>{name}</td><td><b>{count}</b></td></tr>'
    html += '''
    </table>
    </body>
    </html>
    '''
    return html

@app.route('/debug')
def debug():
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("SELECT id, operator_name, conversation_id, closed_at_utc FROM closed_chats").fetchall()
    if not rows:
        return "Таблица пуста"
    result = "<h3>Все записи (включая дубликаты)</h3><table border='1'>"
    result += "<tr><th>ID</th><th>Оператор</th><th>Conversation ID</th><th>closed_at (UTC)</th></table>"
    for row in rows:
        result += f"<tr><td>{row[0]}</td><td>{row[1]}</td><td>{row[2]}</td><td>{row[3]}</td></tr>"
    result += "</table>"
    return result

@app.route('/health')
def health():
    return "OK"

if __name__ == '__main__':
    print(f"🚀 Сервер запущен на порту {PORT}")
    app.run(host='0.0.0.0', port=PORT, threaded=True)