import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

# The intake looks cities up in the database (db.database needs these settings); every
# test points it at an in-memory database of hand-made places instead.
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import anthropic
import httpx
from langgraph.types import Command
from sqlalchemy.orm import sessionmaker
from location_fixtures import DALLAS_FORT_WORTH_MARKET, GREENVILLE_SC_MARKET, ST_LOUIS_MARKET, location_engine
from services.agent import llm
from services.agent.graph import build_graph, parse_line, start_state
from services.agent.nodes.clarify import (COULD_NOT_UNDERSTAND, ERROR_TEXT, PLACE_NOT_FOUND, QUESTIONS,
                                          build_card, merge_answers)
from services.agent.nodes.extract import parse_json_object
from services.agent.prompts import EXTRACT_PROMPT
from services.agent.state import REQUIRED, TripConstraints, compute_missing
from services.location_service import LocationDataMissing

COMPLETE = dict(origin='Chicago', destination='New York', mode='fly',
                trip_type='round_trip', depart_date='2099-01-01',
                return_date='2099-01-05', budget=300, travelers=2)
# The same round trip with a departure window instead of exact dates, and no trip length
FLEXIBLE = {**{k: v for k, v in COMPLETE.items() if k not in ('depart_date', 'return_date')},
            'flexible_start': '2099-03-01', 'flexible_end': '2099-03-31'}


def date_missing(c: TripConstraints) -> list[str]:
    """compute_missing without the place cards: these tests build TripConstraints
    directly, without the lookup that fills origin_place/destination_place."""
    return [f for f in compute_missing(c) if f not in ('origin_place', 'destination_place')]


