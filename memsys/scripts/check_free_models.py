"""Probe only catalog-confirmed preferred free models under the read deadline."""
import argparse
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
from memsys.cli import load_dotenv
from memsys.config import AppConfig, make_client
from memsys.llm import UsageLedger, chat_json
from memsys.opencode_go import FREE_PRIORITY, profile_name
from memsys.read_budget import ReadBudget, read_scope
from memsys.read_retry import ReadTransportChat


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--catalog', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    load_dotenv(str(ROOT/'.env'))
    config = AppConfig.load(str(args.config))
    catalog = json.loads(args.catalog.read_text())
    available = {x['id'] for row in catalog if row['url'].endswith('/zen/v1/models') for x in row.get('models', [])}
    ledger = UsageLedger(str(args.out/'usage.jsonl'))
    results, previous_failure, consecutive = [], None, 0
    for model in FREE_PRIORITY:
        if model not in available:
            results.append(dict(model=model, status='not_in_current_catalog'))
            continue
        if consecutive >= 2:
            results.append(dict(model=model, status='not_started_after_consecutive_failures'))
            continue
        budget = ReadBudget(timeout_s=45, retries_per_call=0, retries_total=0)
        started = time.monotonic()
        client = make_client(config.models[profile_name(model)], ledger)
        try:
            with read_scope(budget):
                answer = chat_json(ReadTransportChat(client, budget),
                    [{'role':'user', 'content':'Return exactly this JSON object: {"ok":true}'}],
                    purpose='free_model_probe', max_tokens=256)
            if answer != {'ok': True}:
                raise ValueError('model did not return the requested JSON')
            row = dict(model=model, status='ok')
            consecutive, previous_failure = 0, None
        except Exception as exc:
            kind = (getattr(exc, 'read_state', 'error'), getattr(exc, 'status', None))
            consecutive = consecutive+1 if kind == previous_failure else 1
            previous_failure = kind
            row = dict(model=model, status='failed', state=kind[0], http_status=kind[1], error=str(exc)[:500])
        row.update(wall_s=round(time.monotonic()-started, 3), read_transport=budget.snapshot())
        results.append(row)
        (args.out/'report.json').write_text(json.dumps(dict(results=results, complete=False), indent=2)+'\n')
        print(json.dumps({k:v for k,v in row.items() if k != 'read_transport'}), flush=True)
    report = dict(results=results, complete=True, usage=ledger.totals(),
                  stopped_after_consecutive_failures=consecutive >= 2,
                  scope='Short JSON/protocol availability probes; not memory accuracy.')
    (args.out/'report.json').write_text(json.dumps(report, indent=2)+'\n')
    return 0 if any(r['status']=='ok' for r in results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
