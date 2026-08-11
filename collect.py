#!/usr/bin/env python3
"""Стык 1 конвейера: сбор свежих записей из одной RSS-ленты.

Ленту тянем через httpx (с прокси и таймаутом), а не feedparser.parse(url):
  · нужен единый egress-прокси — feedparser ходить через socks не умеет;
  · feedparser.parse(url) без таймаута ВИСНЕТ намертво, если хост недоступен
    (поймали на vlad-main). httpx с timeout падает быстро и внятно.
Готовые байты отдаём feedparser.parse(content) — разбор форматов остаётся за ним.
"""

import calendar
import html
import re
from datetime import datetime, timedelta, timezone

import feedparser
import httpx

from clock import to_local
from dedup import dedup_key
from netcfg import EGRESS_PROXY

FEED_URL = "https://habr.com/ru/rss/hubs/artificial_intelligence/articles/?fl=ru"
USER_AGENT = "svc-digest/0.1 (personal news digest)"
# 168ч (неделя): окно = запас прочности на случай простоя сборщика. Если
# контейнер лежал несколько дней — догоним всё за неделю (при 48ч статьи
# старше двух суток выпали бы из ленты безвозвратно). От повторов страхует
# дедуп по всей базе, поэтому широкое окно бесплатно: лишних вызовов ИИ нет,
# дублей нет — в устойчивом режиме дайджест такой же, как на 48ч.
WINDOW_HOURS = 168
FETCH_TIMEOUT = 25.0


def strip_html(raw):
    """Выкидывает теги и картинки, оставляет читаемый текст."""
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def entry_key(entry):
    """Естественный ключ записи: guid, если лента его даёт; иначе — link.

    Остаётся первичным ключом в базе (по нему видно, каким именно адресом
    статья к нам пришла), но дедуп идёт НЕ по нему — см. dedup.dedup_key.
    """
    return entry.get("id") or entry.get("link", "")


def published_utc(entry):
    """Дата публикации как datetime в UTC (или None, если лента её не дала)."""
    parsed = entry.get("published_parsed")
    if not parsed:
        return None
    return datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc)


def collect(feed_url=FEED_URL, window_hours=WINDOW_HOURS):
    """Возвращает (список свежих записей, счётчики стыка).

    При сетевой ошибке возвращает ([], stats с error) — не бросает исключение,
    сборщик разберётся сам (залогирует и ретайнется через час).
    """
    stats = {"http": None, "bozo": False, "total": 0, "fresh": 0,
             "window_hours": window_hours, "error": None}

    try:
        resp = httpx.get(
            feed_url,
            headers={"User-Agent": USER_AGENT},
            proxy=EGRESS_PROXY,
            timeout=FETCH_TIMEOUT,
            follow_redirects=True,
        )
        stats["http"] = resp.status_code
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
    except Exception as exc:
        stats["error"] = f"{type(exc).__name__}: {exc}"
        stats["bozo"] = True
        return [], stats

    stats["bozo"] = bool(feed.bozo)
    stats["total"] = len(feed.entries)

    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    fresh = []
    for entry in feed.entries:
        published = published_utc(entry)
        # Запись БЕЗ даты считаем свежей: потерять новость хуже, чем лишний раз
        # показать её ИИ, а от повтора страхует дедуп. Цена: лента без дат
        # протащит весь архив на первом запуске (у Habr дата есть всегда).
        if published and published < cutoff:
            continue
        key = entry_key(entry)
        fresh.append(
            {
                "key": key,
                "dedup_key": dedup_key(key, entry.get("link", "")),
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
    if stats["error"]:
        print(f"  ! ошибка сбора: {stats['error']}")
    print("-" * 70)

    for i, item in enumerate(items, 1):
        local = to_local(item["published"])
        when = local.strftime("%Y-%m-%d %H:%M %Z") if local else "—"
        print(f"\n{i}. {item['title']}")
        print(f"   {when}  |  ключ: {item['key']}")
        print(f"   {item['summary'][:200]}...")


if __name__ == "__main__":
    main()
