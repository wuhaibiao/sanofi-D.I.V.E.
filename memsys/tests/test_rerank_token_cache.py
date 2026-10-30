"""Token and source-coverage contracts; entirely self-authored local text."""
import unittest
from types import SimpleNamespace

from memsys.model_tokens import PairEncodingCache, bounded_spans

try:
    from tokenizers import Tokenizer, models, pre_tokenizers, processors, trainers
    from transformers import PreTrainedTokenizerFast
except ImportError:
    Tokenizer = None


@unittest.skipIf(Tokenizer is None, 'optional native tokenizer dependencies unavailable')
class PairEncodingTests(unittest.TestCase):
    def setUp(self):
        tokenizer = Tokenizer(models.BPE(unk_token='[UNK]'))
        tokenizer.pre_tokenizer = pre_tokenizers.ByteLevel(add_prefix_space=False)
        tokenizer.train_from_iterator([
            'Aster has 12 green notebooks. Rowan has 120 blue notebooks.',
            '林澈周五骑车，周一坐车。完整字段包含数量、日期和否定状态。',
            'Café e\u0301 🧭\n1. First item\n2. Second item\nRecord: completed.'],
            trainers.BpeTrainer(vocab_size=320, special_tokens=['[UNK]', '[CLS]', '[SEP]', '[PAD]'],
                                initial_alphabet=pre_tokenizers.ByteLevel.alphabet()))
        tokenizer.post_processor = processors.TemplateProcessing(
            single='[CLS] $A [SEP]', pair='[CLS] $A [SEP] $B:1 [SEP]:1',
            special_tokens=[('[CLS]', tokenizer.token_to_id('[CLS]')), ('[SEP]', tokenizer.token_to_id('[SEP]'))])
        self.fast = PreTrainedTokenizerFast(tokenizer_object=tokenizer, unk_token='[UNK]',
            cls_token='[CLS]', sep_token='[SEP]', pad_token='[PAD]', model_max_length=4096)
        self.cache = PairEncodingCache(SimpleNamespace(tokenizer=self.fast, local=False))

    def test_pairs_match_reference_including_empty_unicode_and_headers(self):
        for question in ('Which notebook?', '周五怎么出行？', '', 'Café 🧭'):
            for text in ('12 green notebooks.', '林澈周五骑车。', '', 'e\u0301\n[SEP] café'):
                with self.subTest(question=question, text=text):
                    query = self.cache.encode(question)
                    original = list(query.ids)
                    doc = self.cache.document(query, 'Record: ', text)
                    actual = doc.prepare(text, self.fast.model_input_names)
                    expected = self.fast(question, 'Record: ' + text, truncation=False)
                    self.assertEqual(actual, expected)
                    self.assertEqual(doc.count(text), len(expected['input_ids']))
                    self.assertEqual(query.ids, original)

    def test_legacy_window_boundaries_and_full_source_coverage_are_identical(self):
        question = 'Which item is completed?'
        text = ('1. Aster has 12 green notebooks.\n林澈周五骑车，周一坐车。🧭\n' * 14) + 'FINAL 120.'
        prefix = 'Record: '
        reference = lambda piece: len(self.fast.encode(question, prefix + piece, truncation=False))
        doc = self.cache.document(self.cache.encode(question), prefix, text)
        expected = list(bounded_spans(text, reference, 64, 8))
        actual = list(bounded_spans(text, doc.count, 64, 8))
        self.assertEqual(actual, expected)
        end = 0
        for start, stop in actual:
            self.assertLessEqual(start, end)
            self.assertGreater(stop, end)
            features = doc.prepare(text[start:stop], self.fast.model_input_names)
            self.assertEqual(features, self.fast(question, prefix + text[start:stop], truncation=False))
            self.assertLessEqual(len(features['input_ids']), 64)
            end = stop
        self.assertEqual(end, len(text))

    def test_passage_cache_is_reused_across_questions_and_changed_text_misses(self):
        text = 'Aster has 12 green notebooks.'
        for question in ('How many notebooks?', 'What color are they?'):
            query = self.cache.encode(question)
            before = self.cache.info()
            doc = self.cache.document(query, '', text)
            doc.prepare(text, self.fast.model_input_names)
            if question.startswith('What'):
                self.assertEqual(self.cache.info()['encoded_texts'], before['encoded_texts'])
        before = self.cache.info()['encoded_texts']
        changed = self.cache.document(query, '', text.replace('12', '120'))
        changed.prepare(text.replace('12', '120'), self.fast.model_input_names)
        self.assertGreater(self.cache.info()['encoded_texts'], before)

    def test_persistent_cache_and_probe_memo_are_bounded(self):
        self.cache = PairEncodingCache(SimpleNamespace(tokenizer=self.fast, local=False), max_entries=2, max_tokens=30)
        query = self.cache.encode('Aster?')
        for n in range(20):
            self.cache.document(query, '', 'Record ' + str(n)).prepare('Record ' + str(n), self.fast.model_input_names)
            self.assertLessEqual(self.cache.info()['entries'], 2)
            self.assertLessEqual(self.cache.info()['tokens'], 30)
        doc = self.cache.document(query, '', 'Aster ' * 200)
        for n in range(1, 100):
            doc.count('Aster ' * n)
        self.assertLessEqual(len(doc.memo), 32)
        self.assertLessEqual(doc.memo_tokens, 8192)
        self.cache.clear()
        self.assertEqual(self.cache.info()['tokens'], 0)
        self.assertEqual(self.cache.info()['entries'], 0)

    def test_padding_cached_pairs_matches_native_batch_encoding(self):
        import torch
        pairs = [('How many?', '12 notebooks.'), ('周五？', '林澈周五骑车，周一坐车。')]
        features = [self.cache.document(self.cache.encode(q), '', text).prepare(text, self.fast.model_input_names)
                    for q, text in pairs]
        for side in ('left', 'right'):
            self.fast.padding_side = side
            expected = self.fast(pairs, padding=True, truncation=False, return_tensors='pt')
            actual = self.fast.pad(features, padding=True, return_tensors='pt', verbose=False)
            self.assertEqual(set(actual), set(expected))
            for key in actual:
                self.assertTrue(torch.equal(actual[key], expected[key]), (side, key))

    def test_query_that_fills_pair_budget_is_explicit_failure(self):
        doc = self.cache.document(self.cache.encode('Aster ' * 100), 'Record: ', 'green')
        with self.assertRaisesRegex(ValueError, 'no space'):
            list(bounded_spans('green', doc.count, 16, 2))
