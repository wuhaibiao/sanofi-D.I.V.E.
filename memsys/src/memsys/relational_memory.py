"""Source-backed relation indexes over facts; no model calls or source-text recall."""
from __future__ import annotations

from datetime import date
import hashlib
import json
import re
import unicodedata

from .schema import ARCHIVED, FACT_CURRENT, FACT_SUPERSEDED

ENTITY_TYPES = {'person', 'organization', 'project', 'place', 'item', 'event', 'activity', 'other', 'user', 'assistant'}
RELATION_STATES = {'active', 'ended', 'planned', 'cancelled', 'unknown'}
HUBS = {'user', 'assistant'}
PRONOUNS = {'i', 'you', 'we', 'us', 'myself', 'yourself', 'ourselves', 'it', 'they', 'them', 'he', 'she',
            'him', 'her', 'someone', 'something', 'unknown', 'the user', 'the assistant', '我', '你', '他', '她', '它'}


def name_key(value):
    return ' '.join(unicodedata.normalize('NFKC', str(value or '')).strip().casefold().split())


def contains_name(text, name):
    key, body = name_key(name), name_key(text)
    if not key:
        return False
    if re.search(r'[㐀-鿿]', key):
        return key in body
    return bool(re.search(r'(?<![a-z0-9_])' + re.escape(key) + r'(?![a-z0-9_])', body))


def alias_quote(text, name, alias):
    """Verify a declared pair using a local affirmative naming construction.

    Merely appearing in a sentence with an alias cue is not an identity proof.
    The matched original substring is recorded as a separate provenance span.
    """
    cue = r"(?:also\s+(?:called|known\s+as)|a\.?k\.?a\.?|short\s+for|goes\s+by|(?:nickname|alias)\s+(?:is|was)|abbreviated\s+as|简称(?:为|是)?|别名(?:为|是)?|又叫|也叫)"
    for left, right in ((name, alias), (alias, name)):
        pattern = (r'(?<![A-Za-z0-9_])' + re.escape(left) + r"(?:'s)?\s*[,，:：\(（]?\s*(?:(?:is|was|are)\s+|的)?" + cue +
                   r'\s*[\"“\'‘]?\s*' + re.escape(right) + r'(?![A-Za-z0-9_])')
        matches = list(re.finditer(pattern, text, re.I))
        if len(matches) == 1:
            quote = matches[0].group()
            if text.count(quote) == 1:
                return quote
    return None


def _day(value):
    if value is None or value == '':
        return None
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError:
        return None


def _entity(value):
    if isinstance(value, str):
        value = {'name': value}
    if not isinstance(value, dict):
        return None
    name = str(value.get('name') or '').strip()
    key = name_key(name)
    kind = value.get('type', 'other')
    if not key or len(name) > 256 or key in PRONOUNS:
        return None
    if key in HUBS:
        kind = key
    kind = kind if kind in ENTITY_TYPES else 'other'
    aliases = [a.strip() for a in value.get('aliases', []) if isinstance(a, str) and a.strip()
               and len(a) <= 256 and name_key(a) not in HUBS | PRONOUNS] if isinstance(value.get('aliases'), list) else []
    return dict(name=name, type=kind, identity=str(value.get('identity') or '').strip(), aliases=list(dict.fromkeys(aliases)))


def normalize_relations(value, entities=None):
    """Only normalize the declared protocol; never invent an edge from a scalar value."""
    definitions = {}
    for raw in entities if isinstance(entities, list) else []:
        entity = _entity(raw)
        if entity:
            definitions[name_key(entity['name'])] = entity
    out = []
    for raw in value if isinstance(value, list) else []:
        if not isinstance(raw, dict):
            continue
        endpoints = []
        for field in ('subject', 'object'):
            endpoint = raw.get(field)
            entity = _entity(endpoint)
            if entity and isinstance(endpoint, str):
                entity = definitions.get(name_key(endpoint), entity)
            endpoints.append(entity)
        predicate = str(raw.get('predicate') or '').strip().casefold()
        if not all(endpoints) or not re.fullmatch(r'[a-z][a-z0-9_]{0,79}', predicate):
            continue
        start, end = _day(raw.get('valid_from')), _day(raw.get('valid_to'))
        if raw.get('valid_from') and start is None or raw.get('valid_to') and end is None or start and end and start > end:
            continue
        state = raw.get('state', 'unknown')
        qualifiers = list(dict.fromkeys(q.strip() for q in raw.get('qualifiers', []) if isinstance(q, str) and q.strip())) \
            if isinstance(raw.get('qualifiers'), list) else []
        relation = dict(subject=endpoints[0], predicate=predicate, object=endpoints[1],
                        state=state if state in RELATION_STATES else 'unknown', valid_from=start, valid_to=end,
                        qualifiers=qualifiers)
        if relation not in out:
            out.append(relation)
    return out


