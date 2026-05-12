import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from flask import Flask, request

app = Flask(__name__)

# --- Конфигурация ---
DB_PATH = os.environ.get('DB_PATH', 'stats.db')
PORT = int(os.environ.get('PORT', 5000))
MOSCOW_TZ = ZoneInfo("Europe/Moscow")

# --- Инициализация БД ---
def init_db():
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS closed_chats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                operator_name TEXT NOT NULL,
                conversation_id TEXT NOT NULL UNIQUE,
                closed_at_utc TEXT NOT NULL
            )
        ''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_closed_at ON closed_chats(closed_at_utc)')
    print("✅ База данных готова")

init_db()

# --- Вспомогательные функции для времени ---
def moscow_date_from_utc(utc_iso_str: str):
    """Возвращает дату в МСК (объект date) из UTC строки."""
    # Убираем +00:00 если есть
    if utc_iso_str.endswith('+00:00'):
        utc_iso_str = utc_iso_str.replace('+00:00', '')
    dt_utc = datetime.fromisoformat(utc_iso_str).replace(tzinfo=timezone.utc)
    return dt_utc.astimezone(MOSCOW_TZ).date()

def delete_old_records():
    """Удаляет записи, не относящиеся к текущему дню по МСК."""
    today = datetime.now(MOSCOW_TZ).date()
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        rows = conn.execute("SELECT id, closed_at_utc FROM closed_chats").fetchall()
        to_delete = [row_id for row_id, closed_utc in rows if moscow_date_from_utc(closed_utc) != today]
        if to_delete:
            placeholders = ','.join('?' for _ in to_delete)
            conn.execute(f"DELETE FROM closed_chats WHERE id IN ({placeholders})", to_delete)
            print(f"🧹 Очистка: удалено {len(to_delete)} записей (старше сегодня)")
        else:
            print("🧹 Очистка: нет записей для удаления")

# --- Фоновый поток для ежедневной очистки в полночь ---
def cleaner_worker():
    last_cleanup = None
    while True:
        now = datetime.now(MOSCOW_TZ)
        if now.hour == 0 and now.minute < 5:  # диапазон 00:00 - 00:05
            if last_cleanup != now.date():
                print(f"🕛 Запуск очистки в {now.strftime('%H:%M:%S')}")
                delete_old_records()
                last_cleanup = now.date()
        time.sleep(60)  # проверяем каждую минуту

threading.Thread(target=cleaner_worker, daemon=True).start()

# --- Вебхук ---
@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = request.get_json(silent=True)
        if not data or data.get('event') != 'chat.closed':
            return ("", 200)

        operator = data.get('operator') or {}
        operator_name = operator.get('name')
        conversation = data.get('conversation') or {}
        conv_id = conversation.get('id')
        closed_at = conversation.get('closed_at')

        if not (operator_name and conv_id and closed_at):
            return ("", 200)

        with sqlite3.connect(DB_PATH, timeout=10) as conn:
            conn.execute('''
                INSERT OR IGNORE INTO closed_chats (operator_name, conversation_id, closed_at_utc)
                VALUES (?, ?, ?)
            ''', (operator_name, conv_id, closed_at))
        print(f"✅ Сохранён: {operator_name} / {conv_id}")
        return ("", 200)
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        return ("", 200)

# --- Статистика HTML ---
@app.route('/stats')
def stats():
    today = datetime.now(MOSCOW_TZ).date()
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("SELECT closed_at_utc, operator_name FROM closed_chats").fetchall()
    counter = {}
    for closed_utc, op_name in rows:
        if moscow_date_from_utc(closed_utc) == today:
            counter[op_name] = counter.get(op_name, 0) + 1
    sorted_ops = sorted(counter.items(), key=lambda x: x[1], reverse=True)

    html = f'''
    <!DOCTYPE html>
    <html>
    <head><meta charset="utf-8"><title>Статистика за сегодня</title>
    <style>
        body {{ font-family: sans-serif; margin: 40px; }}
        table {{ border-collapse: collapse; width: 50%; }}
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
        html += '<tr><td colspan="3">Нет данных</td></tr>'
    else:
        for i, (name, count) in enumerate(sorted_ops, 1):
            html += f'<tr><td>{i}</td><td>{name}</td><td><b>{count}</b></td></tr>'
    html += '</table><p><em>Данные за текущий день (МСК)</em></p></body></html>'
    return html

@app.route('/health')
def health():
    return "OK"

if __name__ == '__main__':
    print(f"🚀 Сервер запущен на порту {PORT}, режим threaded=True")
    app.run(host='0.0.0.0', port=PORT, threaded=True)