"""Self-authored semantic regressions through real extraction, updating and answering."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from memsys.bench.mechanisms import CASES, MechanismCase, run_mechanisms, session
from memsys.cli import load_dotenv
from memsys.config import AppConfig
from memsys.llm import UsageLedger


SEMANTIC_CASES = (
    MechanismCase('cross_session_price_difference', (
        session('2025-04-05', 'For my island visit, my taxi quote was $146.'),
        session('2025-04-12', 'I took the ferry for $93 for that island visit.')),
        'How much did I save taking the ferry rather than the taxi for my island visit? '
        'Reply with only the number of dollars.', '53'),
    MechanismCase('reported_price_vs_assistant_estimate', (
        session('2025-04-05', 'My ferry ticket for the island visit cost $15 in total.',
                'Ferry fares commonly cost $30; your reported fare seems unusually low.'),
        session('2025-04-12', 'The taxi quote for that island visit was $81.')),
        'How much did I save by taking the ferry instead of the taxi for my island visit? '
        'Use the prices I reported. Reply with only the number of dollars.', '66'),
    MechanismCase('generic_estimate_is_not_personal_price', (
        session('2025-04-05', 'My taxi quote for the island visit was $81.',
                'A ferry commonly costs $15, depending on the route and season.'),),
        'How much would I save using my quoted ferry fare instead of my taxi quote? '
        'Reply with only the number of dollars or unknown if I have not reported my ferry fare.', 'unknown'),
    MechanismCase('snapshot_then_increment', (
        session('2025-04-05', 'I have read 38 pages of my field journal so far.'),
        session('2025-04-12', 'I have now read 62 pages in total, including those first 38.'),
        session('2025-04-20', 'Today I read 9 more pages after that 62-page total.')),
        'How many pages of the field journal have I read so far? Reply with only the number.', '71'),
    MechanismCase('compare_two_states', (
        session('2025-04-05', 'When I started this project, I supervised 7 analysts.'),
        session('2025-04-12', 'I now supervise 9 analysts, after adding 2 people.')),
        'How many analysts did I supervise when I started, and how many now? '
        'Reply with just the two numbers separated by a comma.', '7, 9'),
    MechanismCase('cross_session_duplicate_and_cumulative', (
        session('2025-04-05', 'I have spent 4 hours on my pottery course so far.'),
        session('2025-04-12', 'The pottery course has now taken 8 hours in total, including the earlier 4.'),
        session('2025-04-20', 'I completed a separate carving workshop in 5 hours.'),
        session('2025-04-23', 'That same carving workshop took 5 hours; I have not taken another one.')),
        'How many hours have I spent in total on the pottery course and carving workshop? '
        'Reply with only the number.', '13'),
    MechanismCase('conditional_routine_update', (
        session('2025-04-05', 'I usually begin studying at 8:00 AM every day.'),
        session('2025-04-12', 'On Tuesdays and Thursdays I now begin studying at 7:20 AM. '
                'Other days still begin at 8:00 AM.')),
        'What time do I begin studying on Tuesdays and Thursdays? Reply with only the time.', '7:20 AM'),
    MechanismCase('unmentioned_object_premise', (
        session('2025-04-05', 'My 20-liter terrarium has 3 snails. '
                'I do not have any other terrariums.'),),
        'How many snails are in my 40-liter terrarium? Reply with only the number '
        'or unknown if this terrarium is not in my records.', 'unknown'),
    MechanismCase('source_relative_offset', (
        session('2025-04-05', 'I toured the botanical exhibit two weeks before the spring launch.'),
        session('2025-04-12', 'I purchased the workbook at the spring launch.')),
        'How many days before purchasing the workbook did I tour the botanical exhibit? '
        'Reply with only the number.', '14'),
    MechanismCase('original_term_preserved', (
        session('2025-04-05', 'Can you suggest a chord progression for my practice?',
                'Try this sequence: C4 E4 G4 E4. These are single melody notes rather than chords.'),),
        'What chord progression did you suggest? Quote the original sequence. '
        'Reply with only the note sequence.', 'C4 E4 G4 E4'),
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--semantic-only', action='store_true')
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    load_dotenv()
    config = AppConfig.load(args.config)
    models = config.build_models(UsageLedger(str(out / 'usage.jsonl')))
    report = run_mechanisms(models, config.memory, str(out),
                            SEMANTIC_CASES if args.semantic_only else CASES + SEMANTIC_CASES)
    (out / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    for row in report['results']:
        print(row['case'] + ': ' + ('passed' if row['passed'] is True else
              row.get('error', row.get('answer', 'failed'))), flush=True)
    print(json.dumps({k: report[k] for k in ('total', 'passed', 'incorrect', 'errors', 'unstarted')}), flush=True)
    return 0 if report['passed'] == report['total'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
