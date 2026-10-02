import copy
import json
import unittest
from unittest.mock import patch

from methodology import economics, evaluation, profiles, runtime
from methodology.scenarios import SCENARIOS, find, variants

TRANSCRIPT = 'Менеджер: Здравствуйте, обсуждаем ГЦК. Клиент: Да, отправьте КП, посмотрю к пятнице. Менеджер: Перезвоню в пятницу в 12:00. Клиент: Да, согласен.'
CATALOG = {'version': 'test', 'records': []}


def response(stage='presentation', role='averon_sales', product='gck'):
    return {'classification': {'sales_role': role, 'product': product, 'stage': stage,
                              'confidence': .9, 'reason': 'согласованные условия', 'evidence': ['обсуждаем ГЦК']},
            'criteria': {k: {'status': 'passed', 'evidence': 'Да, согласен.', 'reason': 'подтверждение'}
                         for k in profiles.criteria_for(role, stage, True)},
            'facts': [], 'claims': [], 'outcome': {'type': 'agreed_proposal', 'description': 'согласовано КП', 'evidence': 'Да, отправьте КП'},
            'next_step': {'action': 'КП и повторный звонок', 'responsible': 'менеджер', 'deadline': 'пятница',
                          'channel': 'телефон', 'scheduled_time': '12:00', 'agreed': True, 'evidence': 'Да, согласен.'},
            'coaching': {'say_instead': 'Уточните срок', 'repeat_scenario': 'У клиента второй участник решения'}}


def grade(raw, transcript=TRANSCRIPT, catalog=CATALOG):
    return evaluation.evaluate(raw, transcript, commercial_catalog=catalog)


