"""Synthetic teaching situations. These are not approved commercial promises."""
from copy import deepcopy


def scenario(key, title, stage, product, opening, facts, goal, skills, *, role="averon_sales", objection=None):
    return {"id": key, "title": title, "role": role, "stage": stage, "product": product,
            "opening": opening, "facts": facts, "goal": goal, "skills": skills,
            "objection": objection or opening, "synthetic": True}


SCENARIOS = [
    scenario("secretary", "Секретарь и общая почта", "gatekeeper", "gck", "Алло, слушаю.",
             {"role": "секретарь", "decision_maker": "руководитель маркетинга", "availability": "завтра после 11:00",
              "prior_acquaintance": False}, "Честный повод; соединение, контакт или конкретный повторный звонок",
             ["honest_transfer", "identifies_contact"], objection="Отправьте всё на общую почту."),
    scenario("owner_minute", "Собственник даёт минуту", "discovery", "gck", "Алло, кто это?",
             {"role": "собственник", "advertising_budget_rub_month": 200000, "applications_month": 40,
              "problem": "дорогие заявки", "callers": 2, "previous_interest": "не подтверждён"},
             "Представление, короткая зацепка, диагностика либо конкретное продолжение",
             ["relevant_hook", "business_diagnosis"], objection="У меня только минута."),
    scenario("all_fine", "Заказов хватает", "discovery", "gck", "Здравствуйте, слушаю.",
             {"orders": "хватает", "callers": 0, "problem": "не обработаем дополнительный поток"},
             "Проверить ресурс; подходящий план или корректная квалификация",
             ["business_diagnosis", "product_fit"], objection="У нас всё нормально."),
    scenario("expensive", "Дорого после презентации", "presentation", "gck", "Да, продолжим обсуждение.",
             {"offer": "предложение уже объяснено, тариф не утверждён", "comparison": "стоимость текущей заявки",
              "channel_budget_rub_month": 200000, "applications_month": 40, "new_channel_conversion": "неизвестна"},
             "Уточнить сравнение, собрать цифры и согласовать проверяемый расчёт",
             ["economics", "objection_response"], objection="Этот пакет для нас дорогой."),
    scenario("free_test", "Бесплатный тест ГЦК", "demo_preparation", "gck", "Здравствуйте, обсудим тест?",
             {"confusion": "считает демо ОПС тестом ГЦК", "website": True, "demo_terms": "требуют утверждения"},
             "Различить продукты; согласовать проверку действующих правил, без выдуманного доступа",
             ["demo_fit", "expectations"], objection="Дайте бесплатный тест ГЦК."),
    scenario("technology", "Проверка технологии", "presentation", "ops", "Да, слушаю ваши пояснения.",
             {"concern": "кликджекинг", "evidence_needed": "проверяемые документы", "legal_approval": "неизвестно"},
             "Признать вопрос и согласовать проверку документов; бренды не заменяют подтверждение",
             ["factual_accuracy", "objection_response"], objection="Это кликджекинг?"),
    scenario("origin", "Откуда номер", "discovery", "gck", "Алло, слушаю.",
             {"prior_request": False, "friend_referral": False, "verified_source": "не предоставлен"},
             "Не выдумывать прошлую заявку; уточнить подтверждённый источник у ответственного",
             ["factual_accuracy", "respectful_communication"], objection="Откуда у вас мой номер?"),
    scenario("no_callers", "Нет сотрудников для звонков", "discovery", "gck", "Здравствуйте.",
             {"role": "собственник", "staff": 1, "callers": 0, "handling_budget": "не определён"},
             "Проверить возможности и стоимость обработки, а не продавать больший поток",
             ["product_fit", "business_diagnosis"], objection="У меня некому звонить этим людям."),
    scenario("low_traffic", "Мало трафика ОПС", "demo_preparation", "ops", "Да, хочу обсудить запуск.",
             {"website": True, "visitors_month": 30, "advertising": False},
             "Выяснить трафик и ограничения; не гарантировать быстрый поток",
             ["demo_fit", "expectations"], objection="Когда пойдут первые заявки?"),
    scenario("demo_no_sales", "После демо нет продаж", "demo_results", "ops", "Давайте обсудим результаты.",
             {"contacts": 20, "processed_contacts": 10, "interested_leads": 1, "sales": 0,
              "feedback": "половину контактов ещё не обработали"},
             "Разделить контакты, интерес и продажи; проверить обработку и согласовать план",
             ["funnel_results", "client_feedback", "explores_barrier"], objection="Демо не принесло ни одной продажи."),
    scenario("installment", "Рассрочку не одобрили", "presentation", "gck", "Да, обсудим оплату.",
             {"bank_decision": "отказ", "fees": "не утверждены", "alternative_budget": "надо выяснить"},
             "Обсудить реалистичный вариант без обещания одобрения или отсутствия переплаты",
             ["factual_accuracy", "objection_response"], objection="Мне отказали в рассрочке."),
    scenario("support", "Сопровождение ГЦК", "support", "gck", "Здравствуйте, как раз есть вопрос по проекту.",
             {"source_a": "100 обработанных контактов, 10 заинтересованных", "source_b": "100 обработанных, 2 заинтересованных",
              "handling_delay": "двое суток", "statuses": "частично не заполнены"},
             "Проверить выборку, обработку и источники; план с ответственным и сроком",
             ["checks_processing", "checks_sources", "corrective_action", "accountability"],
             objection="Источники не дают результат."),
    scenario("unit", "Номер или заявка", "presentation", "gck", "Продолжим про результат услуги.",
             {"result": "номер; интерес и продажа не подтверждены", "guarantee": "не утверждена"},
             "Различить единицы результата и проверить утверждённые условия",
             ["factual_accuracy", "relevant_presentation"], objection="Значит каждый номер — готовая заявка?"),
    scenario("call_center", "ГЦК с обработкой и без", "presentation", "gck", "Да, выбираю формат работы.",
             {"callers": 0, "call_center_terms": "нужна утверждённая версия", "budget": "не определён"},
             "Уточнить обработку, полную стоимость и единицу результата; не смешивать пакеты",
             ["product_fit", "factual_accuracy"], objection="Обзвон уже входит в цену?"),
    scenario("busy", "Клиент занят", "discovery", "voice_robot", "Алло?",
             {"task": "напоминания клиентам из собственной базы", "availability": "завтра в 14:00",
              "base": "есть, порядок использования требует проверки"},
             "Объяснить повод и согласовать конкретный повторный звонок",
             ["relevant_hook", "next_step"], objection="Сейчас неудобно разговаривать."),
    scenario("proposal", "Согласованное КП", "presentation", "ops", "Обсудили решение, что дальше?",
             {"role": "собственник", "interest": True, "proposal_subject": "ОПС для своего сайта",
              "followup": "пятница 12:00", "terms": "проверить по действующей версии"},
             "Согласовать предмет КП, адресат, срок изучения и следующий контакт",
             ["next_step", "checks_understanding"], objection="Пришлите коммерческое предложение."),
    scenario("unready", "Отдел продаж не готов", "demo_preparation", "gck", "Здравствуйте, можно уже запускать?",
             {"script": "не согласован", "responsible": "не назначен", "handling_capacity": "не проверена"},
             "Подготовить скрипт, ответственного и обработку до потока",
             ["processing_readiness", "launch_plan"], objection="Включайте поток сегодня."),
    scenario("sources_volume", "Источники не дают объём", "support", "gck", "Здравствуйте, по объёму есть вопрос.",
             {"desired_daily": 100, "actual_daily": 20, "desired_field": "пожелание, не гарантия",
              "sources": "надо проверить по фактической выборке"},
             "Проверить источники и лимит, не обещать объём по полю пожеланий",
             ["checks_sources", "corrective_action"], objection="Я указал сто в день, почему меньше?"),
    scenario("delivery_date", "Срок начала поставки", "demo_preparation", "gck", "Да, уточним запуск.",
             {"payment": "прошла", "sources": "ещё не согласованы", "supplier": "не выбран", "contract_terms": "не предоставлены"},
             "Проверить договор, поставщика и событие отсчёта; не выдумывать единый срок",
             ["expectations", "launch_plan"], objection="Поставка точно начнётся завтра?"),
    scenario("repeat_calls", "Повторные звонки", "support", "gck", "Здравствуйте, по недозвонам вопрос.",
             {"processing_mode": "собственный отдел клиента", "attempts": 1, "statuses": "недозвон",
              "contract_rule": "не утверждён в базе"},
             "Уточнить режим и статусы; согласовать повторные попытки без универсального регламента",
             ["checks_processing", "accountability"], objection="Никто не отвечает, что делать?"),
    scenario("renewal", "Остаток перед продлением", "support", "gck", "Да, обсудим дальнейшую работу.",
             {"remaining_fraction": .25, "results": "статусы неполные", "renewal_price": "не утверждена"},
             "Проверить остаток и результаты, согласовать план продления без обещаний",
             ["checks_processing", "next_step"], objection="Давайте продлим, какой будет результат?"),
    scenario("qualification", "Квалификация колл-центром", "discovery", "customer_offer", "Алло, слушаю.",
             {"interest": "нужно выяснить", "offer": "консультация отдела заказчика", "availability": "завтра 10:00"},
             "Выявить интерес и согласие на консультацию, передать точный контекст, не продавать вместо заказчика",
             ["interest_qualification", "consultation_consent", "accurate_handoff"],
             role="qualification_operator", objection="Вы хотите сразу что-то продать?"),
    scenario("customer_sales", "Продажа услуги заказчика", "presentation", "customer_offer", "Да, расскажите про ваше предложение.",
             {"offer": "сервисное обслуживание оборудования", "need": "частые простои",
              "price": "не предоставлена", "prior_request": "не подтверждена"},
             "Выяснить задачу, связать предложение с ней и согласовать конкретное действие",
             ["relevant_presentation", "checks_understanding", "next_step"],
             role="customer_sales", objection="Чем вы можете помочь с простоями?"),
]


def find(topic):
    if not topic:
        return None
    low = topic.strip().lower()
    for item in SCENARIOS:
        if low == item["id"] or low in item["title"].lower() or item["title"].lower() in low:
            return deepcopy(item)
    return None


def variants(item):
    # Repeat the same skill with an explicit changed circumstance; facts are pinned per exercise.
    result = []
    for n, circumstance in enumerate(["обычная ситуация", "клиент занят", "есть второй участник решения",
                                      "клиент просит подтверждение", "нужен конкретный срок продолжения"]):
        current = deepcopy(item)
        current.pop('exercises', None)
        current["variant"] = circumstance
        current["exercise"] = n + 1
        if n == 1:
            current['facts']['available_minutes'] = 1
        elif n == 2:
            current['facts']['additional_decision_maker'] = 'партнёр участвует в решении'
        elif n == 3:
            current['facts']['evidence_needed'] = 'проверяемый документ'
        elif n == 4:
            current['facts']['followup_deadline'] = 'нужно согласовать'
        result.append(current)
    return result
