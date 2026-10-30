"""Exercise the installed native SDK, Qdrant and bridge with synthetic sources."""
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
os.environ['MEM0_TELEMETRY']='false'
from memsys.config import AppConfig,MemorySettings,Models
from memsys.fake import HashEmbedder,ScriptedChat
from memsys.llm import UsageLedger
from mem0_bridge import close_memory,memory_for,prohibit_mem0_execution


def main():
    prohibit_mem0_execution()
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--config',type=Path,help='Optional frozen config for two real extraction calls')
    args=parser.parse_args()
    args.out.mkdir(parents=True,exist_ok=True)
    texts=['The user owns a violet telescope.','The telescope was repaired on 2025-05-08.']
    chat=ScriptedChat({'extract':[json.dumps({'memory':[dict(text=text,linked_memory_ids=[])]}) for text in texts]})
    embedding=HashEmbedder()
    models=Models(chat=chat,memory=chat,embedder=embedding,embedding_name='hash-256',names={'memory':'scripted'})
    settings=MemorySettings(memory_input_budget_tokens=65536)
    ledger=UsageLedger(str(args.out/'usage.jsonl'))
    dimension=256
    if args.config:
        from memsys.cli import load_dotenv
        load_dotenv(str(Path.cwd()/'.env'))
        config=AppConfig.load(str(args.config))
        if config.fallbacks:raise ValueError('Real preflight requires fixed model roles')
        settings=config.memory
        models=config.build_models(ledger)
        dimension=len(models.embedder.embed(['memory transport dimension probe'],purpose='embed_probe')[0])
    memory=memory_for(models,settings,args.out/'store','synthetic-transport-check',dimension)
    try:
        memory.add([dict(role='user',content=texts[0]),dict(role='assistant',content='Noted.')],
                   user_id='synthetic-transport-check',metadata={'recorded_at':'2025-04-01'})
        memory.add([dict(role='user',content=texts[1])],user_id='synthetic-transport-check',
                   metadata={'recorded_at':'2025-05-08'})
        found=memory.search('violet telescope repair',filters={'user_id':'synthetic-transport-check'},limit=10,rerank=False)
        rows=found['results']
        if not args.config and {row['memory'] for row in rows}!=set(texts):
            raise AssertionError('Native SDK did not retrieve both distinct source memories')
        if args.config and not (any('violet' in row['memory'].lower() and 'telescope' in row['memory'].lower() for row in rows)
                    and any('repair' in row['memory'].lower() and 'telescope' in row['memory'].lower() for row in rows)):
            raise AssertionError('Real extraction did not preserve both synthetic source facts')
        if {row['metadata']['recorded_at'] for row in rows}!={'2025-04-01','2025-05-08'}:
            raise AssertionError('Recorded source clocks were lost')
        keyword_hits=memory.vector_store.keyword_search('violet telescope',top_k=10,
                            filters={'user_id':'synthetic-transport-check'})
        if not keyword_hits:
            raise AssertionError('Native BM25 keyword search is not active')
        from mem0.utils.spacy_models import get_nlp_full,get_nlp_lemma
        from mem0.utils.entity_extraction import extract_entities
        if get_nlp_full() is None or get_nlp_lemma() is None:
            raise AssertionError('Native entity NLP or lemmatization is not active')
        entities=extract_entities('Clara visited Pinebridge Observatory.')
        if not entities:
            raise AssertionError('Native entity extraction returned no entities for the synthetic fixture')
        import mem0.memory.main as native
        result=dict(status='passed',sdk_version=importlib.metadata.version('mem0ai'),
            native_main_sha256=hashlib.sha256(Path(native.__file__).read_bytes()).hexdigest(),
            extracted=len(texts),retrieved=len(rows),clock_metadata_preserved=True,
            bm25_active=True,entity_nlp_active=True,entity_fixture=entities,
            transport_only=True,cloud_calls=sum(record.purpose=='extract' for record in ledger.records()),
            embedding_dimension=dimension,synthetic=True,real_models=bool(args.config))
        (args.out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
        print(json.dumps(result),flush=True)
    finally:
        close_memory(memory)
    return 0


if __name__=='__main__':
    raise SystemExit(main())
