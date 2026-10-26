import json
import unittest
from unittest.mock import patch
from langgraph.types import Command
from services.agent.graph import build_graph, start_state
from services.agent.nodes.clarify import COULD_NOT_UNDERSTAND, ERROR_TEXT, QUESTIONS
from services.agent.prompts import EXTRACT_PROMPT
from services.agent.state import REQUIRED, TripConstraints, compute_missing

COMPLETE = dict(origin='Chicago', destination='New York', mode='fly',
                trip_type='round_trip', depart_date='2099-01-01',
                return_date='2099-01-05', budget=300, travelers=2)


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

    def test_depart_range_sets_window_and_return_card_limits(self):
        self.start({k: v for k, v in COMPLETE.items() if k not in ('depart_date', 'return_date')})
        card = self.answer({'value': {'start': '2099-03-01', 'end': '2099-03-31'}})
        self.assertEqual(self.constraints()['flexible_start'], '2099-03-01')
        self.assertEqual(self.constraints()['flexible_end'], '2099-03-31')
        self.assertIsNone(self.constraints()['depart_date'])
        self.assertEqual(card['card']['field'], 'return_date')
        self.assertEqual(card['card']['text'], "What's the latest date you could be back by?")
        self.assertEqual(card['card']['min'], '2099-03-31')

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
        for field in REQUIRED + ['depart_date', 'return_date']:
            self.assertIn(field, QUESTIONS, field)
            self.assertIn(field, ERROR_TEXT, field)

    def test_flexible_round_trip_requires_return(self):
        constraints = TripConstraints(**{**COMPLETE, 'depart_date': None,
                                        'return_date': None, 'flexible_start': '2099-01-01',
                                        'flexible_end': '2099-01-03'})
        self.assertIn('return_date', compute_missing(constraints))

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
