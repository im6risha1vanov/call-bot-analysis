"""
Пересчёт вердиктов у уже разобранных звонков по текущему правилу из
analysis.compute_level (часть 2 калибровки).

К модели не обращается: результаты проверки критериев по каждому звонку уже
лежат в astra_analysis.analysis, пересчёт — чистая арифметика по сохранённому.
Нужен, чтобы история была сопоставима с новыми данными: иначе в статистике
рядом окажутся звонки, посчитанные по двум разным шкалам.

Старые значения не сохраняются — перезапись по решению пользователя.

Запуск: cd /opt/callbot-astra && .venv/bin/python recalc_verdicts.py [--apply]
Без --apply только показывает, что изменилось бы.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections import Counter

import asyncpg

from analysis import (LEVEL_CRITICAL_CRITERIA, VERDICT_MIN_FAILURES,
                       VERDICT_MIN_FAILURES_WITH_SIGNAL, compute_level)


async def main() -> None:
    apply = "--apply" in sys.argv
    print(f"Правило: ❌❌ при {VERDICT_MIN_FAILURES} проваленных ключевых "
          f"или при {VERDICT_MIN_FAILURES_WITH_SIGNAL} вместе с упущенным сигналом")
    print(f"Ключевые критерии: {', '.join(sorted(LEVEL_CRITICAL_CRITERIA))}")
    print(f"Режим: {'ПРИМЕНЯЮ изменения' if apply else 'только показываю (без --apply)'}\n")

    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=2)
    rows = await pool.fetch(
        "SELECT call_id, level, analysis FROM astra_analysis "
        "WHERE status = 'analyzed' AND analysis IS NOT NULL ORDER BY call_id"
    )

    changes: list[tuple[int, str | None, str | None]] = []
    for r in rows:
        analysis = json.loads(r["analysis"])
        new_level = compute_level(analysis, analysis.get("rows") or [])
        if new_level != r["level"]:
            changes.append((r["call_id"], r["level"], new_level))

    print(f"Разобранных звонков: {len(rows)}, меняется вердикт у {len(changes)}\n")

    moves = Counter(f"{old or 'нет'} → {new or 'нет'}" for _cid, old, new in changes)
    for move, n in moves.most_common():
        print(f"  {move}: {n}")

    if apply and changes:
        async with pool.acquire() as conn:
            async with conn.transaction():
                for call_id, _old, new_level in changes:
                    await conn.execute(
                        "UPDATE astra_analysis SET level=$2, updated_at=updated_at WHERE call_id=$1",
                        call_id, new_level,
                    )
        print(f"\nОбновлено строк: {len(changes)}")
        print("updated_at намеренно не трогаем: по нему считаются дайджесты за день,")
        print("и массовый пересчёт истории иначе перетащил бы все старые звонки в сегодня.")
    elif changes:
        print("\nЧтобы применить: .venv/bin/python recalc_verdicts.py --apply")

    after = Counter()
    for r in rows:
        analysis = json.loads(r["analysis"])
        after[compute_level(analysis, analysis.get("rows") or []) or "нет вердикта"] += 1
    print("\nРаспределение после пересчёта:")
    for level in ("✅", "❌", "❌❌", "нет вердикта"):
        print(f"  {level:<12} {after.get(level, 0)}")

    await pool.close()


asyncio.run(main())
