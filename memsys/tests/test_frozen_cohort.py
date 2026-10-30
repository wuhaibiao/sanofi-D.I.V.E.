"""Cohort validation runs before clients, preserving fixed source and budgets."""
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import evaluate_frozen_cohort as cohort


class FrozenCohortTest(unittest.TestCase):
    def rows(self,n):
        return [dict(question_id='synthetic-'+kind+str(i),question_type=kind,answer='first')
                for kind in cohort.MODULE_COUNTS for i in range(n)]

    def test_four_and_five_case_cohorts_are_label_independent(self):
        for n in (4,5):
            rows=self.rows(n);before=cohort.validate_cohort(rows)
            for row in rows:row.update(answer='changed',correct=False)
            self.assertEqual(before,cohort.validate_cohort(rows))
            self.assertEqual(before,{kind:n for kind in cohort.MODULE_COUNTS})

    def test_missing_duplicate_or_overweight_case_is_rejected(self):
        for rows in (self.rows(4)[:-1],self.rows(4)+[self.rows(4)[0]],self.rows(6),[]):
            with self.assertRaises(ValueError):cohort.validate_cohort(rows)

    def test_source_change_fails_before_store_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            cohort.save(root/'freeze.json',dict(source=dict(source_sha256='original'),prompts='p'))
            with patch.object(cohort,'identity',return_value=[dict(source_sha256='changed'),'p','w','i']),\
                    patch.object(cohort,'audit_seed') as audit:
                with self.assertRaisesRegex(ValueError,'identity changed'):cohort.validate_reader(root)
                audit.assert_not_called()

    def test_whole_history_bypass_fails_before_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            cohort.save(root/'sample.json',self.rows(5))
            cohort.save(root/'config.frozen.json',dict(memory=dict(complete_episode_context=True)))
            cohort.save(root/'freeze.json',dict(source=dict(source_sha256='s'),prompts='p',
                sample_file='sample.json',sample_sha256=cohort.sha(root/'sample.json'),
                config_sha256=cohort.sha(root/'config.frozen.json')))
            with patch.object(cohort,'identity',return_value=[dict(source_sha256='s'),'p','w','i']),\
                    patch.object(cohort,'audit_seed') as audit:
                with self.assertRaisesRegex(ValueError,'short paths'):cohort.validate_reader(root)
                audit.assert_not_called()
