"""
Диагностика шкалы вердиктов (часть 1 калибровки). Только считает, ничего не
меняет: результаты проверки критериев по каждому звонку уже лежат в базе, и
вердикты можно пересчитать задним числом с разными правилами, не обращаясь к
модели и не трогая данные.

Запуск: cd /opt/callbot-astra && .venv/bin/python diagnose_verdicts.py
"""

from __future__ import annotations

import asyncio
import json
import os
from collections import Counter, defaultdict

import asyncpg

from analysis import CRITERIA, LEVEL_CRITICAL_CRITERIA

TITLES = {key: title for key, _w, title in CRITERIA}
SIGNAL_TRIGGERS = ("third_parties", "problem_agreements")


def _failed_critical(by_key: dict) -> list[str]:
    return [
        key for key in LEVEL_CRITICAL_CRITERIA
        if key in by_key and by_key[key]["applicable"] and not by_key[key]["passed"]
    ]


def _missed_signals(scores: dict) -> list[str]:
    signals = scores.get("signals") or {}
    missed = []
    if any(not t.get("followed_up") for t in signals.get("third_parties") or []):
        missed.append("third_parties")
    if any(not a.get("followed_up") for a in signals.get("problem_agreements") or []):
        missed.append("problem_agreements")
    return missed


def _verdict(scores: dict, by_key: dict, rule: str) -> str | None:
    """Пересчёт вердикта по одному из вариантов правила. None — звонок
    оборван, уровень не ставится ни при каком правиле."""
    if scores.get("call_cut_short"):
        return None
    if scores.get("meeting_booked") or scores.get("proposal_sent") or scores.get("decision_maker_contact"):
        return "✅"

    critical = _failed_critical(by_key)
    missed = _missed_signals(scores)

    if rule == "current":       # ❌ при одном ключевом ИЛИ упущенном сигнале
        double = bool(critical) or bool(missed)
    elif rule == "two":         # ❌ при двух ключевых (сигнал сам по себе не решает)
        double = len(critical) >= 2
    elif rule == "three":       # ❌ только когда провалены все три ключевых
        double = len(critical) >= 3
    elif rule == "two_or_mix":  # ❌ при двух ключевых ИЛИ ключевой вместе с сигналом
        double = len(critical) >= 2 or (len(critical) >= 1 and bool(missed))
    elif rule == "three_or_two_mix":  # ❌ при трёх ключевых ИЛИ двух вместе с сигналом
        double = len(critical) >= 3 or (len(critical) >= 2 and bool(missed))
    elif rule == "two_no_decision":
        # ❌ при двух ключевых, но «выяснено, кто влияет на решение» не
        # считается: он проваливается в 90% и сам по себе обваливает шкалу.
        hard = [k for k in critical if k != "decision_influence"]
        double = len(hard) >= 2 or (len(hard) >= 1 and bool(missed))
    else:
        raise ValueError(rule)
    return "❌" if double else "⚠️"


def _fmt_dist(counter: Counter, total: int) -> str:
    parts = []
    for level in ("✅", "⚠️", "❌"):
        n = counter.get(level, 0)
        share = f"{n / total * 100:.0f}%" if total else "—"
        parts.append(f"{level} {n} ({share})")
    return " · ".join(parts)


