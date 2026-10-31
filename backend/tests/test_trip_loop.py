import json
import unittest
from unittest.mock import patch
from langgraph.types import Command
from services.agent.graph import build_graph, start_state
from services.agent.nodes.clarify import COULD_NOT_UNDERSTAND, ERROR_TEXT, QUESTIONS, build_card, merge_answers
from services.agent.prompts import EXTRACT_PROMPT
from services.agent.state import REQUIRED, TripConstraints, compute_missing

COMPLETE = dict(origin='Chicago', destination='New York', mode='fly',
                trip_type='round_trip', depart_date='2099-01-01',
                return_date='2099-01-05', budget=300, travelers=2)
# The same round trip with a departure window instead of exact dates, and no trip length
FLEXIBLE = {**{k: v for k, v in COMPLETE.items() if k not in ('depart_date', 'return_date')},
            'flexible_start': '2099-03-01', 'flexible_end': '2099-03-31'}


class TripLoopTests(unittest.TestCase):
    def setUp(self):
        # Only the AI call in extract is mocked; each test queues the patches it returns
        patcher = patch('services.agent.nodes.extract.generate')
        self.ai = patcher.start()
        self.addCleanup(patcher.stop)
        self.graph = build_graph()
        self.config = {'configurable': {'thread_id': self.id()}}

    def start(self, first_patch):
        self.ai.side_effect = [json.dumps(first_patch)]
        return self._pause(self.graph.invoke(start_state('my trip', 'mock'), self.config))

    def answer(self, resume, ai_patch=None):
        self.ai.side_effect = None if ai_patch is None else [json.dumps(ai_patch)]
        return self._pause(self.graph.invoke(Command(resume=resume), self.config))

    def _pause(self, result):
        return result['__interrupt__'][0].value if result.get('__interrupt__') else None

    def constraints(self):
        return self.graph.get_state(self.config).values['constraints']

    def new_thread(self, tag):
        """Start another conversation (its own checkpoint) in the same test, e.g. per subTest."""
        self.config = {'configurable': {'thread_id': f'{self.id()}-{tag}'}}

    def test_fixed_answer_saved_without_ai(self):
        card = self.start({k: v for k, v in COMPLETE.items() if k != 'travelers'})
        self.assertEqual(card['card']['field'], 'travelers')
        self.ai.reset_mock()
        self.answer({'value': 2})
        self.ai.assert_not_called()
        self.assertEqual(self.constraints()['travelers'], 2)

    def test_typed_answer_calls_ai_once(self):
        card = self.start({'destination': 'Denver'})
        self.assertEqual(card['card']['field'], 'origin')
        self.ai.reset_mock()
        self.answer({'text': "O'Hare"}, ai_patch={'origin': 'Chicago'})
        self.assertEqual(self.ai.call_count, 1)
        sent = json.loads(self.ai.call_args.args[2])
        self.assertEqual(sent['answers'], [{'field': 'origin', 'question': 'Where are you leaving from?',
                                            'text': "O'Hare"}])
        self.assertIs(self.ai.call_args.args[1], EXTRACT_PROMPT)
        self.assertEqual(self.constraints()['origin'], 'Chicago')

    def test_one_typed_answer_can_fill_several_fields(self):
        self.start({'destination': 'Denver'})
        card = self.answer({'text': "flying out of O'Hare"}, ai_patch={'origin': 'Chicago', 'mode': 'fly'})
        self.assertEqual(self.constraints()['mode'], 'fly')
        self.assertEqual(card['card']['field'], 'trip_type')  # the mode card is skipped

    def test_invalid_fixed_answer_reshows_card_with_error(self):
        self.start({k: v for k, v in COMPLETE.items() if k != 'travelers'})
        card = self.answer({'value': 12})
        self.assertEqual(card['card']['field'], 'travelers')
        self.assertEqual(card['error'], ERROR_TEXT['travelers'])
        self.assertEqual(self.constraints()['origin'], 'Chicago')
        self.assertEqual(self.constraints()['budget'], 300)
        self.ai.assert_called_once()  # only the first request

    def test_unusable_typed_answer_reshows_card(self):
        self.start({k: v for k, v in COMPLETE.items() if k not in ('depart_date', 'return_date')})
        card = self.answer({'text': 'idk'}, ai_patch={})
        self.assertEqual(card['card']['field'], 'depart_date')
        self.assertEqual(card['error'], COULD_NOT_UNDERSTAND)
        card = self.answer({'value': '2099-01-01'})
        self.assertIsNone(card['error'])  # the old error is cleared

    def test_depart_range_sets_window_and_asks_nights_not_return(self):
        self.start({k: v for k, v in COMPLETE.items() if k not in ('depart_date', 'return_date')})
        card = self.answer({'value': {'start': '2099-03-01', 'end': '2099-03-31'}})
        self.assertEqual(self.constraints()['flexible_start'], '2099-03-01')
        self.assertEqual(self.constraints()['flexible_end'], '2099-03-31')
        self.assertIsNone(self.constraints()['depart_date'])
        self.assertEqual(card['card']['field'], 'trip_nights')
        self.assertEqual((card['card']['min'], card['card']['max']), (1, 30))

    def test_backwards_range_shows_range_error(self):
        self.start({k: v for k, v in COMPLETE.items() if k not in ('depart_date', 'return_date')})
        card = self.answer({'value': {'start': '2099-03-31', 'end': '2099-03-01'}})
        self.assertEqual(card['card']['field'], 'depart_date')
        self.assertEqual(card['error'], ERROR_TEXT['depart_date'])
        self.assertIn('end after the start', card['error'])

    def test_ai_null_does_not_erase_other_fields(self):
        self.start({k: v for k, v in COMPLETE.items() if k not in ('depart_date', 'return_date')})
        self.answer({'text': 'sometime in march'},
                    ai_patch={'flexible_start': '2099-03-01', 'flexible_end': '2099-03-31',
                              'origin': None, 'budget': None})
        self.assertEqual(self.constraints()['origin'], 'Chicago')
        self.assertEqual(self.constraints()['budget'], 300)
        self.assertEqual(self.constraints()['flexible_start'], '2099-03-01')

    def test_ai_null_does_not_erase_optional_answer(self):
        self.start({**COMPLETE, 'max_connections': 0})
        done = self.answer({'answers': {'activities': {'text': 'hiking'}}},
                           ai_patch={'activities': ['hiking'], 'max_connections': None})
        self.assertIsNone(done)
        self.assertEqual(self.constraints()['max_connections'], 0)  # never asked again, so must survive

    def test_ai_null_still_switches_exact_date_to_window(self):
        self.start({k: v for k, v in COMPLETE.items() if k != 'return_date'})
        self.assertEqual(self.constraints()['depart_date'], '2099-01-01')
        self.answer({'text': 'actually sometime in march'},
                    ai_patch={'depart_date': None, 'flexible_start': '2099-03-01',
                              'flexible_end': '2099-03-31', 'return_date': '2099-04-05'})
        self.assertIsNone(self.constraints()['depart_date'])
        self.assertEqual(self.constraints()['flexible_end'], '2099-03-31')

    def test_optional_stack_shown_once_and_skip_all_reaches_planning(self):
        stack = self.start(COMPLETE)
        self.assertEqual(stack['type'], 'optional_cards')
        fields = [c['field'] for c in stack['cards']]
        self.assertEqual(fields, ['max_connections', 'activities', 'hotel_preferences'])
        self.assertIsNone(self.answer({'answers': {}}))  # no second stack: the graph finished
        state = self.graph.get_state(self.config).values
        self.assertEqual(state['skipped'], fields)
        self.assertEqual(state['messages'][-1].content, 'Your trip details are ready for planning.')

    def test_mixed_optional_answers_make_one_ai_call(self):
        self.start(COMPLETE)
        self.ai.reset_mock()
        done = self.answer({'answers': {'max_connections': {'value': 0},
                                        'activities': {'text': 'museums and pizza'}}},
                           ai_patch={'activities': ['museums', 'food']})
        self.assertIsNone(done)
        self.assertEqual(self.ai.call_count, 1)
        self.assertEqual(self.constraints()['max_connections'], 0)
        self.assertEqual(self.constraints()['activities'], ['museums', 'food'])
        self.assertEqual(self.graph.get_state(self.config).values['skipped'], ['hotel_preferences'])

    def test_only_depart_date_allows_text(self):
        card = self.start({k: v for k, v in COMPLETE.items() if k not in ('depart_date', 'return_date')})
        self.assertEqual(card['card']['input'], 'date_or_range')
        self.assertTrue(card['card']['allow_text'])
        pickers = [f for f, q in QUESTIONS.items() if q['input'] != 'text' and f != 'depart_date']
        self.assertTrue(pickers)
        for field in pickers:
            self.assertNotIn('allow_text', QUESTIONS[field], field)

    def test_driving_trip_has_no_layover_card(self):
        stack = self.start({**COMPLETE, 'mode': 'drive'})
        self.assertEqual([c['field'] for c in stack['cards']], ['activities', 'hotel_preferences'])

    def test_no_rental_car_or_driver_age_card(self):
        self.assertNotIn('needs_car', QUESTIONS)
        self.assertNotIn('driver_age', QUESTIONS)
        seen = []
        pause = self.start({'origin': 'Chicago', 'destination': 'Nashville'})
        answers = {'mode': {'value': 'drive'}, 'trip_type': {'value': 'round_trip'},
                   'budget': {'value': 800}, 'travelers': {'value': 4},
                   'depart_date': {'value': '2099-11-20'}, 'return_date': {'value': '2099-11-23'}}
        while pause:
            cards = [pause['card']] if pause['type'] == 'required_question' else pause['cards']
            seen += [c['field'] for c in cards]
            resume = (answers[pause['card']['field']] if pause['type'] == 'required_question'
                      else {'answers': {}})
            pause = self.answer(resume)
        self.assertNotIn('needs_car', seen)
        self.assertNotIn('driver_age', seen)
        self.assertEqual(self.constraints()['mode'], 'drive')

    def test_drift_guard(self):
        for field in TripConstraints.model_fields:
            self.assertIn(field, EXTRACT_PROMPT, field)
        for field in REQUIRED + ['depart_date', 'return_date', 'trip_nights']:
            self.assertIn(field, QUESTIONS, field)
            self.assertIn(field, ERROR_TEXT, field)

    def test_flexible_round_trip_requires_nights_not_return(self):
        self.assertEqual(compute_missing(TripConstraints(**FLEXIBLE)), ['trip_nights'])

    # trip length: nights with a departure window, nights or a return date with an exact date (issue #24)

    def test_window_with_nights_in_first_request_asks_no_return_or_nights_card(self):
        # the issue's example: "Chicago to Tampa in December, stay 2 nights" is one AI call, then the mode card
        card = self.start({'origin': 'Chicago', 'destination': 'Tampa', 'flexible_start': '2099-12-01',
                           'flexible_end': '2099-12-31', 'trip_nights': 2})
        self.assertEqual(card['card']['field'], 'mode')
        self.assertEqual(self.constraints()['trip_nights'], 2)
        self.assertIsNone(self.constraints()['return_date'])
        answers = {'mode': {'value': 'fly'}, 'trip_type': {'value': 'round_trip'},
                   'budget': {'value': 500}, 'travelers': {'value': 1}}
        seen = []
        while card['type'] == 'required_question':
            seen.append(card['card']['field'])
            card = self.answer(answers[card['card']['field']])
        self.assertEqual(card['type'], 'optional_cards')
        self.assertEqual(seen, ['mode', 'trip_type', 'budget', 'travelers'])  # no return or nights card
        self.assertEqual(self.ai.call_count, 1)

    def test_flexible_round_trip_without_nights_asks_numeric_nights_card(self):
        self.assertEqual(self.start(FLEXIBLE), {
            'type': 'required_question',
            'card': {'field': 'trip_nights', 'text': 'How many nights do you want to stay?',
                     'input': 'number', 'min': 1, 'max': 30},
            'error': None})

    def test_nights_fixed_answer_saved_without_ai(self):
        self.start(FLEXIBLE)
        self.ai.reset_mock()
        stack = self.answer({'value': 3})
        self.ai.assert_not_called()
        self.assertEqual(self.constraints()['trip_nights'], 3)
        self.assertIsNone(self.constraints()['return_date'])
        self.assertEqual(stack['type'], 'optional_cards')  # intake carries on as usual

    def test_exact_date_with_nights_fills_return_date_without_return_card(self):
        stack = self.start({**{k: v for k, v in COMPLETE.items() if k != 'return_date'}, 'trip_nights': 4})
        self.assertEqual(stack['type'], 'optional_cards')  # the return card is skipped
        self.assertEqual(self.constraints()['return_date'], '2099-01-05')
        self.assertEqual(self.constraints()['trip_nights'], 4)

    def test_exact_date_with_return_date_fills_nights(self):
        self.start(COMPLETE)  # 2099-01-01 to 2099-01-05
        self.assertEqual(self.constraints()['trip_nights'], 4)

    def test_conflicting_return_date_and_nights_keep_return_date(self):
        self.start({**COMPLETE, 'trip_nights': 2})
        self.assertEqual(self.constraints()['return_date'], '2099-01-05')
        self.assertEqual(self.constraints()['trip_nights'], 4)  # recalculated from the return date

    def test_new_nights_or_return_date_replaces_the_derived_one(self):
        self.start(COMPLETE)  # return 2099-01-05, so 4 nights
        self.answer({'answers': {'activities': {'text': 'hiking, and make it 6 nights'}}},
                    ai_patch={'activities': ['hiking'], 'trip_nights': 6})
        self.assertEqual(self.constraints()['trip_nights'], 6)
        self.assertEqual(self.constraints()['return_date'], '2099-01-07')  # derived again, not kept
        current = TripConstraints(**self.constraints())
        self.assertEqual(merge_answers(current, {'return_date': '2099-01-03'}).trip_nights, 2)

    def test_window_with_return_date_clears_it_and_asks_nights(self):
        card = self.start({**FLEXIBLE, 'return_date': '2099-04-05'})
        self.assertIsNone(self.constraints()['return_date'])
        self.assertEqual(self.constraints()['flexible_end'], '2099-03-31')
        self.assertEqual(card['card']['field'], 'trip_nights')

    def test_legacy_window_state_with_return_date_is_normalized_on_load(self):
        legacy = TripConstraints(**{**FLEXIBLE, 'return_date': '2099-04-05', 'trip_nights': 2})
        self.assertIsNone(legacy.return_date)
        self.assertEqual(legacy.trip_nights, 2)
        self.assertEqual(compute_missing(legacy), [])

    def test_invalid_nights_reshow_card_with_error(self):
        self.start(FLEXIBLE)
        for bad in (0, 31):
            with self.subTest(nights=bad):
                card = self.answer({'value': bad})
                self.assertEqual(card['card']['field'], 'trip_nights')
                self.assertEqual(card['error'], 'Enter a number of nights from 1 to 30.')
                self.assertIsNone(self.constraints()['trip_nights'])
        stack = self.answer({'value': 30})
        self.assertEqual(stack['type'], 'optional_cards')  # error cleared, intake carries on
        self.assertEqual(self.constraints()['trip_nights'], 30)
        self.ai.assert_called_once()  # only the first request

    def test_return_date_outside_1_to_30_nights_is_kept_without_nights(self):
        # Exact round trips of any length were valid before trip_nights existed and still are
        for return_date, nights in (('2099-01-01', None), ('2099-02-01', None), ('2099-01-31', 30)):
            with self.subTest(return_date=return_date):
                self.new_thread(return_date)
                self.start({k: v for k, v in COMPLETE.items() if k != 'return_date'})
                stack = self.answer({'value': return_date})
                self.assertEqual(stack['type'], 'optional_cards')  # accepted, no return card again
                self.assertEqual(self.constraints()['return_date'], return_date)
                self.assertEqual(self.constraints()['trip_nights'], nights)
        self.new_thread('first-request')
        stack = self.start({**COMPLETE, 'return_date': '2099-02-15'})  # 45 nights, typed in the first request
        self.assertEqual(stack['type'], 'optional_cards')
        self.assertEqual(self.constraints()['return_date'], '2099-02-15')
        self.assertIsNone(self.constraints()['trip_nights'])
        legacy = TripConstraints(**{**COMPLETE, 'return_date': '2099-02-15'})  # a saved 45-night trip still loads
        self.assertEqual((legacy.return_date.isoformat(), legacy.trip_nights), ('2099-02-15', None))

    def test_return_before_departure_still_reshows_return_card(self):
        self.start({k: v for k, v in COMPLETE.items() if k != 'return_date'})
        card = self.answer({'value': '2098-12-31'})
        self.assertEqual(card['card']['field'], 'return_date')
        self.assertEqual(card['card']['min'], '2099-01-01')
        self.assertEqual(card['error'], ERROR_TEXT['return_date'])

    def test_ai_return_date_with_a_window_keeps_saved_nights(self):
        self.start({**FLEXIBLE, 'trip_nights': 3})
        done = self.answer({'answers': {'activities': {'text': 'hiking, back by the 20th'}}},
                           ai_patch={'activities': ['hiking'], 'return_date': '2099-03-20'})
        self.assertIsNone(done)  # the nights card is not asked again
        self.assertIsNone(self.constraints()['return_date'])
        self.assertEqual(self.constraints()['trip_nights'], 3)

    def test_switching_exact_date_to_window_keeps_nights_and_drops_return(self):
        self.start({k: v for k, v in COMPLETE.items() if k != 'origin'})  # return 2099-01-05, so 4 nights
        stack = self.answer({'text': 'Chicago, and actually sometime in march'},
                            ai_patch={'origin': 'Chicago', 'depart_date': None, 'return_date': None,
                                      'flexible_start': '2099-03-01', 'flexible_end': '2099-03-31'})
        self.assertEqual(stack['type'], 'optional_cards')  # the 4 nights carry over, so no nights card
        self.assertEqual(self.constraints()['trip_nights'], 4)
        self.assertIsNone(self.constraints()['return_date'])

    def test_switching_window_to_exact_date_derives_return_from_saved_nights(self):
        self.start({**FLEXIBLE, 'trip_nights': 3})
        done = self.answer({'answers': {'hotel_preferences': {'text': 'cheap; and I will leave March 5'}}},
                           ai_patch={'hotel_preferences': 'cheap', 'depart_date': '2099-03-05',
                                     'flexible_start': None, 'flexible_end': None})
        self.assertIsNone(done)
        self.assertEqual(self.constraints()['depart_date'], '2099-03-05')
        self.assertEqual(self.constraints()['return_date'], '2099-03-08')
        self.assertEqual(self.constraints()['trip_nights'], 3)

    def test_one_way_trip_asks_no_nights_or_return(self):
        exact = {**{k: v for k, v in COMPLETE.items() if k != 'return_date'}, 'trip_type': 'one_way'}
        window = {**FLEXIBLE, 'trip_type': 'one_way'}
        for tag, trip in (('exact', exact), ('window', window)):
            with self.subTest(departure=tag):
                self.new_thread(tag)
                self.assertEqual(compute_missing(TripConstraints(**trip)), [])
                self.assertEqual(self.start(trip)['type'], 'optional_cards')

    def test_return_card_never_asks_latest_date_back(self):
        card = build_card('return_date', TripConstraints(**FLEXIBLE))
        self.assertEqual(card['text'], 'When do you want to come back?')
        self.assertNotIn('min', card)
        self.assertEqual(build_card('return_date', TripConstraints(**COMPLETE))['min'], '2099-01-01')
        self.assertNotIn('latest', json.dumps(QUESTIONS).lower())

    def test_prompt_extracts_nights_and_leaves_date_math_to_code(self):
        self.assertIn('"Chicago to Tampa in december, stay 2 nights"', EXTRACT_PROMPT)
        self.assertIn('"flexible_end": "2026-12-31", "trip_nights": 2}', EXTRACT_PROMPT)
        self.assertIn('"for a week" -> 7', EXTRACT_PROMPT)
        self.assertNotIn('depart_date + 4 days', EXTRACT_PROMPT)  # the AI no longer computes return dates

    def test_oauth_transport(self):
        from services.agent.llm import generate
        from types import SimpleNamespace
        with patch('services.agent.llm.get_access_token', return_value='mock-token'), \
             patch('services.agent.llm.OpenAI') as client:
            client.return_value.__enter__.return_value.responses.create.return_value.__enter__.return_value = iter([
                SimpleNamespace(type='response.output_text.delta', delta='{}'),
                SimpleNamespace(type='response.completed')])
            self.assertEqual(generate('mock', 'parse', 'hello'), '{}')
            self.assertEqual(client.call_args.kwargs['api_key'], 'mock-token')

if __name__ == '__main__':
    unittest.main()
