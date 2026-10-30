"""Single-call answers with diagnostic-only evidence and arithmetic checks.

Bindings prove only that supplied excerpts/IDs exist in selected facts. They do
not independently establish semantic entailment, event identity or relevance.
Audit findings never reject, replace or correct the model's answer.
"""
from dataclasses import dataclass, field, fields
from decimal import Decimal, InvalidOperation
import json
import re

from .llm import ModelError, ChatResult


@dataclass(frozen=True)
class EvidenceResponse(ChatResult):
    evidence_audit: dict = field(default_factory=dict)

ANSWER_FORMAT = '''
With retrieved facts, return one compact JSON envelope and include memory_answer:true.
For factual questions order its fields: memory_answer, answer (the user-facing text),
memory_use (used|not_needed|insufficient), reason, evidence:[], calculations:[].
For advice order its fields: memory_answer, advice_task (the short planning step below),
answer (the complete user-facing text), memory_use, reason, evidence:[], calculations:[].
Honor requested exact answer formats. Keep the audit compact:
at most 16 evidence entries and 8 calculations; cite each supporting fact once,
using the shortest sufficient quote and answer_span, never a duplicate paragraph.
Omit reason when memory_use=used; otherwise use one short sentence.
Evidence entries: fact_id, quote (verbatim excerpt or literal field label: value), answer_span (exact
answer substring), use:answer|constraint. Apply relevant user constraints in advice;
assistant suggestions are not user preferences. Visible history needs no invented
IDs. The host shows only answer; if no facts apply, explain that in reason.
Write the complete final answer, including literal computed results, directly in answer;
never use calculation placeholders. Ground calculations in what the facts actually say,
including the relevant objects, events and units. If the facts do not establish a
needed premise, explain that uncertainty while preserving other supported conclusions.
Evidence and calculations are diagnostic metadata, not prerequisites for answering.
Missing internal IDs or identity fields do not by themselves mean the facts are missing.
Optional calculations: operation=clock_add with start/duration {fact_id,quote}, or
sum/difference with left/right {fact_id,quote,unit}; include the literal result.
Without retrieved facts, plain text is allowed.
For advice only, first choose the applicable task and return a compact advice_task object
before answer: {goals:[up to 3 selected user fact IDs],constraints:[up to 5 selected user
fact IDs]}. Use empty lists if no remembered goal applies. This is a short planning step:
let the selected task and constraints determine the suggestions in answer, not just its
introduction. Multiple plausible tasks need conditional suggestions or a focused question;
cancelled tasks and assistant options are not current user commitments. Continue with the
complete answer and ordinary audit fields. advice_task is diagnostic; its absence or an
invalid ID never blocks delivery. For other questions keep answer first and omit this field.
'''


def _fail(message):
    raise ModelError('answer evidence contract: '+message,status=422)


def _literal_present(text,quote):
    numeric_start=quote[0].isdigit() or quote[0] in '+-'
    left=r'(?<![A-Za-z0-9_.+\-])' if numeric_start else r'(?<![A-Za-z0-9_])' if quote[0].isascii() and quote[0].isalnum() else ''
    right=r'(?![A-Za-z0-9_]|\.\d)' if quote[-1].isdigit() else r'(?![A-Za-z0-9_])' if quote[-1].isascii() and quote[-1].isalnum() else ''
    return bool(re.search(left+re.escape(quote)+right,text))


def _display_items(evidence_items,memory):
    items={row['key']:dict(row) for row in evidence_items if row.get('kind')=='fact'}
    if memory is None:return items
    matches=list(re.finditer(r'(?m)^\[(F\d+)\] ',memory))
    for index,match in enumerate(matches):
        key=match[1]
        if key not in items or sum(m[1]==key for m in matches)!=1:continue
        end=matches[index+1].start() if index+1<len(matches) else len(memory)
        heading = re.search(r'(?m)^#{2,3} ', memory[match.end():end])
        if heading:
            end = match.end() + heading.start()
        # Only actual recalled memory is passed here, never the surrounding host
        # prompt or use instructions. Metadata quoted by the model is visible too.
        items[key]['text']=memory[match.start():end].strip()
    return items


def _operand(data, items):
    if not isinstance(data,dict):_fail('operand must be an object')
    key,quote=data.get('fact_id'),data.get('quote')
    if not isinstance(key,str) or key not in items:_fail('unknown selected fact ID')
    item=items[key]
    if not isinstance(quote,str) or not quote.strip() or quote not in item.get('text',''):
        _fail('operand quote is absent from selected fact')
    if not _literal_present(item.get('text',''),quote):_fail('quote is only part of a different token')
    return item,quote


