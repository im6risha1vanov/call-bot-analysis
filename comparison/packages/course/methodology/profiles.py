ROLES = {"averon_sales", "qualification_operator", "customer_sales", "unknown"}
PRODUCTS = {"gck", "ops", "voice_robot", "customer_offer", "unknown"}
STAGES = {"gatekeeper", "discovery", "presentation", "demo_preparation", "demo_results", "support", "unknown"}
STATUS = {"passed", "failed", "not_applicable", "insufficient_data"}

PRODUCT_RULES = {
    'gck': 'Внешние источники контактов; собственный сайт клиента не является обязательным. Проверять релевантность источников и ресурс обработки. Номер не равен заявке. Различать с КЦ/без КЦ; цены и сроки только по утверждённому режиму.',
    'ops': 'Собственный сайт и подходящий трафик; проверить фактический трафик до демо. Различать телефонный номер и заинтересованный контакт, замены и отсутствие замен; одинаковая цена не определяет гарантию.',
    'voice_robot': 'Нужны база, повторяемая задача, сценарий и готовность обработать отклики. Проверить ограничения и согласовать бриф/запуск; не гарантировать продажи.',
    'customer_offer': 'Продаётся продукт заказчика; не применять к нему тарифы или цели продажи ГЦК. Использовать только явно сообщённое предложение.',
    'unknown': 'Оценивать только общие действия; не угадывать коммерческие условия.'
}

ROLE_LABELS = {"averon_sales": "Продажа Averon", "qualification_operator": "Квалификация КЦ",
               "customer_sales": "Продажа заказчика", "unknown": "Роль не определена"}
PRODUCT_LABELS = {"gck": "ГЦК", "ops": "ОПС", "voice_robot": "Голосовой робот",
                  "customer_offer": "Продукт заказчика", "unknown": "Продукт не определён"}
STAGE_LABELS = {"gatekeeper": "Секретарь", "discovery": "Первичный разговор",
                "presentation": "Презентация и условия", "demo_preparation": "Подготовка демо",
                "demo_results": "Итоги демо", "support": "Запуск и сопровождение",
                "unknown": "Этап не определён"}

COMMON = {
    "clarity": "Понятное общение и уместный повод",
    "uses_client_response": "Использованы ответы клиента",
    "factual_accuracy": "Корректность подтверждённых фактов",
    "respectful_communication": "Уважительное и честное общение",
    "next_step": "Конкретное согласованное продолжение",
}
PROFILE_CRITERIA = {
    "gatekeeper": {"identifies_contact": "Определён нужный участник", "honest_transfer": "Получен контакт без выдумки и давления"},
    "discovery": {"relevant_hook": "Краткая релевантная зацепка", "checks_role": "Проверена роль собеседника",
                  "business_diagnosis": "Собраны и использованы нужные показатели", "product_fit": "Проверены соответствие продукта и ресурс обработки"},
    "presentation": {"relevant_presentation": "Предложение связано с задачей", "checks_understanding": "Получена обратная связь",
                     "economics": "Корректная экономика на явных данных", "objection_response": "Разобрано содержательное возражение"},
    "demo_preparation": {"demo_fit": "Проверены предпосылки демо", "processing_readiness": "Готова обработка контактов",
                         "expectations": "Разъяснены подтверждённые ожидания и ограничения", "launch_plan": "Согласованы подготовка, ответственный и срок"},
    "demo_results": {"funnel_results": "Разделены контакты, интерес и продажи", "client_feedback": "Получен отзыв клиента",
                     "explores_barrier": "Проверена причина затруднения", "appropriate_offer": "Предложен подходящий следующий шаг"},
    "support": {"checks_processing": "Проверены статусы и качество обработки", "checks_sources": "Проверены источники и выборка",
                "corrective_action": "Предложен конкретный план помощи", "accountability": "Зафиксированы ответственный и срок"},
    "unknown": {},
}
OPERATOR_CRITERIA = {"interest_qualification": "Выяснен интерес", "consultation_consent": "Получено согласие на консультацию",
                     "accurate_handoff": "Переданы факты без обещания продажи"}
OUTCOMES = {"no_result", "connection", "direct_contact", "scheduled_callback", "continued_conversation",
            "meeting", "agreed_proposal", "demo_plan", "brief", "installation", "launch_plan",
            "contract", "payment", "correction_plan", "qualified_consultation", "disqualified", "unknown"}
ALLOWED_OUTCOMES = {
    "gatekeeper": {"connection", "direct_contact", "scheduled_callback"},
    "discovery": {"meeting", "continued_conversation", "scheduled_callback", "demo_plan", "agreed_proposal", "disqualified"},
    "presentation": {"contract", "payment", "agreed_proposal", "scheduled_callback", "continued_conversation", "demo_plan"},
    "demo_preparation": {"brief", "installation", "launch_plan", "demo_plan", "scheduled_callback"},
    "demo_results": {"payment", "contract", "correction_plan", "scheduled_callback", "continued_conversation"},
    "support": {"correction_plan", "launch_plan", "scheduled_callback", "continued_conversation", "payment"},
    "unknown": set(),
}


def criteria_for(role: str, stage: str, confident: bool) -> dict[str, str]:
    result = dict(COMMON)
    if not confident:
        return result
    if role == "qualification_operator":
        return {**result, **OPERATOR_CRITERIA}
    return {**result, **PROFILE_CRITERIA[stage]}
