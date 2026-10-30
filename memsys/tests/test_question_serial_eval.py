"""Self-authored cases verify physical isolation, cleanup and failure retention."""
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
import question_serial_eval as serial
import run_relational_memory_eval as evaluation
import run_relational_memory_regression24 as regression
from memsys.qdrant import QdrantSettings


class Backend:
    def __init__(self):
        self.names = {'personal_records', 'serial_fixtureextra_records'}
        self.deleted = []
        self.ignore_deletes = False

    def request(self, method, path):
        if method == 'GET' and path == '/collections':
            return {'collections': [{'name': name} for name in sorted(self.names)]}
        if method == 'DELETE':
            name = path.removeprefix('/collections/')
            self.deleted.append(name)
            if not self.ignore_deletes:
                self.names.remove(name)
            return {}
        raise AssertionError((method, path))


class QuestionSerialTests(unittest.TestCase):
    def prepare(self, out, label='LABEL_CANARY'):
        sample, selection = [], []
        for qid in ('synthetic_alpha', 'synthetic_beta'):
            sample.append(dict(question_id=qid, question_type='single-session-user',
                question='What color was the self-authored fixture?', question_date='2026-01-03',
                answer=label, haystack_dates=['2026-01-02'], haystack_session_ids=['shared_history'],
                haystack_sessions=[[dict(role='user', content='The fixture is blue.')]]))
            # A shared corpus must still be re-ingested into a fresh question collection.
            selection.append(dict(question_id=qid, dataset='LongMemEval', category='single-session-user',
                corpus_id='shared_corpus', judge_type='single-session-user', abstention=False))
        out.mkdir(exist_ok=True)
        serial.save(out / 'selection.json', selection)
        serial.save(out / 'config.frozen.json', {})
        frozen = dict(source={'source_sha256': 'self_authored_source'}, prefix='serial_fixture', namespace='fixture',
            config_sha256=serial.digest(out / 'config.frozen.json'), runner_sha256=serial.digest(runner.__file__),
            blind_input_builder_sha256=serial.digest(blind.__file__),
            blind_input_sha256=blind.materialize(out, sample, selection))
        serial.freeze_cases(out, frozen)
        serial.save(out / 'freeze.json', frozen)
        return frozen

    def execute(self, out, frozen, backend, invoke):
        return serial.run_serial_evaluation(SimpleNamespace(out=out, runtime=Path('unused')),
            frozen, SimpleNamespace(storage=None), lambda: None, backend=backend, invoke=invoke)

    def append(self, path, row):
        with path.open('a') as stream:
            stream.write(json.dumps(row) + '\n')

    def worker(self, out, backend, events, *, answer_error=False):
        def invoke(directory, phase, runtime):
            child = serial.read(directory / 'freeze.json')
            qid, prefix = child['question_id'], child['prefix']
            events.append((qid, phase))
            if phase == 'audit':
                self.assertEqual(serial.read(directory / 'execution.json')['current'], 'read')
                self.assertTrue(any(name.startswith(prefix + '_') for name in backend.names))
                serial.save(directory / 'storage-audit.json', dict(source_sha256='self_authored_source',
                    corpora=1, facts=1, entities=0, relations=0, orphan_relation_fact_ids=0,
                    entity_types={}, relation_origins={}, by_corpus=[{'corpus_id': 'shared_corpus', 'facts': 1}]))
                return 0
            meta, items = blind.load_phase(directory, child, phase, 'LongMemEval')
            self.assertEqual(list(meta), [qid])
            self.assertEqual(len(items), 1)
            item, gold = items[0]
            if phase == 'write':
                self.assertIsNone(gold)
                self.assertEqual(item.question, '')
                self.assertFalse(any(name.startswith('serial_fixture_q') for name in backend.names))
                backend.names.update(prefix + '_' + kind for kind in ('records', 'lexical', 'jobs', 'dense_1024'))
                row = dict(corpus_id=meta[qid]['corpus_id'], complete=True, facts=[], sessions=[dict(error=None,
                    raw_messages=1, processed_messages=1, source_exact=True, report={})])
                self.append(directory / 'LongMemEval-write.jsonl', row)
            elif phase == 'read':
                self.assertIsNone(gold)
                owned = {name for name in backend.names if name.startswith('serial_fixture_q')}
                self.assertTrue(owned)
                self.assertTrue(all(name.startswith(prefix + '_') for name in owned))
                row = dict(question_id=qid, question=item.question, hypothesis='synthetic response',
                    status='error' if answer_error and qid == 'synthetic_alpha' else 'answered', elapsed_s=1,
                    pending_evidence_items=0, error='synthetic failure' if answer_error else None)
                self.append(directory / 'LongMemEval-answers.jsonl', row)
                (directory / 'traces').mkdir()
                serial.save(directory / 'traces' / (qid + '.json'),
                    dict(selected={'episodes': []}, recall_mode='facts', memory_tokens=20))
            elif phase == 'judge':
                self.assertFalse(any(name.startswith('serial_fixture_q') for name in backend.names))
                self.assertEqual(sum(name == 'read' for _, name in events), 2)
                self.assertEqual(gold.answer, 'LABEL_CANARY')
                answer = json.loads((directory / 'LongMemEval-answers.jsonl').read_text())
                self.append(directory / 'LongMemEval-grades.jsonl', dict(question_id=qid,
                    reference=gold.answer, correct=False,
                    status='answer_error' if answer['status'] == 'error' else 'graded'))
            else:
                self.fail(phase)
            return 0
        return invoke

    def test_each_question_is_answered_then_physically_cleaned_before_next_write(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            frozen = self.prepare(out)
            backend, events = Backend(), []
            state = self.execute(out, frozen, backend, self.worker(out, backend, events))
            self.assertEqual(events, [(qid, phase) for qid in ('synthetic_alpha', 'synthetic_beta')
                for phase in ('write', 'audit', 'read')] + [('synthetic_alpha', 'judge'), ('synthetic_beta', 'judge')])
            self.assertEqual(backend.names, {'personal_records', 'serial_fixtureextra_records'})
            self.assertEqual(len(backend.deleted), 8)
            self.assertTrue(state['complete'])
            self.assertEqual(state['cleanup']['remaining_owned'], [])
            for record in state['cases']:
                self.assertTrue(record['cleanup']['verified_empty'])
                self.assertTrue(record['cleanup']['unrelated_collections_preserved'])
            self.assertEqual(len((out / 'LongMemEval-answers.jsonl').read_text().splitlines()), 2)
            self.assertEqual(serial.read(out / 'storage-audit.json')['facts'], 2)
            first = out / 'questions/0001/LongMemEval-answers.jsonl'
            self.assertTrue(first.exists())
            before = first.read_bytes()
            with self.assertRaisesRegex(ValueError, 'already started'):
                self.execute(out, frozen, backend, lambda *a: self.fail('Automatic rerun'))
            self.assertEqual(first.read_bytes(), before)

    def test_answer_errors_are_retained_and_do_not_skip_the_next_question(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            frozen = self.prepare(out)
            backend, events = Backend(), []
            self.execute(out, frozen, backend, self.worker(out, backend, events, answer_error=True))
            answers = [json.loads(line) for line in (out / 'LongMemEval-answers.jsonl').read_text().splitlines()]
            grades = [json.loads(line) for line in (out / 'LongMemEval-grades.jsonl').read_text().splitlines()]
            self.assertEqual([row['status'] for row in answers], ['error', 'answered'])
            self.assertEqual(grades[0]['status'], 'answer_error')
            self.assertFalse(grades[0]['correct'])
            self.assertEqual(sum(phase == 'read' for _, phase in events), 2)

    def test_process_failure_still_cleans_and_never_retries_or_starts_another_question(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            frozen = self.prepare(out)
            backend, events = Backend(), []
            def failing(directory, phase, runtime):
                events.append(phase)
                backend.names.add(serial.read(directory / 'freeze.json')['prefix'] + '_records')
                (directory / 'LongMemEval-write.log').write_text('preserved synthetic process failure')
                return 7
            with self.assertRaisesRegex(RuntimeError, 'without automatic retry'):
                self.execute(out, frozen, backend, failing)
            self.assertEqual(events, ['write'])
            self.assertEqual(len(backend.deleted), 1)
            self.assertTrue((out / 'questions/0001/LongMemEval-write.log').exists())
            state = serial.read(out / 'execution.json')
            self.assertEqual(state['cases'][0]['outcomes'], [{'phase': 'write', 'exit_code': 7}])
            self.assertEqual(state['cleanup']['remaining_owned'], [])

    def test_cleanup_failure_stops_before_the_next_question(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            frozen = self.prepare(out)
            backend, events = Backend(), []
            backend.ignore_deletes = True
            with self.assertRaisesRegex(RuntimeError, 'cleanup verification failed'):
                self.execute(out, frozen, backend, self.worker(out, backend, events))
            self.assertEqual(events, [('synthetic_alpha', phase) for phase in ('write', 'audit', 'read')])
            state = serial.read(out / 'execution.json')
            self.assertTrue(state['cleanup']['remaining_owned'])
            self.assertFalse(state['cases'][0]['cleanup']['verified_empty'])

    def test_nonempty_run_prefix_is_preserved_and_rejected_before_any_phase(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            frozen = self.prepare(out)
            backend = Backend()
            backend.names.add('serial_fixture_q0001_records')
            with self.assertRaisesRegex(ValueError, 'prefix is not empty'):
                self.execute(out, frozen, backend, lambda *a: self.fail('Unexpected model phase'))
            self.assertEqual(backend.deleted, [])
            self.assertFalse((out / 'execution.json').exists())

    def test_changed_case_input_is_rejected_before_database_or_models(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            frozen = self.prepare(out)
            (out / 'questions/0001/read-inputs.json').write_text('[]')
            with self.assertRaisesRegex(ValueError, 'Frozen question file changed'):
                self.execute(out, frozen, Backend(), lambda *a: self.fail('Unexpected model phase'))

    def test_changed_reference_does_not_change_question_or_source_payloads(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            self.prepare(out / 'first', label='FIRST_LABEL')
            self.prepare(out / 'second', label='SECOND_LABEL')
            for phase in ('write', 'read'):
                for index in ('0001', '0002'):
                    relative = Path('questions') / index / blind.FILES[phase]
                    self.assertEqual((out / 'first' / relative).read_bytes(), (out / 'second' / relative).read_bytes())

    def test_real_phase_setup_only_loads_the_current_question_without_judge_file(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            self.prepare(out)
            case = out / 'questions/0002'
            (case / blind.FILES['judge']).unlink()
            cfg = SimpleNamespace(models={}, storage=QdrantSettings(),
                build_models=lambda *a, **kw: SimpleNamespace(chat=None, memory=None, by_purpose={}))
            args = SimpleNamespace(out=case, dataset='LongMemEval', phase='read', runtime=Path('unused'))
            with patch.object(runner, 'credentials'), patch.object(runner.AppConfig, 'load', return_value=cfg), \
                    patch.object(runner, 'source_manifest', return_value={'source_sha256': 'self_authored_source'}):
                _, _, _, metadata, items = runner.setup(args)
            self.assertEqual(list(metadata), ['synthetic_beta'])
            self.assertEqual(len(items), 1)
            self.assertIsNone(items[0][1])
            self.assertEqual(cfg.storage.collection_prefix, 'serial_fixture_q0002')

    def test_both_public_entrypoints_freeze_single_question_cases(self):
        for controller, counts in ((regression, {key: 4 for key in evaluation.COUNTS}),
                                   (evaluation, evaluation.COUNTS)):
            with self.subTest(controller=controller.__name__), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                cohort = root / 'cohort'
                cohort.mkdir()
                sample, selection = [], []
                for category, count in counts.items():
                    for index in range(count):
                        qid = 'synthetic_' + category + '_' + str(index)
                        sample.append(dict(question_id=qid, question_type=category,
                            question='Which self-authored fixture was described?', question_date='2026-01-03',
                            answer='FIXTURE_LABEL', haystack_dates=['2026-01-02'],
                            haystack_session_ids=['source_' + qid],
                            haystack_sessions=[[dict(role='user', content='A blue fixture was described.')]]))
                        selection.append(dict(question_id=qid, dataset='LongMemEval', category=category,
                            corpus_id='corpus_' + qid, judge_type=category, abstention=False))
                serial.save(cohort / 'selection.json', selection)
                serial.save(cohort / 'sample.json', sample)
                config = root / 'fixture-config.json'
                serial.save(config, {})
                out = root / 'run'
                args = SimpleNamespace(out=out, cohort=cohort, config=config)
                if controller is regression:
                    controller.prepare(args)
                else:
                    out.mkdir()
                    serial.save(out / 'selection.json', selection)
                    hashes = blind.materialize(out, sample, selection)
                    serial.save(out / 'selection-freeze.json', dict(
                        selection_sha256=serial.digest(out / 'selection.json'), blind_input_sha256=hashes,
                        exposure_limit='Self-authored integration fixture, no benchmark samples.'))
                    controller.freeze(args)
                frozen = controller.checks(args)
                cases = serial.check_serial_freeze(out, frozen)
                self.assertEqual(len(cases), sum(counts.values()))
                self.assertEqual(len({case['prefix'] for case in cases}), len(cases))
                for case in cases:
                    child = out / case['directory']
                    self.assertEqual(len(serial.read(child / blind.FILES['write'])), 1)
                    self.assertFalse((child / 'execution.json').exists())


if __name__ == '__main__':
    unittest.main()
