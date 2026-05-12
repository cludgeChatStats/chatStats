import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from flask import Flask, request, jsonify

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

# --- Улучшенная функция для получения даты в МСК из разных форматов UTC ---
def moscow_date_from_utc(utc_iso_str: str):
    """Принимает строки вида:
       2026-05-12T19:18:01.451041+00:00
       2026-05-12T19:18:01.451Z
       2026-05-12T19:18:01+00:00
       Возвращает дату в МСК (объект date).
    """
    # Заменяем Z на +00:00
    utc_iso_str = utc_iso_str.replace('Z', '+00:00')
    # Если нет временной зоны, добавляем +00:00
    if '+' not in utc_iso_str and utc_iso_str[-1] != 'Z':
        utc_iso_str += '+00:00'
    dt_utc = datetime.fromisoformat(utc_iso_str).replace(tzinfo=timezone.utc)
    return dt_utc.astimezone(MOSCOW_TZ).date()

# --- Очистка старых записей ---
def delete_old_records():
    today = datetime.now(MOSCOW_TZ).date()
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        rows = conn.execute("SELECT id, closed_at_utc FROM closed_chats").fetchall()
        to_delete = []
        for row_id, closed_utc in rows:
            try:
                if moscow_date_from_utc(closed_utc) != today:
                    to_delete.append(row_id)
            except Exception as e:
                print(f"❌ Ошибка парсинга даты {closed_utc}: {e}")
                to_delete.append(row_id)  # на всякий случай удаляем проблемные
        if to_delete:
            placeholders = ','.join('?' for _ in to_delete)
            conn.execute(f"DELETE FROM closed_chats WHERE id IN ({placeholders})", to_delete)
            print(f"🧹 Очистка: удалено {len(to_delete)} записей (не сегодня)")
        else:
            print("🧹 Очистка: нет записей для удаления")

def cleaner_worker():
    last_cleanup = None
    while True:
        now = datetime.now(MOSCOW_TZ)
        if now.hour == 0 and now.minute < 5:
            if last_cleanup != now.date():
                print(f"🕛 Запуск очистки в {now.strftime('%H:%M:%S')}")
                delete_old_records()
                last_cleanup = now.date()
        time.sleep(60)

threading.Thread(target=cleaner_worker, daemon=True).start()

# --- ВЕБХУК (с подробным логированием) ---
@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        # Получаем JSON
        data = request.get_json(silent=True)
        if not data:
            print("⚠️ Вебхук: нет JSON тела")
            return ("", 200)

        event = data.get('event')
        print(f"📥 Вебхук: event={event}")

        if event != 'chat.closed':
            print(f"ℹ️ Игнорируем событие: {event}")
            return ("", 200)

        operator = data.get('operator') or {}
        operator_name = operator.get('name')
        conversation = data.get('conversation') or {}
        conv_id = conversation.get('id')
        closed_at = conversation.get('closed_at')

        print(f"🔍 operator_name={operator_name}, conv_id={conv_id}, closed_at={closed_at}")

        if not operator_name:
            print("❌ Нет operator_name")
            return ("", 200)
        if not conv_id:
            print("❌ Нет conversation.id")
            return ("", 200)
        if not closed_at:
            print("❌ Нет closed_at")
            return ("", 200)

        # Проверка даты – если не парсится, всё равно вставим, но залогируем
        try:
            test_date = moscow_date_from_utc(closed_at)
            print(f"📅 Дата в МСК: {test_date}")
        except Exception as e:
            print(f"⚠️ Не удалось распарсить closed_at '{closed_at}': {e}")

        # Вставка (игнорируем дубликаты)
        with sqlite3.connect(DB_PATH, timeout=10) as conn:
            conn.execute('''
                INSERT OR IGNORE INTO closed_chats (operator_name, conversation_id, closed_at_utc)
                VALUES (?, ?, ?)
            ''', (operator_name, conv_id, closed_at))
            changes = conn.total_changes
            if changes:
                print(f"✅ Вставлена новая запись: {operator_name} / {conv_id}")
            else:
                print(f"⚠️ Дубликат (или ошибка): {conv_id} уже существует")
        return ("", 200)

    except Exception as e:
        print(f"❌ Критическая ошибка в вебхуке: {e}")
        return ("", 200)

# --- ДИАГНОСТИКА: все записи ---
@app.route('/debug')
def debug():
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("SELECT id, operator_name, conversation_id, closed_at_utc FROM closed_chats").fetchall()
    if not rows:
        return "Таблица пуста"
    result = "<h3>Все записи в БД</h3><table border='1'>"
    result += "<tr><th>ID</th><th>Оператор</th><th>Conversation ID</th><th>closed_at (UTC)</th></tr>"
    for row in rows:
        result += f"<tr><td>{row[0]}</td><td>{row[1]}</td><td>{row[2]}</td><td>{row[3]}</td></tr>"
    result += "</table>"
    return result

# --- СТАТИСТИКА JSON (без фильтра) ---
@app.route('/raw_stats')
def raw_stats():
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("SELECT operator_name, closed_at_utc FROM closed_chats").fetchall()
    # Группируем по оператору (все записи)
    counter = {}
    for op_name, closed_utc in rows:
        counter[op_name] = counter.get(op_name, 0) + 1
    sorted_ops = sorted(counter.items(), key=lambda x: x[1], reverse=True)
    return jsonify({
        "total_records": len(rows),
        "stats": [{"operator": name, "count": cnt} for name, cnt in sorted_ops]
    })

# --- ОСНОВНАЯ СТАТИСТИКА HTML (только за сегодня) ---
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
        html += '<tr><td colspan="3">Нет данных за сегодня (проверьте /debug и /raw_stats)</td></tr>'
    else:
        for i, (name, count) in enumerate(sorted_ops, 1):
            html += f'<tr><td>{i}</td><td>{name}</td><td><b>{count}</b></td></tr>'
    html += '''
    </table>
    <p><em>Данные за текущий день (МСК). Если данных нет, но вебхуки приходили, смотрите <a href="/debug">/debug</a> и <a href="/raw_stats">/raw_stats</a>.</em></p>
    </body>
    </html>
    '''
    return html

@app.route('/health')
def health():
    return "OK"

if __name__ == '__main__':
    print(f"🚀 Сервер запущен на порту {PORT}, режим threaded=True")
    app.run(host='0.0.0.0', port=PORT, threaded=True)