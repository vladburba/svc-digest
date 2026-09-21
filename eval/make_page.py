#!/usr/bin/env python3
"""Собирает страницу для ручной разметки выборки.

    python eval/make_page.py 1 30                          # первая порция
    python eval/make_page.py 31 60 2026-09-28-sample.json  # другой набор

Зачем страница, а не список в терминале: тридцать позиций надо разметить
подряд, не отвлекаясь на набор номеров, — иначе внимание уходит на механику,
а не на содержание, и оценки к концу портятся.

ВАЖНО: вердикт бота (`bot_verdict`) в страницу НЕ попадает. Разметка слепая,
иначе меряется не вкус человека, а то, насколько он согласен с ботом.
"""

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "kartoteka.html"
SAMPLE = HERE / (sys.argv[3] if len(sys.argv) > 3 else "2026-09-21-sample.json")


def main():
    start = int(sys.argv[1]) if len(sys.argv) > 2 else 1
    end = int(sys.argv[2]) if len(sys.argv) > 2 else 30

    rows = json.loads(SAMPLE.read_text(encoding="utf-8"))
    chunk = [r for r in rows if start <= r["n"] <= end]
    blind = [{k: v for k, v in r.items() if k != "bot_verdict"} for r in chunk]

    html = TEMPLATE.read_text(encoding="utf-8")
    html = html.replace("__DATA__", json.dumps(blind, ensure_ascii=False))
    html = html.replace("__RANGE__", f"{start}–{end}")
    html = html.replace("__TOTAL__", str(len(rows)))

    out = HERE / f"kartoteka-{start}-{end}.html"
    out.write_text(html, encoding="utf-8")
    print(f"страница готова: {out}")
    print(f"  карточек: {len(blind)} (из {len(rows)} в выборке)")


if __name__ == "__main__":
    main()
