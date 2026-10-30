"""Protocol integration fixtures disclose synthetic replies and forward real repairs."""
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from check_frozen_resolve_protocol import CASES,FaultOnceResolve
from memsys.fake import ScriptedChat


class ResolveFaultInjectionTest(unittest.TestCase):
    def test_every_fault_is_injected_once_and_only_repair_is_forwarded(self):
        messages=[dict(role='user',content='[N3] A new property.\n  [F9] An existing property.')]
        for case in CASES:
            client=ScriptedChat({'resolve':'{"decisions":[{"new":"N3","action":"add","target":null}]}'})
            wrapper=FaultOnceResolve(client,case)
            first=wrapper.chat(messages,purpose='resolve')
            self.assertEqual(first.model,'synthetic_protocol_fault')
            self.assertEqual(client.calls,[])
            self.assertEqual(wrapper.injected[0]['indexes'],[3])
            wrapper.chat(messages,purpose='resolve',tags={'resolve_protocol_repair':'1'})
            self.assertEqual(wrapper.actual_repairs,1);self.assertEqual(len(client.calls),1)
            self.assertEqual(len(wrapper.injected),1)
            data=json.loads(first.text)['decisions']
            if case=='missing_decision':self.assertEqual(data,[])
            elif case=='duplicate_decision':self.assertEqual([r['new'] for r in data],['N3','N3'])
            elif case=='missing_target':self.assertIsNone(data[0]['target'])
            else:self.assertEqual(data[0]['target'],'F999999999')

    def test_empty_input_does_not_call_client_or_create_a_fault(self):
        client=ScriptedChat({});wrapper=FaultOnceResolve(client,'missing_target')
        with self.assertRaises(ValueError):wrapper.chat([dict(role='user',content='No identifiers.')],purpose='resolve')
        self.assertEqual(client.calls,[]);self.assertEqual(wrapper.injected,[])
