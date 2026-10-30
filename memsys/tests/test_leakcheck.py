"""评测数据泄漏检查（手册 F.1）：提示词和代码里不能有评测题的内容。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from memsys import prompts
from memsys.bench.leakcheck import check, project_files

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REAL_DATA = os.path.join(ROOT, "data", "longmemeval_oracle.json")


def dataset(path, haystack_text, question="How many charity runs did I do this spring?"):
    with open(path, "w") as fh:
        json.dump([{"question_id": "q_test_0001", "question": question, "answer": "three runs in total, all in April",
                    "haystack_sessions": [[{"role": "user", "content": haystack_text}]]}], fh)


class LeakcheckTest(unittest.TestCase):
    def test_helper_scripts_are_included_in_the_source_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "scripts"))
            helper = os.path.join(tmp, "scripts", "validate.py")
            with open(helper, "w") as fh:
                fh.write("print('self-authored fixture')\n")
            self.assertIn(helper, project_files(tmp))

    def test_detects_names_ids_and_answers(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = os.path.join(tmp, "data.json")
            dataset(data, "Last weekend I joined the Maple Hollow Lantern Parade with my sister.")
            source = os.path.join(tmp, "x.py")
            with open(source, "w") as fh:
                fh.write("SPECIAL = 'q_test_0001'  # three runs in total, all in April\n")
            kinds = {(l.kind, l.text) for l in check(data, [source])}
            self.assertIn(("name", "Maple Hollow Lantern Parade"), kinds)   # 提示词里的例子出现在了对话里
            self.assertIn(("question_id", "q_test_0001"), kinds)
            self.assertIn(("answer", "three runs in total, all in april"), kinds)

    def test_clean_dataset_has_no_leaks(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = os.path.join(tmp, "data.json")
            dataset(data, "I adopted a cat named Pixel.")
            self.assertEqual(check(data, []), [])

    @unittest.skipUnless(os.path.exists(REAL_DATA), "LongMemEval Oracle 数据不在本地")
    def test_repository_against_real_data(self):
        leaks = check(REAL_DATA, project_files(ROOT))
        self.assertEqual(leaks, [], "\n".join("%s %s %s" % (l.where, l.kind, l.text) for l in leaks))
        self.assertTrue(prompts.EXTRACT_USER)


if __name__ == "__main__":
    unittest.main()
