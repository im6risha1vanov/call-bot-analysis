"""
Тренажёр возражений (Этап 4 мультиагентной спецификации). Модель на Claude
играет скептичного клиента; менеджер отрабатывает разговор ГОЛОСОВЫМИ
сообщениями в Telegram (текст — запасной путь, если голос не пришёл или TTS
недоступен).

Вход менеджера: голос → Deepgram (тот же движок, что и на реальных звонках —
тренировка и работа мерятся одной линейкой не только по критериям, но и по
качеству распознавания). Выход клиента: голос через tts.synthesize_ogg(),
пока не настроен — откат на текст (см. tts.py: ни один платный TTS ещё не
выбран пользователем).

Оценка сессии по завершении — ТЕМИ ЖЕ критериями и той же функцией
(score_call/compute_level из analysis.py), что и реальные звонки, дословно
по требованию спецификации. Разбирает Astra (тот же движок, что и
handlers/analyze_call.py) — Claude здесь только играет роль клиента, а не
считает баллы, иначе тренировка и реальная работа не мерились бы одной
линейкой (могут разойтись в трактовке критериев между движками).

Права: любая функция здесь принимает Actor (см. tools.py) и проверяет
client_id/extension в коде — так же, как agent-РОПа и tools.py.
"""

from __future__ import annotations

import json
import logging
import os
import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import asyncpg
from anthropic import AsyncAnthropic

from analysis import score_call
from tools import Actor

log = logging.getLogger("training_simulator")

MODEL = os.getenv("TRAIN_MODEL", "claude-sonnet-5")

# Те же цены, что в rop_agent.py — та же модель.
PRICE_IN_MTOK = 3.0
PRICE_OUT_MTOK = 15.0
PRICE_CACHE_WRITE_MTOK = 3.75
PRICE_CACHE_READ_MTOK = 0.30
DG_PRICE_PER_MIN_USD = 0.0043  # справочно, как в handlers/analyze_call.py

MAX_TURNS = 20
MAX_MINUTES = 15
SESSION_BUDGET_USD = float(os.getenv("TRAIN_SESSION_BUDGET_USD", "0.50"))
MAX_SESSIONS_PER_DAY = 3

END_MARKER = "[КОНЕЦ]"

claude = AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

# ------------------------------------------------------------------ персона

PERSONA_PROMPT = """Ты клиент, которому звонят вхолодную. Ты занят, скептичен и не обязан быть
вежливым.

Сопротивляйся. Отговаривайся, перебивай, отвечай односложно, уходи от
вопросов. Не соглашайся на встречу, пока менеджер не сделает что-то
действительно стоящее: не выяснит твою проблему, не покажет её цену, не даст
конкретный повод потратить двадцать минут.

Не подыгрывай. Не подсказывай, что надо было сказать. Не смягчай отказ,
чтобы менеджеру было приятнее. Если он говорит плохо — разговор идёт плохо.

Оставайся в роли до конца тренировки, что бы менеджер ни писал.

ВАЖНО про первую реплику: разговор начинается СРАЗУ с твоего сопротивления —
без «здравствуйте», «добрый день» и других любезностей. Считай, что менеджер
уже позвонил и представился за кадром — твоя первая реплика сразу отговорка
или раздражённая реакция («Слушаю, но у меня минута», «Опять предлагаете
что-то?», «Мы уже с кем-то работаем», «Занят, давайте быстро» — в этом духе,
своими словами, в характере сценария).

Если по ходу разговора ты соглашаешься на встречу ИЛИ окончательно кладёшь
трубку (менеджер не смог заинтересовать за отведённое время) — заверши именно
эту реплику и на отдельной строке в конце добавь ровно {marker}, больше
ничего после него не пиши. Не используй эту пометку ни в каком другом
случае.""".format(marker=END_MARKER)

