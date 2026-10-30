import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import unittest

from helpers import settings
from memsys.bench.longmemeval import load
from memsys.bench.runner import Runner
from memsys.cli import _fake_models

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('ingestion_dates',ROOT/'scripts/audit_frozen_ingestion.py')
auditor=importlib.util.module_from_spec(spec)
spec.loader.exec_module(auditor)


class FrozenIngestionDatesTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.item=load(str(ROOT/'tests/fixtures/mini_oracle.json'))[0][0]
        Runner(_fake_models(),settings(),self.tmp.name)._memory(self.item,ingest_only=True)
        self.path=Path(self.tmp.name)/'stores'/(self.item.question_id+'.sqlite')

    def test_original_sources_and_clock_anchors_pass(self):
        report=auditor.verify_store(self.path,self.item)
        self.assertTrue(report['exact_temporal_anchors'])
        self.assertEqual(report['turns'],sum(len(s.turns) for s in self.item.sessions))

    def test_corrupted_turn_or_episode_date_is_rejected(self):
        for table in ['turns','episodes']:
            with self.subTest(table=table):
                with sqlite3.connect(self.path) as db:
                    old=db.execute('SELECT at FROM '+table+' ORDER BY rowid LIMIT 1').fetchone()[0]
                    db.execute('UPDATE '+table+" SET at='2020-01-01T00:00:00' WHERE rowid=(SELECT min(rowid) FROM "+table+')')
                with self.assertRaisesRegex(ValueError,'date|temporal anchor'):
                    auditor.verify_store(self.path,self.item)
                with sqlite3.connect(self.path) as db:
                    db.execute('UPDATE '+table+' SET at=? WHERE rowid=(SELECT min(rowid) FROM '+table+')',(old,))
