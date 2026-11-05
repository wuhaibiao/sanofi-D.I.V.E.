import argparse
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
sys.path.insert(0,str(ROOT/'src'))
import run_oracle500_recovery as recovery


class Backend:
    def __init__(self):
        self.names={'external_records'}
    def request(self,method,path,body=None):
        if method=='GET' and path=='/collections':
            return {'collections':[{'name':n} for n in sorted(self.names)]}
        if method=='DELETE':
            self.names.remove(path.split('/')[-1]);return True
        raise AssertionError((method,path))


class RecoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.out=Path(self.tmp.name);self.backend=Backend();self.events=[]
        self.cases=[];self.fail_once=set();self.fail_always=set();self.write_counts={}
        for index in (1,2,3):
            case=dict(index=index,question_id='fixture-'+str(index),directory='questions/'+str(index).zfill(4),
                      prefix='fixture_q'+str(index).zfill(4),files={})
            directory=self.out/case['directory'];directory.mkdir(parents=True)
            for name in ('freeze.json','selection.json','config.frozen.json','write-inputs.json'):
                recovery.save(directory/name,{})
            self.cases.append(case)
        self.plan=dict(workers=2,max_write_attempts=2,retry_backoff_s=0,external_collections=['external_records'])
        self.args=argparse.Namespace(out=self.out,runtime=self.out/'unused-private-runtime.json')
        self.runner=recovery.Recovery(self.args,dict(prefix='fixture'),self.plan,self.cases,
                                      backend=self.backend,invoke=self.invoke,check=lambda:None)
        self.patcher=patch.object(recovery.serial,'check_case',lambda out,case:out/case['directory'])
        self.patcher.start();self.addCleanup(self.patcher.stop)

    def invoke(self,directory,phase,runtime):
        if phase=='write':
            case=next(c for c in self.cases if str(directory).startswith(str(self.out/c['directory'])))
            qid=case['question_id'];self.write_counts[qid]=self.write_counts.get(qid,0)+1
            complete=qid not in self.fail_always and not(qid in self.fail_once and self.write_counts[qid]==1)
            self.backend.names.add(case['prefix']+'_records')
            session=dict(source_exact=True,error=None if complete else {'type':'TransientError'},
                         status='closed' if complete else 'open',raw_messages=2,processed_messages=2 if complete else 1)
            (directory/'LongMemEval-write.jsonl').write_text(json.dumps(dict(complete=complete,pending_messages=0 if complete else 1,sessions=[session]))+'\n')
            recovery.save(directory/'LongMemEval-write-summary.json',dict(elapsed_s=1))
            (directory/'LongMemEval-write-usage.jsonl').write_text(json.dumps(dict(attempt=self.write_counts[qid]))+'\n')
            self.events.append(('write',qid,complete))
        elif phase=='audit':
            recovery.save(directory/'storage-audit.json',dict(episode_vectors=0,episode_chunks=0))
            self.events.append(('audit',directory.name))
        elif phase=='read':
            case=next(c for c in self.cases if directory==self.out/c['directory'])
            self.assertTrue((self.out/'ingestion-barrier.json').exists())
            self.assertTrue(all(r.get('ingested') for r in self.runner.state['cases'].values()))
            (directory/'LongMemEval-answers.jsonl').write_text(json.dumps(dict(question_id=case['question_id'],status='answered',hypothesis='fixture answer'))+'\n')
            self.events.append(('read',case['question_id']))
        elif phase=='judge':
            self.assertFalse(any(n.startswith('fixture_') for n in self.backend.names))
            case=next(c for c in self.cases if directory==self.out/c['directory'])
            (directory/'LongMemEval-grades.jsonl').write_text(json.dumps(dict(question_id=case['question_id'],status='graded',correct=True))+'\n')
            self.events.append(('judge',case['question_id']))
        else:
            raise AssertionError(phase)
        return 0

    def test_all_ingestion_and_audits_precede_every_answer(self):
        self.runner.run()
        phases=[r[0] for r in self.events]
        self.assertLess(max(i for i,p in enumerate(phases) if p in ('write','audit')),phases.index('read'))
        self.assertLess(max(i for i,p in enumerate(phases) if p=='read'),phases.index('judge'))
        self.assertTrue(self.runner.state['complete'])
        self.assertEqual(self.backend.names,{'external_records'})

    def test_incomplete_source_blocks_all_answers(self):
        self.fail_always.add('fixture-2')
        with self.assertRaisesRegex(ValueError,'Ingestion barrier blocked'):
            self.runner.run()
        self.assertFalse(any(e[0] in ('read','judge') for e in self.events))
        self.assertFalse(self.runner.state['complete'])
        self.assertIn('fixture_q0001_records',self.backend.names)
        self.assertIn('external_records',self.backend.names)

    def test_retry_evidence_and_usage_are_preserved(self):
        self.fail_once.add('fixture-1')
        self.runner.run()
        directory=self.out/self.cases[0]['directory']
        self.assertTrue((directory/'write-attempts/01/LongMemEval-write.jsonl').exists())
        self.assertTrue((directory/'write-attempts/02/LongMemEval-write.jsonl').exists())
        self.assertEqual(len(recovery.rows(directory/'LongMemEval-write-usage.jsonl')),2)
        self.assertEqual(self.write_counts['fixture-1'],2)
        self.assertEqual(self.write_counts['fixture-2'],1)

    def test_changed_write_proof_blocks_global_barrier(self):
        for c in self.cases:self.runner.write(c)
        directory=self.out/self.cases[0]['directory']
        with (directory/'LongMemEval-write.jsonl').open('a') as stream:stream.write('\n')
        with self.assertRaisesRegex(ValueError,'write evidence changed'):
            self.runner.barrier()
        self.assertFalse(any(e[0]=='read' for e in self.events))

    def test_audit_raw_vectors_halt_before_answers(self):
        native=self.invoke
        def wrong_audit(directory,phase,runtime):
            code=native(directory,phase,runtime)
            if phase=='audit':
                recovery.save(directory/'storage-audit.json',dict(episode_vectors=1,episode_chunks=0))
            return code
        self.runner.invoke=wrong_audit
        with self.assertRaisesRegex(ValueError,'Original or chunk vectors detected'):
            self.runner.run()
        self.assertFalse(any(e[0] in ('read','judge') for e in self.events))

    def test_cleanup_preserves_staged_sibling(self):
        for c in self.cases:self.runner.write(c)
        self.runner.barrier();self.runner.answer(self.cases[0])
        self.assertNotIn('fixture_q0001_records',self.backend.names)
        self.assertIn('fixture_q0002_records',self.backend.names)
        self.assertIn('external_records',self.backend.names)

    def test_changed_global_barrier_cannot_release_answers(self):
        for c in self.cases:self.runner.write(c)
        self.runner.barrier()
        proof=recovery.read(self.out/'ingestion-barrier.json');proof['total']=0
        recovery.save(self.out/'ingestion-barrier.json',proof)
        with self.assertRaisesRegex(ValueError,'global ingestion barrier'):
            self.runner.answer(self.cases[0])
        self.assertFalse(any(e[0]=='read' for e in self.events))

    def test_closed_failed_session_is_not_duplicated_on_retry(self):
        native=self.invoke;self.fail_always.add('fixture-1')
        def closed_failure(directory,phase,runtime):
            code=native(directory,phase,runtime)
            if phase=='write':
                path=directory/'LongMemEval-write.jsonl';value=recovery.rows(path)[0]
                if not value['complete']:
                    value['sessions'][0]['status']='closed'
                    path.write_text(json.dumps(value)+'\n')
            return code
        self.runner.invoke=closed_failure
        with self.assertRaisesRegex(ValueError,'non-open session'):
            self.runner.write(self.cases[0])
        self.assertEqual(self.write_counts['fixture-1'],1)


if __name__=='__main__':unittest.main()