class EvaluationTests(unittest.TestCase):
    def test_mixed_exercises_use_their_own_profiles_and_quotes(self):
        selected = [find(key) for key in ('secretary','expensive','support','qualification','customer_sales')]
        contexts, answers = [], []
        raw = response()
        raw['exercise_results'] = []
        for n, item in enumerate(selected, 1):
            item.update(exercise=n, mixed_drill=True)
            answer = f'Уточняю задачу {n}.'
            contexts.append({'scenario': item, 'objection': item['objection'], 'answer': answer})
            answers.append(answer)
            criterion = next(key for key in item['skills'] if key in profiles.criteria_for(item['role'], item['stage'], True))
            raw['exercise_results'].append({'exercise':n,'criterion':criterion,'status':'passed',
                                           'evidence':answer,'reason':'Предметный ответ','say_instead':''})
        transcript = '\n'.join(answers)
        result = evaluation.evaluate(raw, transcript, training_exercises=contexts, commercial_catalog=CATALOG)
        self.assertTrue(result['mixed_drill'])
        self.assertEqual({r['key'] for r in result['rows']}, set(profiles.COMMON))
        self.assertEqual([e['stage'] for e in result['exercise_results']], [s['stage'] for s in selected])
        self.assertTrue(all(e['status']=='passed' for e in result['exercise_results']))
        self.assertIn('Секретарь и общая почта',evaluation.render(result,detailed=True))
        raw['exercise_results'][0]['evidence'] = answers[1]
        changed = evaluation.evaluate(raw, transcript, training_exercises=contexts, commercial_catalog=CATALOG)
        self.assertEqual(changed['exercise_results'][0]['status'],'insufficient_data')

    def test_mixed_exercise_rejects_criterion_from_another_profile(self):
        item=find('secretary');item.update(exercise=1,mixed_drill=True)
        source={'scenario':item,'objection':item['objection'],'answer':'Здравствуйте.'}
        raw=response()
        raw['exercise_results']=[{'exercise':1,'criterion':'economics','status':'passed','evidence':'Здравствуйте.','reason':''}]
        with self.assertRaises(evaluation.InvalidEvaluation):
            evaluation.evaluate(raw,'Здравствуйте.',training_exercises=[source])

    def test_partial_drill_keeps_answered_feedback_and_ignores_remaining_exercises(self):
        item=find('expensive');item.update(exercise=1,mixed_drill=True)
        answer='С чем сравниваете стоимость?'
        source={'scenario':item,'objection':item['objection'],'answer':answer}
        raw=response()
        raw['exercise_results']=[{'exercise':n,'criterion':'economics','status':'passed',
                                 'evidence':answer,'reason':'Уточнение сравнения','say_instead':''} for n in range(1,6)]
        result=evaluation.evaluate(raw,answer,training_exercises=[source])
        self.assertEqual([e['exercise'] for e in result['exercise_results']],[1])
        self.assertEqual(result['exercise_results'][0]['status'],'passed')
        self.assertTrue(result['mixed_drill'])

    def test_all_six_profiles_and_three_roles(self):
        for role in profiles.ROLES - {'unknown'}:
            for stage in profiles.STAGES - {'unknown'}:
                with self.subTest(role=role, stage=stage):
                    result = grade(response(stage, role))
                    self.assertTrue(result['narrow_profile_applied'])
                    self.assertEqual(set(r['key'] for r in result['rows']), set(profiles.criteria_for(role, stage, True)))

    def test_profile_uncertain_does_not_apply_narrow_penalties(self):
        raw = response(); raw['classification']['confidence'] = .4
        result = grade(raw)
        self.assertFalse(result['narrow_profile_applied'])
        self.assertEqual(set(r['key'] for r in result['rows']), set(profiles.COMMON))

    def test_unsupported_classification_quote_falls_back(self):
        raw = response(); raw['classification']['evidence'] = ['Это выдуманная цитата']
        self.assertFalse(grade(raw)['narrow_profile_applied'])

    def test_four_states_are_distinct(self):
        raw = response()
        for k, state in zip(raw['criteria'], ['passed', 'failed', 'insufficient_data', 'passed', 'not_applicable']):
            raw['criteria'][k]['status'] = state
        counts = grade(raw)['quality']['counts']
        self.assertEqual(counts['failed'], 1)
        self.assertEqual(counts['not_applicable'], 1)
        self.assertEqual(counts['insufficient_data'], 1)

    def test_missing_quote_is_not_manager_failure(self):
        raw = response(); raw['criteria']['clarity'] = {'status': 'failed', 'evidence': 'выдумка'}
        self.assertEqual(grade(raw)['rows'][0]['status'], 'insufficient_data')

    def test_unknown_commercial_terms_cannot_penalize(self):
        raw = response(); raw['criteria']['factual_accuracy']['status'] = 'failed'
        raw['claims'] = [{'product': 'gck', 'field': 'price', 'value': 100, 'evidence': 'обсуждаем ГЦК'}]
        result = grade(raw)
        self.assertEqual(next(r for r in result['rows'] if r['key']=='factual_accuracy')['status'], 'insufficient_data')
        self.assertEqual(result['commercial_claims'][0]['verdict'], 'unverified')

    def test_approved_record_requires_full_product_mode(self):
        record = {'id': 'gck_test', 'product': 'gck', 'unit': 'phone_number', 'package': 'synthetic',
                  'call_center': False, 'replacement_mode': 'none', 'tax': 'excluded', 'price': 100,
                  'approved': True, 'owner': 'test_owner', 'source': 'test://document', 'effective_from': '2026-01-01'}
        claim = {k: record[k] for k in ['product', 'unit', 'package', 'call_center', 'replacement_mode', 'tax']}
        claim.update(record_id='gck_test', field='price', value=100.0, evidence='обсуждаем ГЦК')
        cat = {'version': 'test', 'records': [record]}
        self.assertEqual(evaluation.verify_claim(claim, TRANSCRIPT, cat)['verdict'], 'confirmed')
        claim['replacement_mode'] = 'replacements'
        self.assertEqual(evaluation.verify_claim(claim, TRANSCRIPT, cat)['verdict'], 'unverified')
        claim['replacement_mode'] = 'none'; claim['value'] = 200
        self.assertEqual(evaluation.verify_claim(claim, TRANSCRIPT, cat)['verdict'], 'contradicted')
        record['approved'] = False
        self.assertEqual(evaluation.verify_claim(claim, TRANSCRIPT, cat)['verdict'], 'unverified')

    def test_agreed_proposal_is_valid_result(self):
        self.assertTrue(grade(response())['outcome']['confirmed'])

    def test_gatekeeper_generic_mail_does_not_confirm_proposal(self):
        self.assertFalse(grade(response('gatekeeper'))['outcome']['confirmed'])

    def test_callback_needs_clock_but_brief_does_not(self):
        raw = response(); raw['outcome']['type'] = 'scheduled_callback'; raw['next_step']['scheduled_time'] = None
        self.assertFalse(grade(raw)['outcome']['confirmed'])
        raw = response('demo_preparation'); raw['outcome']['type'] = 'brief'; raw['next_step']['scheduled_time'] = None
        self.assertTrue(grade(raw)['outcome']['confirmed'])

    def test_operator_goal_is_consultation_not_full_sale(self):
        raw = response(role='qualification_operator'); raw['outcome']['type'] = 'qualified_consultation'
        result = grade(raw)
        self.assertTrue(result['outcome']['confirmed'])
        self.assertIn('interest_qualification', [r['key'] for r in result['rows']])
        self.assertNotIn('economics', [r['key'] for r in result['rows']])

    def test_no_invented_weights_or_level(self):
        result = grade(response())
        self.assertIsNone(result['quality']['score'])
        self.assertIsNone(result['quality']['level'])
        self.assertFalse(result['quality']['calibrated'])

    def test_invalid_json_shapes_and_boolean_strings(self):
        for mutation in [lambda r: r.update(classification=[]), lambda r: r['classification'].update(confidence=True),
                         lambda r: r['classification'].update(confidence=float('nan')),
                         lambda r: r['next_step'].update(agreed='false'),
                         lambda r: r['criteria']['clarity'].update(status='whatever'),
                         lambda r: r.update(facts=[{'metric': [], 'evidence':'обсуждаем ГЦК'}])]:
            raw = response(); mutation(raw)
            with self.assertRaises((evaluation.InvalidEvaluation, TypeError)):
                grade(raw)

    def test_contradictory_numbers_stay_unknown(self):
        transcript = TRANSCRIPT + ' Контактов 100, потом 200, затем 300.'
        raw = response(); raw['facts'] = [{'metric':'contacts','value':v,'unit':'contacts','period':'month','channel':'new','assumption':False,'evidence': 'Контактов 100, потом 200, затем 300.'} for v in [100, 200, 300]]
        self.assertIsNone(grade(raw, transcript)['economics']['values']['sales'])

    def test_extracted_numbers_must_appear_in_evidence(self):
        self.assertTrue(evaluation.number_in_quote(1000000, 'Средний чек 1 000 000 рублей.', 'RUB'))
        self.assertTrue(evaluation.number_in_quote(70000, 'Цена 70 тыс. рублей.', 'RUB'))
        self.assertTrue(evaluation.number_in_quote(.2, 'Интерес у 20%.', 'fraction'))
        self.assertFalse(evaluation.number_in_quote(500, 'Число не называли.', 'contacts'))
        self.assertFalse(evaluation.number_in_quote(.2, 'Интерес у 20%.', 'RUB'))

    def test_short_and_detail_share_same_graded_result(self):
        result = grade(response()); result['classification']['reason'] = '<b>not markup</b>'
        for detailed in [False, True]:
            text = evaluation.render(result, detailed=detailed)
            self.assertIn('согласовано КП', text)
            self.assertIn('Получилось:', text)
            self.assertIn('Повторите ситуацию:', text)

    def test_methodology_is_not_a_copy_of_meeting_only_prompt(self):
        prompt = evaluation.score_prompt()
        self.assertIn('Краткая отраслевая зацепка', prompt)
        self.assertIn('согласованное КП', prompt)
        self.assertNotIn('situational_ratio', profiles.COMMON)
        self.assertNotIn('sold_meeting', profiles.PROFILE_CRITERIA['presentation'])

    def test_empty_approved_catalog_is_explicit(self):
        self.assertEqual(runtime.approved_catalog()['records'], [])

    def test_bad_config_fails_closed(self):
        with patch('pathlib.Path.read_text', return_value='{"mode":"active"}'):
            self.assertFalse(runtime.shadow_enabled())


