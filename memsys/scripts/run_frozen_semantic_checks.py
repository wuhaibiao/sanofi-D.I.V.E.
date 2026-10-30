"""Run selected real semantic cases using a verified frozen iteration."""
import argparse
import json
from pathlib import Path
import sys
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--after-run', type=Path, action='append', default=[])
    parser.add_argument('--after-report', type=Path, action='append', default=[])
    parser.add_argument('--contract-only', action='store_true')
    parser.add_argument('--category-only', action='store_true')
    args = parser.parse_args()
    out = args.out.resolve()
    report_path = out / 'mechanisms/report.json'
    if report_path.exists():
        print('Existing report retained:', report_path, flush=True)
        return 0
    def ready(directory):
        try:
            state = json.loads((directory / 'freeze.json').read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return False
        return any(stage['stage'] == 'run' and stage['returncode'] == 0
                   for stage in state['stages'])
    while not (all(ready(directory) for directory in args.after_run)
               and all(path.exists() for path in args.after_report)):
        time.sleep(15)
    sys.path.insert(0, str(out / 'frozen-src'))
    from memsys.bench.provenance import source_manifest, prompt_fingerprint
    state = json.loads((out / 'freeze.json').read_text())
    assert source_manifest()['source_sha256'] == state['source']['source_sha256']
    assert prompt_fingerprint() == state['prompts']
    # Import the frozen runtime before the case catalog adds the normal source path.
    from memsys.bench.mechanisms import CASES, MechanismCase, run_mechanisms, session
    from memsys.config import AppConfig
    from memsys.cli import load_dotenv
    from memsys.llm import UsageLedger
    from check_semantic_reasoning import SEMANTIC_CASES
    names = {'snapshot_then_increment', 'cross_session_duplicate_and_cumulative',
             'unmentioned_object_premise', 'reported_price_vs_assistant_estimate',
             'generic_estimate_is_not_personal_price', 'source_relative_offset',
             'shared_source_forgetting', 'original_term_preserved'}
    cases = tuple(case for case in CASES + SEMANTIC_CASES if case.name in names)
    assert len(cases) == len(names)
    if args.contract_only:
        cases = (
            MechanismCase('duration_total_half_unit', (
                session('2025-04-05', 'My watercolor course took three weeks.'),
                session('2025-04-12', 'My woodcut course took a week and a half.')),
                'How many weeks did it take me to complete the watercolor and woodcut courses? '
                'Reply with only the number.', '4.5'),
            MechanismCase('program_copied_event_dates', (
                session('2025-04-05', 'Today I ordered a replacement lens cap.'),
                session('2025-04-12', 'The replacement lens cap arrived today.')),
                'How many days passed between ordering the replacement lens cap and receiving it? '
                'Reply with only the number.', '7'),
        )
    if args.category_only:
        cases = (MechanismCase('category_total_literal_nouns', (
            session('2025-04-05', 'At the book fair, I collected four stone beads.'),
            session('2025-04-12', 'At the craft market, I collected six wooden beads.')),
            'What is the total number of beads I collected at both events? '
            'Reply with only the number.', '10'),)
    load_dotenv()
    config = AppConfig.load(str(out / 'config.frozen.json'))
    directory = out / 'mechanisms'
    directory.mkdir(exist_ok=True)
    models = config.build_models(UsageLedger(str(directory / 'usage.jsonl')))
    report = run_mechanisms(models, config.memory, str(directory), cases)
    assert source_manifest()['source_sha256'] == state['source']['source_sha256']
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({key: report[key] for key in
                      ('total', 'passed', 'incorrect', 'errors', 'unstarted')}), flush=True)
    return 0 if report['passed'] == report['total'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