async def main() -> None:
    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=2)
    rows = await pool.fetch(
        """
        SELECT a.call_id, a.level, a.analysis, c.extension, c.call_started_at,
               e.full_name
        FROM astra_analysis a
        JOIN calls c ON c.id = a.call_id
        LEFT JOIN employees e ON e.client_id = c.client_id AND e.extension = c.extension
                              AND e.role = 'manager'
        WHERE a.status = 'analyzed' AND a.analysis IS NOT NULL
        ORDER BY c.call_started_at
        """
    )
    await pool.close()

    parsed = []
    for r in rows:
        analysis = json.loads(r["analysis"])
        by_key = {row["key"]: row for row in (analysis.get("rows") or [])}
        parsed.append({
            "call_id": r["call_id"],
            "level": r["level"],
            "scores": analysis,
            "by_key": by_key,
            "extension": r["extension"],
            "name": r["full_name"] or f"доб. {r['extension']}",
            "day": r["call_started_at"].date() if r["call_started_at"] else None,
        })

    total = len(parsed)
    print(f"Разобранных звонков в базе: {total}\n")

    # ---------------------------------------------------- текущее распределение
    print("=" * 62)
    print("ТЕКУЩЕЕ РАСПРЕДЕЛЕНИЕ")
    print("=" * 62)
    stored = Counter(p["level"] or "нет вердикта" for p in parsed)
    for level in ("✅", "⚠️", "❌", "нет вердикта"):
        print(f"  {level:<12} {stored.get(level, 0)}")

    no_verdict = [p for p in parsed if not p["level"]]
    cut_short = [p for p in no_verdict if p["scores"].get("call_cut_short")]
    print(f"\nПочему сумма вердиктов меньше числа звонков: {len(no_verdict)} звонков без уровня.")
    print(f"  из них помечены моделью как оборванные (call_cut_short): {len(cut_short)}")
    if len(no_verdict) != len(cut_short):
        print(f"  БЕЗ объяснения (не оборваны, но уровня нет): {len(no_verdict) - len(cut_short)} — это уже баг")
    print("  Оборванный звонок — короткий/прерванный разговор: судить о работе менеджера")
    print("  по нему нельзя, поэтому уровень не ставится. Но в отчёт такие звонки")
    print("  попадают в общий счёт, отсюда и расхождение в числах.")

    scored = [p for p in parsed if not p["scores"].get("call_cut_short")]
    print(f"\nЗвонков, по которым вердикт вообще возможен: {len(scored)}")

    # ------------------------------------------------------- триггеры ❌
    print("\n" + "=" * 62)
    print("ЧАСТОТА СРАБАТЫВАНИЯ КАЖДОГО ТРИГГЕРА ❌")
    print("=" * 62)
    print("(считаем только по звонкам без успеха — там, где вердикт решают триггеры)")
    no_success = [
        p for p in scored
        if not (p["scores"].get("meeting_booked") or p["scores"].get("proposal_sent")
                or p["scores"].get("decision_maker_contact"))
    ]
    print(f"Таких звонков: {len(no_success)}\n")

    trigger_counts: Counter = Counter()
    for p in no_success:
        for key in _failed_critical(p["by_key"]):
            trigger_counts[key] += 1
        for sig in _missed_signals(p["scores"]):
            trigger_counts[sig] += 1

    for key in LEVEL_CRITICAL_CRITERIA:
        n = trigger_counts.get(key, 0)
        share = f"{n / len(no_success) * 100:.0f}%" if no_success else "—"
        print(f"  ключевой критерий · {TITLES.get(key, key):<34} {n:>3} ({share})")
    for sig, label in (("third_parties", "третье лицо не отработано"),
                        ("problem_agreements", "согласие с проблемой не отработано")):
        n = trigger_counts.get(sig, 0)
        share = f"{n / len(no_success) * 100:.0f}%" if no_success else "—"
        print(f"  сигнал           · {label:<34} {n:>3} ({share})")

    counts_by_n = Counter(len(_failed_critical(p["by_key"])) for p in no_success)
    print("\n  Сколько ключевых критериев провалено одновременно:")
    for n in sorted(counts_by_n):
        print(f"    {n} из 3 — в {counts_by_n[n]} звонках")

    # ---------------------------------------------- распределение по правилам
    print("\n" + "=" * 62)
    print("РАСПРЕДЕЛЕНИЕ ПРИ РАЗНЫХ ПРАВИЛАХ")
    print("=" * 62)
    rules = [
        ("current", "❌ при одном ключевом или упущенном сигнале (текущее)"),
        ("two", "❌ при двух проваленных ключевых"),
        ("three", "❌ при всех трёх проваленных ключевых"),
        ("two_or_mix", "❌ при двух ключевых ИЛИ ключевой + упущенный сигнал"),
        ("three_or_two_mix", "❌ при трёх ключевых ИЛИ двух + упущенный сигнал [сверх списка]"),
        ("two_no_decision", "❌ при двух ключевых без учёта «кто влияет на решение» [сверх списка]"),
    ]
    for rule, label in rules:
        counter = Counter()
        for p in scored:
            counter[_verdict(p["scores"], p["by_key"], rule)] += 1
        n = sum(counter.values())
        double_share = counter.get("❌", 0) / n * 100 if n else 0
        flag = "  ← цель: ❌ не больше трети" if double_share <= 34 else ""
        print(f"\n{label}")
        print(f"  {_fmt_dist(counter, n)}{flag}")

    # --------------------------------------------------- доли по критериям
    print("\n" + "=" * 62)
    print("ДОЛЯ ПРОВАЛОВ ПО КАЖДОМУ КРИТЕРИЮ")
    print("=" * 62)
    applicable: Counter = Counter()
    failed: Counter = Counter()
    for p in scored:
        for key, row in p["by_key"].items():
            if row.get("applicable"):
                applicable[key] += 1
                if not row.get("passed"):
                    failed[key] += 1

    stats = []
    for key, _w, title in CRITERIA:
        n = applicable.get(key, 0)
        f = failed.get(key, 0)
        stats.append((f / n if n else 0, title, f, n, key))
    stats.sort(reverse=True)
    for share, title, f, n, key in stats:
        mark = " ← не различает никого" if n and f == n else ""
        critical = " [ключевой]" if key in LEVEL_CRITICAL_CRITERIA else ""
        print(f"  {share * 100:>5.0f}%  {title:<38} {f}/{n}{critical}{mark}")

    # ------------------------------------------------------ по менеджерам
    print("\n" + "=" * 62)
    print("РАЗБИВКА ПО МЕНЕДЖЕРАМ")
    print("=" * 62)
    by_manager = defaultdict(list)
    for p in parsed:
        by_manager[p["name"]].append(p)
    for name, items in sorted(by_manager.items(), key=lambda kv: -len(kv[1])):
        counter = Counter(p["level"] or "нет вердикта" for p in items)
        line = " · ".join(f"{lvl} {counter.get(lvl, 0)}" for lvl in ("✅", "⚠️", "❌", "нет вердикта"))
        print(f"  {name:<24} звонков {len(items):>3}   {line}")

    days = {p["day"] for p in parsed if p["day"]}
    print(f"\nДней с данными: {len(days)} (для правила «две недели до оценочных суждений»)")


asyncio.run(main())
