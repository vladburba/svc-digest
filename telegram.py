#!/usr/bin/env python3
"""Доставка в Telegram.

Бот здесь — не приёмник, а ЛИЧНОСТЬ-отправитель: берём токен
@vlad_burba_bot и шлём от его имени себе в чат. Контейнер бота не трогаем,
в его код не лезем — связи между сервисами нет.

Конфликта с polling бота не будет: очередь на токен Telegram держит только
для getUpdates. sendMessage — разовый stateless-запрос, отправителей с одним
токеном может быть сколько угодно.
"""

import os
from pathlib import Path

import httpx
from dotenv import load_dotenv

from netcfg import EGRESS_PROXY

load_dotenv(Path(__file__).resolve().parent / ".env")

API_BASE = "https://api.telegram.org"
TIMEOUT = 30.0


def _client():
    """Единый egress-прокси из netcfg (пусто на маке → напрямую)."""
    return httpx.Client(proxy=EGRESS_PROXY, timeout=TIMEOUT)


def send_message(text, parse_mode="HTML", disable_preview=True):
    """Шлёт сообщение админу. Возвращает message_id — расписку Telegram.

    Исключения НЕ глотает: вызывающий должен знать, что доставка не удалась.
    """
    token = os.getenv("BOT_TOKEN")
    chat_id = os.getenv("ADMIN_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("нет BOT_TOKEN или ADMIN_CHAT_ID в .env")

    with _client() as client:
        response = client.post(
            f"{API_BASE}/bot{token}/sendMessage",
            data={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": disable_preview,
            },
        )

    if response.status_code != 200:
        raise RuntimeError(f"Telegram {response.status_code}: {response.text[:200]}")

    return response.json()["result"]["message_id"]


if __name__ == "__main__":
    mid = send_message("🔧 <b>svc-digest</b>: проверка канала доставки")
    print(f"доставлено, message_id={mid}")