def relation_identity(relation):
    return json.dumps(dict(subject={k: relation['subject'][k] for k in ('name', 'type', 'identity')},
        object={k: relation['object'][k] for k in ('name', 'type', 'identity')},
        **{k: relation[k] for k in ('predicate', 'state', 'valid_from', 'valid_to', 'qualifiers')}),
        sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def merge_relations(left, right):
    """Merge provenance/verified aliases only for an identical logical relation."""
    retained = json.loads(json.dumps(left, ensure_ascii=False))
    by_key = {relation_identity(r): r for r in retained}
    for candidate in right:
        key = relation_identity(candidate)
        if key not in by_key:
            relation = json.loads(json.dumps(candidate, ensure_ascii=False))
            retained.append(relation)
            by_key[key] = relation
        else:
            relation = by_key[key]
            for field in ('subject', 'object'):
                relation[field]['aliases'] = list(dict.fromkeys(relation[field]['aliases'] + candidate[field]['aliases']))
            if candidate.get('grounded'):
                relation['grounded'] = True
                relation['evidence_roles'] = sorted(set(relation.get('evidence_roles', [])) | set(candidate.get('evidence_roles', [])))
    return retained


def ground_relations(facts, rounds, reference_text='', report=None):
    """Check real, unique source quotes and literal entity anchors before publication.

    Quotes establish provenance, not semantic entailment. Ambiguous meanings remain
    the extractor's responsibility; we do not claim this check proves completeness.
    """
    source = {r.number: r for r in rounds}
    batch = '\n'.join(r.user_text + '\n' + r.assistant_text for r in rounds)
    for fact in facts:
        quotes = []
        for q in fact.source_quotes:
            try:
                number = int(str(q.get('round', '')).lstrip('Rr'))
            except (ValueError, TypeError):
                continue
            role, quote = q.get('role'), q.get('quote')
            if number not in fact.rounds or number not in source or role not in ('user', 'assistant') or not isinstance(quote, str) or not quote.strip():
                continue
            if getattr(source[number], role + '_text').count(quote) == 1:
                quotes.append((role, quote))
        accepted = []
        for candidate in fact.relations:
            if not quotes or fact.kind != 'assistant' and not any(role == 'user' for role, _ in quotes):
                if report is not None:
                    report.relation_warnings.append('Relation lacks an unambiguous source quote in the asserted speaker scope.')
                continue
            relation = json.loads(json.dumps(candidate, ensure_ascii=False))
            supported = True
            for field in ('subject', 'object'):
                entity = relation[field]
                if name_key(entity['name']) not in HUBS and not contains_name(batch + '\n' + reference_text, entity['name']):
                    supported = False
                # An alias is an identity claim: co-occurrence alone is insufficient.
                verified = []
                for alias in entity['aliases']:
                    witness = None
                    for number in fact.rounds:
                        for role in ('user', 'assistant') if fact.kind == 'assistant' else ('user',):
                            quote = alias_quote(getattr(source[number], role + '_text'), entity['name'], alias)
                            if quote:
                                witness = dict(round=number, role=role, quote=quote)
                                break
                        if witness:
                            break
                    if witness:
                        verified.append(alias)
                        if witness not in fact.source_quotes:
                            fact.source_quotes.append(witness)
                entity['aliases'] = verified
                if entity['identity'] and not contains_name(batch, entity['identity']):
                    entity['identity'] = ''
            if not supported:
                if report is not None:
                    report.relation_warnings.append('Relation endpoint has no literal anchor in new sources or supplied reference context.')
                continue
            relation['evidence_roles'] = sorted({role for role, _ in quotes})
            relation['source_grounded'] = True
            from .state_evidence import check_termination
            if check_termination(relation,quotes,max(source[n].at.date() for n in fact.rounds if n in source)) is False:
                fact.confidence=min(fact.confidence,.5)
                qualifiers=fact.semantics.setdefault('qualifiers',[])
                note='relationship termination is unverified; do not infer current absence from this summary'
                if note not in qualifiers:qualifiers.append(note)
                if report is not None:
                    report.relation_warnings.append('Unverified relationship termination retained as unknown; provenance is not state entailment.')
            relation['grounded'] = True
            accepted.append(relation)
        fact.relations = accepted


def query_aliases(query):
    """Literal bounded name lookup; query wording never becomes a stored fact."""
    body = name_key(query)[:1024]
    matches = list(re.finditer(r"[a-z0-9]+(?:['’][a-z]+)?", body))
    words = [match.group() for match in matches]
    values = {body}
    for width in range(1, 7):
        values.update(' '.join(words[i:i + width]) for i in range(len(words) - width + 1))
        values.update(body[matches[i].start():matches[i + width - 1].end()] for i in range(len(words) - width + 1))
    for run in re.findall(r'[㐀-鿿a-z0-9]+', body):
        if re.search(r'[㐀-鿿]', run):
            for width in range(2, min(16, len(run)) + 1):
                values.update(run[i:i + width] for i in range(len(run) - width + 1))
    return sorted(values - HUBS - PRONOUNS, key=lambda v: (-len(v), v))[:1024]


class RelationIndex:
    def __init__(self, store):
        self.store = store

    def _resolve(self, spec, fact_id):
        key = name_key(spec['name'])
        # Repeated event mentions are not proof of one occurrence.
        occurrence = spec['identity'] or ('fact:' + str(fact_id) if spec['type'] == 'event' else '')
        options, _ = self.store.matching_records('entity', 'alias_keys', [key] + [name_key(a) for a in spec['aliases']], 32)
        compatible = [e for e in options if e['identity'] == occurrence and
                      (e['type'] == spec['type'] or 'other' in (e['type'], spec['type']))]
        # Exact spelling is a scoped key, not fuzzy name matching. Distinct explicit
        # identities and incompatible types stay separate; ambiguous aliases do too.
        exact = [e for e in compatible if e['name_key'] == key]
        aliases = [e for e in compatible if spec['aliases'] or self.store.facts(
            e.get('witnesses', {}).get(key, []), statuses=(FACT_CURRENT, FACT_SUPERSEDED, ARCHIVED))]
        selected = exact if exact else aliases
        if len(selected) == 1:
            entity = selected[0]
        else:
            identifier = 'n-' + hashlib.sha256(json.dumps([spec['type'], key, occurrence]).encode()).hexdigest()[:24]
            entity = self.store.record('entity', identifier) or dict(id=identifier, name=spec['name'], name_key=key,
                type=spec['type'], identity=occurrence, aliases=[], alias_keys=[key], witnesses={})
        if entity['type'] == 'other' and spec['type'] != 'other':
            entity['type'] = spec['type']
        aliases = [spec['name']] + spec['aliases']
        for alias in aliases:
            normalized = name_key(alias)
            if normalized not in entity['alias_keys']:
                entity['alias_keys'].append(normalized)
            if normalized != entity['name_key'] and alias not in entity['aliases']:
                entity['aliases'].append(alias)
            ids = entity['witnesses'].setdefault(normalized, [])
            if fact_id not in ids:
                ids.append(fact_id)
        self.store.put_record('entity', entity['id'], entity)
        return entity['id']

    def publish(self, fact_id, relations=None):
        """Called inside the same transaction as the fact and source-span writes."""
        row = self.store.record('fact', fact_id)
        if not row:
            return 0
        accepted = merge_relations(row.get('relations') or [], relations or [])
        edges = 0
        for relation in accepted:
            if not relation.get('grounded') or not row.get('spans'):
                continue
            subject = self._resolve(relation['subject'], fact_id)
            target = self._resolve(relation['object'], fact_id)
            edge_id = 'r-' + hashlib.sha256(json.dumps([fact_id, subject, relation['predicate'], target,
                relation['valid_from'], relation['valid_to'], relation['qualifiers']], sort_keys=True).encode()).hexdigest()[:24]
            self.store.put_record('relation', edge_id, dict(id=edge_id, fact_id=fact_id, entity_ids=list(dict.fromkeys([subject, target])),
                subject_id=subject, object_id=target, predicate=relation['predicate'],
                state=relation['state'], valid_from=relation['valid_from'], valid_to=relation['valid_to'],
                qualifiers=relation['qualifiers'], recorded_at=row['recorded_at'],
                source_ids=row.get('source_ids', []), evidence_roles=relation['evidence_roles'], origin='extracted'))
            edges += 1
        # For old schemas, add only explicit subject/object keys as association
        # indexes. These are NOT inferred ownership/employment/participation edges.
        refs = []
        if name_key(row['subject']) not in HUBS | PRONOUNS and row['subject'].strip():
            refs.append(dict(name=row['subject'], type='other', identity='', aliases=[]))
        object_key = (row.get('semantics') or {}).get('object_key')
        if object_key and contains_name(row['text'] + '\n' + str(row['value']), object_key):
            refs.append(dict(name=object_key, type='other', identity='', aliases=[]))
        for spec in refs:
            node = self._resolve(spec, fact_id)
            edge_id = 'a-' + hashlib.sha256((str(fact_id) + ':' + node).encode()).hexdigest()[:24]
            self.store.put_record('relation', edge_id, dict(id=edge_id, fact_id=fact_id, entity_ids=[node], subject_id=node,
                object_id=None, predicate='about', state='unknown', valid_from=None, valid_to=None, qualifiers=[],
                recorded_at=row['recorded_at'], source_ids=row.get('source_ids', []), evidence_roles=[], origin='legacy_key'))
            edges += 1
        if accepted != (row.get('relations') or []):
            self.store._patch('fact', fact_id, relations=accepted)
        return edges

    @staticmethod
    def _allowed(edge, plan, today):
        # Unknown time remains unknown. Do not equate a fact's recorded_at or
        # lifecycle status with business validity or completion.
        start, end = edge.get('valid_from'), edge.get('valid_to')
        if plan.time_range:
            if start and start > plan.time_range.end.isoformat() or end and end < plan.time_range.start.isoformat():
                return False
        elif plan.focus == 'current':
            if edge.get('state') in ('ended', 'cancelled'):
                return False
            if start and start > today.isoformat() or end and end < today.isoformat():
                return False
        return True

    def expand(self, query, seed_facts, statuses, plan, today, settings):
        trace = dict(enabled=True, seed_entities=[], visited_nodes=0, visited_edges=0,
                     candidate_fact_ids=[], paths=[], limited=False, limits=dict(hops=settings.relation_hops,
                     nodes=settings.relation_max_nodes, edges=settings.relation_max_edges, candidates=settings.relation_max_candidates))
        eligible = {}
        def facts(ids):
            missing = set(ids) - set(eligible)
            for fact in self.store.facts(missing, statuses):
                eligible[fact.id] = fact
            return {fid: eligible[fid] for fid in ids if fid in eligible}
        names, limited = self.store.matching_records('entity', 'alias_keys', query_aliases(query), settings.relation_max_nodes)
        trace['limited'] |= limited
        named = []
        alias_entities = {}
        for entity in names:
            if entity['type'] in HUBS:
                continue
            witnesses = set()
            for alias, ids in entity.get('witnesses', {}).items():
                if alias not in HUBS and contains_name(query, alias):
                    witnesses.update(ids)
                    if facts(ids):
                        alias_entities.setdefault(alias, []).append(entity['id'])
            if facts(witnesses):
                named.append(entity['id'])
        trace['ambiguous_aliases'] = sorted(alias for alias, ids in alias_entities.items() if len(set(ids)) > 1)
        seeds = list(named)
        direct, capped = self.store.matching_records('relation', 'fact_id', [f.id for f in seed_facts], settings.relation_max_edges)
        trace['limited'] |= capped
        for edge in direct if not named else []:
            if not self._allowed(edge, plan, today):
                continue
            for node in edge['entity_ids']:
                entity = self.store.record('entity', node)
                if entity and entity['type'] not in HUBS and node not in seeds:
                    seeds.append(node)
        frontier = [(node, []) for node in seeds[:settings.relation_max_nodes]]
        trace['seed_entities'] = [node for node, _ in frontier]
        trace['limited'] |= len(seeds) > settings.relation_max_nodes
        seen_nodes, seen_edges, result = set(), set(), {}
        for hop in range(settings.relation_hops):
            if not frontier:
                break
            nodes = [node for node, _ in frontier]
            paths = dict(frontier)
            seen_nodes.update(nodes)
            remaining = settings.relation_max_edges - len(seen_edges)
            edges, capped = self.store.matching_records('relation', 'entity_ids', nodes, remaining + 1)
            trace['limited'] |= capped or len(edges) > remaining
            def assertion_key(edge):
                return (edge.get('subject_id'), edge.get('predicate'), edge.get('object_id'),
                        tuple(sorted(edge.get('qualifiers') or [])), tuple(edge.get('evidence_roles') or []))
            closures = {}
            if plan.focus == 'current' and plan.time_range is None:
                for edge in edges:
                    if (edge.get('state') in ('ended', 'cancelled') and edge.get('origin') == 'extracted'
                            and str(edge['recorded_at'])[:10] <= today.isoformat() and facts([edge['fact_id']])):
                        key = assertion_key(edge)
                        closures[key] = max(closures.get(key, ''), edge['recorded_at'])
            next_frontier = []
            for edge in edges:
                if edge['id'] in seen_edges:
                    continue
                if len(seen_edges) >= settings.relation_max_edges:
                    trace['limited'] = True
                    break
                seen_edges.add(edge['id'])
                if not facts([edge['fact_id']]) or str(edge['recorded_at'])[:10] > today.isoformat():
                    continue
                traversable = self._allowed(edge, plan, today)
                closure = plan.focus == 'current' and plan.time_range is None and edge.get('state') in ('ended', 'cancelled')
                if not traversable and not closure:
                    continue
                if edge['recorded_at'] <= closures.get(assertion_key(edge), ''):
                    traversable = False
                reached = next((node for node in edge['entity_ids'] if node in paths), None)
                if reached is None:
                    continue
                path = paths[reached] + [edge['id']]
                if edge['fact_id'] not in result:
                    if len(result) >= settings.relation_max_candidates:
                        trace['limited'] = True
                        continue
                    result[edge['fact_id']] = dict(fact=eligible[edge['fact_id']], hop=hop + 1, path=path)
                # Negative/ended evidence is useful to answer, but cannot bridge
                # into an entity's current neighborhood. An explicit later closure
                # also blocks older assertions of the same scoped relation.
                if not traversable:
                    continue
                for node in edge['entity_ids']:
                    if node in seen_nodes or any(n == node for n, _ in next_frontier):
                        continue
                    entity = self.store.record('entity', node)
                    if not entity or entity['type'] in HUBS:
                        continue
                    if len(seen_nodes) + len(next_frontier) >= settings.relation_max_nodes:
                        trace['limited'] = True
                        continue
                    next_frontier.append((node, path))
            frontier = next_frontier
        trace.update(visited_nodes=len(seen_nodes), visited_edges=len(seen_edges), candidate_fact_ids=list(result),
                     paths=[dict(fact_id=fid, hops=row['hop'], edges=row['path']) for fid, row in result.items()])
        return result, trace

    def rebuild(self):
        """Reindex already extracted facts only; no original-text read or LLM call."""
        with self.store.transaction():
            for kind in ('entity', 'relation'):
                for row in self.store.records(kind):
                    self.store.delete_record(kind, row['id'])
            count = 0
            facts = self.store.facts(statuses=(FACT_CURRENT, FACT_SUPERSEDED, ARCHIVED))
            for fact in facts:
                count += self.publish(fact.id)
            self.store.set_meta('relation_index_format', '1')
        return dict(facts=len(facts), relations=count)