class EconomicsTests(unittest.TestCase):
    def inputs(self):
        values = {'contacts':(1000,'contacts'), 'interest_rate':(.2,'fraction'), 'lead_to_sale_rate':(.05,'fraction'),
                  'average_check':(100000,'RUB'), 'margin':(.3,'fraction'), 'contact_cost':(70000,'RUB'), 'processing_cost':(30000,'RUB')}
        result = {k:{'value':v,'unit':unit,'channel':'synthetic_new','period':'synthetic_month','assumption':True} for k,(v,unit) in values.items()}
        result['processing_in_contact_cost'] = False
        return result

    def test_synthetic_course_example(self):
        result = economics.calculate(self.inputs())['values']
        self.assertEqual(result['sales'], 10)
        self.assertEqual(result['revenue'], 1000000)
        self.assertEqual(result['contribution'], 300000)
        self.assertEqual(result['net_result'], 200000)

    def test_no_double_counted_handling(self):
        values = self.inputs(); values['processing_in_contact_cost'] = True
        self.assertEqual(economics.calculate(values)['values']['net_result'], 230000)

    def test_missing_margin_is_not_profit(self):
        values = self.inputs(); del values['margin']
        result = economics.calculate(values)['values']
        self.assertEqual(result['revenue'], 1000000)
        self.assertIsNone(result['net_result'])

    def test_different_channel_conversion_is_not_used(self):
        values = self.inputs(); values['lead_to_sale_rate']['channel'] = 'incoming'
        self.assertIsNone(economics.calculate(values)['values']['sales'])

    def test_unknown_period_does_not_silently_transfer_rate(self):
        values = self.inputs(); values['interest_rate']['period'] = None
        self.assertIsNone(economics.calculate(values)['values']['sales'])

    def test_fraction_and_number_validation(self):
        for bad in [20, float('nan'), float('inf'), True, -1, 'unknown']:
            values = self.inputs(); values['interest_rate']['value'] = bad
            self.assertIsNone(economics.calculate(values)['values']['sales'])

    def test_application_cost_same_period_and_zero_guard(self):
        values = {'channel_budget': {'value':200000, 'unit':'RUB','period':'month','channel':'ads'},
                  'applications': {'value':40,'unit':'applications','period':'month','channel':'ads'}}
        self.assertEqual(economics.calculate(values)['values']['current_application_cost'], 5000)
        values['applications']['value'] = 0
        self.assertIsNone(economics.calculate(values)['values']['current_application_cost'])


