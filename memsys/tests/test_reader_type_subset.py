"""Necessary category regressions retain all four cases, including negatives."""
import importlib.util
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
spec=importlib.util.spec_from_file_location('reader_type_subset',ROOT/'scripts/evaluate_reader_types.py')
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ReaderTypeSubsetTest(unittest.TestCase):
    def fixture(self):
        return [dict(question_id='synthetic-'+kind+str(n),question_type=kind,answer='first',correct=n!=0)
                for kind in module.MODULE_COUNTS for n in range(4)]

    def test_selection_uses_only_type_and_keeps_all_cases(self):
        rows=self.fixture()
        first=module.select_types(rows,['temporal-reasoning'])
        self.assertEqual(len(first),4)
        for row in rows:
            row.update(answer='changed',correct=not row['correct'])
        self.assertEqual([r['question_id'] for r in first],
                         [r['question_id'] for r in module.select_types(rows,['temporal-reasoning'])])

    def test_missing_duplicate_or_unknown_type_is_rejected(self):
        rows=self.fixture()
        with self.assertRaises(ValueError):
            module.select_types(rows[:-1],['single-session-preference'])
        with self.assertRaises(ValueError):
            module.select_types(rows+[rows[0]],['multi-session'])
        with self.assertRaises(ValueError):
            module.select_types(rows,['temporal-reasoning','temporal-reasoning'])
        with self.assertRaises(ValueError):
            module.select_types(rows,['unknown'])


if __name__=='__main__':
    unittest.main()
