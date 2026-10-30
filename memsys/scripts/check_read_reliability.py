"""Local twelve-case capability guard and replay of saved provider failures."""
import argparse
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
sys.path.insert(0, str(ROOT/'tests'))
from memsys.bench.provenance import source_manifest
from memsys.llm import ModelError
from memsys.read_budget import ReadBudget, is_endpoint_outage

CASES = [
    'test_mixed_named_counts.MixedNamedCountsTest.test_group_sizes_and_named_singleton_can_contribute_together',
    'test_mixed_named_counts.MixedNamedCountsTest.test_repeated_named_identity_is_counted_once_across_quotes',
    'test_cross_session_precision.CrossSessionPrecisionTest.test_unnamed_cumulative_count_is_synthesized_with_earlier_identity',
    'test_cross_session_precision.CrossSessionPrecisionTest.test_aggregate_recovery_rereads_originals_and_still_requires_proof',
    'test_attendance_dates.AttendanceDatesTest.test_calendar_union_deduplicates_same_day_across_events',
    'test_attendance_dates.AttendanceDatesTest.test_completed_attendance_can_anchor_to_previous_calendar_year',
    'test_duration_and_date_citations.DurationAndDateCitationTests.test_fixed_duration_sum_uses_literal_half_and_rejects_integer_component',
    'test_duration_and_date_citations.DurationAndDateCitationTests.test_program_copies_source_and_retains_literal_date_and_clock_guards',
    'test_complete_week_precision.CompleteWeekPrecisionTest.test_partial_week_preserves_exact_days_and_qualifies_only_bare_result',
    'test_advice_user_context.AdviceUserContextTest.test_index_preserves_questions_plans_source_roles_and_full_originals',
    'test_completed_event_bounds.CompletedEventBoundsTest.test_recording_day_proves_only_a_lower_bound',
    'test_read_transport_retry.ReadTransportRetryTest.test_a_successful_refusal_is_never_resampled',
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--audit', type=Path, action='append', default=[])
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromNames(CASES))
    replay = []
    for path in args.audit:
        for row in json.loads(path.read_text()):
            for event in row.get('read_transport', {}).get('events', []):
                error = ModelError(event['error'], status=event['status'])
                if not is_endpoint_outage(error):
                    raise AssertionError('saved outage was not recognized')
                budget, calls = ReadBudget(retry_wait_s=0), []
                def fail(timeout):
                    calls.append(timeout)
                    raise error
                try:
                    budget.call(fail, purpose=event['purpose'])
                except ModelError as exc:
                    assert exc is error and exc.read_state == 'service_error'
                else:
                    raise AssertionError('outage became a completed answer')
                assert len(calls) == 2 and budget.additional_retries == 1
                replay.append(dict(audit=str(path), question_id=row['question_id'],
                                   purpose=event['purpose'], status=event['status'],
                                   attempts=len(calls), state='service_error'))
    report = dict(provenance=source_manifest(), capability_cases=CASES, tests=result.testsRun,
                  failures=len(result.failures), errors=len(result.errors), historical_errors_replayed=len(replay),
                  replay=replay, cloud_calls=0,
                  scope='Deterministic capability and error-handling checks; not a model accuracy score.')
    (args.out/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps({k:v for k,v in report.items() if k not in ('replay','capability_cases','provenance')}, ensure_ascii=False))
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
