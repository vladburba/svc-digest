#!/usr/bin/env python3
"""Единая сетевая конфигурация: прокси для всего исходящего трафика.

Зачем весь egress через один прокси (проверено на vlad-main 2026-07-26):
из контейнера НАПРЯМУЮ работает только Telegram; habr.com (РФ IP) виснет,
openrouter.ai отдаёт 403 (блок IP дата-центра). Через socks-прокси Xray
(тот же 127.0.0.1:1080, что у бота — урок 5.09) все три отвечают 200.
Xray сам маршрутизирует: РФ → напрямую, заграница → upstream-US.

Локально (мак) EGRESS_PROXY пуст → всё идёт напрямую, прокси не нужен.
На сервере EGRESS_PROXY=socks5://127.0.0.1:1080 (требует network_mode: host,
чтобы контейнер видел loopback хоста).
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

# None (не ""), чтобы httpx понял «без прокси».
EGRESS_PROXY = (os.getenv("EGRESS_PROXY") or "").strip() or None