def _operation(data):
    if not isinstance(data,dict):_fail('calculation must be an object')
    operation=data.get('operation',data.get('type'))
    if data.get('operation') and data.get('type') and data['operation']!=data['type']:_fail('conflicting calculation operations')
    if operation not in ('clock_add','sum','difference'):_fail('unsupported calculation operation')
    return operation


def _single_quantity(quote,pattern):
    matches=list(re.finditer(pattern,quote,re.I))
    if len(matches)!=1:_fail('ambiguous or unsupported quantity')
    match=matches[0];remainder=quote[:match.start()]+quote[match.end():]
    if re.search(r'\d|\b(?:not|never|no|unknown|maybe|might)\b|(?:不|没|未|可能)',remainder,re.I):
        _fail('quantity excerpt has conflicting or uncertain context')
    return match


def _number(quote):
    literal=_numeric_field(quote)
    if literal is not None:quote=literal[1]
    if not re.fullmatch(r'[-+]?\d{1,12}(?:\.\d{1,8})?',quote.strip()):_fail('unsupported numeric operand')
    try:return Decimal(quote.strip())
    except InvalidOperation:_fail('invalid decimal operand')


def _numeric_field(quote):
    """One literal JSON field, never a guessed number from a larger object."""
    raw=quote.strip()
    try:
        data=json.loads(raw if raw.startswith('{') else '{'+raw+'}',parse_int=str,parse_float=str,
                        object_pairs_hook=lambda pairs:dict(pairs) if len(pairs)==1 else None)
    except (ValueError,TypeError):return None
    if not isinstance(data,dict) or len(data)!=1:return None
    key,value=next(iter(data.items()))
    if not isinstance(value,str) or not re.fullmatch(r'[-+]?\d{1,12}(?:\.\d{1,8})?',value.strip()):return None
    return key,value.strip()


def _duration_minutes(quote):
    literal=_numeric_field(quote)
    if literal is not None:
        label,value=literal
        # The unit must be written in the actual field label. Bare "duration"
        # has no unit, so neither the model's claimed unit nor a default suffices.
        label=re.sub(r'([a-z])([A-Z])',r'\1_\2',label)
        units=re.findall(r'[A-Za-z]+|分钟|小时',label)
        unit=units[-1].lower() if units else ''
        if unit not in ('minute','minutes','min','mins','hour','hours','hr','hrs','分钟','小时'):
            _fail('duration field lacks an explicit supported time unit')
        amount=Decimal(value)
    else:
        match=_single_quantity(quote,r'(?<![\d.+\-])(\d{1,5}(?:\.\d{1,4})?)\s*(minutes?|mins?|hours?|hrs?|分钟|小时)(?![A-Za-z])')
        amount,unit=Decimal(match[1]),match[2].lower()
    return amount*(60 if unit in ('hour','hours','hr','hrs','小时') else 1)


