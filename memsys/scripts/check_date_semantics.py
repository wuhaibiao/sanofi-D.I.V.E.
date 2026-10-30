"""Self-authored date-source checks with a real answer model and fixed memories."""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from memsys.bench.provenance import model_details, source_manifest
from memsys.cli import load_dotenv
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
    chat = AppConfig.load(args.config).client('chat', UsageLedger(str(out / 'usage.jsonl')))
    memories = [('2025-01-03', 'Today I calibrated the observatory telescope.'),
                ('2025-01-18', 'Today I inspected the observatory dome.')]
    cases = [
        ('before_two_events', memories,
         'How many days before the dome inspection did I calibrate the telescope? Reply with only the number.', '15'),
        ('elapsed_two_events', memories,
         'How many days had elapsed between the telescope calibration and the dome inspection? Reply with only the number.', '15'),
        ('caller_clock', [('2025-01-10', 'I reserved the cabin on November 10, 2024 for a trip on March 10, 2025.')],
         'How many calendar months ago did I reserve the cabin? Reply with only the number.', '3'),
    ]
    rows = []
    for name, sources, question, expected in cases:
        system = MemorySystem(Models(chat=chat, memory=None, embedder=HashEmbedder(64), embedding_name='hash-64'),
            MemorySettings(query_analysis=False, rerank_backend="fusion", context_budget_tokens=3000,
                           budget_mode='utf8', max_history_share=0, min_similarity=0))
        try:
            store = system.store(name)
            with store.transaction():
                for n, (day, text) in enumerate(sources):
                    at = datetime.fromisoformat(day + 'T10:00')
                    sid = store.create_session(at)
                    store.add_episode(sid, n, at, text, '', .8, 'active')
            reply = system.respond(name, question, datetime(2025, 2, 10, 10), tags={'date_case': name})
            audit = reply.trace['aggregation']
            rows.append(dict(case=name, expected=expected, answer=reply.text, trace=reply.trace,
                             passed=reply.text == expected and audit.get('status') == 'checks_passed'
                             and audit.get('operation') == 'date_difference'))
        except ModelError as exc:
            rows.append(dict(case=name, error=str(exc), passed=False))
            if exc.account_problem:
                break
        finally:
            system.close()
    report = dict(mode='self-authored fixed memories; real date answer model; no extraction or benchmark score',
                  passed=len(rows) == len(cases) and all(row['passed'] for row in rows), total=len(cases),
                  results=rows, model=model_details(chat), provenance=source_manifest())
    (out / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    for row in rows:
        print(row['case'] + ': ' + ('passed' if row['passed'] else row.get('error', row.get('answer', 'failed'))), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
