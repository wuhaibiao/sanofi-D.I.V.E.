import json
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch, sentinel

from helpers import settings
from memsys.aggregation import answer_with_validation
from memsys.aggregate_batch import paged_aggregate
from memsys.config import Models
from memsys.core import MemorySystem
from memsys.evidence_batch import paged_answer
from memsys.fake import HashEmbedder, ScriptedChat
from memsys.llm import ModelError
from memsys.read_retry import ReadTransportChat, is_unavailable_endpoint


def unavailable():
    return ModelError('HTTP429: server_error: Endpoint is unavailable.', status=429)


class ReadTransportRetryTest(unittest.TestCase):
    def test_only_failed_transport_retries_with_the_same_request_and_model(self):
        model = Mock()
        model.config = sentinel.config
        model.chat.side_effect = [unavailable(), sentinel.result]
        wrapper = ReadTransportChat(model)
        messages = [{'role':'user','content':'Use the supplied source.'}]
        tags = {'request':'one'}
        with patch('memsys.read_budget.threading.Event.wait') as sleep:
            answer = wrapper.chat(messages, purpose='answer', max_tokens=4096, json_mode=True, tags=tags)
        self.assertIs(answer, sentinel.result)
        self.assertIs(wrapper.config, sentinel.config)
        self.assertEqual(model.chat.call_count, 2)
        first, second = model.chat.call_args_list
        self.assertEqual(first.args, second.args)
        self.assertEqual({k:v for k,v in first.kwargs.items() if k!='tags'},
                         {k:v for k,v in second.kwargs.items() if k!='tags'})
        self.assertEqual(second.kwargs['tags'], tags)
        self.assertEqual(tags, {'request':'one'})
        sleep.assert_called_once_with(30)
        self.assertEqual(wrapper.budget.events[0]['status'], 429)

    def test_a_successful_refusal_is_never_resampled(self):
        model = Mock()
        model.chat.return_value = 'ANSWER: Insufficient source evidence.'
        wrapper = ReadTransportChat(model)
        with patch('memsys.read_budget.threading.Event.wait') as sleep:
            self.assertEqual(wrapper.chat([], purpose='answer'), model.chat.return_value)
        model.chat.assert_called_once()
        sleep.assert_not_called()
        self.assertEqual(wrapper.retries, [])

    def test_auth_quota_and_content_errors_are_not_endpoint_retries(self):
        for error in (ModelError('Unauthorized', status=401),
                      ModelError('Quota exhausted', status=429),
                      ModelError('Unavailable for legal reasons', status=451),
                      ModelError('HTTP429: server_error: unrelated failure', status=429),
                      ModelError('Invalid calculation')):
            model = Mock()
            model.chat.side_effect = error
            with patch('memsys.read_budget.threading.Event.wait') as sleep:
                with self.assertRaises(ModelError):
                    ReadTransportChat(model).chat([], purpose='answer')
            self.assertEqual(model.chat.call_count, 1)
            sleep.assert_not_called()

    def test_persistent_endpoint_failure_stops_after_one_additional_attempt(self):
        model = Mock()
        model.chat.side_effect = unavailable()
        wrapper = ReadTransportChat(model)
        with patch('memsys.read_budget.threading.Event.wait') as sleep:
            with self.assertRaises(ModelError):
                wrapper.chat([], purpose='answer')
        self.assertEqual(model.chat.call_count, 2)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [30])
        self.assertEqual(len(wrapper.retries), 1)

    def test_repeated_endpoint_failures_can_recover_without_changing_the_request(self):
        model = Mock()
        model.chat.side_effect = [unavailable(), sentinel.result]
        messages = [{'role': 'user', 'content': 'Use the supplied source.'}]
        with patch('memsys.read_budget.threading.Event.wait') as sleep:
            result = ReadTransportChat(model).chat(messages, purpose='answer_review', max_tokens=600)
        self.assertIs(result, sentinel.result)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [30])
        self.assertEqual([call.args[0] for call in model.chat.call_args_list], [messages] * 2)
        self.assertEqual(model.chat.call_args.kwargs['purpose'], 'answer_review')

    def test_provider451_only_retries_the_explicit_unavailable_endpoint(self):
        model = Mock()
        model.chat.side_effect = [ModelError('HTTP451: server_error: Endpoint is unavailable.', status=451),
                                  sentinel.result]
        with patch('memsys.read_budget.threading.Event.wait') as sleep:
            result = ReadTransportChat(model).chat([], purpose='answer_review')
        self.assertIs(result, sentinel.result)
        self.assertEqual(model.chat.call_count, 2)
        sleep.assert_called_once_with(30)

    def test_a_nontransport_failure_after_a_retry_is_not_retried_again(self):
        model = Mock()
        model.chat.side_effect = [unavailable(), ModelError('Unauthorized', status=401)]
        with patch('memsys.read_budget.threading.Event.wait') as sleep:
            with self.assertRaises(ModelError):
                ReadTransportChat(model).chat([], purpose='answer')
        self.assertEqual(model.chat.call_count, 2)
        sleep.assert_called_once_with(30)

    def test_memory_response_records_retry_without_mutating_shared_models(self):
        calls = []
        def answer(messages):
            calls.append(messages)
            if len(calls)==1:
                raise unavailable()
            return 'ANSWER: copper'
        model = ScriptedChat({'answer':answer})
        system = MemorySystem(Models(chat=model, memory=None, embedder=HashEmbedder(), embedding_name='hash'),
                              settings(query_analysis=False, semantic_validation=False))
        self.addCleanup(system.close)
        at = datetime(2025,5,1,10)
        handle = system.open_session('u', at)
        system.add_turn(handle, 'user', 'I chose copper as the color.', at)
        system.close_session(handle, at)
        with patch('memsys.read_budget.threading.Event.wait'):
            result = system.respond('u', 'What color did I choose?', datetime(2025,5,2,10))
        self.assertEqual(result.text, 'copper')
        self.assertIs(system.models.chat, model)
        self.assertEqual(result.trace['read_transport']['additional_retries'], 1)
        self.assertEqual(calls[0], calls[1])

    def test_endpoint_classification_does_not_turn_content_or_account_errors_into_pending_reads(self):
        self.assertTrue(is_unavailable_endpoint(unavailable()))
        self.assertTrue(is_unavailable_endpoint(ModelError('Endpoint is unavailable.', status=451)))
        for error in (ModelError('quota exhausted', status=429),
                      ModelError('Unavailable for legal reasons', status=451),
                      ModelError('Endpoint is unavailable.', status=403),
                      ValueError('Endpoint is unavailable.')):
            self.assertFalse(is_unavailable_endpoint(error))

    def test_exhausted_calculation_repair_outage_cannot_become_a_completed_refusal(self):
        error = unavailable()
        draft = 'CALCULATION: ' + json.dumps(dict(operation='sum', unit='maps', result='3',
            items=[dict(source='E1', quote='I have 2 maps.', value='2', unit='maps')])) + '\nANSWER: 3'
        calls = []
        def generate(messages):
            calls.append(messages)
            if len(calls) == 1:
                return draft
            raise error
        model = ScriptedChat({'answer': generate})
        with patch('memsys.read_budget.threading.Event.wait') as sleep:
            with self.assertRaises(ModelError) as raised:
                answer_with_validation(ReadTransportChat(model), [],
                    evidence={'E1': 'User: I have 2 maps.'}, question='How many maps in total?')
        self.assertIs(raised.exception, error)
        self.assertEqual(len(calls), 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [30])

    def test_exhausted_support_review_outage_cannot_become_missing_memory(self):
        error = unavailable()
        def review(messages):
            raise error
        model = ScriptedChat({'answer': 'ANSWER: copper', 'answer_review': review})
        with patch('memsys.read_budget.threading.Event.wait'):
            with self.assertRaises(ModelError) as raised:
                answer_with_validation(ReadTransportChat(model), [], enabled=False,
                    evidence={'E1': 'User: I chose copper as the color.'},
                    question='What color did I choose?', review_policy='always')
        self.assertIs(raised.exception, error)
        self.assertEqual(model.purposes(), ['answer'] + ['answer_review'] * 2)

    def test_exhausted_page_outage_propagates_through_both_page_readers(self):
        page = {'E1': {'text': 'User: I have 2 maps.',
                      'rendered': '[E1] 2025-05-01\nUser: I have 2 maps.', 'can_contribute': True}}
        result = SimpleNamespace(coverage={'search_complete': True}, items={})
        for reader, module, purpose in ((paged_answer, 'memsys.evidence_batch', 'evidence_page'),
                                        (paged_aggregate, 'memsys.aggregate_batch', 'aggregate_page')):
            for status in (429, 451):
                with self.subTest(reader=purpose, status=status):
                    error = ModelError('Endpoint is unavailable.', status=status)
                    def fail(messages):
                        raise error
                    model = ScriptedChat({purpose: fail})
                    with patch(module + '.aggregation_pages', return_value=[page]), \
                         patch('memsys.read_budget.threading.Event.wait'):
                        with self.assertRaises(ModelError) as raised:
                            reader(ReadTransportChat(model), 'How many maps in total?', result,
                                   {}, settings(), datetime(2025, 5, 2).date())
                    self.assertIs(raised.exception, error)
                    self.assertEqual(model.purposes(), [purpose] * 2)


if __name__ == '__main__':
    unittest.main()
