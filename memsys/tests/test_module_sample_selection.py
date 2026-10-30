import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('module_sample',ROOT/'scripts/run_accuracy_iteration.py')
sampler=importlib.util.module_from_spec(spec)
spec.loader.exec_module(sampler)


class ModuleSampleSelectionTest(unittest.TestCase):
    def test_five_per_module_retains_four_case_base_without_label_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            shutil.copytree(ROOT/'src/memsys',root/'src/memsys',ignore=shutil.ignore_patterns('__pycache__'))
            rows=[dict(question_id=typ+'-'+str(i),question_type=typ,answer='first reference',
                       haystack_sessions=[[dict(role='user',content='Recorded source.')]])
                  for typ in sampler.MODULE_COUNTS for i in range(12)]
            fixed=[r for r in rows if int(r['question_id'].rsplit('-',1)[1])<4]
            data=root/'data.json';paired=root/'paired.json';config=root/'config.json'
            data.write_text(json.dumps(rows));paired.write_text(json.dumps(fixed))
            config.write_text(json.dumps(dict(models={'hash':dict(model='hash-256',base_url='local:hash')},
                                             roles={'embedding':'hash'},memory={})))
            def prepare(name):
                args=SimpleNamespace(out=root/'runs'/name,data=data,paired=paired,exclude_sample=None,
                    seed='fixed-five-module-seed',sample_size=30,config=config,workers=1)
                with patch.object(sampler,'ROOT',root):sampler.prepare(args)
                return json.loads((args.out/'sample30.json').read_text())
            first=prepare('one')
            for row in rows:row['answer']='changed reference'
            data.write_text(json.dumps(rows))
            prior=root/'runs/prior';prior.mkdir()
            (prior/'hypotheses.jsonl').write_text('\n'.join(json.dumps(dict(question_id=r['question_id'],
                hypothesis='prior answer')) for r in rows))
            second=prepare('two')
            self.assertEqual([r['question_id'] for r in first],[r['question_id'] for r in second])
            self.assertTrue({r['question_id'] for r in fixed}<={r['question_id'] for r in first})
            self.assertEqual({typ:sum(r['question_type']==typ for r in first) for typ in sampler.MODULE_COUNTS},
                             sampler.FIVE_COUNTS)

    def test_four_per_module_is_blind_to_answers_and_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            shutil.copytree(ROOT/'src/memsys',root/'src/memsys',ignore=shutil.ignore_patterns('__pycache__'))
            rows=[dict(question_id=typ+'-'+str(i),question_type=typ,answer='first label',
                       haystack_sessions=[[dict(role='user',content='Recorded source.')]])
                  for typ in sampler.MODULE_COUNTS for i in range(20)]
            fixed=[r for r in rows if int(r['question_id'].rsplit('-',1)[1])<sampler.SMALL_COUNTS[r['question_type']]]
            excluded=[r for r in rows if r['question_id'].endswith('-19')]
            def write(name,value):
                p=root/name;p.write_text(json.dumps(value));return p
            data=write('data.json',rows)
            paired=write('paired.json',fixed)
            exclusion=write('exclude.json',excluded)
            config=write('config.json',dict(models={'hash':dict(model='hash-256',base_url='local:hash')},
                                         roles={'embedding':'hash'},memory={}))
            def prepare(name):
                args=SimpleNamespace(out=root/'runs'/name,data=data,paired=paired,exclude_sample=exclusion,
                    seed='fixed-module-seed',sample_size=24,config=config,workers=1)
                with patch.object(sampler,'ROOT',root):sampler.prepare(args)
                return json.loads((args.out/'sample24.json').read_text())
            first=prepare('one')
            for row in rows:row['answer']='different reference label'
            data.write_text(json.dumps(rows))
            prior=root/'runs/previous';prior.mkdir()
            (prior/'hypotheses.jsonl').write_text('\n'.join(json.dumps(dict(question_id=r['question_id'],hypothesis='old output')) for r in rows))
            second=prepare('two')
            self.assertEqual([r['question_id'] for r in first],[r['question_id'] for r in second])
            self.assertEqual(len({r['question_id'] for r in first}),24)
            self.assertEqual({typ:sum(r['question_type']==typ for r in first) for typ in sampler.MODULE_COUNTS},sampler.MODULE_COUNTS)
            self.assertFalse({r['question_id'] for r in first}&{r['question_id'] for r in excluded})
            for typ in ['single-session-user','single-session-assistant','single-session-preference']:
                self.assertTrue({r['question_id'] for r in fixed if r['question_type']==typ}<={r['question_id'] for r in first})
