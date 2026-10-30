import json
import unittest
from unittest.mock import patch

from memsys.schema import EPISODE_WITHHELD
from memsys.structure_memory import assistant_structures
from memsys.store import WriteConflict
from memsys.write import Writer
from tests.test_structured_facts import StructuredFactFixture


class ParserTests(unittest.TestCase):
    def test_literal_positions_and_names_are_preserved(self):
        text='Intro not retained.\n12. cobalt_fixture - Use the amber component.\n13. Seal the fixture.\nOutro not retained.'
        data=assistant_structures(text)
        self.assertEqual(len(data),1)
        self.assertEqual([x['position'] for x in data[0].value['items']],[12,13])
        self.assertEqual(data[0].value['items'][0]['name'],'cobalt_fixture')
        self.assertEqual(data[0].value['items'][0]['details'],'Use the amber component.')
        self.assertNotIn('Intro',json.dumps(data[0].value))
        self.assertNotIn('Outro',json.dumps(data[0].value))
        self.assertIn(data[0].quote,text)

    def test_single_numbered_action_is_retained(self):
        data=assistant_structures('42. Rotate the fixture clockwise.')
        self.assertEqual(data[0].value['items'][0]['position'],42)
        self.assertEqual(data[0].value['items'][0]['statement'],'Rotate the fixture clockwise.')

    def test_table_cells_and_escaped_separator_are_preserved(self):
        data=assistant_structures('| day | person |\n| --- | --- |\n| Friday | member_fixture\\|west |\n')
        self.assertEqual(data[0].value['rows'],[['Friday','member_fixture|west']])

    def test_code_and_malformed_tables_do_not_become_facts(self):
        text='```text\n1. code_fixture\n```\n| day | member |\n| --- | --- |\n| Friday |\n'
        self.assertEqual(assistant_structures(text),[])

    def test_decimal_and_plain_prose_do_not_become_a_sequence(self):
        self.assertEqual(assistant_structures('3.14 is the fixture ratio.\nPlain explanation.'),[])

    def test_times_and_urls_are_not_mistaken_for_title_separators(self):
        data=assistant_structures('1. Meet at 08:30.\n2. Visit https://fixture.example/path.')
        self.assertEqual(data[0].value['items'][0]['statement'],'Meet at 08:30.')
        self.assertEqual(data[0].value['items'][1]['statement'],'Visit https://fixture.example/path.')

    def test_long_unlabelled_prose_is_not_silently_clipped_into_a_fact(self):
        long='Generic explanatory prose. '*30
        data=assistant_structures('1. '+long+'\n2. Short fixture action.')
        self.assertEqual(data[0].value['omitted_positions'],[1])
        self.assertEqual(data[0].value['items'][0]['position'],2)
        self.assertNotIn('Generic explanatory',json.dumps(data[0].value))


class RestorationTests(StructuredFactFixture, unittest.TestCase):
    def test_empty_llm_array_still_retains_literal_assistant_actions(self):
        _,report=self.write('',items=[],source='42. Rotate the fixture clockwise.')
        self.assertEqual(report.extract_calls,1)
        self.assertEqual(report.literal_structures,1)
        self.assertEqual(len(self.store.facts()),1)
        self.assertIn('Rotate the fixture clockwise.',self.store.facts()[0].value)

    def test_saved_safe_structure_restore_is_idempotent_and_calls_no_llm(self):
        source='42. Rotate the fixture clockwise.'
        with patch('memsys.write.assistant_structures',return_value=[]):
            self.write('',items=[],source=source)
        self.model.calls.clear()
        reports=Writer(self.store,self.models,self.settings).restore_structures()
        self.assertEqual(sum(len(r.added) for r in reports),1)
        again=Writer(self.store,self.models,self.settings).restore_structures()
        self.assertEqual(sum(len(r.added) for r in again),0)
        self.assertEqual(self.model.calls,[])

    def test_withheld_sources_are_never_restored(self):
        with patch('memsys.write.assistant_structures',return_value=[]):
            self.write('',items=[],source='42. Rotate the fixture clockwise.')
        eid=self.store.episodes()[0].id
        self.store.set_status('episode',[eid],EPISODE_WITHHELD)
        self.model.calls.clear()
        self.assertEqual(Writer(self.store,self.models,self.settings).restore_structures(),[])
        self.assertEqual(self.store.facts(),[])
        self.assertEqual(self.model.calls,[])

    def test_source_hidden_between_discovery_and_preparation_is_not_restored(self):
        with patch('memsys.write.assistant_structures',return_value=[]):
            self.write('',items=[],source='42. Rotate the fixture clockwise.')
        original=self.store.episodes
        discovered=[False]
        def hide_after_discovery(*args,**kwargs):
            episodes=original(*args,**kwargs)
            if not discovered[0]:
                discovered[0]=True
                self.store.set_status('episode',[episodes[0].id],EPISODE_WITHHELD)
            return episodes
        self.model.calls.clear()
        with patch.object(self.store,'episodes',side_effect=hide_after_discovery):
            reports=Writer(self.store,self.models,self.settings).restore_structures()
        self.assertEqual(reports,[])
        self.assertEqual(self.store.facts(),[])
        self.assertEqual(self.model.calls,[])

    def test_source_hidden_during_embedding_rejects_entire_restore(self):
        with patch('memsys.write.assistant_structures',return_value=[]):
            self.write('',items=[],source='42. Rotate the fixture clockwise.')
        eid=self.store.episodes()[0].id
        original=self.models.embedder.embed
        def hide_during_embedding(*args,**kwargs):
            vectors=original(*args,**kwargs)
            self.store.set_status('episode',[eid],EPISODE_WITHHELD)
            return vectors
        self.model.calls.clear()
        with patch.object(self.models.embedder,'embed',side_effect=hide_during_embedding):
            with self.assertRaises(WriteConflict):
                Writer(self.store,self.models,self.settings).restore_structures()
        self.assertEqual(self.store.facts(),[])
        self.assertEqual(self.model.calls,[])
