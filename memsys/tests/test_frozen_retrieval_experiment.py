"""Verify controls that prevent misleading chunking comparisons."""
import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from memsys.llm import ChatResult, ModelConfig
import memsys.core as core

spec = importlib.util.spec_from_file_location(
    'retrieval_experiment', Path(__file__).parents[1] / 'scripts/evaluate_retrieval_variant.py')
experiment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(experiment)


class FrozenExperimentTests(unittest.TestCase):
    def test_plans_check_input_and_return_independent_values(self):
        now = datetime(2025, 1, 2, tzinfo=timezone.utc)
        item = SimpleNamespace(question_id='case', question='Which color?', question_date=now)
        plan = dict(queries=['Which color?'], keywords=['color'], time_range=['2025-01-01','2025-01-02'],
                    needs_all=False, focus='past', source='llm', error=None,
                    operation='lookup', qualifiers=[], trigger='always')
        with tempfile.TemporaryDirectory() as directory, patch.object(core, 'plan_query'):
            path = Path(directory) / 'plans.json'
            path.write_text(json.dumps({'case':dict(question=item.question,
                question_date=now.isoformat(), plan=plan)}))
            experiment.install_frozen_plans(path, [item])
            first = core.plan_query(None, item.question, now, None, {'question_id':'case'})
            first.queries.append('mutation')
            second = core.plan_query(None, item.question, now, None, {'question_id':'case'})
            self.assertEqual(second.queries, ['Which color?'])
            self.assertEqual(second.time_range.start.isoformat(), '2025-01-01')
            with self.assertRaisesRegex(ValueError, 'mismatch'):
                experiment.install_frozen_plans(path, [SimpleNamespace(
                    question_id='case', question='Different question', question_date=now)])

    def test_replay_reuses_identical_requests_but_not_truncation_or_changed_input(self):
        class Model:
            config = ModelConfig(model='test', base_url='http://localhost')
            def __init__(self): self.calls = 0
            def chat(self, messages, **kwargs):
                self.calls += 1
                return ChatResult(str(self.calls), 'test', 1, 1, 0, 0, 0,
                                  'length' if kwargs['purpose']=='truncated' else 'stop')
        with tempfile.TemporaryDirectory() as directory:
            model = Model()
            cache = experiment.ResponseReplay(model, Path(directory)/'cache', Path(directory)/'events.jsonl')
            messages = [{'role':'user','content':'input'}]
            first = cache.chat(messages, purpose='answer', tags={'question_id':'one'})
            self.assertEqual(first, cache.chat(messages, purpose='answer', tags={'question_id':'two'}))
            self.assertEqual(model.calls, 1)
            cache.chat([{'role':'user','content':'changed'}], purpose='answer')
            cache.chat(messages, purpose='truncated')
            cache.chat(messages, purpose='truncated')
            self.assertEqual(model.calls, 4)

    def test_local_scores_require_identical_windows(self):
        import memsys.reranking as reranking
        from memsys.config import MemorySettings
        from memsys.reranking import RerankDocument
        class Tokenizer:
            def __init__(self, *args): pass
            def count(self, text): return len(text)
            def pair_count(self, question, text): return len(question+text)
            def manifest(self): return {'test':'tokenizer'}
        settings = MemorySettings()
        live = Mock(return_value=({'E1':9.0},{'backend':'live'}))
        measured = dict(backend='bge', model=settings.rerank_model, revision=settings.rerank_revision,
            dtype=settings.rerank_dtype, tokenizer={'test':'tokenizer'}, scores={'E1':2.0},
            windows=[dict(key='E1',text='original',source_start=0,source_end=8,
                          source_role='',pair_tokens=9)])
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(reranking,'local_reranker',return_value=SimpleNamespace(rank=live)), \
                patch('memsys.model_tokens.ModelTokenizer',Tokenizer):
            (Path(directory)/'case.json').write_text(json.dumps({'message':'Q','reranking':measured}))
            experiment.install_rerank_replay(directory)
            cache = reranking.local_reranker(settings)
            scores, trace = cache.rank('Q',[RerankDocument('E1','original')])
            self.assertEqual(scores, {'E1':2.0})
            self.assertTrue(trace['score_replay'])
            self.assertEqual(live.call_count,0)
            cache.rank('Q',[RerankDocument('E1','changed')])
            self.assertEqual(live.call_count,1)

    def test_replay_ignores_per_call_transport_but_not_generation_controls(self):
        model=SimpleNamespace(config=ModelConfig(model='test',base_url='http://localhost'),
            chat=Mock(return_value=ChatResult('completed response','test',1,1,0,0,0,'stop')))
        with tempfile.TemporaryDirectory() as directory:
            cache=experiment.ResponseReplay(model,Path(directory)/'cache',Path(directory)/'events.jsonl')
            messages=[{'role':'user','content':'synthetic request'}]
            cache.chat(messages,purpose='answer',temperature=0,max_tokens=32,timeout_s=120,max_retries=0)
            cache.chat(messages,purpose='answer',temperature=0,max_tokens=32,timeout_s=77,max_retries=1)
            self.assertEqual(model.chat.call_count,1)
            cache.chat(messages,purpose='answer',temperature=0.5,max_tokens=32,timeout_s=77)
            cache.chat(messages,purpose='answer',temperature=0,max_tokens=64,timeout_s=77)
            self.assertEqual(model.chat.call_count,3)

    def test_replay_does_not_cache_unknown_filtered_or_empty_completion(self):
        for reason,text in ((None,'possibly incomplete'),('content_filter','filtered'),
                            ('tool_calls','unfinished tool call'),('stop','')):
            with self.subTest(reason=reason),tempfile.TemporaryDirectory() as directory:
                model=SimpleNamespace(config=ModelConfig(model='test',base_url='http://localhost'),
                    chat=Mock(return_value=ChatResult(text,'test',1,1,0,0,0,reason)))
                cache=experiment.ResponseReplay(model,Path(directory)/'cache',Path(directory)/'events.jsonl')
                for _ in range(2):
                    cache.chat([{'role':'user','content':'synthetic request'}],purpose='answer')
                self.assertEqual(model.chat.call_count,2)

    def test_replay_keeps_model_options_in_request_identity(self):
        config=ModelConfig(model='test',base_url='http://localhost',extra_body={'top_p':1})
        model=SimpleNamespace(config=config,
            chat=Mock(return_value=ChatResult('completed response','test',1,1,0,0,0,'stop')))
        with tempfile.TemporaryDirectory() as directory:
            cache=experiment.ResponseReplay(model,Path(directory)/'cache',Path(directory)/'events.jsonl')
            messages=[{'role':'user','content':'synthetic request'}]
            cache.chat(messages,purpose='answer')
            config.extra_body['top_p']=0.5
            cache.chat(messages,purpose='answer')
            self.assertEqual(model.chat.call_count,2)


if __name__ == '__main__':
    unittest.main()