class ScenarioTests(unittest.TestCase):
    def test_random_short_drills_use_current_registry_without_repeats(self):
        from methodology import training
        current = copy.deepcopy(SCENARIOS[:5])
        current[-1].update(id='new_short_case',title='Новая короткая ситуация')
        with patch.object(training,'SCENARIOS',current):
            series = training.mixed_drill(5)
        self.assertEqual({e['id'] for e in series['exercises']},{s['id'] for s in current})
        self.assertEqual([e['exercise'] for e in series['exercises']],[1,2,3,4,5])
        saved = next(e for e in series['exercises'] if e['id']==current[0]['id'])
        original = saved['objection']
        current[0]['objection']='Изменение реестра после запуска'
        self.assertEqual(saved['objection'],original)

    def test_unique_ids_and_role_stage_coverage(self):
        self.assertEqual(len(SCENARIOS), len({s['id'] for s in SCENARIOS}))
        self.assertEqual({s['role'] for s in SCENARIOS}, profiles.ROLES - {'unknown'})
        self.assertEqual({s['stage'] for s in SCENARIOS}, profiles.STAGES - {'unknown'})

    def test_facts_pinned_between_variants(self):
        item = find('expensive'); repeated = variants(item)
        self.assertEqual(len(repeated), 5)
        repeated[0]['facts']['offer'] = 'changed'
        self.assertNotEqual(repeated[0]['facts'], repeated[1]['facts'])
        self.assertNotEqual(repeated[0]['facts'], item['facts'])

    def test_full_opening_not_premade_pitch(self):
        self.assertEqual(find('owner_minute')['opening'], 'Алло, кто это?')
        self.assertFalse(find('origin')['facts']['prior_request'])

    def test_plain_chunks_handle_html_and_surrogate_pairs(self):
        from methodology.messages import split_plain
        text = '<b>сырой текст</b>\n' + '🙂'*5000 + '&amp;'
        chunks = list(split_plain(text))
        self.assertEqual(''.join(chunks), text)
        self.assertTrue(all(len(c.encode('utf-16-le'))//2 <= 3900 for c in chunks))


if __name__ == '__main__':
    unittest.main()