class TripLoopTests(unittest.TestCase):
    def setUp(self):
        # Only the AI call in extract is mocked; each test queues the patches it returns
        patcher = patch('services.agent.nodes.extract.generate')
        self.ai = patcher.start()
        self.addCleanup(patcher.stop)
        self.engine = location_engine()
        self.addCleanup(self.engine.dispose)
        places = patch('services.agent.nodes.extract.SessionLocal', sessionmaker(bind=self.engine))
        places.start()
        self.addCleanup(places.stop)
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
        self.start({k: v for k, v in COMPLETE.items() if k not in ('origin', 'return_date')})
        self.assertEqual(self.constraints()['depart_date'], '2099-01-01')
        self.answer({'text': 'Chicago, and actually sometime in march'},
                    ai_patch={'origin': 'Chicago', 'depart_date': None, 'flexible_start': '2099-03-01',
                              'flexible_end': '2099-03-31'})
        self.assertIsNone(self.constraints()['depart_date'])
        self.assertEqual(self.constraints()['flexible_end'], '2099-03-31')

    # each card accepts only its own kind of answer

    def test_typed_text_on_picker_card_is_refused_without_ai(self):
        self.start({k: v for k, v in COMPLETE.items() if k != 'travelers'})
        self.ai.reset_mock()
        card = self.answer({'text': 'me and 3 friends'})
        self.ai.assert_not_called()
        self.assertEqual(card['card']['field'], 'travelers')
        self.assertEqual(card['error'], ERROR_TEXT['travelers'])
        self.answer({'value': 4})  # the chat isn't stuck: a correct answer still works
        self.assertEqual(self.constraints()['travelers'], 4)

    def test_value_on_text_card_is_refused(self):
        card = self.start({'destination': 'Denver'})
        self.assertEqual(card['card']['field'], 'origin')
        card = self.answer({'value': "O'Hare"})
        self.assertEqual(card['card']['field'], 'origin')
        self.assertEqual(card['error'], ERROR_TEXT['origin'])
        self.assertIsNone(self.constraints()['origin'])

    def test_depart_date_card_accepts_both_kinds(self):
        partial = {k: v for k, v in COMPLETE.items() if k not in ('depart_date', 'return_date')}
        self.start(partial)
        card = self.answer({'value': '2099-01-01'})
        self.assertEqual(card['card']['field'], 'return_date')
        self.setUp()
        self.start(partial)
        card = self.answer({'text': 'early january'},
                           ai_patch={'flexible_start': '2099-01-01', 'flexible_end': '2099-01-10'})
        self.assertEqual(card['card']['field'], 'trip_nights')  # a window asks for the length

    def test_wrong_kind_in_optional_stack_counts_as_skipped(self):
        self.start(COMPLETE)
        self.ai.reset_mock()
        done = self.answer({'answers': {'max_connections': {'text': 'nonstop please'}}})
        self.assertIsNone(done)
        self.ai.assert_not_called()
        self.assertIsNone(self.constraints()['max_connections'])
        self.assertIn('max_connections', self.graph.get_state(self.config).values['skipped'])

    # error text for typed answers

    def test_understood_but_invalid_typed_answer_shows_card_error(self):
        self.start({k: v for k, v in COMPLETE.items() if k not in ('depart_date', 'return_date')})
        card = self.answer({'text': 'last january'},
                           ai_patch={'flexible_start': '2020-01-01', 'flexible_end': '2020-01-31'})
        self.assertEqual(card['card']['field'], 'depart_date')
        self.assertEqual(card['error'], ERROR_TEXT['depart_date'])  # not "couldn't understand"

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

    def test_only_depart_date_and_place_cards_allow_text(self):
        card = self.start({k: v for k, v in COMPLETE.items() if k not in ('depart_date', 'return_date')})
        self.assertEqual(card['card']['input'], 'date_or_range')
        self.assertTrue(card['card']['allow_text'])
        typed_ok = ('depart_date', 'origin_place', 'destination_place')  # "I meant Greenville, Texas"
        pickers = [f for f, q in QUESTIONS.items() if q['input'] != 'text' and f not in typed_ok]
        self.assertTrue(pickers)
        for field in pickers:
            self.assertNotIn('allow_text', QUESTIONS[field], field)
        for field in typed_ok[1:]:
            self.assertTrue(QUESTIONS[field]['allow_text'], field)

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
        self.assertEqual(date_missing(TripConstraints(**FLEXIBLE)), ['trip_nights'])

    # trip length: nights with a departure window, nights or a return date with an exact date (issue #24)

    def test_window_with_nights_in_first_request_asks_no_return_or_nights_card(self):
        # the issue's example: "Chicago to Tampa in December, stay 2 nights" is one AI call, then the mode card
        # (Denver stands in for Tampa: the test location data has no Tampa)
        card = self.start({'origin': 'Chicago', 'destination': 'Denver', 'flexible_start': '2099-12-01',
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
        self.assertEqual(date_missing(legacy), [])

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
                self.assertEqual(date_missing(TripConstraints(**trip)), [])
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

    # Places: origin and destination are resolved once during intake

    def test_unique_city_is_saved_without_a_card(self):
        stack = self.start({**COMPLETE, 'origin': 'Chicago', 'destination': 'Fort Worth'})
        self.assertEqual(stack['type'], 'optional_cards')  # no place card was needed
        place = self.constraints()['destination_place']
        self.assertEqual(place['id'], 'us-tx-fort-worth')
        self.assertEqual((place['city'], place['state']), ('Fort Worth', 'TX'))
        self.assertEqual(place['airports'], ['FTW', 'DFW', 'DAL'])
        self.assertEqual(place['dot_market_id'], DALLAS_FORT_WORTH_MARKET)
        self.assertEqual(set(place), {'id', 'city', 'state', 'latitude', 'longitude', 'airports', 'dot_market_id'})
        self.assertEqual(self.constraints()['origin_place']['id'], 'us-il-chicago')
        self.assertEqual(self.constraints()['destination'], 'Fort Worth')  # the typed text is kept

    def test_state_given_with_the_city_resolves_directly(self):
        self.start({**COMPLETE, 'destination': 'Greenville, SC'})
        self.assertEqual(self.constraints()['destination_place']['id'], 'us-sc-greenville')

    def test_ambiguous_city_asks_which_one(self):
        card = self.start({**COMPLETE, 'destination': 'Greenville'})
        self.assertEqual(card['type'], 'required_question')
        self.assertIsNone(card['error'])
        self.assertEqual(card['card']['field'], 'destination_place')
        self.assertEqual(card['card']['text'], 'Which Greenville do you mean?')
        self.assertEqual(card['card']['input'], 'choice')
        self.assertTrue(card['card']['allow_text'])
        self.assertEqual(card['card']['options'], [  # biggest first, at most five
            {'value': 'us-nc-greenville', 'label': 'Greenville, NC'},
            {'value': 'us-sc-greenville', 'label': 'Greenville, SC'},
            {'value': 'us-tx-greenville', 'label': 'Greenville, TX'},
            {'value': 'us-ms-greenville', 'label': 'Greenville, MS'},
            {'value': 'us-wi-greenville', 'label': 'Greenville, WI'}])
        self.assertIsNone(self.constraints()['destination_place'])

    def test_much_bigger_match_still_asks(self):
        # Several matches always ask, as issue #21 says, even when one is far bigger:
        # Charlotte, NC (904k) vs Charlotte, MI (9k), St. Louis, MO (289k) vs MI (7k)
        card = self.start({**COMPLETE, 'origin': 'Charlotte', 'destination': 'Saint Louis'})
        self.assertEqual(card['card']['field'], 'origin_place')
        self.assertEqual(card['card']['options'], [{'value': 'us-nc-charlotte', 'label': 'Charlotte, NC'},
                                                   {'value': 'us-mi-charlotte', 'label': 'Charlotte, MI'}])
        card = self.answer({'value': 'us-nc-charlotte'})
        self.assertEqual(card['card']['text'], 'Which St. Louis do you mean?')
        self.assertEqual([o['value'] for o in card['card']['options']], ['us-mo-saint-louis', 'us-mi-saint-louis'])
        self.answer({'value': 'us-mo-saint-louis'})
        self.assertEqual(self.constraints()['origin_place']['id'], 'us-nc-charlotte')
        self.assertEqual(self.constraints()['destination_place']['dot_market_id'], ST_LOUIS_MARKET)

    def test_atlantis_is_a_real_town(self):
        stack = self.start({**COMPLETE, 'destination': 'Atlantis'})
        self.assertEqual(stack['type'], 'optional_cards')  # one match: saved, no error
        self.assertEqual(self.constraints()['destination_place']['id'], 'us-fl-atlantis')

    def test_picking_a_place_saves_it_without_ai(self):
        self.start({**COMPLETE, 'destination': 'Greenville'})
        self.ai.reset_mock()
        stack = self.answer({'value': 'us-sc-greenville'})
        self.ai.assert_not_called()
        self.assertEqual(stack['type'], 'optional_cards')
        place = self.constraints()['destination_place']
        self.assertEqual((place['id'], place['airports'], place['dot_market_id']),
                         ('us-sc-greenville', ['GSP'], GREENVILLE_SC_MARKET))
        self.assertEqual(self.constraints()['destination'], 'Greenville')
        self.assertNotIn('destination_place', self.graph.get_state(self.config).values['place_choices'])

    def test_same_state_choices_are_distinct_and_save_the_selected_entity(self):
        card = self.start({**COMPLETE, 'destination': 'Greenville, NY'})
        self.assertEqual(card['card']['text'], 'Which Greenville do you mean?')
        self.assertEqual(card['card']['options'], [
            {'value': 'us-ny-greenville-3630642', 'label': 'Greenville (CDP; Westchester County), NY'},
            {'value': 'us-ny-greenville-3630609', 'label': 'Greenville (CDP; Greene County), NY'}])
        self.ai.reset_mock()
        rejected = self.answer({'value': 'us-ny-greenville'})
        self.assertEqual(rejected['error'], ERROR_TEXT['destination_place'])
        self.answer({'value': 'us-ny-greenville-3630609'})
        self.ai.assert_not_called()
        place = self.constraints()['destination_place']
        self.assertEqual((place['id'], place['latitude'], place['airports'], place['dot_market_id']),
                         ('us-ny-greenville-3630609', 42.413789, [], None))

    def test_same_state_origin_card_distinguishes_city_and_village(self):
        card = self.start({**COMPLETE, 'origin': 'Waukesha, WI'})
        self.assertEqual(card['card']['field'], 'origin_place')
        self.assertEqual(card['card']['text'], 'Which Waukesha do you mean?')
        self.assertEqual([o['label'] for o in card['card']['options']],
                         ['Waukesha (city; Waukesha County), WI', 'Waukesha (village; Waukesha County), WI'])
        self.ai.reset_mock()
        self.answer({'value': 'us-wi-waukesha-5584275'})
        self.ai.assert_not_called()
        self.assertEqual(self.constraints()['origin_place']['latitude'], 42.960422)

    def test_typed_qualified_city_replaces_the_previous_same_name_place(self):
        self.start({**COMPLETE, 'destination': 'Greenville, NY'})
        self.answer({'value': 'us-ny-greenville-3630609'})
        self.answer({'answers': {'activities': {'text': 'actually the Westchester one'}}},
                    ai_patch={'destination': 'Greenville (CDP; Westchester County), NY'})
        self.assertEqual(self.constraints()['destination_place']['id'], 'us-ny-greenville-3630642')
        self.assertEqual(self.constraints()['destination_place']['dot_market_id'], 31703)

    def test_place_id_not_on_the_card_is_rejected(self):
        self.start({**COMPLETE, 'destination': 'Greenville'})
        self.ai.reset_mock()
        # A real place, but not one of this card's options (the 6th Greenville, then Chicago)
        for value in ('us-oh-greenville', 'us-il-chicago', {'id': 'us-sc-greenville'}):
            with self.subTest(value=value):
                card = self.answer({'value': value})
                self.assertEqual(card['card']['field'], 'destination_place')
                self.assertEqual(card['error'], ERROR_TEXT['destination_place'])
                self.assertEqual(len(card['card']['options']), 5)
                self.assertIsNone(self.constraints()['destination_place'])
        self.ai.assert_not_called()

    def test_typing_another_city_on_the_place_card(self):
        self.start({**COMPLETE, 'destination': 'Greenville'})
        self.ai.reset_mock()
        stack = self.answer({'text': 'oh, I meant Greenville, Texas'}, ai_patch={'destination': 'Greenville, TX'})
        self.assertEqual(self.ai.call_count, 1)
        sent = json.loads(self.ai.call_args.args[2])
        self.assertEqual(sent['answers'][0]['field'], 'destination_place')
        self.assertEqual(sent['answers'][0]['question'], 'Which Greenville do you mean?')
        self.assertEqual(stack['type'], 'optional_cards')
        self.assertEqual(self.constraints()['destination_place']['id'], 'us-tx-greenville')
        self.assertEqual(self.constraints()['destination'], 'Greenville, TX')

    def test_typed_city_that_is_still_ambiguous_gets_the_card_error(self):
        self.start({**COMPLETE, 'destination': 'Greenville'})
        card = self.answer({'text': 'Portland actually'}, ai_patch={'destination': 'Portland'})
        self.assertEqual(card['card']['text'], 'Which Portland do you mean?')
        self.assertEqual([o['label'] for o in card['card']['options']], ['Portland, OR', 'Portland, ME'])
        self.assertEqual(card['error'], ERROR_TEXT['destination_place'])
        card = self.answer({'text': 'hmm'}, ai_patch={})
        self.assertEqual(card['card']['text'], 'Which Portland do you mean?')
        self.assertEqual(card['error'], COULD_NOT_UNDERSTAND)

    def test_unknown_city_shows_the_city_card_with_the_us_only_error(self):
        card = self.start({**COMPLETE, 'destination': 'Narnia'})
        self.assertEqual(card['card']['field'], 'destination')
        self.assertEqual(card['card']['input'], 'text')
        self.assertEqual(card['error'], PLACE_NOT_FOUND)
        self.assertIn('in the US', card['error'])
        self.assertIn('"Greenville, SC"', card['error'])
        self.assertIsNone(self.constraints()['destination'])
        card = self.answer({'text': 'Denver'}, ai_patch={'destination': 'Denver'})
        self.assertEqual(card['type'], 'optional_cards')
        self.assertEqual(self.constraints()['destination_place']['id'], 'us-co-denver')

    def test_unknown_city_is_asked_before_other_missing_fields(self):
        card = self.start({'destination': 'Narnia'})  # origin not given yet
        self.assertEqual(card['card']['field'], 'destination')
        self.assertEqual(card['error'], PLACE_NOT_FOUND)
        card = self.answer({'text': 'Denver'}, ai_patch={'destination': 'Denver'})
        self.assertEqual(card['card']['field'], 'origin')
        self.assertIsNone(card['error'])

    # A typed answer can change the city (here on the activities text card; picker cards
    # such as travelers only take a value, so they never reach the AI)

    def test_changing_the_city_clears_the_old_place(self):
        self.start({**COMPLETE, 'destination': 'Denver'})
        self.assertEqual(self.constraints()['destination_place']['id'], 'us-co-denver')
        # "hiking, and make it Greenville": the new city is ambiguous, so Denver's place must go
        card = self.answer({'answers': {'activities': {'text': 'hiking, in Greenville instead'}}},
                           ai_patch={'activities': ['hiking'], 'destination': 'Greenville'})
        self.assertIsNone(self.constraints()['destination_place'])
        self.assertEqual(card['card']['field'], 'destination_place')
        self.answer({'value': 'us-sc-greenville'})
        self.assertEqual(self.constraints()['destination_place']['id'], 'us-sc-greenville')

    def test_changing_to_a_unique_city_replaces_the_place(self):
        self.start({**COMPLETE, 'destination': 'Denver'})
        self.answer({'answers': {'activities': {'text': 'hiking, going to Fort Worth'}}},
                    ai_patch={'activities': ['hiking'], 'destination': 'Fort Worth'})
        self.assertEqual(self.constraints()['destination_place']['id'], 'us-tx-fort-worth')
        self.assertEqual(self.constraints()['destination_place']['airports'], ['FTW', 'DFW', 'DAL'])

    def test_same_city_written_differently_keeps_the_picked_place(self):
        self.start({**COMPLETE, 'destination': 'Greenville'})
        self.answer({'value': 'us-sc-greenville'})
        self.answer({'answers': {'activities': {'text': 'hiking in greenville'}}},
                    ai_patch={'activities': ['hiking'], 'destination': 'greenville'})
        self.assertEqual(self.ai.call_count, 2)  # the typed answer did reach the AI
        self.assertEqual(self.constraints()['destination_place']['id'], 'us-sc-greenville')

    def test_ai_cannot_set_place_fields(self):
        fake = {'id': 'us-xx-fake', 'city': 'Fake', 'state': 'XX', 'latitude': 0, 'longitude': 0,
                'airports': ['XXX'], 'dot_market_id': 1}
        self.start({**COMPLETE, 'destination': 'Denver', 'destination_place': fake, 'origin_place': fake})
        self.assertEqual(self.constraints()['destination_place']['id'], 'us-co-denver')
        self.assertEqual(self.constraints()['origin_place']['id'], 'us-il-chicago')
        # The AI never sees the place fields: not in the schema, not in the constraints
        self.answer({'answers': {'activities': {'text': 'hiking'}}},
                    ai_patch={'activities': ['hiking'], 'destination_place': fake})
        sent = json.loads(self.ai.call_args.args[2])
        for field in ('origin_place', 'destination_place'):
            self.assertNotIn(field, sent['schema']['properties'])
            self.assertNotIn(field, sent['constraints'])
        self.assertNotIn('$defs', sent['schema'])
        self.assertEqual(self.constraints()['destination_place']['id'], 'us-co-denver')

    def test_origin_place_card_comes_right_after_the_origin(self):
        card = self.start({**COMPLETE, 'origin': 'Greenville', 'destination': 'Portland'})
        self.assertEqual(card['card']['field'], 'origin_place')
        card = self.answer({'value': 'us-nc-greenville'})
        self.assertEqual(card['card']['field'], 'destination_place')
        self.assertEqual(card['card']['text'], 'Which Portland do you mean?')
        self.assertIsNone(card['error'])
        self.answer({'value': 'us-me-portland'})
        self.assertEqual(self.constraints()['origin_place']['id'], 'us-nc-greenville')
        self.assertEqual(self.constraints()['destination_place']['id'], 'us-me-portland')

    def test_missing_place_comes_right_after_its_city(self):
        missing = compute_missing(TripConstraints(origin='Greenville', destination='Portland'))
        self.assertEqual(missing[:3], ['origin_place', 'destination_place', 'mode'])
        missing = compute_missing(TripConstraints(destination='Portland'))
        self.assertEqual(missing[:3], ['origin', 'destination_place', 'mode'])

    def test_driving_trip_keeps_a_place_without_airports(self):
        stack = self.start({**COMPLETE, 'mode': 'drive', 'destination': 'Jordan, MT'})
        self.assertEqual([c['field'] for c in stack['cards']], ['activities', 'hotel_preferences'])
        place = self.constraints()['destination_place']
        self.assertEqual((place['id'], place['airports'], place['dot_market_id']), ('us-mt-jordan', [], None))
        self.assertIsNone(self.answer({'answers': {}}))  # intake still finishes

    # Planning nodes after intake (stubs for now; see the contracts in state.py)

    def test_flying_trip_runs_all_planning_nodes(self):
        self.start(COMPLETE)
        self.assertIsNone(self.answer({'answers': {}}))
        values = self.graph.get_state(self.config).values
        for key in ('flight_trends', 'flight_offers', 'hotel_options'):
            self.assertEqual(values[key]['status'], 'empty', key)
        self.assertEqual(values['hotel_options']['checkin'], COMPLETE['depart_date'])
        self.assertEqual(values['messages'][-1].content, 'Your trip details are ready for planning.')

    def test_driving_trip_skips_the_flight_nodes(self):
        self.start({**COMPLETE, 'mode': 'drive'})
        self.assertIsNone(self.answer({'answers': {}}))
        values = self.graph.get_state(self.config).values
        self.assertNotIn('flight_trends', values)
        self.assertNotIn('flight_offers', values)
        self.assertEqual(values['hotel_options']['status'], 'empty')

    def test_empty_location_tables_are_reported(self):
        empty = location_engine(filled=False)
        self.addCleanup(empty.dispose)
        with patch('services.agent.nodes.extract.SessionLocal', sessionmaker(bind=empty)), \
             self.assertRaises(LocationDataMissing):
            self.start(COMPLETE)

    # fenced or wrapped JSON from the AI

    def test_fenced_json_reply_is_saved(self):
        self.ai.side_effect = ['```json\n{"origin": "Chicago", "destination": "Denver"}\n```']
        card = self._pause(self.graph.invoke(start_state('Chicago to Denver', 'mock'), self.config))
        self.assertEqual(self.constraints()['origin'], 'Chicago')
        self.assertEqual(card['card']['field'], 'mode')  # origin and destination aren't asked

    def test_text_around_json_reply_is_saved(self):
        self.start({'destination': 'Denver'})
        self.ai.side_effect = ['Here are the details: {"origin": "Chicago"} Hope that helps!']
        self._pause(self.graph.invoke(Command(resume={'text': 'Chicago'}), self.config))
        self.assertEqual(self.constraints()['origin'], 'Chicago')

    def test_reply_without_json_object_saves_nothing(self):
        for reply in ('not json', '["Chicago"]', '{"origin": "Chicago"'):
            with self.subTest(reply=reply):
                self.assertEqual(parse_json_object(reply), {})
        self.start({'destination': 'Denver'})
        self.ai.side_effect = ['["Chicago"]']
        card = self._pause(self.graph.invoke(Command(resume={'text': 'Chicago'}), self.config))
        self.assertEqual(card['card']['field'], 'origin')
        self.assertEqual(card['error'], COULD_NOT_UNDERSTAND)


class DemoParseLineTests(unittest.TestCase):
    """The terminal demo turns lines into answers the way the frontend's widgets would."""

    def test_picker_lines_become_values(self):
        self.assertEqual(parse_line(QUESTIONS['trip_type'], 'Round'), {'value': 'round_trip'})
        self.assertEqual(parse_line(QUESTIONS['mode'], 'fly'), {'value': 'fly'})
        self.assertEqual(parse_line(QUESTIONS['budget'], '$1,200'), {'value': 1200})
        self.assertEqual(parse_line(QUESTIONS['max_connections'], '0'), {'value': 0})
        self.assertEqual(parse_line(QUESTIONS['depart_date'], '2099-03-01 to 2099-03-05'),
                         {'value': {'start': '2099-03-01', 'end': '2099-03-05'}})

    def test_lines_that_dont_fit_a_picker_are_asked_again(self):
        self.assertIsNone(parse_line(QUESTIONS['return_date'], 'dec 15'))
        self.assertIsNone(parse_line(QUESTIONS['travelers'], 'me and 3 friends'))
        self.assertIsNone(parse_line(QUESTIONS['mode'], 'train'))

    def test_text_cards_and_allow_text_send_text(self):
        self.assertEqual(parse_line(QUESTIONS['origin'], "O'Hare"), {'text': "O'Hare"})
        self.assertEqual(parse_line(QUESTIONS['depart_date'], 'sometime in march'),
                         {'text': 'sometime in march'})


class ClaudeCallTests(unittest.TestCase):
    """llm.generate with the Anthropic client faked; never calls the real API."""

    def setUp(self):
        for name, value in {'ANTHROPIC_API_KEY': 'test-key'}.items():
            patcher = patch.object(llm.settings, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch('services.agent.llm.Anthropic')
        self.client_class = patcher.start()
        self.addCleanup(patcher.stop)
        self.create = self.client_class.return_value.messages.create

    def reply(self, *blocks, stop_reason='end_turn'):
        self.create.return_value = SimpleNamespace(content=list(blocks), stop_reason=stop_reason)

    def test_returns_text_and_sends_prompt(self):
        self.reply(SimpleNamespace(type='thinking', thinking=''),
                   SimpleNamespace(type='text', text=' {"origin": "Chicago"} '))
        self.assertEqual(llm.generate('claude-haiku-5-5', EXTRACT_PROMPT, 'context'), '{"origin": "Chicago"}')
        self.assertEqual(self.client_class.call_args.kwargs['api_key'], 'test-key')
        sent = self.create.call_args.kwargs
        self.assertEqual(sent['model'], 'claude-haiku-5-5')
        self.assertIs(sent['system'], EXTRACT_PROMPT)
        self.assertEqual(sent['messages'], [{'role': 'user', 'content': 'context'}])
        self.assertEqual(sent['output_config'], {'effort': 'low'})

    def test_missing_key_fails_before_calling(self):
        with patch.object(llm.settings, 'ANTHROPIC_API_KEY', None):
            with self.assertRaisesRegex(RuntimeError, 'ANTHROPIC_API_KEY'):
                llm.generate('m', 'i', 'c')
        self.create.assert_not_called()

    def test_cut_off_declined_or_empty_reply_raises(self):
        for stop_reason, blocks in (('max_tokens', [SimpleNamespace(type='text', text='{"ori')]),
                                    ('refusal', []), ('end_turn', [])):
            with self.subTest(stop_reason=stop_reason):
                self.reply(*blocks, stop_reason=stop_reason)
                with self.assertRaises(RuntimeError):
                    llm.generate('m', 'i', 'c')

    def test_api_errors_become_short_messages_without_the_key(self):
        request = httpx.Request('POST', 'https://api.anthropic.com/v1/messages')
        response = httpx.Response(401, request=request)
        self.create.side_effect = anthropic.AuthenticationError('bad key', response=response, body=None)
        with self.assertRaises(RuntimeError) as caught:
            llm.generate('m', 'i', 'c')
        self.assertIn('rejected', str(caught.exception))
        self.assertNotIn('test-key', str(caught.exception))

if __name__ == '__main__':
    unittest.main()
