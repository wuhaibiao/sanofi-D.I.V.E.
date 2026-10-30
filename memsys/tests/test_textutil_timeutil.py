from __future__ import annotations

import unittest
from datetime import date, datetime

from memsys.textutil import estimate_tokens, fts_document, fts_query, slug, split_tokens, terms, truncate_tokens
from memsys.timeutil import DateSpan, describe_offset, parse_datetime, parse_span, shift


class TextTest(unittest.TestCase):
    def test_token_splitting_is_lossless_and_respects_small_mixed_language_budgets(self):
        text = "\n  示例文本 with whitespace.\n\n1. Named item.\n2. Another item.\n" * 20
        for budget in (1, 3, 19, 200):
            parts = split_tokens(text, budget)
            self.assertEqual("".join(parts), text)
            self.assertTrue(all(part and estimate_tokens(part) <= budget for part in parts))
        self.assertEqual(split_tokens("", 10), [])
        with self.assertRaises(ValueError):
            split_tokens(text, 0)

    def test_estimate_tokens_counts_cjk_per_character(self):
        self.assertEqual(estimate_tokens("abcdefgh"), 2)
        self.assertEqual(estimate_tokens("你好"), 2)
        self.assertEqual(estimate_tokens(""), 0)

    def test_chinese_is_split_into_bigrams_for_full_text_search(self):
        self.assertEqual(fts_document("我住在杭州").split(), ["我住", "住在", "在杭", "杭州"])
        self.assertIn("Seattle", fts_document("moved to Seattle"))

    def test_terms_drop_stopwords_and_keep_numbers(self):
        self.assertEqual(terms("I moved to Seattle in 2023, how many days ago?"), ["moved", "seattle", "2023"])
        self.assertEqual(terms("我住在杭州"), ["我住", "住在", "在杭", "杭州"])

    def test_fts_query_quotes_terms(self):
        self.assertEqual(fts_query(["Seattle rain", "rain"]), '"seattle" OR "rain"')
        self.assertIsNone(fts_query(["the and of"]))

    def test_truncate_and_slug(self):
        self.assertTrue(truncate_tokens("word " * 100, 10).endswith("[truncated]"))
        self.assertEqual(truncate_tokens("short", 10), "short")
        self.assertEqual(slug("Home City!"), "home_city")

    def test_truncation_marker_is_included_in_budget(self):
        for text in ("word " * 100, "中英文混合 abc " * 100):
            for limit in (0, 1, 3, 10, 30):
                self.assertLessEqual(estimate_tokens(truncate_tokens(text, limit)), limit)


class TimeTest(unittest.TestCase):
    def test_parse_longmemeval_and_iso_dates(self):
        self.assertEqual(parse_datetime("2023/07/01 (Sat) 23:13"), datetime(2023, 7, 1, 23, 13))
        self.assertEqual(parse_datetime("2023-07-01"), datetime(2023, 7, 1))
        self.assertEqual(parse_datetime("2023-07-01T08:05:09"), datetime(2023, 7, 1, 8, 5, 9))
        with self.assertRaises(ValueError):
            parse_datetime("July 1st")

    def test_parse_span_precision(self):
        self.assertEqual(parse_span("2023-05-20"), DateSpan(date(2023, 5, 20), date(2023, 5, 20), "day"))
        self.assertEqual(parse_span("2023/02"), DateSpan(date(2023, 2, 1), date(2023, 2, 28), "month"))
        self.assertEqual(parse_span("2023").precision, "year")
        for bad in (None, "", "2023-02-30", "last week", 2023):
            self.assertIsNone(parse_span(bad))

    def test_describe_offset(self):
        today = date(2023, 7, 1)
        self.assertEqual(describe_offset(DateSpan.day(date(2023, 6, 2)), today), "29 days ago")
        self.assertEqual(describe_offset(DateSpan.day(today), today), "today")
        self.assertEqual(describe_offset(DateSpan.day(date(2023, 7, 4)), today), "in 3 days")
        self.assertEqual(describe_offset(parse_span("2023-04"), today), "about 3 months ago")
        self.assertEqual(describe_offset(parse_span("2021"), today), "about 2 years ago")

    def test_overlap_with_slack(self):
        june = parse_span("2023-06")
        self.assertTrue(DateSpan.day(date(2023, 7, 2)).overlaps(june, slack_days=2))
        self.assertFalse(DateSpan.day(date(2023, 7, 5)).overlaps(june, slack_days=2))

    def test_shift_for_the_simulated_clock(self):
        start = datetime(2026, 1, 31, 9, 0)
        self.assertEqual(shift(start, "+3w"), datetime(2026, 2, 21, 9, 0))
        self.assertEqual(shift(start, "+1m"), datetime(2026, 2, 28, 9, 0))
        self.assertEqual(shift(start, "-2d"), datetime(2026, 1, 29, 9, 0))
        self.assertEqual(shift(start, "+1y"), datetime(2027, 1, 31, 9, 0))
        with self.assertRaises(ValueError):
            shift(start, "next week")


if __name__ == "__main__":
    unittest.main()
