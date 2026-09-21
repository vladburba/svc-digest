#!/usr/bin/env python3
"""Собирает свежую выборку для замера качества отбора.

    python eval/collect_sample.py 2026-09-28 --since 2026-09-21

Берёт поровну того, что бот отправил, и того, что забраковал, за указанный
период, перемешивает детерминированно и кладёт рядом файл <дата>-sample.json.

Поровну — намеренно: нужно видеть обе ошибки сразу. Перекос в сторону
отправленного покажет только «лишнее прислал» и скроет «нужное выбросил»,
а на практике вторая ошибка оказывалась куда частее.
"""

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB = HERE.parent / "digest.db"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("date", help="метка набора, например 2026-09-28")
    ap.add_argument("--since", required=True, help="с какой даты брать новости")
    ap.add_argument("--per-side", type=int, default=30, help="по столько с каждой стороны")
    ap.add_argument("--db", default=str(DB))
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    pool = []
    for verdict, status in (("sent", "sent"), ("rejected", "rejected")):
        rows = conn.execute(
            "SELECT key, dedup_key, title, link, published, summary FROM news "
            "WHERE status = ? AND first_seen_at >= ? ORDER BY dedup_key",
            (status, args.since),
        ).fetchall()
        pool += [dict(r, bot_verdict=verdict) for r in rows[:args.per_side]]

    # Порядок перемешиваем по хешу ключа: воспроизводимо и без подсказок о том,
    # что бот решил, — иначе человек разметит «по блокам» и замер поедет.
    pool.sort(key=lambda r: hashlib.md5(r["dedup_key"].encode()).hexdigest())

    out = [{"n": i, "key": r["key"], "title": r["title"], "link": r["link"],
            "date": (r["published"] or "")[:10],
            "text": (r["summary"] or "").strip()[:300],
            "bot_verdict": r["bot_verdict"]}
           for i, r in enumerate(pool, 1)]

    path = HERE / f"{args.date}-sample.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    sent = sum(1 for r in out if r["bot_verdict"] == "sent")
    print(f"{path.name}: {len(out)} новостей (отправлено {sent}, забраковано {len(out)-sent})")


if __name__ == "__main__":
    main()
