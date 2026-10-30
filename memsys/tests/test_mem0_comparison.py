"""Retired Mem0 entry points fail before endpoint, credentials or SDK access."""
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from helpers import settings
from memsys.budget import budget_counter

SCRIPTS = Path(__file__).resolve().parents[1]/'scripts'
sys.path.insert(0, str(SCRIPTS))
import mem0_bridge


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS/filename)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


comparison = module('retired_mem0_evaluation', 'evaluate_mem0.py')


class Mem0ComparisonTest(unittest.TestCase):
    def bound_bridge(self, cls):
        model = Mock()
        token = mem0_bridge._binding.set(mem0_bridge.Binding(model, settings(), {}))
        try:
            bridge = cls()
        finally:
            mem0_bridge._binding.reset(token)
        return bridge, model

    def test_chat_cannot_touch_the_bound_model(self):
        bridge, model = self.bound_bridge(mem0_bridge.ChatBridge)
        with self.assertRaisesRegex(mem0_bridge.Mem0ExecutionDisabled, 'user instruction'):
            bridge.generate_response([dict(role='user', content='Synthetic source.')])
        self.assertEqual(model.mock_calls, [])

    def test_embedding_cannot_touch_the_bound_model(self):
        bridge, model = self.bound_bridge(mem0_bridge.EmbeddingBridge)
        for call in (lambda: bridge.embed('Synthetic'), lambda: bridge.embed_batch(['Synthetic'], 'search')):
            with self.assertRaises(mem0_bridge.Mem0ExecutionDisabled):
                call()
        self.assertEqual(model.mock_calls, [])

    def test_sdk_is_rejected_before_import_or_output_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)/'must-not-exist'
            with self.assertRaises(mem0_bridge.Mem0ExecutionDisabled):
                mem0_bridge.memory_for(Mock(), settings(), target, 'synthetic', 8)
            self.assertFalse(target.exists())

    def test_runner_rejected_before_initialization(self):
        with self.assertRaises(mem0_bridge.Mem0ExecutionDisabled):
            comparison.Mem0Runner()

    def test_cli_entry_points_fail_before_config_or_credentials(self):
        for filename in ('evaluate_mem0.py', 'check_mem0_bridge.py', 'run_mem0_comparison.py'):
            with self.subTest(filename=filename):
                driver = module('retired_'+filename[:-3], filename)
                with patch('memsys.cli.load_dotenv') as credentials:
                    with self.assertRaises(mem0_bridge.Mem0ExecutionDisabled):
                        driver.main()
                    credentials.assert_not_called()

    def test_imports_do_not_discover_implicit_bindings(self):
        with self.assertRaisesRegex(RuntimeError, 'explicit model binding'):
            mem0_bridge.ChatBridge()
        with self.assertRaisesRegex(RuntimeError, 'explicit embedding binding'):
            mem0_bridge.EmbeddingBridge()

    def test_historical_memory_packing_is_read_only_and_atomic(self):
        counter = budget_counter(settings(budget_mode='utf8'))
        rows = [dict(id='one', memory='The amber scanner costs $18.', metadata={'recorded_at':'2025-04-01'}),
                dict(id='two', memory='The violet camera costs $27.', metadata={'recorded_at':'2025-05-01'})]
        text, trace = comparison.pack_memories(rows, counter, 140)
        self.assertLessEqual(counter.count(text), 140)
        for row in rows:
            self.assertEqual(row['memory'] in text, row['id'] in trace['selected'])


if __name__ == '__main__':
    unittest.main()