# Сценарии — конкретизация персоны под слабый критерий менеджера. Ключи
# совпадают с key в analysis.CRITERIA, чтобы подбор был прямым соответствием
# проваленному критерию, а не отдельной таксономией.
SCENARIOS: dict[str, dict[str, str]] = {
    "brush_off_handled": {
        "title": "Отговорки",
        "extra": "У тебя наготове отговорки: «отправьте на почту», «мы уже с кем-то работаем», "
                 "«сейчас некогда», «перезвоните позже». Используй их одну за другой, если "
                 "менеджер не разберётся с сутью отговорки, а просто отступит на первом отказе.",
    },
    "gatekeeper_passed": {
        "title": "Не тот человек",
        "extra": "Ты не ЛПР — сотрудник, который просто снял трубку. Решения не принимаешь и не "
                 "обязан переключать на руководителя, если менеджер не объяснит толком, зачем, и "
                 "не спросит вежливо, к кому лучше обратиться.",
    },
    "implication_questions": {
        "title": "Поверхностные вопросы",
        "extra": "Отвечай на вопросы менеджера односложно и по верхам, если он не копает глубже "
                 "(не спрашивает, во что реально обходится проблема) — не развивай тему сам.",
    },
    "explicit_need": {
        "title": "Не проговорена потребность",
        "extra": "Не признавай проблему явно, пока менеджер сам её не сформулирует и не спросит "
                 "прямо, согласен ли ты с ней.",
    },
    "decision_influence": {
        "title": "Скрытое влияние на решение",
        "extra": "У решения есть ещё один участник (например, партнёр или начальник) — упомяни "
                 "это мельком, только если менеджер спросит, кто ещё участвует в решении.",
    },
    "no_early_pitch": {
        "title": "Ранняя презентация",
        "extra": "Если менеджер рано начинает презентовать продукт, не выяснив твою ситуацию — "
                 "реагируй скептично: «а мне это зачем», теряй интерес.",
    },
}

GENERAL_SCENARIO = {
    "title": "Общий сценарий",
    "extra": "Веди себя как обычный скептичный руководитель без привязки к конкретной слабости "
             "менеджера — данных для точного подбора пока недостаточно.",
}

MIN_CALLS_FOR_TARGETED_SCENARIO = 10  # мягче правила 30 у РОПа: тренажёру нужна тенденция, а не строгий вывод


def _cost_of(usage) -> float:
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    return (
        usage.input_tokens / 1e6 * PRICE_IN_MTOK
        + usage.output_tokens / 1e6 * PRICE_OUT_MTOK
        + cache_write / 1e6 * PRICE_CACHE_WRITE_MTOK
        + cache_read / 1e6 * PRICE_CACHE_READ_MTOK
    )


def _system_prompt(scenario: dict) -> str:
    return PERSONA_PROMPT + "\n\nСЦЕНАРИЙ: " + scenario["extra"]


def strip_speaker_tags(transcript: str) -> str:
    """Убирает разметку [MM:SS Спикер N] из короткого голосового сообщения —
    для одиночной реплики важны только слова, не диаризация. Публичная —
    используется и в demo_bot.py при разборе voice-сообщений менеджера."""
    import re
    return re.sub(r"\[\d{2}:\d{2} Спикер \d+\]\s*", "", transcript).strip()


# --------------------------------------------------------------- ограничения

async def sessions_today(pool: asyncpg.Pool, client_id: int, extension: str, tz_name: str) -> int:
    tz = ZoneInfo(tz_name)
    start = datetime.combine(datetime.now(tz).date(), datetime.min.time(), tzinfo=tz)
    return await pool.fetchval(
        "SELECT count(*) FROM training_sessions WHERE client_id=$1 AND extension=$2 AND created_at >= $3",
        client_id, extension, start,
    )


async def get_active_session(pool: asyncpg.Pool, actor: Actor) -> asyncpg.Record | None:
    """Руководителю тренажёр тоже доступен — чтобы он мог попробовать его сам и
    решить, годится ли инструмент для отдела. Сессия привязывается к его
    строке в employees, как и у менеджера."""
    return await pool.fetchrow(
        "SELECT * FROM training_sessions WHERE client_id=$1 AND extension=$2 AND status='active'",
        actor.client_id, actor_key(actor),
    )


def actor_key(actor: Actor) -> str:
    """Чем помечена сессия. У менеджера это добавочный, у руководителя его нет
    — берём то, что стоит в его строке employees (там оно уникально в паре с
    клиентом)."""
    return actor.extension or ""


# ------------------------------------------------------------- сценарий

