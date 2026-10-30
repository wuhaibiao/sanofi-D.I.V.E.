"""Request isolation and exact-pair reuse with a tiny deterministic CPU model."""
import math
import unittest
from types import SimpleNamespace

from memsys.config import MemorySettings
from memsys.model_tokens import PairEncodingCache
from memsys.reranking import BGEReranker, RerankDocument, RerankFailure
from tests import test_rerank_token_cache as token_cache


@unittest.skipIf(token_cache.Tokenizer is None, 'optional native tokenizer dependencies unavailable')
class RerankExecutionTests(unittest.TestCase):
    def setUp(self):
        token_cache.PairEncodingTests.setUp(self)
        import torch
        class Scorer:
            calls = 0
            invalid = False
            mismatch = False

            def __call__(self, input_ids, attention_mask, return_dict=True, **kwargs):
                self.calls += 1
                values = (input_ids.float() * attention_mask).sum(dim=1, keepdim=True)
                if self.invalid:
                    values.fill_(math.nan)
                return SimpleNamespace(logits=values[:0] if self.mismatch else values)
        tokenizer = SimpleNamespace(tokenizer=self.fast, local=False,
            count=lambda text: len(self.fast.encode(text, truncation=False)), manifest=lambda: {'model': 'synthetic'})
        self.backend = BGEReranker(MemorySettings(rerank_max_tokens=64, rerank_overlap_tokens=8,
                                                rerank_batch_size=2, rerank_device='cpu'))
        self.backend.tokenizer = tokenizer
        self.backend.encodings = PairEncodingCache(tokenizer)
        self.backend.model = Scorer()
        self.backend.torch = torch
        self.backend.device = 'cpu'
        self.backend.dependencies = {'test': 'synthetic'}

    def test_identical_inputs_share_work_but_all_source_windows_survive(self):
        text = 'Aster has 12 green notebooks. ' * 12
        documents = [RerankDocument('one', text), RerankDocument('two', text, source_start=33),
                     RerankDocument('three', 'Rowan has 120 blue notebooks.')]
        scores, trace = self.backend.rank('Which notebook?', documents)
        self.assertGreater(trace['reused_pair_windows'], 0)
        self.assertEqual(trace['inference_windows'] + trace['reused_pair_windows'], trace['pair_windows'])
        expected, ends = {}, {}
        for window in trace['windows']:
            ids = self.fast('Which notebook?', window['text'], truncation=False)['input_ids']
            value = float(sum(ids))
            self.assertEqual(window['score'], value)
            expected[window['key']] = max(expected.get(window['key'], -math.inf), value)
            self.assertLessEqual(window['start'], ends.get(window['key'], 0))
            ends[window['key']] = window['end']
            if window['key'] == 'two':
                self.assertEqual(window['source_start'], window['start'] + 33)
        self.assertEqual(scores, expected)
        self.assertEqual(ends, {d.key: len(d.text) for d in documents})
        self.assertEqual(self.backend.model.calls, len(trace['batches']))
        self.assertEqual(self.backend.encodings.info()['entries'], 0)

    def test_headers_are_part_of_input_identity(self):
        docs = [RerankDocument('one', 'green notebooks', header='Record 12: '),
                RerankDocument('two', 'green notebooks', header='Record 120: ')]
        _, trace = self.backend.rank('Which notebook?', docs)
        self.assertEqual(trace['inference_windows'], 2)
        self.assertEqual(trace['reused_pair_windows'], 0)

    def test_a_b_a_uses_same_model_but_no_request_cache_survives(self):
        model = self.backend.model
        doc = RerankDocument('one', 'Aster has 12 green notebooks.')
        first, _ = self.backend.rank('Which notebook?', [doc])
        self.backend.rank('周五怎么出行？', [RerankDocument('two', '林澈周五骑车。')])
        second, trace = self.backend.rank('Which notebook?', [doc])
        self.assertIs(self.backend.model, model)
        self.assertEqual(first, second)
        self.assertGreaterEqual(model.calls, 3)
        self.assertGreater(trace['tokenization']['encoded_texts'], 0)
        self.assertEqual(self.backend.encodings.info()['entries'], 0)
        self.assertEqual(self.backend.encodings.info()['tokens'], 0)

    def test_failed_inference_clears_request_encodings(self):
        self.backend.model.invalid = True
        with self.assertRaisesRegex(RerankFailure, 'invalid window scores'):
            self.backend.rank('Which notebook?', [RerankDocument('one', 'Aster green notebook.')])
        self.assertEqual(self.backend.encodings.info()['entries'], 0)

    def test_wrong_batch_shape_clears_encodings_and_does_not_return_partial_scores(self):
        self.backend.model.mismatch = True
        with self.assertRaisesRegex(RerankFailure, 'score count'):
            self.backend.rank('Which notebook?', [RerankDocument('one', 'Aster green notebook.')])
        self.assertEqual(self.backend.encodings.info()['tokens'], 0)

    def test_empty_documents_need_no_model_inference(self):
        scores, trace = self.backend.rank('Which notebook?', [])
        self.assertEqual(scores, {})
        self.assertEqual(trace['inference_windows'], 0)
        self.assertEqual(self.backend.model.calls, 0)
