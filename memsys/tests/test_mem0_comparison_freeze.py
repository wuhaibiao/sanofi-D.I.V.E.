"""Retain historical audit helpers, but never resume native Mem0 runs."""
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
import mem0_bridge
spec = importlib.util.spec_from_file_location('retired_comparison_freeze', ROOT/'scripts/run_mem0_comparison.py')
controller = importlib.util.module_from_spec(spec)
spec.loader.exec_module(controller)


class ComparisonFreezeTest(unittest.TestCase):
    def test_prepare_and_execute_are_disabled_even_without_cli(self):
        for call in (controller.prepare, controller.execute):
            with self.assertRaises(mem0_bridge.Mem0ExecutionDisabled):
                call(SimpleNamespace())

    def test_historical_sample_audit_does_not_depend_on_labels(self):
        for count in (4,5):
            rows = [dict(question_id='synthetic-'+typ+str(i), question_type=typ, answer='first')
                    for typ in controller.TYPES for i in range(count)]
            expected = controller.checked_sample(rows)
            for row in rows:
                row['answer'] = 'changed'
            self.assertEqual(expected, controller.checked_sample(rows))
            self.assertEqual(expected, {typ:count for typ in controller.TYPES})

    def test_concurrent_audit_records_do_not_lose_other_variants(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            controller.save(root/'comparison.json', dict(variants={name:{'stages':[]} for name in controller.VARIANTS}))
            entries = [(name, dict(stage='run', log='run-%d.log'%i, returncode=0))
                       for i in range(6) for name in controller.VARIANTS]
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(lambda args: controller.record_stage(root, *args), entries))
            state = controller.read(root/'comparison.json')
            self.assertTrue(all(len(row['stages'])==6 for row in state['variants'].values()))


if __name__ == '__main__':
    unittest.main()
