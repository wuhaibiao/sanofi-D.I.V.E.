"""Own-system ablations preserve budgets and reject mutated inputs before calls."""
import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from memsys.config import MemorySettings

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
spec = importlib.util.spec_from_file_location('own_memory_comparison', ROOT/'scripts/run_memsys_comparison.py')
controller = importlib.util.module_from_spec(spec)
spec.loader.exec_module(controller)


class MemsysComparisonTest(unittest.TestCase):
    def fixture(self,root):
        (root/'tools').mkdir()
        (root/'tools/synthetic.py').write_text('synthetic = True\n')
        controller.save(root/'source-audit.json', {'status':'passed'})
        variants = {}
        for name in controller.VARIANTS:
            folder = root/name
            (folder/'store-seed').mkdir(parents=True)
            (folder/'frozen-src/memsys').mkdir(parents=True)
            (folder/'store-seed/synthetic.sqlite').write_bytes(b'immutable synthetic fixture')
            (folder/'frozen-src/memsys/read.py').write_text('synthetic = True\n')
            settings = MemorySettings(selection_policy=name, complete_episode_context=False,
                complete_context_skip_rerank=False, complete_context_direct_query=False).to_dict()
            controller.save(folder/'config.frozen.json',dict(models={},roles={},memory=settings,fallbacks={}))
            controller.save(folder/'sample24.json',[dict(question_id='synthetic')])
            variants[name] = dict(config_sha256=controller.sha(folder/'config.frozen.json'),
                sample_sha256=controller.sha(folder/'sample24.json'),sample_file='sample24.json',
                source_files=controller.package_files(folder/'frozen-src'),stages=[])
        state = dict(mem0_execution=False,variants=variants,
            tool_sha256={'synthetic.py':controller.sha(root/'tools/synthetic.py')},
            source_audit_sha256=controller.sha(root/'source-audit.json'),
            stores={'synthetic':controller.sha(root/'ranked/store-seed/synthetic.sqlite')})
        controller.save(root/'comparison.json',state)
        return state

    def test_identical_budgets_only_policy_differs(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            state=self.fixture(root)
            controller.check(root,state)
            for name in controller.VARIANTS:
                profile=controller.read(root/name/'config.frozen.json')
                self.assertEqual(profile['memory']['memory_input_budget_tokens'],16384)

    def test_mutated_frozen_inputs_fail_before_subprocess(self):
        for name in ('config.frozen.json','sample24.json','frozen-src/memsys/read.py','store-seed/synthetic.sqlite'):
            with self.subTest(name=name),tempfile.TemporaryDirectory() as directory:
                root=Path(directory)
                self.fixture(root)
                path=root/'coverage'/name
                path.write_bytes(path.read_bytes()+b'changed')
                with patch.object(controller.subprocess,'run') as process:
                    with self.assertRaisesRegex(ValueError,'changed'):
                        controller.execute(SimpleNamespace(out=root,variant='all'))
                    process.assert_not_called()

    def test_mem0_variant_or_flag_is_always_rejected(self):
        for change in ('variant','flag'):
            with self.subTest(change=change),tempfile.TemporaryDirectory() as directory:
                root=Path(directory)
                state=self.fixture(root)
                if change=='variant':
                    state['variants']['mem0']=copy.deepcopy(state['variants']['ranked'])
                else:
                    state['mem0_execution']=True
                with self.assertRaisesRegex(ValueError,'Only memsys'):
                    controller.check(root,state)

    def test_bypass_cannot_be_enabled_even_with_updated_config_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            state=self.fixture(root)
            path=root/'coverage/config.frozen.json'
            config=controller.read(path)
            config['memory']['complete_episode_context']=True
            controller.save(path,config)
            state['variants']['coverage']['config_sha256']=controller.sha(path)
            with self.assertRaisesRegex(ValueError,'Whole-history bypass'):
                controller.check(root,state)

    def test_model_or_budget_difference_is_not_a_policy_ablation(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            state=self.fixture(root)
            path=root/'coverage/config.frozen.json'
            config=controller.read(path)
            config['memory']['answer_input_budget_tokens']+=1
            controller.save(path,config)
            state['variants']['coverage']['config_sha256']=controller.sha(path)
            with self.assertRaisesRegex(ValueError,'budgets differ'):
                controller.check(root,state)

    def test_sampling_never_uses_reference_or_correctness(self):
        rows=[dict(question_id='synthetic-'+kind+str(i),question_type=kind,answer='first')
              for kind in controller.MODULE_COUNTS for i in range(4)]
        expected=controller.validate_sample(rows)
        for row in rows:
            row.update(answer='second',correct=False)
        self.assertEqual(controller.validate_sample(rows),expected)
        with self.assertRaisesRegex(ValueError,'four unique'):
            controller.validate_sample(rows+[rows[0]])

    def test_shared_plans_cannot_change_after_freeze(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            state=self.fixture(root)
            (root/'controls/rerank-traces').mkdir(parents=True)
            controller.save(root/'controls/plans.json',{'synthetic':{'plan':{}}})
            state['controls']=dict(plans_sha256=controller.sha(root/'controls/plans.json'),
                                   rerank_trace_sha256={})
            controller.check(root,state)
            controller.save(root/'controls/plans.json',{'synthetic':{'plan':{'operation':'advice'}}})
            with self.assertRaisesRegex(ValueError,'shared query plans changed'):
                controller.check(root,state)

    def test_replay_windows_cannot_be_added_or_modified_after_freeze(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            state=self.fixture(root)
            (root/'controls/rerank-traces').mkdir(parents=True)
            controller.save(root/'controls/plans.json',{})
            state['controls']=dict(plans_sha256=controller.sha(root/'controls/plans.json'),
                                   rerank_trace_sha256={})
            controller.save(root/'controls/rerank-traces/unexpected.json',{'message':'synthetic'})
            with self.assertRaisesRegex(ValueError,'reranking replay inputs changed'):
                controller.check(root,state)


if __name__=='__main__':
    unittest.main()