async def pick_scenario(pool: asyncpg.Pool, client_id: int, extension: str,
                         topic_hint: str | None) -> tuple[str, dict]:
    """Возвращает (scenario_kind, scenario). Явная тема от РОПа побеждает
    автоподбор, если совпадает с одним из известных сценариев по названию."""
    if topic_hint:
        low = topic_hint.strip().lower()
        for key, sc in SCENARIOS.items():
            if low in sc["title"].lower() or sc["title"].lower() in low:
                return key, sc

    rows = await pool.fetch(
        """
        SELECT a.analysis FROM astra_analysis a JOIN calls c ON c.id = a.call_id
        WHERE c.client_id=$1 AND c.extension=$2 AND a.status='analyzed' AND a.analysis IS NOT NULL
        """,
        client_id, extension,
    )
    if len(rows) < MIN_CALLS_FOR_TARGETED_SCENARIO:
        return "general", GENERAL_SCENARIO

    total: dict[str, int] = {}
    failed: dict[str, int] = {}
    for r in rows:
        for row in (json.loads(r["analysis"]).get("rows") or []):
            if not row.get("applicable"):
                continue
            key = row.get("key")
            if key not in SCENARIOS:
                continue
            total[key] = total.get(key, 0) + 1
            if not row.get("passed"):
                failed[key] = failed.get(key, 0) + 1

    if not failed:
        return "general", GENERAL_SCENARIO
    worst_key = max(failed, key=lambda k: failed[k] / total[k])
    return worst_key, SCENARIOS[worst_key]


# --------------------------------------------------------------- жизненный цикл

def _to_claude_messages(transcript: list[dict]) -> list[dict]:
    """transcript: [{"role": "client"|"manager", "text": ...}, ...] в
    хронологическом порядке. Клиент — это Claude (assistant), менеджер —
    собеседник (user)."""
    return [
        {"role": "assistant" if t["role"] == "client" else "user", "content": t["text"]}
        for t in transcript
    ]


async def start_session(pool: asyncpg.Pool, actor: Actor, topic: str | None,
                         assigned_by: str | None) -> asyncpg.Record:
    """Создаёт сессию и генерирует первую (клиентскую) реплику. Не проверяет
    лимиты — вызывающий код (demo_bot.py) обязан проверить get_active_session
    и sessions_today ДО вызова, чтобы дать пользователю понятное сообщение,
    а не проглатывать отказ здесь."""
    scenario_kind, scenario = await pick_scenario(pool, actor.client_id, actor_key(actor), topic)
    system_text = _system_prompt(scenario)

    resp = await claude.messages.create(
        model=MODEL, max_tokens=200, thinking={"type": "disabled"},
        system=[{"type": "text", "text": system_text}],
        messages=[{"role": "user", "content": (
            "[Служебная команда, менеджер её не увидит: сгенерируй свою самую первую реплику "
            "в этом звонке. Без приветствия и представления — сразу отговорка или раздражённая "
            "реакция, в характере сценария. Только сама реплика.]"
        )}],
    )
    cost = _cost_of(resp.usage)
    opening = "".join(b.text for b in resp.content if b.type == "text").strip()
    opening = opening.replace(END_MARKER, "").strip()  # на первой реплике конец невозможен

    row = await pool.fetchrow(
        """
        INSERT INTO training_sessions (client_id, extension, assigned_by, topic, scenario_kind,
                                        transcript, turns_count, cost_usd, is_test)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb, 1, $7, $8)
        RETURNING *
        """,
        actor.client_id, actor_key(actor), assigned_by, topic, scenario_kind,
        json.dumps([{"role": "client", "text": opening}], ensure_ascii=False), cost,
        actor.role != "manager",  # пробная сессия: руководитель или владелец пробует тренажёр
    )
    log.info("training session id=%s начата, доб.=%s сценарий=%s пробная=%s",
             row["id"], actor_key(actor), scenario_kind, actor.role != "manager")
    return row


class TurnResult:
    def __init__(self, reply_text: str, ended: bool, level: str | None = None, note: str | None = None):
        self.reply_text = reply_text  # реплика клиента — её озвучиваем
        self.ended = ended
        self.level = level
        # Служебный комментарий (разбор предыдущего ответа в режиме отработки).
        # Отправляется текстом, а не голосом: иначе «клиент» посреди звонка
        # начал бы вслух оценивать работу менеджера.
        self.note = note


