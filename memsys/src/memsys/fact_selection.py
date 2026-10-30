"""Question-scoped ordering of an existing fact pool, without extra retrieval."""
from collections import Counter
import re


def order_facts(result, plan):
    question=' '.join(plan.queries)
    assistant=bool(re.search(
        r'\b(?:you|the assistant)\s+(?:(?:previously|earlier|once)\s+)?(?:recommended|suggested|mentioned|told|gave|listed|provided|computed)\b|'
        r'\b(?:you|the assistant)\s+(?:previously|earlier|once)\s+(?:recommend|suggest|mention|tell|give|list|provide|compute)\b|'
        r'\bdid you\s+(?:recommend|suggest|mention|tell|give|list|provide|compute)\b|'
        r'\byour (?:earlier|previous) (?:answer|suggestion|recommendation)\b|'
        r'你.{0,6}(?:以前|之前|上次|曾经|当时).{0,12}(?:推荐|建议|列出|举例)|'
        r'你.{0,8}(?:推荐过|说过|提过|建议过|列出过)|你给我的.{0,12}(?:建议|推荐|例子)',question,re.I))
    personal=bool(re.search(r'\b(?:i|my|me|mine|our|we)\b|我',question,re.I))
    scope='assistant' if assistant else 'user' if plan.operation=='advice' or personal else 'mixed'
    original=list(result.ranked)
    if scope=='mixed':return original,dict(scope=scope,operation=plan.operation,reordered=False)
    def priority(candidate):
        fact=result.items[candidate.key]
        if candidate.kind!='fact':return 2
        if scope=='assistant':return 0 if fact.kind=='assistant' else 1
        if plan.operation=='advice' and fact.kind=='preference':return 0
        return 1 if fact.kind=='assistant' else 0
    ordered=sorted(original,key=priority)
    # For a requested collection, give each explicit occurrence/object a first
    # opportunity before packing its additional descriptions. Missing identities
    # remain independent; no text-similarity merging or candidate deletion.
    if plan.needs_all:
        seen=set();front=[];tail=[]
        for candidate in ordered:
            fact=result.items[candidate.key]
            key=(priority(candidate),fact.semantics.get('event_key') or fact.semantics.get('object_key')) if candidate.kind=='fact' else None
            if key is None or not key[1]:key=(candidate.key,)
            (tail if key in seen else front).append(candidate)
            seen.add(key)
        # Keep scope priority across the diversity passes as well.
        ordered=sorted(front+tail,key=priority)
    return ordered,dict(scope=scope,operation=plan.operation,reordered=[c.key for c in ordered]!=[c.key for c in original],
        candidate_kinds=dict(Counter(result.items[c.key].kind for c in ordered if c.kind=='fact')),
        retained_candidates=len(ordered))
