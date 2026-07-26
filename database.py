#!/usr/bin/env python3
"""База — точка развязки сборщика и отправителя.

Сборщик ПИШЕТ: новые записи ленты после ИИ-отбора ложатся сюда со статусом
  pending (отобрано) или rejected (ИИ забраковал). rejected хранится, чтобы
  сборщик не гонял через ИИ одно и то же каждый час.
Отправитель ЧИТАЕТ: в 9:00 берёт всё pending, шлёт одним дайджестом, метит sent.

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