async def handle_manager_turn(pool: asyncpg.Pool, session: asyncpg.Record, manager_text: str) -> TurnResult:
    """Один обмен репликами. manager_text уже расшифрован (Deepgram для
    голоса, как есть для текста) и очищен от разметки диаризации."""
    transcript: list[dict] = json.loads(session["transcript"])
    transcript.append({"role": "manager", "text": manager_text})
    turns_count = session["turns_count"] + 1
    cost = float(session["cost_usd"])

    elapsed = datetime.now(ZoneInfo("UTC")) - session["started_at"]
    hit_limit = (
        turns_count >= MAX_TURNS
        or elapsed >= timedelta(minutes=MAX_MINUTES)
        or cost >= SESSION_BUDGET_USD
    )

    if hit_limit:
        await pool.execute(
            "UPDATE training_sessions SET transcript=$2::jsonb, turns_count=$3, updated_at=now() WHERE id=$1",
            session["id"], json.dumps(transcript, ensure_ascii=False), turns_count,
        )
        level = await _finalize(pool, session["id"], transcript)
        return TurnResult("Время/лимит тренировки исчерпан — на этом закончим.", True, level)

    scenario = SCENARIOS.get(session["scenario_kind"], GENERAL_SCENARIO)
    system_text = _system_prompt(scenario)

    resp = await claude.messages.create(
        model=MODEL, max_tokens=300, thinking={"type": "disabled"},
        system=[{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}],
        messages=_to_claude_messages(transcript),
    )
    cost += _cost_of(resp.usage)
    reply = "".join(b.text for b in resp.content if b.type == "text").strip()
    ended = END_MARKER in reply
    reply = reply.replace(END_MARKER, "").strip()

    transcript.append({"role": "client", "text": reply})
    turns_count += 1

    await pool.execute(
        "UPDATE training_sessions SET transcript=$2::jsonb, turns_count=$3, cost_usd=$4, updated_at=now() WHERE id=$1",
        session["id"], json.dumps(transcript, ensure_ascii=False), turns_count, cost,
    )

    level = None
    if ended:
        level = await _finalize(pool, session["id"], transcript)

    return TurnResult(reply, ended, level)


# ------------------------------------------- режим 2: отработка возражений
#
# Отличается от разговора тем, что связного звонка нет: одно возражение — один
# ответ — короткий разбор — следующее возражение. Нужен, когда менеджеру надо
# набить руку именно на отговорках, а не проходить весь звонок целиком. Какой
# из двух режимов назначить, решает РОП.

DRILL_SIZE = 5

OBJECTIONS = [
    "Нам ничего не нужно, спасибо.",
    "Отправьте всё на почту, я посмотрю.",
    "Мы уже работаем с другими, нас всё устраивает.",
    "У меня нет времени сейчас разговаривать.",
    "Это дорого для нас.",
    "Я не занимаюсь этим вопросом.",
    "Перезвоните через месяц, сейчас не до этого.",
    "А откуда у вас мои контакты?",
    "Нам это неинтересно.",
    "Пришлите коммерческое, если заинтересует — сами позвоним.",
]

# Формулировка критерия — дословно из prompt_score.md: тренировка меряется той
# же линейкой, что и реальные звонки, иначе прогресс не с чем сравнивать.
DRILL_CRITERION = """**brush_off_handled** — отговорка пройдена.
Правильно: зацепиться за сказанное клиентом, задать уточняющий вопрос и на
основании ответа вернуть разговор к встрече. Не засчитывается, если менеджер
спорил по существу, повторил презентацию или согласился и свернул разговор.
Согласие с «отправьте на почту» — не пройден."""

DRILL_JUDGE_PROMPT = f"""Ты оцениваешь один ответ менеджера на одну отговорку клиента в тренажёре
холодных звонков. Критерий — тот же, что применяется к реальным звонкам:

{DRILL_CRITERION}

Верни строго JSON без пояснений вокруг:
{{"passed": true|false, "comment": "одна фраза"}}

comment — не длиннее 20 слов, конкретно про этот ответ: что именно сработало или
чего не хватило. Без общих советов вроде «поработайте над возражениями»."""


