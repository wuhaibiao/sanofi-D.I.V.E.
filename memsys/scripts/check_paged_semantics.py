"""自拟分页聚合回归：固定记忆、真实回答模型；不代表抽取或 Oracle 准确率。"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from memsys.bench.provenance import model_details, source_manifest
from memsys.cli import load_dotenv, memsys_home
from memsys.config import AppConfig, MemorySettings, Models
from memsys.core import MemorySystem
from memsys.fake import HashEmbedder
from memsys.llm import ModelError, UsageLedger


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    load_dotenv()
    load_dotenv(str(Path(memsys_home()) / '.env'))
    ledger = UsageLedger(str(out / 'usage.jsonl'))
    chat = AppConfig.load(args.config).client('chat', ledger)
    at = datetime(2025, 5, 4, 10)
    cases = (
        ('fact_original_overlap',
         [('I paid $%d for independent receipt R%d. This was a separate completed purchase.' % (n+7, n), '',
           'The user paid $%d for independent receipt R%d.' % (n+7, n)) for n in range(8)],
         'What is the total I paid for all eight independent receipts? Reply with only the total number of dollars.', '84'),
        ('word_group_sizes',
         [('Three apprentices joined workshop session G%d. This session had a different group of attendees.' % n, '', '')
          for n in range(8)],
         'How many people joined across all eight separate workshop sessions? Reply with only the number.', '24'),
        ('owned_object_vs_suggestions',
         [('My only weather instrument is a thermometer. This is ownership update T%d.' % n,
           'You could consider a hygrometer and a rain gauge.', '') for n in range(8)],
         'How many distinct weather instruments have I said I own? Reply with only the number.', '1'),
        ('ordered_recommendations',
         [('Which organizers do you recommend for my art portfolio? ' + 'Extra notes about packing materials. ' * 15,
           'For the art portfolio, I recommend a ring binder and tab dividers, in that order. For course notes, spiral notebooks are another option.', '')
          for n in range(2)],
         'List all organizers recommended for my art portfolio in their original order. Reply with just the names separated by a comma.',
         'ring binder, tab dividers'),
    )
    rows = []
    for name, memories, question, expected in cases:
        system = MemorySystem(Models(chat=chat, memory=None, embedder=HashEmbedder(64), embedding_name='hash-64'),
            MemorySettings(query_analysis=False, rerank_backend="fusion", budget_mode='utf8', context_budget_tokens=600,
                           max_history_share=0, min_similarity=0))
        try:
            store = system.store(name)
            with store.transaction():
                sid = store.create_session(at)
                for n, (user, assistant, text) in enumerate(memories):
                    eid = store.add_episode(sid, n, at, user, assistant, .8, 'active')
                    if text:
                        store.add_fact(text=text, kind='event', subject='user', attribute='payment', value=text,
                            event=None, recorded_at=at, valid_from=None, session_id=sid, importance=.6, episode_ids=[eid])
            reply = system.respond(name, question, at, tags={'paged_case': name})
            audit = reply.trace['aggregation']
            rows.append({'case': name, 'expected': expected, 'answer': reply.text, 'trace': reply.trace,
                         'passed': reply.text == expected and audit.get('pages_complete') is True
                                   and audit.get('pages_processed', 0) > 1})
        except ModelError as exc:
            rows.append({'case': name, 'error': str(exc), 'passed': False})
            if exc.account_problem:
                break
        finally:
            system.close()
    report = {'mode': 'self-authored fixed memories; real paged answer model; hash retrieval; no extraction or benchmark score',
              'passed': len(rows) == len(cases) and all(row['passed'] for row in rows), 'total': len(cases),
              'results': rows, 'model': model_details(chat), 'provenance': source_manifest()}
    (out / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    for row in rows:
        print('%s: %s' % (row['case'], 'passed' if row['passed'] else row.get('error', row.get('answer'))), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
