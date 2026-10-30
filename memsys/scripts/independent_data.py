"""Whitelist public conversation inputs; keep annotations outside memory input."""
from datetime import datetime, timedelta
import re


UNDATED = ('[Source provenance: original conversation timestamps were not released. '
           'Header times indicate import order, not original event dates.]\n')


def convert_convomem(item, question_type, question_id, import_at):
    dates, sessions, ids = [], [], []
    conversations = item['conversations']
    if not conversations:
        raise ValueError('No original conversations')
    for i, conversation in enumerate(conversations):
        turns=[]
        for message in conversation['messages']:
            role=message['speaker'].casefold()
            if role not in ('user','assistant') or not isinstance(message['text'],str):
                raise ValueError('Unsupported original speaker or text')
            turns.append(dict(role=role,content=UNDATED+message['text']))
        if not turns:
            raise ValueError('Empty original conversation')
        sessions.append(turns)
        dates.append((import_at+timedelta(seconds=i)).isoformat())
        ids.append(str(conversation['id']))
    if len(set(ids))!=len(ids):
        raise ValueError('Duplicate original conversation IDs')
    if not isinstance(item['question'],str) or not isinstance(item['answer'],str):
        raise ValueError('Unsupported question or reference schema')
    return dict(question_id=question_id,question_type=question_type,question=item['question'],
                question_date=(import_at+timedelta(seconds=len(conversations))).isoformat(),
                answer=item['answer'],haystack_dates=dates,haystack_sessions=sessions,
                haystack_session_ids=ids,
                _adaptation=dict(dataset='ConvoMem',mode='complete published evidence conversations, oracle diagnostic',
                    source_clock='original dates absent; explicit import-order clock',
                    excluded_annotations=['message_evidences','containsEvidence','scenario_description',
                                          'personId','use_case_model_name','core_model_name']))


def convert_locomo(conversation, qa, question_id):
    data=conversation['conversation']
    keys=sorted((k for k in data if re.fullmatch(r'session_\d+',k)),key=lambda k:int(k.split('_')[1]))
    if not keys or qa['category']!=2:
        raise ValueError('Expected original sessions and temporal category2')
    dates, sessions, ids=[],[],[]
    for key in keys:
        at=datetime.strptime(data[key+'_date_time'],'%I:%M %p on %d %B, %Y')
        lines=['Recorded conversation between human participants '+data['speaker_a']+' and '+data['speaker_b']+'.']
        for turn in data[key]:
            if turn['speaker'] not in (data['speaker_a'],data['speaker_b']):
                raise ValueError('Unknown original human participant')
            lines.append(turn['speaker']+': '+turn['text'])
            if turn.get('blip_caption'):
                lines.append('Released image caption: '+turn['blip_caption'])
        sessions.append([dict(role='user',content='\n'.join(lines))])
        dates.append(at.isoformat())
        ids.append(str(conversation['sample_id'])+'/'+key)
    if not isinstance(qa['question'],str) or not isinstance(qa['answer'],str):
        raise ValueError('Unsupported question or reference schema')
    return dict(question_id=question_id,question_type='temporal-reasoning',question=qa['question'],
                question_date=max(dates),answer=qa['answer'],haystack_dates=dates,
                haystack_sessions=sessions,haystack_session_ids=ids,
                _adaptation=dict(dataset='LoCoMo',mode='complete released human transcripts',
                    source_clock='original published session timestamps',
                    speaker_mapping='User supplies transcripts; named human speakers retained in text',
                    excluded_annotations=['evidence','event_summary','observation','session_summary'],
                    images='Released captions retained; external images not fetched'))
