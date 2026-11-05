import argparse
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'));sys.path.insert(0,str(ROOT/'src'))
import run_oracle500_batches as batches


class Backend:
    def __init__(self):self.names={'personal_records'}
    def request(self,method,path,body=None):
        if method=='GET':return {'collections':[{'name':n} for n in sorted(self.names)]}
        if method=='DELETE':self.names.remove(path.split('/')[-1]);return True
        raise AssertionError((method,path))


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.out=Path(self.temp.name);self.backend=Backend();self.events=[];self.judge_labels={}
        self.cases=[];initial={}
        for i in (1,2,3):
            c=dict(index=i,question_id='authored-'+str(i),directory='questions/'+str(i).zfill(4),prefix='fixture_q'+str(i).zfill(4),files={})
            self.cases.append(c);d=self.out/c['directory'];d.mkdir(parents=True)
            for name in ('freeze.json','selection.json','config.frozen.json','write-inputs.json'):
                batches.save(d/name,{})
            initial[str(i)]=dict(index=i,question_id=c['question_id'],ingested=i<3,prior_attempt_count=0,attempts=[],prior_directory=str(self.out/'old'/str(i)))
            if i<3:
                self.write_proof(d);self.audit(d);self.backend.names.add(c['prefix']+'_records')
                initial[str(i)].update(write_sha256=batches.sha(d/'LongMemEval-write.jsonl'),audit_sha256=batches.sha(d/'storage-audit.json'))
        self.frozen=dict(prefix='fixture',source=dict(source_sha256='authored-source'))
        self.plan=dict(workers=2,max_write_attempts=3,retry_backoff_s=0,external_collections=['personal_records'],
                       initial_records=initial,batches=[[1,2],[3]])
        args=argparse.Namespace(out=self.out,runtime=self.out/'unused')
        self.runner=batches.BatchRun(args,self.frozen,self.plan,self.cases,backend=self.backend,invoke=self.invoke)
        for target,value in [(batches,'validate'),(batches,'report')]:
            p=patch.object(target,value,return_value=(self.frozen,self.plan,self.cases) if value=='validate' else {})
            p.start();self.addCleanup(p.stop)
        p=patch.object(batches.serial,'check_case',lambda out,case:out/case['directory']);p.start();self.addCleanup(p.stop)

    def write_proof(self,d,complete=True):
        value=dict(complete=complete,pending_messages=0 if complete else 1,
                   sessions=[dict(source_exact=True,error=None if complete else {'type':'TransientError'},
                                  status='closed' if complete else 'open',raw_messages=2,processed_messages=2 if complete else 1)])
        (d/'LongMemEval-write.jsonl').write_text(json.dumps(value)+'\n')
        batches.save(d/'LongMemEval-write-summary.json',dict(elapsed_s=1))

    def audit(self,d):
        batches.save(d/'storage-audit.json',dict(source_sha256='authored-source',episode_vectors=0,episode_chunks=0))

    def invoke(self,d,phase,runtime):
        c=next(c for c in self.cases if str(d).startswith(str(self.out/c['directory'])))
        self.events.append((phase,c['index']))
        if phase=='write':
            self.assertTrue(all(self.runner.state['cases'][str(i)].get('grade_status')=='graded' for i in (1,2)))
            self.backend.names.add(c['prefix']+'_records');self.write_proof(d)
        elif phase=='audit':self.audit(d)
        elif phase=='read':
            members=self.plan['batches'][self.runner.state['batch']-1]
            self.assertTrue(all(self.runner.state['cases'][str(i)]['ingested'] for i in members))
            (d/'LongMemEval-answers.jsonl').write_text(json.dumps(dict(question_id=c['question_id'],status='answered',hypothesis='Authored fixture blue.'))+'\n')
        elif phase=='judge':
            members=self.plan['batches'][self.runner.state['batch']-1]
            self.assertFalse(any(n.startswith(self.cases[i-1]['prefix']+'_') for i in members for n in self.backend.names))
            label=self.judge_labels.get(c['index'],True)
            (d/'LongMemEval-grades.jsonl').write_text(json.dumps(dict(question_id=c['question_id'],status='graded' if label is not None else 'judge_error',correct=label))+'\n')
        else:raise AssertionError(phase)
        return 0

    def test_existing_ingestions_answered_without_reextracting(self):
        self.runner.run()
        self.assertEqual([i for phase,i in self.events if phase=='write'],[3])
        self.assertTrue(self.runner.state['complete'])
        self.assertEqual(self.backend.names,{'personal_records'})

    def test_next_batch_cannot_ingest_until_first_batch_scored(self):
        self.runner.run()
        point=self.events.index(('write',3))
        self.assertTrue(all(self.events.index(('judge',i))<point for i in (1,2)))
        self.assertLess(max(self.events.index(('read',i)) for i in (1,2)),min(self.events.index(('judge',i)) for i in (1,2)))

    def test_incorrect_grade_does_not_change_next_batch(self):
        self.judge_labels[1]=False;self.runner.run()
        self.assertIn(('write',3),self.events)
        self.assertEqual(self.runner.state['batches'][1]['cases'],[3])

    def test_unscored_batch_blocks_remaining_ingestion(self):
        self.judge_labels[1]=None
        with self.assertRaisesRegex(ValueError,'Batch scoring incomplete'):
            self.runner.run()
        self.assertNotIn(('write',3),self.events)

    def test_cumulative_attempt_number_is_not_reset(self):
        self.runner.state['cases']['3']['prior_attempt_count']=2
        self.runner.run()
        self.assertTrue((self.out/self.cases[2]['directory']/'write-attempts/03/LongMemEval-write.jsonl').exists())
        self.assertFalse((self.out/self.cases[2]['directory']/'write-attempts/01').exists())

    def test_exhausted_ingestion_budget_never_answers_that_case(self):
        self.runner.state['cases']['3']['prior_attempt_count']=3
        with self.assertRaisesRegex(ValueError,'retry budget exhausted'):
            self.runner.run()
        self.assertNotIn(('read',3),self.events)

    def test_first_case_cleanup_preserves_ready_sibling(self):
        self.runner.state.update(batch=1);self.runner.barrier(self.cases[:2],1)
        self.runner.answer(self.cases[0])
        self.assertIn('fixture_q0002_records',self.backend.names)
        self.assertIn('personal_records',self.backend.names)

    def test_fixed_plan_includes_all_ready_then_all_remaining_once(self):
        cases=[dict(index=i) for i in range(1,11)]
        result=batches.fixed_batches(cases,{2,5,9},3)
        self.assertEqual(result,[[2,5,9],[1,3,4],[6,7,8],[10]])
        self.assertEqual(sorted(i for b in result for i in b),list(range(1,11)))


if __name__=='__main__':unittest.main()
