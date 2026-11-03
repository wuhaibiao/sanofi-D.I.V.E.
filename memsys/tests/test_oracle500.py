"""Authored fixtures check full-cohort integrity and submission/telemetry boundaries."""
from dataclasses import dataclass
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import run_oracle500 as full
from memsys.llm import ChatResult, ModelError


class Oracle500Tests(unittest.TestCase):
    def test_all_instances_retained_including_shared_base_and_abstentions(self):
        pool = [dict(question_id='fixture-' + str(i), question_type=full.TYPES[i % 6]) for i in range(470)]
        pool += [dict(question_id='fixture-' + str(i) + '_abs', question_type=full.TYPES[i % 6]) for i in range(30)]
        selection = full.partition(pool)
        self.assertEqual([m['question_id'] for m in selection], [r['question_id'] for r in pool])
        self.assertEqual(sum(m['abstention'] for m in selection), 30)
        self.assertEqual(len({m['corpus_id'] for m in selection}), 500)

    def test_duplicate_id_and_incomplete_cohort_rejected(self):
        with self.assertRaises(ValueError):
            full.partition([dict(question_id='duplicate', question_type=full.TYPES[0])] * 500)
        with self.assertRaises(ValueError):
            full.partition([])

    def test_submission_requires_every_success_and_exports_only_two_original_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            selection = [{'question_id': 'authored'}, {'question_id': 'authored_abs'}]
            answers = [dict(question_id='authored', status='answered', hypothesis='blue', private='telemetry')]
            self.assertFalse(full.export_answers(out, selection, answers))
            self.assertFalse((out / 'hypotheses.jsonl').exists())
            answers.append(dict(question_id='authored_abs', status='error', hypothesis='partial'))
            self.assertFalse(full.export_answers(out, selection, answers))
            answers[-1].update(status='answered', hypothesis='Unknown')
            self.assertTrue(full.export_answers(out, selection, answers))
            self.assertEqual(full.rows(out / 'hypotheses.jsonl'), [dict(question_id='authored', hypothesis='blue'),
                dict(question_id='authored_abs', hypothesis='Unknown')])
            with self.assertRaisesRegex(ValueError, 'Duplicate'):
                full.export_answers(out, selection, answers + answers[:1])

    def test_provider_capture_preserves_request_and_response_without_extra_calls(self):
        @dataclass
        class Stats:
            retries: int = 0
        class Client:
            config = None
            calls = 0
            def _post(self, endpoint, body, tags=None, **kwargs):
                self.calls += 1
                return {'raw_provider_marker': 'RESPONSE'}, Stats()
            def chat(self, messages, **kwargs):
                self._post('/responses', {'input': messages}, kwargs.get('tags'), purpose=kwargs['purpose'])
                return ChatResult('blue', 'authored-model', 1, 1, 0, 0, 0, 'stop')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'LongMemEval-read-prompts.jsonl'
            client = Client()
            model = full.CapturedChat(client, path, 'isolated-read')
            result = model.chat([{'role':'user','content':'Authored question'}], purpose='answer', max_tokens=10)
            self.assertEqual(client.calls, 1)
            self.assertEqual(result.text, 'blue')
            event = full.rows(model.provider_path)[0]
            self.assertEqual(event['response']['raw_provider_marker'], 'RESPONSE')
            self.assertTrue(event['tags']['session_id'].startswith('isolated-read:'))
            self.assertNotIn('headers', event)

    def test_account_failure_recorded_to_stop_batch(self):
        class Client:
            config = None
            def _post(self, *args, **kwargs):
                raise ModelError('Authored quota exhausted', status=402)
            def chat(self, messages, **kwargs):
                self._post('/responses', {'input': messages}, purpose=kwargs['purpose'])
        with tempfile.TemporaryDirectory() as directory:
            model = full.CapturedChat(Client(), Path(directory) / 'LongMemEval-write-prompts.jsonl')
            with self.assertRaises(ModelError):
                model.chat([{'role':'user','content':'Authored history'}], purpose='extract')
            self.assertEqual(full.read(model.fatal_path)['status'], 402)
            self.assertTrue(full.rows(model.response_path)[0]['error'])

    def test_report_keeps_failures_in_full_denominator_and_never_reads_gold_input(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            selection = [dict(question_id='fixture', category=full.TYPES[0], abstention=False),
                dict(question_id='fixture_abs', category=full.TYPES[0], abstention=True)]
            full.save(out / 'selection.json', selection)
            full.save(out / 'freeze.json', dict(evaluation_status='authored', protocol='authored', judge_model='fixture-model',
                prompt_sha256='fixture-prompt', source={'source_sha256':'fixture-source'}, data={}))
            cases = []
            for i, meta in enumerate(selection):
                relative = 'questions/' + str(i)
                case = out / relative
                case.mkdir(parents=True)
                cases.append({'directory':relative})
                full.worker.append(case / 'LongMemEval-answers.jsonl', dict(question_id=meta['question_id'],
                    status='answered' if i == 0 else 'error', hypothesis='blue' if i == 0 else '', elapsed_s=1))
                full.worker.append(case / 'LongMemEval-grades.jsonl', dict(question_id=meta['question_id'],
                    status='graded' if i == 0 else 'answer_error', correct=i == 0))
            full.save(out / 'question-cases.json', cases)
            original_read = full.read
            def restricted(path):
                if Path(path).name == 'judge-inputs.json':
                    self.fail('Report read reference answers')
                return original_read(path)
            with patch.object(full, 'read', side_effect=restricted):
                metrics = full.report(SimpleNamespace(out=out))
            self.assertEqual(metrics['overall_accuracy'], .5)
            self.assertFalse(metrics['submission_ready'])
            self.assertEqual(len(metrics['answer_errors']), 1)
            self.assertEqual(metrics['task_averaged_accuracy'], .5)


if __name__ == '__main__':
    unittest.main()
