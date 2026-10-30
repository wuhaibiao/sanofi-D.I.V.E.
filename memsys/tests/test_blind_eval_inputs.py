"""Self-authored canaries verify the actual public runner's isolation boundary."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import blind_eval_inputs as blind
import evaluate_public_types as runner
from memsys.qdrant import QdrantSettings


class BlindInputsTests(unittest.TestCase):
    def setUp(self):
        self.sample = [dict(question_id='synthetic_fixture', question_type='single-session-assistant',
            question='Which action did the assistant describe?', question_date='2026-01-03',
            answer='LABEL_CANARY_ONE', haystack_dates=['2026-01-02'], haystack_session_ids=['fixture_source'],
            haystack_sessions=[[dict(role='user', content='Keep this fixture instruction.', has_answer=True),
                dict(role='assistant', content='42. Rotate the fixture clockwise.')]], answer_session_ids=['fixture_source'])]
        self.selection = [dict(question_id='synthetic_fixture', dataset='LongMemEval',
            category='single-session-assistant', corpus_id='fixture_corpus',
            judge_type='single-session-assistant', abstention=False)]

    def test_changed_gold_cannot_change_write_or_read_payloads(self):
        before = blind.payloads(self.sample, self.selection)
        changed = copy.deepcopy(self.sample)
        changed[0]['answer'] = 'LABEL_CANARY_TWO'
        after = blind.payloads(changed, self.selection)
        self.assertEqual(before['write'], after['write'])
        self.assertEqual(before['read'], after['read'])
        self.assertNotEqual(before['judge'], after['judge'])

    def test_changed_question_cannot_change_ingestion(self):
        before = blind.payloads(self.sample, self.selection)
        changed = copy.deepcopy(self.sample)
        changed[0]['question'] = 'A different self-authored fixture question?'
        self.assertEqual(before['write'], blind.payloads(changed, self.selection)['write'])

    def test_evidence_labels_are_removed_from_source_turns(self):
        source = blind.payloads(self.sample, self.selection)['write'][0]
        self.assertEqual(set(source['sessions'][0]['turns'][0]), {'role', 'content'})
        self.assertNotIn('answer', source)
        self.assertNotIn('question', source)

    def test_source_and_read_files_work_without_sample_or_judge_files(self):
        with tempfile.TemporaryDirectory() as directory:
            hashes = blind.materialize(directory, self.sample, self.selection)
            Path(directory, blind.FILES['judge']).unlink()
            freeze = dict(blind_input_sha256=hashes)
            _, write = blind.load_phase(directory, freeze, 'write', 'LongMemEval')
            _, read = blind.load_phase(directory, freeze, 'read', 'LongMemEval')
            self.assertEqual(write[0][0].question, '')
            self.assertIsNone(write[0][1])
            self.assertIsNone(read[0][1])
            self.assertEqual(read[0][0].sessions[0].turns, ())
            self.assertEqual(read[0][0].question, self.sample[0]['question'])

    def test_gold_field_injected_in_read_input_is_rejected_even_with_new_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            hashes = blind.materialize(directory, self.sample, self.selection)
            path = Path(directory, blind.FILES['read'])
            data = json.loads(path.read_text())
            data[0]['answer'] = 'LABEL_CANARY_ONE'
            path.write_text(json.dumps(data))
            hashes['read'] = hashlib.sha256(path.read_bytes()).hexdigest()
            with self.assertRaisesRegex(ValueError, 'Unexpected fields'):
                blind.load_phase(directory, dict(blind_input_sha256=hashes), 'read', 'LongMemEval')

    def test_missing_blind_inputs_never_fall_back_to_legacy_sample(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, 'sample.json').write_text(json.dumps(self.sample))
            with self.assertRaisesRegex(ValueError, 'Blind inputs are missing'):
                blind.load_phase(directory, {}, 'read', 'LongMemEval')

    def test_phase_and_run_sessions_are_distinct_and_logs_match_actual_call(self):
        class Client:
            def chat(self, messages, **kwargs):
                self.tags = kwargs['tags']
                return 'fixture response'
        with tempfile.TemporaryDirectory() as directory:
            ids = []
            for scope in ('run_a:read', 'run_a:judge', 'run_b:read'):
                client = Client()
                path = Path(directory, scope.replace(':', '-') + '.jsonl')
                wrapper = runner.CapturedChat(client, path, session_scope=scope)
                wrapper.chat([dict(role='user', content='fixture query')], purpose='answer',
                    tags={'question_id': 'synthetic_fixture'})
                self.assertEqual(client.tags, json.loads(path.read_text())['tags'])
                ids.append(client.tags['session_id'])
            self.assertEqual(len(set(ids)), 3)

    def test_actual_setup_does_not_require_or_load_label_or_sample_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hashes = blind.materialize(root, self.sample, self.selection)
            (root / blind.FILES['judge']).unlink()
            (root / blind.FILES['write']).unlink()
            config = root / 'config.frozen.json'
            config.write_text('{}')
            runner.save(root / 'freeze.json', dict(blind_input_sha256=hashes,
                config_sha256=runner.digest(config), source={'source_sha256': 'fixture_source'},
                runner_sha256=runner.digest(runner.__file__), prefix='fixture_prefix', namespace='fixture_namespace',
                blind_input_builder_sha256=runner.digest(Path(blind.__file__))))
            cfg = SimpleNamespace(models={}, storage=QdrantSettings(),
                build_models=lambda *a, **kw: SimpleNamespace(chat=None, memory=None, by_purpose={}))
            args = SimpleNamespace(out=root, dataset='LongMemEval', phase='read', runtime=Path('unused'))
            with patch.object(runner, 'credentials'), patch.object(runner.AppConfig, 'load', return_value=cfg), \
                    patch.object(runner, 'source_manifest', return_value={'source_sha256': 'fixture_source'}):
                _, _, _, metadata, items = runner.setup(args)
            self.assertEqual(items[0][0].question, self.sample[0]['question'])
            self.assertIsNone(items[0][1])
            self.assertNotIn('answer', metadata['synthetic_fixture'])
            cfg.models = {'fixture': SimpleNamespace(extra_body={'previous_response_id': 'fixture_response'})}
            cfg.build_models = lambda *a, **kw: self.fail('Stateful client was constructed')
            with patch.object(runner, 'credentials'), patch.object(runner.AppConfig, 'load', return_value=cfg), \
                    patch.object(runner, 'source_manifest', return_value={'source_sha256': 'fixture_source'}):
                with self.assertRaisesRegex(ValueError, 'Stateful provider links'):
                    runner.setup(args)


if __name__ == '__main__':
    unittest.main()
