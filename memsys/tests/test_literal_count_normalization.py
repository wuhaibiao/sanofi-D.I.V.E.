import json
import unittest

from memsys.aggregation import (_normalize_literal_count_schema, answer_with_validation,
                                inspect_calculation)
from memsys.fake import ScriptedChat


class LiteralCountNormalizationTest(unittest.TestCase):
    def fixture(self):
        quotes=['This is the fourth round I completed in my indoor exercise routine.',
                'I completed 6 exercises outside, with one round for each exercise.']
        evidence={'E1':'User: '+quotes[0],'E2':'User: '+quotes[1]}
        items=[dict(source='E1',quote=quotes[0],value='4',unit='exercise rounds',
                    ordinal_progress=True,value_type='cumulative'),
               dict(source='E2',quote=quotes[1],value='6',unit='exercise rounds')]
        spec=dict(operation='sum',unit='exercise rounds',items=items,result='10')
        return spec,evidence

    def output(self,spec):
        return 'CALCULATION: '+json.dumps(spec)+'\nANSWER: 10 exercise rounds.'

    def normalize(self,spec,evidence):
        output=self.output(spec)
        audit=inspect_calculation(output,evidence)[1]
        return _normalize_literal_count_schema(output,audit,evidence,{},None,'What was my total exercise count?')

    def test_only_schema_changes_and_public_validator_remains_strict(self):
        spec,evidence=self.fixture()
        original=json.loads(json.dumps(spec))
        self.assertEqual(inspect_calculation(self.output(spec),evidence)[1]['status'],'invalid')
        stripped,audit=self.normalize(spec,evidence)
        self.assertEqual(audit['status'],'checks_passed')
        self.assertEqual(audit['computed'],'10')
        self.assertEqual([i['quantity_label'] for i in audit['calculation']['items']],['round','exercises'])
        self.assertEqual(spec,original)
        self.assertIn('ANSWER: 10 exercise rounds.',stripped)
        for old,new in zip(spec['items'],audit['calculation']['items']):
            self.assertEqual(old,{k:v for k,v in new.items() if k!='quantity_label'})

    def test_normalization_saves_generation_repair_and_still_reviews_support(self):
        spec,evidence=self.fixture()
        model=ScriptedChat({'answer':self.output(spec),
            'answer_review':json.dumps(dict(responsive=True,supported=True))})
        _,answer,audit=answer_with_validation(model,[],evidence=evidence,
            question='What was my total exercise count?',semantic_enabled=True,review_policy='always')
        self.assertEqual(answer,'10 exercise rounds.')
        self.assertEqual(model.purposes(),['answer','answer_review'])
        self.assertIn('schema_normalization',audit)
        self.assertEqual(audit['attempts'][0]['status'],'invalid')
        json.dumps(audit)

    def test_semantic_rejection_cannot_publish_normalized_quantity(self):
        spec,evidence=self.fixture()
        model=ScriptedChat({'answer':self.output(spec),
            'answer_review':json.dumps(dict(responsive=True,supported=False,reason='different scope'))})
        _,answer,audit=answer_with_validation(model,[],evidence=evidence,
            question='What was my total exercise count?',semantic_enabled=True,review_policy='always')
        self.assertEqual(audit['status'],'incomplete')
        self.assertNotEqual(answer,'10 exercise rounds.')
        self.assertGreaterEqual(model.purposes().count('answer_review'),1)

    def test_wrong_value_wrong_result_and_missing_ordinal_claim_remain_invalid(self):
        spec,evidence=self.fixture()
        for change in ('value','result','ordinal'):
            changed=json.loads(json.dumps(spec))
            if change=='value': changed['items'][1]['value']='7'
            elif change=='result': changed['result']='12'
            else:
                changed['items'][0].pop('ordinal_progress')
                changed['items'][0].pop('value_type')
            with self.subTest(change=change):
                self.assertIsNone(self.normalize(changed,evidence))

    def test_ambiguous_labels_and_measurements_do_not_normalize(self):
        cases=[('packet file','3 packets and 3 files.'),
               ('hike miles','I walked 3 miles.'),
               ('work hours','I worked 3 hours.'),
               ('US dollars','I paid 3 dollars.')]
        for unit,quote in cases:
            evidence={'E1':'User: '+quote}
            spec=dict(operation='sum',unit=unit,items=[dict(source='E1',quote=quote,value='3',unit=unit)],result='3')
            with self.subTest(unit=unit): self.assertIsNone(self.normalize(spec,evidence))

    def test_derived_facts_and_assistant_examples_do_not_normalize(self):
        spec,evidence=self.fixture()
        assistant={key:value.replace('User: ','Assistant: ') for key,value in evidence.items()}
        self.assertIsNone(self.normalize(spec,assistant))
        spec['items'][0]['source']='F1'
        evidence['F1']=evidence.pop('E1')
        self.assertIsNone(self.normalize(spec,evidence))

    def test_review_disabled_retains_existing_repair_instead_of_normalization(self):
        spec,evidence=self.fixture()
        model=ScriptedChat({'answer':self.output(spec)})
        _,_,audit=answer_with_validation(model,[],evidence=evidence,
            question='What was my total exercise count?',semantic_enabled=True,review_policy='off')
        self.assertEqual(model.purposes(),['answer','answer'])
        self.assertEqual(audit['status'],'invalid')
        self.assertNotIn('schema_normalization',audit)

    def test_literal_adjective_and_indefinite_article_preserve_noun_phrase(self):
        quotes=['I kept 7 signed postcards.','I added a new postcard to my pile.']
        evidence={'E1':'User: '+quotes[0],'E2':'User: '+quotes[1]}
        spec=dict(operation='sum',unit='postcards',items=[dict(source='E1',quote=quotes[0],value='7',unit='postcards'),
                 dict(source='E2',quote=quotes[1],value='1',unit='postcards')],result='8')
        output='CALCULATION: '+json.dumps(spec)+'\nANSWER: 8 postcards.'
        audit=inspect_calculation(output,evidence)[1]
        normalized=_normalize_literal_count_schema(output,audit,evidence,{},None,'What is my total postcard count?')
        self.assertIsNotNone(normalized)
        self.assertEqual([i['quantity_label'] for i in normalized[1]['calculation']['items']],['signed postcards','new postcard'])
        self.assertEqual(normalized[1]['computed'],'8')

    def test_container_of_objects_and_ordinal_adjective_cannot_be_implicit_singletons(self):
        for quote in ['I received a box of postcards.','I have a fourth postcard.','I received a 7 postcard bundle.']:
            evidence={'E1':'User: '+quote}
            spec=dict(operation='sum',unit='postcards',items=[dict(source='E1',quote=quote,value='1',unit='postcards')],result='1')
            with self.subTest(quote=quote):
                self.assertIsNone(self.normalize(spec,evidence))

    def test_existing_count_label_can_expand_to_literal_adjective_phrase(self):
        quote='I added a polished badge to my collection.'
        evidence={'E1':'User: '+quote}
        spec=dict(operation='count_groups',unit='badges',items=[dict(source='E1',quote=quote,
            quantity_label='badge',value='1',unit='badges')],result='1')
        output='CALCULATION: '+json.dumps(spec)+'\nANSWER: 1 badge.'
        audit=inspect_calculation(output,evidence)[1]
        normalized=_normalize_literal_count_schema(output,audit,evidence,{},None,'How many badges?')
        self.assertIsNotNone(normalized)
        self.assertEqual(normalized[1]['calculation']['items'][0]['quantity_label'],'polished badge')
        self.assertEqual(normalized[1]['schema_normalization']['from_operation'],'count_groups')
        changed=json.loads(json.dumps(spec))
        changed['items'][0]['quantity_label']='pin'
        self.assertIsNone(self.normalize(changed,evidence))

    def test_generation_repair_can_normalize_but_still_requires_review(self):
        spec,evidence=self.fixture()
        model=ScriptedChat({'answer':['ANSWER: 10 exercise rounds.',self.output(spec)],
            'answer_review':json.dumps(dict(responsive=True,supported=True))})
        _,answer,audit=answer_with_validation(model,[],evidence=evidence,
            question='What was my total exercise count?',require_calculation=True,
            semantic_enabled=True,review_policy='always')
        self.assertEqual(answer,'10 exercise rounds.')
        self.assertEqual(model.purposes(),['answer','answer','answer_review'])
        self.assertEqual([a['status'] for a in audit['attempts']],['invalid','invalid','checks_passed'])
        self.assertIn('schema_normalization',audit)
        self.assertIn('ordinal_progress=true',model.calls[1][1][-1]['content'])

    def test_existing_group_label_does_not_infer_ordinal_or_change_values(self):
        quote='I completed my sixth practice round.'
        evidence={'E1':'User: '+quote}
        spec=dict(operation='count_groups',unit='rounds',items=[dict(source='E1',quote=quote,
            quantity_label='round',value='6',unit='rounds')],result='6')
        self.assertIsNone(self.normalize(spec,evidence))
        spec['items'][0].update(ordinal_progress=True,value_type='cumulative',value='5')
        self.assertIsNone(self.normalize(spec,evidence))
