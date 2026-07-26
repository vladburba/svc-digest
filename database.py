#!/usr/bin/env python3
"""База — точка развязки сборщика и отправителя.

Сборщик ПИШЕТ: новые записи ленты после ИИ-отбора ложатся сюда со статусом
  pending (отобрано) или rejected (ИИ забраковал). rejected хранится, чтобы
  сборщик не гонял через ИИ одно и то же каждый час.
Отправитель ЧИТАЕТ: в 8:30 берёт всё pending, шлёт одним дайджестом, метит sent.

База своя, отдельная от vladburba-bot.db: у SQLite один писатель (решение 6.04),
и PII заявок/платежей дайджесту рядом не нужны.
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "digest.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS news (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    key           TEXT NOT NULL UNIQUE,   -- guid ленты, иначе link
    title         TEXT NOT NULL,
    link          TEXT NOT NULL,
    published     TEXT,                   -- ISO 8601 с зоной
    ai_summary    TEXT,
    status        TEXT NOT NULL,          -- pending | sent | rejected
    first_seen_at TEXT NOT NULL,          -- когда сборщик обработал запись
    sent_at       TEXT,                   -- когда Telegram подтвердил приём
    message_id    INTEGER                 -- расписка Telegram
);
CREATE INDEX IF NOT EXISTS idx_news_status ON news(status);

-- Журнал заходов сборщика: воронка каждого прогона. Нужен, чтобы отправитель
-- в 08:30 собрал полную статистику за сутки и вложил её в сообщение
-- (сам отправитель сбора не видит — работы развязаны).
CREATE TABLE IF NOT EXISTS collect_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ran_at        TEXT NOT NULL,       -- ISO 8601 с зоной
    total_in_feed INTEGER,             -- сколько записей в ленте
    in_window     INTEGER,             -- из них попали в окно 48ч
    already_seen  INTEGER,             -- дедуп отсеял (старые, уже в базе)
    new_items     INTEGER,             -- новых пошло в ИИ
    selected      INTEGER,             -- ИИ отобрал
    rejected      INTEGER,             -- ИИ забраковал
    error         TEXT                 -- текст ошибки, если заход упал
);
"""


def now_iso():
    """Локальное время с явной зоной — без ловушки «UTC без зоны»."""
    return datetime.now(timezone.utc).astimezone().isoformat()


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Идемпотентно: зовётся на старте каждой работы."""
    with connect() as conn:
        conn.executescript(SCHEMA)


def filter_unseen(items):
    """Оставляет то, чего в базе ещё НЕТ (любой статус). → (новые, отсеяно).

    Дедуп по всей базе: pending + sent + rejected. Если ключ уже есть в любом
    статусе — запись мы уже обрабатывали, второй раз через ИИ не гоним.
    """
    if not items:
        return [], 0
    with connect() as conn:
        rows = conn.execute("SELECT key FROM news").fetchall()
    seen = {row["key"] for row in rows}
    unseen = [item for item in items if item["key"] not in seen]
    return unseen, len(items) - len(unseen)


def record(items, status):
    """Пишет записи с заданным статусом. INSERT OR IGNORE — повтор безвреден."""
    if not items:
        return 0
    stamp = now_iso()
    rows = [
        (
            item["key"],
            item["title"],
            item["link"],
            item["published"].isoformat() if item.get("published") else None,
            item.get("ai_summary", ""),
            status,
            stamp,
        )
        for item in items
    ]
    with connect() as conn:
        cursor = conn.executemany(
            "INSERT OR IGNORE INTO news "
            "(key, title, link, published, ai_summary, status, first_seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        return cursor.rowcount


def get_pending():
    """Накопленное к отправке: всё pending, свежее сверху."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT key, title, link, published, ai_summary FROM news "
            "WHERE status = 'pending' "
            "ORDER BY COALESCE(published, first_seen_at) DESC"
        ).fetchall()
    return [dict(row) for row in rows]


def mark_sent(keys, message_id):
    """После доставки: pending → sent + расписка Telegram."""
    if not keys:
        return 0
    stamp = now_iso()
    placeholders = ",".join("?" * len(keys))
    with connect() as conn:
        cursor = conn.execute(
            f"UPDATE news SET status='sent', sent_at=?, message_id=? "
            f"WHERE key IN ({placeholders}) AND status='pending'",
            [stamp, message_id, *keys],
        )
        return cursor.rowcount


def record_run(funnel):
    """Пишет воронку одного захода сборщика в журнал collect_runs."""
    with connect() as conn:
        conn.execute(
            "INSERT INTO collect_runs "
            "(ran_at, total_in_feed, in_window, already_seen, new_items, selected, rejected, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (now_iso(), funnel.get("total_in_feed"), funnel.get("in_window"),
             funnel.get("already_seen"), funnel.get("new_items"),
             funnel.get("selected"), funnel.get("rejected"), funnel.get("error")),
        )


def funnel_since(since_iso):
    """Суммарная воронка заходов сборщика ПОСЛЕ момента since_iso.

    Аргрегат за период между отправками: новых/отобрано/отсеяно — суммы;
    лента/окно — снимок ПОСЛЕДНЕГО захода (складывать 40×24 бессмысленно).
    """
    with connect() as conn:
        agg = conn.execute(
            "SELECT COUNT(*) runs, "
            "COALESCE(SUM(new_items),0) new_items, "
            "COALESCE(SUM(selected),0) selected, "
            "COALESCE(SUM(rejected),0) rejected "
            "FROM collect_runs WHERE ran_at > ? AND error IS NULL",
            (since_iso,),
        ).fetchone()
        last = conn.execute(
            "SELECT total_in_feed, in_window, already_seen FROM collect_runs "
            "WHERE ran_at > ? AND error IS NULL ORDER BY ran_at DESC LIMIT 1",
            (since_iso,),
        ).fetchone()
    out = dict(agg)
    out["last_feed"] = last["total_in_feed"] if last else None
    out["last_window"] = last["in_window"] if last else None
    out["last_seen"] = last["already_seen"] if last else None
    return out


def last_sent_at():
    """Момент прошлой отправки (для окна агрегации воронки). None если не было."""
    with connect() as conn:
        row = conn.execute("SELECT MAX(sent_at) AS ts FROM news WHERE status='sent'").fetchone()
    return row["ts"]


def stats():
    """Сводка по статусам — для диагностики и логов."""
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(status='pending')  AS pending, "
            "SUM(status='sent')     AS sent, "
            "SUM(status='rejected') AS rejected, "
            "MAX(sent_at) AS last_sent FROM news"
        ).fetchone()
    return dict(row)


if __name__ == "__main__":
    init_db()
    s = stats()
    print(f"база: {DB_PATH}")
    print(f"всего {s['total']} | pending {s['pending'] or 0} | "
          f"sent {s['sent'] or 0} | rejected {s['rejected'] or 0} | "
          f"последняя отправка: {s['last_sent'] or '—'}")
