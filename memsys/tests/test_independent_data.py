import importlib.util
from pathlib import Path
from datetime import datetime
import unittest

spec=importlib.util.spec_from_file_location('independent_data',Path(__file__).parents[1]/'scripts/independent_data.py')
adapter=importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)


class IndependentDataTest(unittest.TestCase):
    def test_convomem_keeps_all_messages_and_strips_annotation_fields(self):
        item=dict(question='Recall my material.',answer='scoring-only',message_evidences='annotation-canary',
            scenario_description='annotation-canary',conversations=[dict(id='original-session',containsEvidence=True,
            messages=[dict(speaker='User',text='My material is clay.'),dict(speaker='Assistant',text='Recorded.')])])
        row=adapter.convert_convomem(item,'single-session-user','fixture',datetime(2025,1,1))
        history=str(row['haystack_sessions'])
        self.assertIn('My material is clay.',history)
        self.assertIn('Recorded.',history)
        self.assertNotIn('annotation-canary',history)
        self.assertNotIn('scoring-only',history)
        self.assertIn('not original event dates',history)
        self.assertEqual(row['haystack_session_ids'],['original-session'])

    def test_locomo_preserves_human_speakers_dates_and_captions_without_annotations(self):
        conversation=dict(sample_id='fixture',event_summary='annotation-canary',observation='annotation-canary',
            conversation=dict(speaker_a='A',speaker_b='B',session_1_date_time='1:56 pm on 8 May, 2023',
                session_1=[dict(speaker='A',text='I painted a tray.',blip_caption='A painted tray.'),
                           dict(speaker='B',text='I made a basket.')]))
        row=adapter.convert_locomo(conversation,dict(category=2,question='When was this recorded?',answer='scoring-only',
                                                   evidence='annotation-canary'),'fixture')
        history=str(row['haystack_sessions'])
        self.assertIn('A: I painted a tray.',history)
        self.assertIn('B: I made a basket.',history)
        self.assertIn('A painted tray.',history)
        self.assertNotIn('annotation-canary',history)
        self.assertNotIn('scoring-only',history)
        self.assertEqual(row['haystack_dates'],['2023-05-08T13:56:00'])
