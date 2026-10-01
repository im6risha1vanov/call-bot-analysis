"""Tenant and manager scope is enforced in SQL, independently of model arguments."""
import json
from .runtime import VERSION, shadow_enabled
from .profiles import STAGES, PRODUCTS, ROLES


async def get_course_evaluation(pool, actor, call_id=None, training_session_id=None):
    from tools import Forbidden
    if not shadow_enabled():
        return {'available': False, 'reason': 'Методика курса выключена'}
    if (call_id is None) == (training_session_id is None):
        raise ValueError('Specify exactly one source')
    table, column, source_id = ('calls', 'call_id', call_id) if call_id is not None else ('training_sessions', 'training_session_id', training_session_id)
    source = await pool.fetchrow(f'SELECT client_id,extension FROM {table} WHERE id=$1 AND client_id=$2', int(source_id), actor.client_id)
    if not source or (actor.role == 'manager' and source['extension'] != actor.extension):
        raise Forbidden('Запись недоступна')
    row = await pool.fetchrow(f"SELECT * FROM methodology_evaluations WHERE {column}=$1 AND client_id=$2 AND version=$3 ORDER BY created_at DESC,id DESC LIMIT 1", int(source_id), actor.client_id, VERSION)
    if not row:
        return {'available': False, 'reason': 'Оценка по курсу ещё не рассчитана'}
    reasons = {'processing': 'Разбор обрабатывается.', 'failed': 'Разбор не завершён из-за ошибки; требуется проверка сохранённого запроса.',
               'uncertain': 'Результат запроса неизвестен. Автоматическая повторная оплата отключена; требуется проверка.'}
    return {'available': True, 'version': row['version'], 'status': row['status'],
            'reason': reasons.get(row['status']),
            'result': json.loads(row['result']) if row['result'] else None,
            'legacy_result': json.loads(row['legacy_result']) if row['legacy_result'] else None,
            'cost_units': float(row['cost_units']), 'feedback_sent': row['feedback_sent']}


async def get_course_stats(pool, actor, period, manager_extension=None, product=None, stage=None, sales_role=None):
    from tools import _scope_extension, _require_client, _period_bounds
    if not shadow_enabled():
        return {'available': False}
    for value, allowed in [(product, PRODUCTS), (stage, STAGES), (sales_role, ROLES)]:
        if value is not None and value not in allowed:
            raise ValueError('Unknown profile filter')
    ext = _scope_extension(actor, manager_extension)
    client = await _require_client(pool, actor.client_id)
    start, end = _period_bounds(client['timezone'], period)
    if start >= end:
        raise ValueError('Period end must be after its start')
    # Use the date of the call; re-evaluation never moves an observation to another period.
    rows = await pool.fetch("""SELECT item->>'key' AS criterion,item->>'status' AS status,count(*) AS n
        FROM methodology_evaluations e JOIN calls c ON c.id=e.call_id
        CROSS JOIN LATERAL jsonb_array_elements(e.result->'rows') item
        WHERE e.client_id=$1 AND c.client_id=$1 AND ($2::text IS NULL OR c.extension=$2)
        AND c.call_started_at >= $3 AND c.call_started_at < $4 AND e.version=$5 AND e.status='complete'
        AND ($6::text IS NULL OR e.result->'classification'->>'product'=$6)
        AND ($7::text IS NULL OR e.result->'classification'->>'stage'=$7)
        AND ($8::text IS NULL OR e.result->'classification'->>'sales_role'=$8)
        AND e.id=(SELECT max(latest.id) FROM methodology_evaluations latest WHERE latest.call_id=e.call_id AND latest.version=e.version AND latest.status='complete')
        GROUP BY 1,2 ORDER BY 1,2""", actor.client_id, ext, start, end, VERSION, product, stage, sales_role)
    summary = {}
    for r in rows:
        item = summary.setdefault(r['criterion'], {'passed': 0, 'failed': 0, 'not_applicable': 0, 'insufficient_data': 0})
        item[r['status']] = r['n']
    for item in summary.values():
        item['measured'] = item['passed'] + item['failed']
        item['failed_fraction'] = item['failed'] / item['measured'] if item['measured'] else None
    return {'available': True, 'version': VERSION, 'calibrated': False, 'period': period,
            'criteria': summary, 'note': 'Наблюдения по применимым критериям; баллы и рейтинги не калиброваны. Не смешивать с исходной шкалой.'}


SCHEMAS = [
    {'name': 'get_course_evaluation', 'description': 'Отдельный разбор по курсу с продуктом, этапом, цитатами и экономикой; исходный балл сохранён отдельно.',
     'input_schema': {'type': 'object', 'properties': {'call_id': {'type': 'integer'}, 'training_session_id': {'type': 'integer'}}, 'required': []}},
    {'name': 'get_course_stats', 'description': 'Наблюдения по новой методике, отдельно от исходных баллов. Доля ошибок только среди выполнено/не выполнено, остальные состояния исключены из знаменателя.',
     'input_schema': {'type': 'object', 'properties': {
         'period': {'type': 'object', 'properties': {'start': {'type': 'string', 'format': 'date'}, 'end': {'type': 'string', 'format': 'date'}}, 'required': ['start', 'end']},
         'manager_extension': {'type': ['string', 'null']}, 'product': {'type': ['string', 'null']}, 'stage': {'type': ['string', 'null']}, 'sales_role': {'type': ['string', 'null']}}, 'required': ['period']}}
]
