"""Direct answer delivery, transport faults and bounded generation growth."""
import json
import unittest

from memsys.generation import generate_answer
from memsys.config import MemorySettings
from memsys.fake import ScriptedChat
from memsys.generation import complete_chat
from memsys.llm import ChatResult, ModelError
from memsys.protocols import ResponsesClient
from memsys.llm import ModelConfig, UsageLedger
from memsys.llm import OpenAICompatClient
from test_llm import FakeServer, HI
from test_protocols import response_reply


class DeliveryTests(unittest.TestCase):
    def test_plain_answers_keep_format_and_uncertainty_without_an_audit(self):
        for answer in ('**Use a cork mat.**\n\nKeep the surface dry.',
                       'The total is 11 crates.',
                       'The available facts do not establish the delivery date.'):
            with self.subTest(answer=answer):
                model = ScriptedChat({'answer': answer})
                response = generate_answer(model, [], max_tokens=8192)
                self.assertEqual(response.text, answer)
                self.assertFalse(hasattr(response, 'evidence_audit'))
                self.assertEqual(model.purposes(), ['answer'])
                self.assertEqual(len(response.generation_attempts), 1)

    def test_requested_json_is_delivered_verbatim_without_field_interpretation(self):
        for data in ({'answer': 'example', 'explanation': 'business content'},
                     {'memory_answer': True, 'answer': 'business content',
                      'memory_use': 'business value', 'evidence': [], 'calculations': []}):
            raw = json.dumps(data)
            model = ScriptedChat({'answer': raw})
            response = generate_answer(model, [dict(role='user', content='Return this JSON exactly.')], max_tokens=8192)
            self.assertEqual(response.text, raw)
            self.assertEqual(model.purposes(), ['answer'])

    def test_empty_and_blocked_responses_fail_without_format_retries(self):
        for text, reason in [('', 'stop'), ('   ', 'stop'), ('blocked', 'content_filter'), ('refused', 'refusal')]:
            with self.subTest(text=text, reason=reason):
                model = ScriptedChat({'answer': lambda _, t=text, r=reason: ChatResult(t, 'fixture', 0, 0, 0, 0, 0, r)})
                with self.assertRaises(ModelError):
                    generate_answer(model, [], max_tokens=8192, max_tokens_limit=32768)
                self.assertEqual(model.purposes(), ['answer'])

    def test_real_http_answer_has_no_forced_json_format(self):
        server = FakeServer(); self.addCleanup(server.close)
        server.responses = [(200, response_reply('A direct answer.'), {})]
        client = ResponsesClient(ModelConfig('fixture', server.base_url, api='responses'))
        result = generate_answer(client, HI, max_tokens=1024)
        self.assertEqual(result.text, 'A direct answer.')
        request = server.requests[0][2]
        self.assertNotIn('response_format', request)
        self.assertNotEqual(request.get('text', {}).get('format', {}).get('type'), 'json_object')



