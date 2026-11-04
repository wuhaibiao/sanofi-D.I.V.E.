"""Synthetic concurrent cases prove isolation, admission and retained outcomes."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import run_oracle500_parallel as parallel
import question_serial_eval as serial
import blind_eval_inputs as blind
import evaluate_public_types as worker


class Backend:
    def __init__(self):
        self.names = {'personal_records', 'parallel_fixtureextra_records'}
        self.lock = threading.RLock()
        self.deleted = []

    def request(self, method, path):
        with self.lock:
            if method == 'GET':
                return {'collections':[{'name':n} for n in self.names]}
            name = path.removeprefix('/collections/')
            self.names.remove(name)
            self.deleted.append(name)
            return {}


class ParallelTests(unittest.TestCase):
    def prepare(self, out):
        metas, sample = [], []
        for i in range(4):
            qid='authored-'+str(i)
            metas.append(dict(question_id=qid,dataset='LongMemEval',category='single-session-user',
                judge_type='single-session-user',abstention=False,corpus_id='authored-shared-source'))
            sample.append(dict(question_id=qid,question_type='single-session-user',question='Authored color?',
                question_date='2026-01-03',answer='LABEL_CANARY',haystack_dates=['2026-01-02'],
                haystack_session_ids=['authored-history'],haystack_sessions=[[dict(role='user',content='The fixture is blue.')]]))
        serial.save(out/'selection.json',metas);serial.save(out/'config.frozen.json',{})
        frozen=dict(source={'source_sha256':'synthetic'},prefix='parallel_fixture',namespace='fixture',
            config_sha256=serial.digest(out/'config.frozen.json'),runner_sha256=serial.digest(worker.__file__),
            blind_input_builder_sha256=serial.digest(blind.__file__),blind_input_sha256=blind.materialize(out,sample,metas))
        serial.freeze_cases(out,frozen);serial.save(out/'freeze.json',frozen)
        state=dict(started_at=datetime.now(timezone.utc).isoformat(),cases=[],current=[],complete=False)
        serial.save(out/'execution.json',state)
        return frozen

    def append(self,path,value):
        with path.open('a') as f:f.write(json.dumps(value)+'\n')

    def test_parallel_writes_single_read_and_global_judge_barrier(self):
        with tempfile.TemporaryDirectory() as directory:
            out=Path(directory);frozen=self.prepare(out);backend=Backend()
            barrier=threading.Barrier(2)
            lock=threading.Lock();counts={'reads':0,'peak':0,'answered':0};events=[]
            def invoke(case,phase,runtime):
                child=serial.read(case/'freeze.json');prefix=child['prefix'];qid=child['question_id']
                with lock:events.append((qid,phase))
                if phase=='write':
                    with backend.lock:backend.names.add(prefix+'_records')
                    barrier.wait(timeout=3)
                    self.append(case/'LongMemEval-write.jsonl',dict(complete=True))
                elif phase=='audit':
                    self.assertEqual(serial.read(case/'execution.json')['current'],'read')
                    with backend.lock:self.assertIn(prefix+'_records',backend.names)
                elif phase=='read':
                    with lock:
                        counts['reads']+=1;counts['peak']=max(counts['peak'],counts['reads'])
                    time.sleep(.02)
                    self.append(case/'LongMemEval-answers.jsonl',dict(question_id=qid,status='answered',hypothesis='blue'))
                    with lock:counts['reads']-=1;counts['answered']+=1
                elif phase=='judge':
                    self.assertEqual(counts['answered'],4)
                    with backend.lock:self.assertFalse(any(n.startswith(frozen['prefix']+'_') for n in backend.names))
                    self.append(case/'LongMemEval-grades.jsonl',dict(question_id=qid,correct=True,status='graded'))
                return 0
            plan=dict(workers=2,serial_completed=0,external_collections=sorted(backend.names))
            run=parallel.ParallelRun(SimpleNamespace(out=out,runtime=Path('unused')),frozen,plan,
                backend=backend,invoke=invoke,validate=lambda:None,publish=lambda:None)
            run.run()
            self.assertEqual(counts['peak'],1)
            self.assertEqual(backend.names,{'personal_records','parallel_fixtureextra_records'})
            self.assertTrue(run.state['complete'])
            self.assertEqual(len(backend.deleted),4)
            self.assertTrue(all(r['cleanup']['verified_empty'] for r in run.state['cases']))
            first_judge=next(i for i,e in enumerate(events) if e[1]=='judge')
            self.assertEqual(sum(e[1]=='read' for e in events[:first_judge]),4)

    def test_completed_and_failed_answers_are_skipped_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as directory:
            out=Path(directory);frozen=self.prepare(out);backend=Backend();cases=serial.read(out/'question-cases.json')
            preserved={};state=serial.read(out/'execution.json')
            for i,case in enumerate(cases[:2]):
                p=out/case['directory'];self.append(p/'LongMemEval-answers.jsonl',dict(question_id=case['question_id'],
                    status='error' if i else 'answered',hypothesis='' if i else 'blue'))
                record=dict(index=case['index'],question_id=case['question_id'],prefix=case['prefix'],outcomes=[],
                    cleanup=dict(verified_empty=True),current=None)
                serial.save(p/'execution.json',record);state['cases'].append(record)
                preserved[p/'LongMemEval-answers.jsonl']=(p/'LongMemEval-answers.jsonl').read_bytes()
            serial.save(out/'execution.json',state)
            events=[]
            def invoke(case,phase,runtime):
                i=int(case.name);events.append((i,phase))
                if i<=2:self.assertEqual(phase,'judge')
                if phase=='write':
                    with backend.lock:backend.names.add(cases[i-1]['prefix']+'_records')
                if phase=='read':self.append(case/'LongMemEval-answers.jsonl',dict(question_id=cases[i-1]['question_id'],status='answered',hypothesis='blue'))
                return 0
            run=parallel.ParallelRun(SimpleNamespace(out=out,runtime=Path('unused')),frozen,
                dict(workers=2,serial_completed=2,external_collections=sorted(backend.names)),
                backend=backend,invoke=invoke,validate=lambda:None,publish=lambda:None)
            run.run()
            for path,data in preserved.items():self.assertEqual(path.read_bytes(),data)
            self.assertEqual(sum(phase=='write' for _,phase in events),2)

    def test_failed_process_does_not_admit_more_cases_and_still_cleans(self):
        with tempfile.TemporaryDirectory() as directory:
            out=Path(directory);frozen=self.prepare(out);backend=Backend();events=[];barrier=threading.Barrier(2)
            def invoke(case,phase,runtime):
                i=int(case.name);events.append((i,phase))
                if phase=='write':
                    with backend.lock:backend.names.add(serial.read(case/'freeze.json')['prefix']+'_records')
                    barrier.wait(timeout=3)
                    return 7
                return 0
            run=parallel.ParallelRun(SimpleNamespace(out=out,runtime=Path('unused')),frozen,
                dict(workers=2,serial_completed=0,external_collections=sorted(backend.names)),
                backend=backend,invoke=invoke,validate=lambda:None,publish=lambda:None)
            with self.assertRaises(RuntimeError):run.run()
            self.assertEqual({i for i,_ in events},{1,2})
            self.assertTrue(all(r['cleanup']['verified_empty'] for r in run.state['cases']))
            self.assertFalse(run.state['complete'])

    def test_last_partial_append_ignored_but_invalid_complete_line_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)/'rows.jsonl';p.write_text('{"a":1}\n{"partial":')
            self.assertEqual(parallel.rows(p),[{'a':1}])
            p.write_text('{"a":1}\ninvalid\n')
            with self.assertRaises(json.JSONDecodeError):parallel.rows(p)

    def test_existing_read_is_adopted_without_any_model_call(self):
        with tempfile.TemporaryDirectory() as directory:
            out=Path(directory);frozen=self.prepare(out);backend=Backend();case=serial.read(out/'question-cases.json')[0]
            p=out/case['directory'];self.append(p/'LongMemEval-answers.jsonl',dict(question_id=case['question_id'],status='answered',hypothesis='blue'))
            serial.save(p/'LongMemEval-read-summary.json',{'elapsed_s':1})
            state=dict(cases=[dict(index=1,question_id=case['question_id'],prefix=case['prefix'],outcomes=[],current='read')])
            backend.names.add(case['prefix']+'_records')
            before=(p/'LongMemEval-answers.jsonl').read_bytes()
            _,completed,external=parallel.adopt_finished_read(out,frozen,state,backend)
            self.assertEqual(completed,1)
            self.assertEqual(before,(p/'LongMemEval-answers.jsonl').read_bytes())
            self.assertTrue(state['cases'][0]['cleanup']['verified_empty'])
            self.assertTrue(state['cases'][0]['outcomes'][0]['adopted_existing_process'])
            self.assertEqual(set(external),backend.names)


if __name__=='__main__':unittest.main()