async def start_drill_session(pool: asyncpg.Pool, actor: Actor, topic: str | None,
                               assigned_by: str | None) -> asyncpg.Record:
    """Лимиты (активная сессия, штук в день) проверяет вызывающий код — как и
    для режима разговора."""
    objections = random.sample(OBJECTIONS, min(DRILL_SIZE, len(OBJECTIONS)))
    drill_state = {"objections": objections, "results": []}

    row = await pool.fetchrow(
        """
        INSERT INTO training_sessions (client_id, extension, assigned_by, topic, scenario_kind,
                                        mode, transcript, drill_state, turns_count, cost_usd, is_test)
        VALUES ($1, $2, $3, $4, 'brush_off_handled', 'drill', $5::jsonb, $6::jsonb, 1, 0, $7)
        RETURNING *
        """,
        actor.client_id, actor_key(actor), assigned_by, topic,
        json.dumps([{"role": "client", "text": objections[0]}], ensure_ascii=False),
        json.dumps(drill_state, ensure_ascii=False),
        actor.role != "manager",  # пробная сессия: руководитель или владелец пробует тренажёр
    )
    log.info("drill session id=%s начата, доб.=%s возражений=%s пробная=%s",
             row["id"], actor_key(actor), len(objections), actor.role != "manager")
    return row