class GrowingGenerationTests(unittest.TestCase):
    def chat(self,results):
        class Capture(ScriptedChat):
            def __init__(self):
                self.responses=iter(results);self.requests=[]
                super().__init__({'answer':lambda _:next(self.responses)})
            def chat(self,messages,**kwargs):
                self.requests.append((messages,kwargs))
                return super().chat(messages,**kwargs)
        return Capture()

    def result(self,text,reason='stop'):
        return ChatResult(text,'fixture',10,10,0,0,0,reason)

    def test_truncated_draft_is_discarded_and_only_complete_second_response_is_delivered(self):
        model=self.chat([self.result('An unfinished draft','length'),self.result('Complete response.')])
        response=generate_answer(model,[dict(role='user',content='A request')],max_tokens=8192,max_tokens_limit=32768)
        self.assertEqual(response.text,'Complete response.')
        self.assertEqual([r[1]['max_tokens'] for r in model.requests],[8192,16384])
        self.assertEqual(model.requests[0][0],model.requests[1][0])
        self.assertTrue(all(not r[1]['json_mode'] for r in model.requests))
        self.assertEqual(len(response.generation_attempts),2)

    def test_growth_is_bounded_and_exhausted_drafts_never_escape(self):
        model=self.chat([self.result('clipped','length')]*3)
        with self.assertRaises(ModelError) as caught:
            complete_chat(model,[],purpose='answer',max_tokens=4096,max_tokens_limit=1000000)
        self.assertEqual([r[1]['max_tokens'] for r in model.requests],[4096,8192,16384])
        self.assertEqual(len(caught.exception.generation_attempts),3)

    def test_explicit_ceiling_is_respected(self):
        model=self.chat([self.result('clipped','length'),self.result('ready')])
        complete_chat(model,[],purpose='answer',max_tokens=8192,max_tokens_limit=10000)
        self.assertEqual([r[1]['max_tokens'] for r in model.requests],[8192,10000])

    def test_real_http_budget_growth_includes_reasoning_headroom_and_each_usage_record(self):
        server=FakeServer();self.addCleanup(server.close)
        clipped=response_reply('An unfinished draft',status='incomplete')
        clipped['incomplete_details']={'reason':'max_output_tokens'}
        server.responses=[(200,clipped,{}),(200,response_reply('Complete answer.'),{})]
        ledger=UsageLedger()
        client=ResponsesClient(ModelConfig('fixture',server.base_url,api='responses',output_token_headroom=512),ledger)
        response=generate_answer(client,HI,max_tokens=1024,max_tokens_limit=2048)
        self.assertEqual(response.text,'Complete answer.')
        self.assertEqual([r[2]['max_output_tokens'] for r in server.requests],[1536,2560])
        self.assertEqual(len(ledger.records()),2)
        self.assertEqual(response.generation_attempts[-1]['request_output_tokens'],2560)

    def test_refusal_and_transport_errors_do_not_get_output_growth_retries(self):
        model=self.chat([self.result('refused','refusal')])
        with self.assertRaises(ModelError):generate_answer(model,[],max_tokens=8192,max_tokens_limit=32768)
        self.assertEqual(len(model.requests),1)
        def fail(_):raise ModelError('bad credentials',status=401)
        model=ScriptedChat({'answer':fail});attempts=[]
        with self.assertRaises(ModelError):complete_chat(model,[],purpose='answer',max_tokens=8192,max_tokens_limit=32768,attempts=attempts)
        self.assertEqual(model.purposes(),['answer'])
        self.assertEqual(attempts[0]['status'],401)

    def test_configuration_rejects_impossible_limits(self):
        for values in [dict(answer_max_tokens=0),dict(extract_max_tokens_limit=1)]:
            with self.assertRaises(ValueError):MemorySettings.from_dict(values)


class EmbeddingWindowTests(unittest.TestCase):
    def setUp(self):
        self.server=FakeServer();self.addCleanup(self.server.close)

    def reply(self,vectors):
        return {'data':[dict(index=i,embedding=v) for i,v in reversed(list(enumerate(vectors)))],
                'usage':{'prompt_tokens':12}}

    def client(self,limit=4):
        return OpenAICompatClient(ModelConfig('fixture',self.server.base_url,embed_max_input_bytes=limit,max_retries=0))

    def test_all_chunks_are_encoded_and_pooled_back_to_one_vector_per_original(self):
        self.server.responses=[(200,self.reply([[1.,0.],[0.,1.],[1.,0.],[9.,0.]]),{})]
        client=self.client()
        vectors=client.embed(['abcdefghijkl','x'])
        self.assertEqual(self.server.requests[0][2]['input'],['abcd','efgh','ijkl','x'])
        self.assertEqual(len(vectors),2)
        self.assertAlmostEqual(vectors[0][0],2/(5**.5))
        self.assertAlmostEqual(vectors[0][1],1/(5**.5))
        self.assertEqual(vectors[1],[9.,0.])
        self.assertEqual(client.ledger.summary()['embed']['prompt_tokens'],12)

    def test_unicode_chunks_preserve_the_full_original(self):
        self.server.responses=[(200,self.reply([[1.,0.]]*3),{})]
        self.client().embed(['🙂🙂🙂'])
        pieces=self.server.requests[0][2]['input']
        self.assertEqual(''.join(pieces),'🙂🙂🙂')
        self.assertTrue(all(len(p.encode('utf-8'))<=4 for p in pieces))

    def test_inconsistent_chunk_dimensions_are_rejected(self):
        self.server.responses=[(200,self.reply([[1.,0.],[1.,0.,0.]]),{})]
        with self.assertRaises(ModelError):self.client().embed(['abcdefgh'])

    def test_impossible_byte_budgets_are_rejected(self):
        for limit in [True,1,-1]:
            with self.assertRaises(ValueError):ModelConfig.from_dict(dict(model='fixture',base_url=self.server.base_url,embed_max_input_bytes=limit))