def calculate(data, items):
    operation=_operation(data)
    if operation=='clock_add':
        first,start=_operand(data.get('start'),items)
        second,duration=_operand(data.get('duration'),items)
        a=(first.get('semantics') or {}).get('event_key')
        b=(second.get('semantics') or {}).get('event_key')
        if not a or a!=b:_fail('clock operands lack a shared stored event identity')
        match=_single_quantity(start,r'(?<![\d:])(\d{1,2}):(\d{2})\s*(AM|PM)?(?![\d:])')
        hour,minute=int(match[1]),int(match[2]);half=(match[3] or '').upper()
        if minute>=60 or hour>23 or half and not 1<=hour<=12:_fail('invalid clock time')
        if half:hour=hour%12+(12 if half=='PM' else 0)
        minutes=_duration_minutes(duration)
        if minutes!=int(minutes) or not 0<=minutes<=7*24*60:_fail('duration is outside supported bounds')
        total=hour*60+minute+int(minutes);days,total=divmod(total,24*60)
        return '%02d:%02d%s'%(total//60,total%60,(' (+%d day%s)'%(days,'' if days==1 else 's')) if days else '')
    if operation not in ('sum','difference'):_fail('unsupported calculation operation')
    left,a=_operand(data.get('left'),items);right,b=_operand(data.get('right'),items)
    key=(left.get('semantics') or {}).get('object_key')
    if not key or key!=(right.get('semantics') or {}).get('object_key'):_fail('numeric operands lack a shared stored object identity')
    units=[data[k].get('unit') for k in ('left','right')]
    if not isinstance(units[0],str) or not units[0].strip() or units[0]!=units[1] or any(
            not _literal_present(item.get('text',''),unit) for unit,item in zip(units,(left,right))):_fail('numeric units are absent or different')
    if operation=='sum' and data['left']['fact_id']==data['right']['fact_id']:_fail('sum repeats one contribution')
    if operation=='sum' and any((item.get('semantics') or {}).get('value_type') not in ('increment','event_quantity')
                               for item in (left,right)):_fail('sum operands are not independent contributions')
    value=_number(a)+_number(b) if operation=='sum' else _number(a)-_number(b)
    return format(value,'f').rstrip('0').rstrip('.') if '.' in format(value,'f') else format(value,'f')


def _equivalent_result(operation,result,computed):
    if not isinstance(result,(str,int,float)) or isinstance(result,bool):return False
    text=str(result).strip()
    if operation=='clock_add':
        pattern=r'(\d{1,2}):(\d{2})\s*(AM|PM)?(?:\s*\(\+(\d+) days?\))?'
        def clock(value):
            match=re.fullmatch(pattern,value,re.I)
            if not match:return None
            h,m=int(match[1]),int(match[2]);half=(match[3] or '').upper()
            if m>=60 or h>23 or half and not 1<=h<=12:return None
            if half:h=h%12+(12 if half=='PM' else 0)
            return h*60+m+int(match[4] or 0)*24*60
        return clock(text) is not None and clock(text)==clock(computed)
    try:return bool(re.fullmatch(r'[-+]?\d{1,13}(?:\.\d{1,8})?',text)) and Decimal(text)==Decimal(computed)
    except InvalidOperation:return False


def _answer_envelope_fields(keys):
    # A missing audit marker cannot force application metadata into the answer.
    # An ordinary JSON object with an answer business field still passes through.
    return ('memory_answer' in keys or 'answer' in keys and ('advice_task' in keys or
        'memory_use' in keys and any(field in keys for field in ('evidence', 'calculations'))))


def decode_answer(text, evidence_items, *, memory=None, question=''):
    raw=text.strip()
    if raw.startswith('```json') and raw.endswith('```'):raw=raw[7:-3].strip()
    try:data=json.loads(raw)
    except (ValueError,TypeError):
        keys = set(re.findall(r'(?<!\\)"([a-z_]+)"\s*:', raw))
        if raw.startswith('{') and _answer_envelope_fields(keys):_fail('malformed answer object')
        data=None
    envelope = isinstance(data, dict) and _answer_envelope_fields(data)
    if not envelope:
        return text,dict(status='protocol_not_supplied',binding_verified=False,semantic_support_verified=False,
                         delivery_policy='diagnostic_only',answer_modified=False)
    answer=data.get('answer')
    if not isinstance(answer,str) or not answer.strip():_fail('answer is empty')
    try:
        audit=_audit_answer(data,evidence_items,memory=memory,question=question)
    except Exception as exc:
        # Delivery depends only on the complete answer, never on audit machinery.
        audit=dict(status='audit_error',binding_verified=False,semantic_support_verified=False,
                   errors=[type(exc).__name__+': '+str(exc)[:500]])
    audit.update(delivery_policy='diagnostic_only',answer_modified=False,
                 envelope_marker_supplied='memory_answer' in data)
    try:
        from .queryops import operation_for
        if operation_for(question) == 'advice':
            audit['advice_task'] = _advice_task_audit(data.get('advice_task'), evidence_items)
    except Exception as exc:
        audit['advice_task'] = dict(status='audit_error', error=type(exc).__name__,
                                    semantic_application_verified=False)
    return answer,audit


def _advice_task_audit(data, evidence_items):
    """Check selected IDs only; never claim that suggestions semantically fit."""
    if not isinstance(data, dict):
        return dict(status='not_supplied', semantic_application_verified=False)
    items = {r.get('key'): r for r in evidence_items if r.get('kind') == 'fact'}
    bound, errors = {}, []
    for field, limit in (('goals', 3), ('constraints', 5)):
        values = data.get(field)
        if not isinstance(values, list):
            errors.append('invalid_' + field); values = []
        if len(values) > limit:
            errors.append('too_many_' + field)
        bound[field] = []
        for key in values[:limit]:
            item = items.get(key) if isinstance(key, str) else None
            if item is None or item.get('memory_kind') == 'assistant':
                errors.append('not_selected_user_fact')
            elif key not in bound[field]:
                bound[field].append(key)
    return dict(status='ids_bound' if not errors else 'ids_incomplete', **bound,
                errors=errors, semantic_application_verified=False)


def _audit_answer(data, evidence_items, *, memory=None, question=''):
    """Return diagnostics only; this function has no answer-rendering authority."""
    items=_display_items(evidence_items,memory)
    answer=data['answer'];evidence=data.get('evidence');calculations=data.get('calculations',[])
    errors=[];unverified=[]
    def issue(message):
        errors.append(message)
    if data.get('memory_answer') is not True:issue('missing or invalid answer object marker')
    if not isinstance(evidence,list):
        issue('invalid evidence list');evidence=[]
    elif len(evidence)>64:
        issue('evidence list exceeds audit limit');evidence=evidence[:64]
    if not isinstance(calculations,list):
        issue('invalid calculation list');calculations=[]
    elif len(calculations)>16:
        issue('calculation list exceeds audit limit')
        calculations=calculations[:16]
    use=data.get('memory_use')
    if use not in ('used','not_needed','insufficient'):issue('invalid memory-use declaration')
    if use=='used' and not evidence:issue('used memories without evidence bindings')
    if use!='used' and not str(data.get('reason') or '').strip():issue('missing explanation for unused evidence')
    bound=set();field_bindings=0;constraints=0
    plain=lambda value:value.replace('**','').replace('__','')
    for row in evidence:
        if not isinstance(row,dict) or not isinstance(row.get('fact_id'),str) or row['fact_id'] not in items:
            issue('unknown selected fact ID');continue
        item=items[row['fact_id']];quote=row.get('quote')
        if not isinstance(quote,str) or not quote.strip():
            issue('empty evidence quotation');continue
        exact=_literal_present(item.get('text',''),quote)
        from .literal_binding import field_association
        associated=not exact and field_association(item.get('value'),quote)
        span=row.get('answer_span')
        span_present=isinstance(span,str) and bool(span.strip()) and (span in answer or plain(span) in plain(answer))
        if not span_present:unverified.append(dict(fact_id=row['fact_id'],reason='answer_span_not_literal'))
        purpose_valid=row.get('use') in ('answer','constraint')
        if not purpose_valid:issue('invalid evidence purpose')
        if row.get('use')=='constraint' and item.get('memory_kind')=='assistant':
            issue('assistant information is not a user constraint');purpose_valid=False
        if exact or associated:bound.add((row['fact_id'],quote))
        else:unverified.append(dict(fact_id=row['fact_id'],reason='quote_not_literal_or_uniquely_associated'))
        if (exact or associated) and span_present and purpose_valid:
            from .queryops import operation_for
            constraints+=bool(row.get('use')=='constraint' or operation_for(question)=='advice'
                              and item.get('memory_kind')=='preference')
        field_bindings+=bool(associated)
    checked=0;rejected=[];calculation_checks=[]
    for index,calculation in enumerate(calculations):
        try:
            operation=_operation(calculation)
            operand_fields=('start','duration') if operation=='clock_add' else ('left','right')
            for name in operand_fields:
                value=calculation.get(name)
                if not isinstance(value,dict) or not isinstance(value.get('quote'),str) or not value['quote'].strip() or not any(
                        key==value.get('fact_id') and value['quote'] in quote for key,quote in bound):
                    issue('calculation operand is missing from the evidence audit')
            # Strict identity/quote checks describe what this auditor can verify;
            # failure says nothing conclusive about the answer's semantics.
            computed=calculate(calculation,items)
            supplied=calculation.get('result')
            calculation_checks.append(dict(index=index,computed_result=computed,supplied_result=supplied))
            if supplied is None or not isinstance(supplied,(str,int,float)) or isinstance(supplied,bool) or not str(supplied).strip() or not _literal_present(answer,str(supplied)):
                _fail('calculation result is not bound to the answer')
            if not _equivalent_result(operation,supplied,computed):
                _fail('calculation result differs from computed evidence')
            checked+=1
        except ModelError as exc:
            issue(str(exc));rejected.append(dict(index=index,error=str(exc)))
    if '{{calculation:' in answer:
        issue('unresolved calculation placeholder')
    verified=not errors and not unverified
    audit=dict(status='bindings_checked' if verified else 'bindings_incomplete',binding_verified=verified,semantic_support_verified=False,
        binding_basis='selected_fact_display' if memory is not None else 'stored_fact_content',
        field_association_bindings=field_bindings,unverified_evidence=unverified,errors=errors,
        memory_use=use,evidence_count=len(evidence),constraint_bindings=constraints,
        deterministic_calculations=checked,rejected_calculations=rejected,calculation_checks=calculation_checks)
    return audit


def generate_answer(model, messages, evidence_items, *, max_tokens, max_tokens_limit=None, tags=None, memory=None):
    from .generation import complete_chat
    attempts=[]
    response=complete_chat(model,messages,purpose='answer',max_tokens=max_tokens,
        max_tokens_limit=max_tokens_limit,json_mode=bool(evidence_items),tags=tags,attempts=attempts)
    if response.finish_reason in ('length','incomplete','content_filter','refusal'):
        raise ModelError('answer did not finish: '+response.finish_reason)
    if not response.text.strip():raise ModelError('empty answer')
    question=next((m['content'] for m in reversed(messages) if m['role']=='user'),'')
    text,audit=decode_answer(response.text,evidence_items,memory=memory,question=question)
    audit['generation_attempts']=attempts
    values={f.name:getattr(response,f.name) for f in fields(ChatResult)}
    values['text']=text.strip()
    return EvidenceResponse(**values,evidence_audit=audit)
