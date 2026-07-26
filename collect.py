#!/usr/bin/env python3
"""Шаг 1 конвейера: сбор свежих записей из одной RSS-ленты.

Пока без ИИ, без базы и без Telegram — проверяем только, что источник работает.
"""

import calendar
import html
import re
from datetime import datetime, timedelta, timezone

import feedparser

FEED_URL = "https://habr.com/ru/rss/hubs/artificial_intelligence/articles/?fl=ru"
USER_AGENT = "svc-digest/0.1 (personal news digest)"
# 48ч, а не 24: окно ловит с запасом, чтобы запись не проскочила мимо на
# стыке суток (сдвиг запуска, пограничная дата). От повторов страхует дедуп —
# он всё равно отсечёт то, что уже слали.
WINDOW_HOURS = 48


def strip_html(raw):
    """Выкидывает теги и картинки, оставляет читаемый текст."""
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def entry_key(entry):
    """Ключ дедупликации: guid, если лента его даёт; иначе — link."""
    return entry.get("id") or entry.get("link", "")


def published_utc(entry):
    """Дата публикации как datetime в UTC (или None, если лента её не дала)."""
    parsed = entry.get("published_parsed")
    if not parsed:
        return None
    return datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc)


def collect(feed_url=FEED_URL, window_hours=WINDOW_HOURS):
    """Возвращает (список свежих записей, счётчики стыка)."""
    feed = feedparser.parse(feed_url, agent=USER_AGENT)

    stats = {
        "http": getattr(feed, "status", None),
        "bozo": bool(feed.bozo),
        "total": len(feed.entries),
        "fresh": 0,
        # Окно кладём в счётчики, а не подставляем в текст лога константой:
        # вызвать collect() можно с любым window_hours, и лог обязан
        # показывать то окно, по которому реально фильтровали.
        "window_hours": window_hours,
    }

    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    fresh = []

    for entry in feed.entries:
        published = published_utc(entry)
        # Запись БЕЗ даты считаем свежей и пропускаем дальше: потерять новость
        # хуже, чем лишний раз показать её ИИ, а от повтора страхует дедуп.
        # Цена решения: лента без дат протащит весь свой архив на первом
        # запуске (у Habr дата есть всегда — сработает на втором источнике).
        if published and published < cutoff:
            continue
        fresh.append(
            {
                "key": entry_key(entry),
                "title": entry.get("title", "").strip(),
                "link": entry.get("link", ""),
                "published": published,
                "summary": strip_html(entry.get("summary", ""))[:400],
            }
        )

    stats["fresh"] = len(fresh)
    return fresh, stats


def main():
    items, stats = collect()

    print(f"стык 1 — сбор: HTTP {stats['http']} | "
          f"в ленте {stats['total']} | свежих за {stats['window_hours']}ч: {stats['fresh']}")
    if stats["bozo"]:
        print("  ! лента разобрана с замечаниями (bozo=1)")
    print("-" * 70)

    for i, item in enumerate(items, 1):
        when = item["published"].astimezone().strftime("%Y-%m-%d %H:%M") if item["published"] else "—"
        print(f"\n{i}. {item['title']}")
        print(f"   {when}  |  ключ: {item['key']}")
        print(f"   {item['summary'][:200]}...")


if __name__ == "__main__":
    main()
