import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path

from helpers import settings
from memsys.bench.longmemeval import load
from memsys.bench.runner import Runner
from memsys.cli import _fake_models

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('fresh_ingestion',ROOT/'scripts/ingest_frozen_iteration.py')
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class FreshIngestionCoverageTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.item=load(str(ROOT/'tests/fixtures/mini_oracle.json'))[0][0]
        Runner(_fake_models(),settings(),self.tmp.name)._memory(self.item,ingest_only=True)
        self.path=Path(self.tmp.name)/'stores'/(self.item.question_id+'.sqlite')

    def test_exact_sources_pass_and_changed_content_is_rejected(self):
        result=module.original_coverage(self.path,self.item)
        self.assertTrue(result['exact_originals'])
        self.assertEqual(result['turns'],sum(len(s.turns) for s in self.item.sessions))
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE turns SET content='changed' WHERE rowid=(SELECT min(rowid) FROM turns)")
        with self.assertRaisesRegex(ValueError,'Original turn coverage'):
            module.original_coverage(self.path,self.item)

    def test_missing_session_is_rejected(self):
        with sqlite3.connect(self.path) as db:
            db.execute('DELETE FROM sessions WHERE rowid=(SELECT min(rowid) FROM sessions)')
        with self.assertRaisesRegex(ValueError,'Original session coverage'):
            module.original_coverage(self.path,self.item)
