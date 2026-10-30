"""Conservative literal termination witnesses, not general semantic entailment."""
import re
from datetime import date


def termination_witness(relation, quotes, today=None):
    """Accept only an affirmative, object-anchored source clause.

    Unsupported languages/constructions stay unverified. Past tense, replacement,
    recording dates and the model's proposed end date are not termination proof.
    """
    from .relational_memory import contains_name, name_key, HUBS
    state=relation['state']
    ownership=bool(re.search(r'own|possess|keep|kept|has|had|have|belongs',relation['predicate'])) or relation['predicate'] in {
        'sold','donated','gave_away','disposed_of','sold_item','donated_item'}
    def token(name):return r'(?<![A-Za-z0-9_])'+re.escape(name)+r'(?![A-Za-z0-9_])'
    target='(?:'+'|'.join(token(n) for n in [relation['object']['name'],*relation['object'].get('aliases',[])])+')'
    subject=relation['subject']['name']
    actor=r'(?:I|we)' if name_key(subject)=='user' else token(subject)
    prefix=actor+r'\s+(?:(?:have|has|had|already|just|recently)\s+){0,3}'
    determiner=r'(?:(?:my|the|a|an|his|her|their|our|that|this|old|older|former|spare)\s+){0,3}'
    if state=='cancelled':
        action=prefix+r'(?:cancelled|canceled|called off|revoked)\s+'+determiner+target
        chinese=r'(?:我|我们)' if name_key(subject)=='user' else re.escape(subject)
        action+='|'+chinese+r'(?:已经|已)?(?:取消|撤销|终止)(?:了)?'+target
    elif ownership:
        verbs=r'(?:sold|donated|disposed of|gave away|given away|threw away|no longer (?:own|have|keep|possess))'
        action=prefix+verbs+r'\s+'+determiner+target
        action+='|'+prefix+r'(?:gave|given)\s+'+determiner+target+r'\s+away\b'
        if name_key(subject)=='user':
            action+='|'+r'\bmy\s+'+target+r'\s+(?:was|has been|had been)\s+(?:sold|donated|given away|disposed of)\b'
        chinese=r'(?:我|我们)' if name_key(subject)=='user' else re.escape(subject)
        action+='|'+chinese+r'(?:已经|已)?(?:把|将)'+target+r'(?:卖掉|卖了|售出|送给|赠予|捐出|丢掉|扔掉)'
        action+='|'+chinese+r'不再拥有'+target
    else:
        # Restricted grammatical constructions, not a nearby-verb keyword test.
        forms={'leads':('leading','lead','led'), 'works_at':('working at','work at','worked at'),
            'works_for':('working for','work for','worked for'),
            'participates_in':('participating in','participate in','participated in'),
            'attends':('attending','attend','attended')}.get(relation['predicate'])
        if not forms:return None
        present=relation['predicate'].replace('_',' ')
        action=prefix+r'(?:stopped|ceased)\s+'+re.escape(forms[0])+r'\s+'+determiner+target
        action+='|'+prefix+r'no longer\s+(?:'+re.escape(present)+'|'+re.escape(forms[1])+r')\s+'+determiner+target
        action+='|'+prefix+re.escape(forms[2])+r'\s+'+determiner+target+r'\s+until\s+\d{4}-\d{2}-\d{2}\b'
    negative=r"\b(?:not|never|haven't|hasn't|hadn't|didn't|don't|doesn't|might|may|could|would|will|if|unless|maybe|perhaps|suppose|imagine|assuming|whether|plan(?:s|ned)? to|consider(?:ing)?)\b|(?:没有|还没|尚未|并未|没把|未曾|可能|打算|准备|如果|计划|是否|假设|假如|会不会)"
    names=lambda entity:[entity['name'],*entity.get('aliases',[])]
    for role,quote in quotes:
        # A generic assistant assertion cannot terminate a user's relationship.
        if name_key(relation['subject']['name'])=='user' and role!='user':continue
        for clause in re.split(r'(?<=[.!?。！？;；])\s*|\n|[,，]\s*(?:but|while|但是|但|不过)\s*',quote):
            if '?' in clause or '？' in clause:continue
            if not any(contains_name(clause,n) for n in names(relation['object'])):continue
            if name_key(relation['subject']['name']) not in HUBS and not any(
                    contains_name(clause,n) for n in names(relation['subject'])):continue
            # "no longer" is an explicit ending, not a negation of that ending.
            check=re.sub(r'\bno longer\b|不再','',clause,flags=re.I)
            if re.search(negative,check,re.I) or not re.search(action,clause,re.I):continue
            until=re.search(r'\buntil\s+(\d{4}-\d{2}-\d{2})\b',clause,re.I)
            if until and today is not None:
                try:
                    if date.fromisoformat(until[1])>today:continue
                except ValueError:continue
            return dict(role=role,quote=clause.strip())
    return None


def check_termination(relation, quotes, today=None):
    if relation['state'] not in ('ended','cancelled'):
        return None
    witness=termination_witness(relation,quotes,today)
    if witness:
        relation['state_evidence']=dict(status='literal_termination_witness',**witness)
        if relation.get('valid_to') and relation['valid_to'] not in witness['quote']:
            relation['valid_to']=None
        return True
    asserted=relation['state']
    relation.update(state='unknown',valid_to=None,state_evidence=dict(status='unverified',asserted_state=asserted))
    return False
