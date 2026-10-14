import json
import unittest
from unittest.mock import patch
from langchain_core.messages import HumanMessage
from langgraph.types import Command
from services.agent.graph import build_graph
from services.agent.nodes.extract import extract
from services.agent.state import TripConstraints, compute_missing

COMPLETE = dict(origin='Chicago', destination='New York', mode='fly',
                trip_type='round_trip', depart_date='2099-01-01',
                return_date='2099-01-05', budget=300, travelers=2)

class TripLoopTests(unittest.TestCase):
    def test_text_loop_preserves_state_and_corrects_fields(self):
        with patch('services.agent.nodes.extract.generate', side_effect=[
            json.dumps({'origin': 'Chicago', 'budget': 300}),
            json.dumps({**COMPLETE, 'budget': 500})
        ]), patch('services.agent.llm.generate', return_value='Where and when are you going?') as ask:
            graph = build_graph()
            config = {'configurable': {'thread_id': 'test'}}
            first = graph.invoke({'messages': [HumanMessage(content='Chicago, $300')],
                                  'constraints': {}, 'model': 'mock'}, config)
            self.assertIn('__interrupt__', first)
            self.assertEqual(ask.call_count, 1)
            # Reading a paused checkpoint does not trigger another model request.
            self.assertEqual(graph.get_state(config).values['constraints']['origin'], 'Chicago')
            final = graph.invoke(Command(resume='New York, round trip, $500'), config)
            self.assertTrue(final['ready'])
            self.assertEqual(final['constraints']['budget'], 500)
            self.assertEqual(len(final['messages']), 4)
            self.assertNotIn('__interrupt__', final)

    def test_invalid_patch_keeps_previous_values(self):
        state = {'constraints': COMPLETE, 'model': 'mock',
                 'messages': [HumanMessage(content='Budget -1, destination Boston')]}
        with patch('services.agent.nodes.extract.generate', return_value='{"budget": -1, "destination": "Boston"}'):
            result = extract(state)
        self.assertEqual(result['constraints']['budget'], 300)
        self.assertEqual(result['constraints']['destination'], 'Boston')
        self.assertFalse(result['ready'])
        self.assertTrue(result['errors'])

    def test_malformed_json_and_unknown_fields(self):
        for response in ['not json', '[]', '{"unexpected": 1}']:
            with patch('services.agent.nodes.extract.generate', return_value=response):
                result = extract({'constraints': {}, 'model': 'mock', 'messages': []})
            self.assertFalse(result['ready'])
            self.assertTrue(result['errors'])

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
