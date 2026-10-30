import unittest

from memsys.bench.judge import _grade
from memsys.llm import ChatResult
from memsys.read_budget import ReadBudget,ReadExecutionError
from memsys.read_retry import ReadTransportChat


class JudgeReadBudgetTest(unittest.TestCase):
    def test_label_format_retry_shares_the_original_question_deadline(self):
        clock=[0.0]
        class Model:
            calls=0
            def chat(self,messages,**kwargs):
                self.calls+=1
                clock[0]+=2
                return ChatResult('unclear' if self.calls==1 else 'yes','fixture',0,0,0,0,0,'stop')
        model=Model()
        budget=ReadBudget(3,clock=lambda:clock[0])
        with self.assertRaises(ReadExecutionError):
            _grade(ReadTransportChat(model,budget),'Return yes or no.','fixture')
        self.assertEqual(model.calls,2)
        self.assertEqual([o['text'] for o in budget.outputs],['unclear'])
        self.assertEqual(budget.additional_retries,0)
