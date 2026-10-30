"""Cohort extension reuses only identical audited sources, never scored stores."""
import copy
import importlib.util
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

from helpers import settings
from memsys.bench.longmemeval import load
from memsys.bench.runner import Runner
from memsys.cli import _fake_models

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import clone_frozen_reader_seeds as cloner
import extend_frozen_ingestion as extender
import seal_frozen_ingestion as sealer


class ExtendedIngestionTest(unittest.TestCase):
    def fixture(self,root):
        seed,target=root/'seed',root/'target'
        seed.mkdir();target.mkdir()
        raw=json.loads((ROOT/'tests/fixtures/mini_oracle.json').read_text())[0]
        added=copy.deepcopy(raw);added['question_id']='synthetic-added'
        source=dict(source_sha256='code',write_source_sha256='writer',index_source_sha256='index')
        config=dict(models={'fake':dict(model='fake',base_url='local:hash')},
                    roles={r:'fake' for r in ('extract','resolve','embedding')},memory={},fallbacks={})
        for folder,rows in ((seed,[raw]),(target,[raw,added])):
            cloner.save(folder/'sample.json',rows);cloner.save(folder/'config.frozen.json',config)
            cloner.save(folder/'freeze.json',dict(source=source,prompts='prompt',sample_file='sample.json',
                sample_sha256=cloner.sha(folder/'sample.json'),config_sha256=cloner.sha(folder/'config.frozen.json')))
        item=load(str(seed/'sample.json'))[0][0]
        _,timing=Runner(_fake_models(),settings(),str(seed/'current'))._memory(item,ingest_only=True)
        path=seed/'current/stores'/(item.question_id+'.sqlite')
        row=dict(question_id=item.question_id,store_sha256=cloner.sha(path),**timing)
        cloner.save(seed/'ingestion.json',dict(status='completed',failures=[],results=[row]))
        cloner.save(seed/'source-audit.json',dict(status='passed'))
        (seed/'store-seed').mkdir();shutil.copy2(path,seed/'store-seed'/path.name)
        cloner.save(seed/'ingest-freeze.json',dict(immutable_seed=True,stores=[dict(question_id=item.question_id,
            store_sha256=cloner.sha(path))],source_audit_sha256=cloner.sha(seed/'source-audit.json'),
            ingestion_sha256=cloner.sha(seed/'ingestion.json')))
        return seed,target,[source,'prompt','writefp','indexfp']

    def test_exact_clone_still_rejects_a_superset(self):
        with tempfile.TemporaryDirectory() as directory:
            seed,target,identity=self.fixture(Path(directory))
            with patch.object(cloner,'identity',return_value=identity):
                with self.assertRaisesRegex(ValueError,'source inputs differ'):cloner.clone(seed,target)
            self.assertFalse((target/'current').exists())

    def test_extension_preserves_bytes_and_marks_only_added_sources_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            seed,target,identity=self.fixture(Path(directory))
            with patch.object(cloner,'identity',return_value=identity):extender.extend(seed,target)
            proof=json.loads((target/'ingestion-reuse.json').read_text())
            self.assertEqual((proof['reused'],proof['pending_new_writes']),(1,1))
            rows=[json.loads(s) for s in (target/'current/ingestion.jsonl').read_text().splitlines()]
            self.assertTrue(rows[0]['cached_ingest']);self.assertEqual(rows[0]['ingest_s'],0)
            name=rows[0]['question_id']+'.sqlite'
            self.assertEqual(cloner.sha(seed/'store-seed'/name),cloner.sha(target/'current/stores'/name))
            self.assertFalse((target/'store-seed').exists())
            self.assertFalse((target/'current/hypotheses.jsonl').exists())
            self.assertEqual(json.loads((target/'added-sample.json').read_text())[0]['question_id'],'synthetic-added')

    def test_changed_writer_input_or_store_fails_before_reuse(self):
        for change in ('writer','input','store'):
            with self.subTest(change=change),tempfile.TemporaryDirectory() as directory:
                seed,target,identity=self.fixture(Path(directory))
                state=json.loads((target/'freeze.json').read_text())
                if change=='writer':state['source']['write_source_sha256']='changed'
                elif change=='input':
                    rows=json.loads((target/'sample.json').read_text());rows[0]['haystack_sessions'][0][0]['content']='changed original'
                    cloner.save(target/'sample.json',rows);state['sample_sha256']=cloner.sha(target/'sample.json')
                else:
                    path=next((seed/'store-seed').glob('*.sqlite'));path.write_bytes(b'changed store')
                cloner.save(target/'freeze.json',state)
                with patch.object(cloner,'identity',return_value=identity):
                    with self.assertRaises(ValueError):extender.extend(seed,target)
                self.assertFalse((target/'current').exists())

    def test_seal_checks_real_originals_and_refuses_to_replace_seed(self):
        with tempfile.TemporaryDirectory() as directory:
            seed,target,identity=self.fixture(Path(directory))
            with patch.object(cloner,'identity',return_value=identity):extender.extend(seed,target)
            records=[json.loads(s) for s in (target/'current/ingestion.jsonl').read_text().splitlines()]
            item=load(str(target/'added-sample.json'))[0][0]
            _,timing=Runner(_fake_models(),settings(),str(target/'current'))._memory(item,ingest_only=True)
            path=target/'current/stores'/(item.question_id+'.sqlite')
            records.append(dict(question_id=item.question_id,store_sha256=cloner.sha(path),**timing))
            cloner.save(target/'ingestion.json',dict(status='completed',failures=[],results=records))
            cloner.save(target/'source-audit.json',dict(status='passed',source_sha256='code',
                sample_sha256=cloner.sha(target/'sample.json')))
            with patch.object(sealer,'identity',return_value=identity):
                sealer.seal(target)
                with self.assertRaisesRegex(ValueError,'already exist'):sealer.seal(target)
            self.assertEqual(len(list((target/'store-seed').glob('*.sqlite'))),2)

    def test_seal_rejects_answers_before_any_store_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            seed,target,identity=self.fixture(Path(directory))
            with patch.object(cloner,'identity',return_value=identity):extender.extend(seed,target)
            cloner.save(target/'ingestion.json',dict(status='completed',failures=[],results=[]))
            cloner.save(target/'source-audit.json',dict(status='passed',source_sha256='code',
                sample_sha256=cloner.sha(target/'sample.json')))
            (target/'current/hypotheses.jsonl').write_text('existing answer\n')
            with patch.object(sealer,'identity',return_value=identity):
                with self.assertRaisesRegex(ValueError,'pre-answer'):sealer.seal(target)
            self.assertFalse((target/'store-seed').exists())