async def _judge_answer(objection: str, answer: str) -> tuple[bool, str, float]:
    resp = await claude.messages.create(
        model=MODEL, max_tokens=200, thinking={"type": "disabled"},
        system=[{"type": "text", "text": DRILL_JUDGE_PROMPT, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": f"Отговорка клиента: «{objection}»\nОтвет менеджера: «{answer}»"}],
    )
    cost = _cost_of(resp.usage)
    raw = "".join(b.text for b in resp.content if b.type == "text").strip()
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        verdict = json.loads(raw)
        return bool(verdict.get("passed")), str(verdict.get("comment") or "").strip(), cost
    except json.JSONDecodeError:
        # Не роняем тренировку из-за формата ответа модели: засчитываем как
        # непройденное и говорим прямо, что разбор не получился.
        log.warning("не разобрал ответ судьи тренажёра: %s", raw[:200])
        return False, "не удалось разобрать оценку", cost


async def handle_drill_turn(pool: asyncpg.Pool, session: asyncpg.Record, manager_text: str) -> TurnResult:
    drill_state = json.loads(session["drill_state"])
    objections: list[str] = drill_state["objections"]
    results: list[dict] = drill_state["results"]

    current = objections[len(results)]
    passed, comment, call_cost = await _judge_answer(current, manager_text)
    results.append({"objection": current, "answer": manager_text, "passed": passed, "comment": comment})

    transcript: list[dict] = json.loads(session["transcript"])
    transcript.append({"role": "manager", "text": manager_text})
    cost = float(session["cost_usd"]) + call_cost
    turns_count = session["turns_count"] + 1

    mark = "✅ зачтено" if passed else "❌ не зачтено"
    note = f"{mark}. {comment}" if comment else mark

    elapsed = datetime.now(ZoneInfo("UTC")) - session["started_at"]
    out_of_room = (
        len(results) >= len(objections)
        or elapsed >= timedelta(minutes=MAX_MINUTES)
        or cost >= SESSION_BUDGET_USD
    )

    if out_of_room:
        drill_state["results"] = results
        await pool.execute(
            """UPDATE training_sessions SET transcript=$2::jsonb, drill_state=$3::jsonb,
               turns_count=$4, cost_usd=$5, updated_at=now() WHERE id=$1""",
            session["id"], json.dumps(transcript, ensure_ascii=False),
            json.dumps(drill_state, ensure_ascii=False), turns_count, cost,
        )
        summary = await _finalize_drill(pool, session["id"], drill_state)
        return TurnResult(summary, True, None, note=note)

    next_objection = objections[len(results)]
    transcript.append({"role": "client", "text": next_objection})
    turns_count += 1
    drill_state["results"] = results
    await pool.execute(
        """UPDATE training_sessions SET transcript=$2::jsonb, drill_state=$3::jsonb,
           turns_count=$4, cost_usd=$5, updated_at=now() WHERE id=$1""",
        session["id"], json.dumps(transcript, ensure_ascii=False),
        json.dumps(drill_state, ensure_ascii=False), turns_count, cost,
    )
    return TurnResult(next_objection, False, None, note=note)


async def _finalize_drill(pool: asyncpg.Pool, session_id: int, drill_state: dict) -> str:
    results = drill_state["results"]
    passed = sum(1 for r in results if r["passed"])
    score = round(passed / len(results) * 100) if results else None
    analysis = {"mode": "drill", "results": results}
    await pool.execute(
        """UPDATE training_sessions SET status='completed', ended_at=now(), score=$2,
           analysis=$3::jsonb, drill_state=$4::jsonb, updated_at=now() WHERE id=$1""",
        session_id, score, json.dumps(analysis, ensure_ascii=False),
        json.dumps(drill_state, ensure_ascii=False),
    )
    log.info("drill session id=%s завершена: зачтено %s из %s", session_id, passed, len(results))

    weak = [r["objection"] for r in results if not r["passed"]]
    tail = ""
    if weak:
        tail = " Не зачтены: " + "; ".join(f"«{o}»" for o in weak[:3])
    return f"Отработка закончена: зачтено {passed} из {len(results)}.{tail}"


async def end_session_early(pool: asyncpg.Pool, session: asyncpg.Record) -> tuple[str, str | None]:
    """Досрочное завершение по кнопке. Возвращает (текст пользователю, уровень).

    Оценивать то, что менеджер успел сказать, честнее, чем выбрасывать сессию:
    прогресс считается по тому же правилу, что и всегда. Но если он не ответил
    ни разу — оценивать нечего, помечаем сессию брошенной, чтобы она не портила
    статистику нулём."""
    if session["mode"] == "drill":
        drill_state = json.loads(session["drill_state"])
        if not drill_state.get("results"):
            await pool.execute(
                "UPDATE training_sessions SET status='abandoned', ended_at=now(), updated_at=now() WHERE id=$1",
                session["id"],
            )
            return "Тренировка прервана — отвечать вы не начали, оценивать нечего.", None
        return await _finalize_drill(pool, session["id"], drill_state), None

    transcript: list[dict] = json.loads(session["transcript"])
    if not any(t["role"] == "manager" for t in transcript):
        await pool.execute(
            "UPDATE training_sessions SET status='abandoned', ended_at=now(), updated_at=now() WHERE id=$1",
            session["id"],
        )
        return "Тренировка прервана — отвечать вы не начали, оценивать нечего.", None

    level = await _finalize(pool, session["id"], transcript)
    return "Тренировка завершена досрочно, разбор посчитан по тому, что успели сказать.", level


def _build_fake_transcript(transcript: list[dict]) -> str:
    """Собирает транскрипт в формате deepgram_client.py ([MM:SS Спикер N]),
    чтобы score_call() — откалиброванный на реальных транскриптах — увидел
    привычную разметку. Менеджер = Спикер 0 (звонит первым, как в реальном
    звонке), клиент = Спикер 1. Секунда на реплику — оценка, не факт;
    score_call не использует время, только порядок и текст."""
    lines = []
    t = 0
    for turn in transcript:
        mm, ss = divmod(t, 60)
        spk = 0 if turn["role"] == "manager" else 1
        lines.append(f"[{mm:02d}:{ss:02d} Спикер {spk}] {turn['text']}")
        t += 12
    return "\n".join(lines)


async def _finalize(pool: asyncpg.Pool, session_id: int, transcript: list[dict]) -> str | None:
    fake_transcript = _build_fake_transcript(transcript)
    try:
        scores, score, level, rows, _astra_cost = await score_call(fake_transcript)
        analysis = {**scores, "rows": rows}
        await pool.execute(
            """UPDATE training_sessions SET status='completed', ended_at=now(), level=$2, score=$3,
               analysis=$4::jsonb, updated_at=now() WHERE id=$1""",
            session_id, level, score, json.dumps(analysis, ensure_ascii=False),
        )
        log.info("training session id=%s завершена, уровень=%s", session_id, level)
        return level
    except Exception as exc:
        log.exception("ошибка оценки тренировочной сессии id=%s", session_id)
        await pool.execute(
            "UPDATE training_sessions SET status='failed', ended_at=now(), error=$2, updated_at=now() WHERE id=$1",
            session_id, str(exc)[:2000],
        )
        return None
