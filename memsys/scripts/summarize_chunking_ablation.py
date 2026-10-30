"""Report paired quality and experimental controls; never rewrite judge labels."""
import argparse
import collections
import json
import math
from pathlib import Path


def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


def summarize(directory, kinds, response_cache=None):
    status = json.loads((directory/'status.json').read_text())
    if status['status'] != 'complete':
        raise ValueError('Incomplete comparison: '+str(directory))
    evaluations = list(directory.glob('hypotheses.jsonl.eval-results-*'))
    if len(evaluations) != 1:
        raise ValueError('Expected one judge file')
    labels = {r['question_id']:bool(r['autoeval_label']['label']) for r in rows(evaluations[0])}
    if set(labels) != set(kinds):
        raise ValueError('Judge coverage differs from fixed sample')
    counts = collections.defaultdict(lambda:dict(correct=0,total=0))
    for key, correct in labels.items():
        counts[kinds[key]]['total'] += 1
        counts[kinds[key]]['correct'] += int(correct)
    records = rows(directory/'usage.jsonl')
    events = rows(directory/'response-events.jsonl')
    traces = [json.loads(p.read_text()) for p in (directory/'traces').glob('*.json')]
    audit_statuses = collections.Counter(t.get('aggregation',{}).get('status','none') for t in traces)
    selected_expansion = collections.Counter(
        event['reason'] for trace in traces for candidate in trace['candidates']
        if candidate['key'] in trace['selected']['episodes']
        for event in candidate.get('parent_expansion',[]))
    actual_tokens = sum(r['prompt_tokens']+r['completion_tokens'] for r in records
                        if r['purpose'] not in ('judge','embed','embed_query'))
    logical_tokens = actual_tokens
    uncached_requests = 0
    if response_cache:
        for event in events:
            if event['purpose'] in ('judge','embed','embed_query'):
                continue
            cached = response_cache/(event['request_sha256']+'.json')
            if event['replayed'] and cached.exists():
                response = json.loads(cached.read_text())
                logical_tokens += response['prompt_tokens']+response['completion_tokens']
            elif event['replayed']:
                raise ValueError('Replayed response missing from cache: '+str(cached))
            elif not cached.exists():
                uncached_requests += 1
    return dict(correct=sum(labels.values()),total=len(labels),by_type=dict(counts),labels=labels,
        answer_cloud_tokens=actual_tokens,
        actual_cloud_calls=sum(r['purpose'] not in ('embed','embed_query') for r in records),
        replayed_model_requests=sum(r['replayed'] for r in events),
        replayed_local_scores=sum(t.get('reranking',{}).get('score_replay',False) for t in traces),
        audit_statuses=dict(audit_statuses),
        selected_parent_expansion=dict(selected_expansion),
        aggregate_pages=sum(t.get('aggregation',{}).get('pages_total',0) for t in traces),
        aggregate_pages_processed=sum(t.get('aggregation',{}).get('pages_processed',0) for t in traces),
        answer_requested_tokens_including_replay=logical_tokens if response_cache else None,
        answer_requests_without_completed_cache=uncached_requests if response_cache else None,
        quote_validation_failures=sum(len(t.get('aggregation',{}).get('page_validation_failures',[])) for t in traces),
        full_search=sum(t.get('coverage',{}).get('search_complete',False) for t in traces))


def paired(base, variant):
    changes = {key:('improved' if variant[key] else 'regressed') for key in base if base[key]!=variant[key]}
    counts = collections.Counter(changes.values())
    improved, regressed = counts['improved'], counts['regressed']
    n = improved+regressed
    p = min(1,2*sum(math.comb(n,k) for k in range(min(improved,regressed)+1))/2**n) if n else 1
    return dict(improved=improved,regressed=regressed,net=improved-regressed,
                exact_mcnemar_p=p,changes=changes)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--sample',type=Path,required=True)
    parser.add_argument('--variants',nargs='+',required=True)
    parser.add_argument('--baseline',default='fixed')
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--response-cache',type=Path)
    args = parser.parse_args()
    sample = json.loads(args.sample.read_text())
    kinds = {r['question_id']:r['question_type'] for r in sample}
    summaries = {v:summarize(args.root/v,kinds,args.response_cache) for v in args.variants}
    base = summaries[args.baseline]['labels']
    for v,s in summaries.items(): s['paired_vs_fixed'] = paired(base,s['labels'])
    result = dict(scope='Small paired diagnostic; raw LLM judge labels preserved', variants=summaries,
        paired_comparisons={left+'__'+right:paired(summaries[left]['labels'],summaries[right]['labels'])
                            for left,right in (('parent','parent_wide'),('fixed','fixed_wide'),
                                               ('parent','parent_episode'),('fixed','fixed_episode'),
                                               ('parent','parent_fine'),('parent','parent_repair'),
                                               ('fixed_wide','parent_wide'))
                            if left in summaries and right in summaries},
        limitation='Fixed plans and shared response replay control variation but create correlated trials; replayed latency and token totals are not normal deployment cost comparisons')
    args.out.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    for v,s in summaries.items():
        print(v,str(s['correct'])+'/'+str(s['total']),'paired',s['paired_vs_fixed']['net'],
              'p',round(s['paired_vs_fixed']['exact_mcnemar_p'],4))


if __name__=='__main__': main()
