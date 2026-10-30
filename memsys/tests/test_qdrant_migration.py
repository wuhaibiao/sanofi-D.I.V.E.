"""Only the explicit legacy reader may read SQLite. Migration never calls a model."""
import hashlib
import json
import sqlite3
import tempfile
import unittest
from array import array
from pathlib import Path
from memsys.migrate import import_sqlite
from memsys.schema import Fact, Episode
from memsys.store import Store
from memsys.qdrant import QdrantSettings
from tests.qdrant_fixture import Engine
from tests.test_qdrant_storage import NOW
from memsys.memory_pages import evidence_pages
from memsys.read import Candidate, SearchResult
from memsys.config import MemorySettings
from memsys.budget import budget_counter


class MigrationTests(unittest.TestCase):
    def test_legacy_source_unchanged_and_safe_views_preserved(self):
        engine = Engine()
        target = Store(user_id="import", settings=QdrantSettings(namespace="migration-test"), client=engine)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"legacy.sqlite"
            conn = sqlite3.connect(path)
            conn.executescript('''
                CREATE TABLE sessions(id TEXT, external_id TEXT, opened_at TEXT, closed_at TEXT, status TEXT, turns_done INTEGER);
                CREATE TABLE turns(session_id TEXT, idx INTEGER, role TEXT, content TEXT, at TEXT);
                CREATE TABLE episodes(id INTEGER, session_id TEXT, round_idx INTEGER, at TEXT, user_text TEXT, assistant_text TEXT, salience REAL, status TEXT, access_count INTEGER, last_access TEXT);
                CREATE TABLE facts(id INTEGER, text TEXT, kind TEXT, subject TEXT, attribute TEXT, value TEXT, event_start TEXT, event_end TEXT, precision TEXT, recorded_at TEXT, valid_from TEXT, valid_to TEXT, status TEXT, superseded_by INTEGER, uncertain INTEGER, confidence REAL, importance REAL, session_id TEXT, mentions INTEGER, access_count INTEGER, last_access TEXT);
                CREATE TABLE fact_sources(fact_id INTEGER, episode_id INTEGER);
                CREATE TABLE fact_spans(fact_id INTEGER, episode_id INTEGER, role TEXT, start INTEGER, end INTEGER, quote TEXT);
                CREATE TABLE episode_views(episode_id INTEGER, user_text TEXT, assistant_text TEXT);
                CREATE TABLE turn_views(session_id TEXT, idx INTEGER, content TEXT);
                CREATE TABLE conversation_exclusions(session_id TEXT, upto_turn INTEGER);
                CREATE TABLE vectors(kind TEXT, item_id INTEGER, vec BLOB);
                CREATE TABLE meta(key TEXT,value TEXT);
            ''')
            conn.execute('INSERT INTO sessions VALUES(?,?,?,?,?,?)', ('s1','agent',NOW.isoformat(),NOW.isoformat(),'closed',1))
            conn.execute('INSERT INTO turns VALUES(?,?,?,?,?)', ('s1',0,'user','I like tea and chess',NOW.isoformat()))
            conn.execute('INSERT INTO episodes VALUES(?,?,?,?,?,?,?,?,?,?)', (1,'s1',0,NOW.isoformat(),'I like tea and chess','',.8,'active',2,None))
            conn.execute('INSERT INTO episode_views VALUES(?,?,?)', (1,'I like [forgotten] and chess',''))
            conn.execute('INSERT INTO turn_views VALUES(?,?,?)', ('s1',0,'I like [forgotten] and chess'))
            values=(1,'likes tea','preference','me','drink','tea',None,None,None,NOW.isoformat(),None,None,'forgotten',None,0,.8,.8,'s1',1,0,None)
            conn.execute('INSERT INTO facts VALUES('+','.join('?'*21)+')', values)
            conn.execute('INSERT INTO fact_sources VALUES(?,?)', (1,1))
            conn.execute('INSERT INTO vectors VALUES(?,?,?)', ('fact',1,array('f',[1,0]).tobytes()))
            conn.commit()
            conn.close()
            before=hashlib.sha256(path.read_bytes()).hexdigest()
            result=import_sqlite(path,target)
            self.assertTrue(result['source_read_only'])
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),before)
            self.assertEqual(target.sources([1]),{1:[1]})
            self.assertEqual(target.fact(1).status,'forgotten')
            self.assertEqual(target.episodes([1])[0].user_text,'I like [forgotten] and chess')
            self.assertEqual(target.conversation_turns('s1')[0]['content'],'I like [forgotten] and chess')
            self.assertEqual(target.turns('s1')[0]['content'],'I like tea and chess')
            with self.assertRaises(ValueError):
                import_sqlite(path,target)

    def test_evidence_pages_cover_role_text_without_generation(self):
        episode = Episode(1,'s1',0,NOW,'用户甲'*100,'助手乙'*100,.8,'active')
        result=SearchResult([Candidate('episode',1)],{'E1':episode},False)
        settings=MemorySettings(context_budget_tokens=300,rerank_backend='fusion')
        pages=evidence_pages(result,settings,{})
        self.assertTrue(pages)
        for speaker,text in (('user',episode.user_text),('assistant',episode.assistant_text)):
            entries=[i for page in pages for i in page['items'] if i['speaker']==speaker]
            self.assertEqual(''.join(i['text'] for i in entries),text)
            self.assertEqual(entries[0]['source_start'],0)
            self.assertEqual(entries[-1]['source_end'],len(text))
        self.assertTrue(all(budget_counter(settings).count(page['context'])<=300 for page in pages))


if __name__ == '__main__':
    unittest.main()
