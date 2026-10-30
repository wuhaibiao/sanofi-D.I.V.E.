"""Budget-bounded evidence pages for a host Agent; no generation or answer validation."""
from .budget import budget_counter
from .schema import Fact
from .fact_content import fact_text


def evidence_pages(result, settings, origins):
    counter = budget_counter(settings)
    budget = settings.context_budget_tokens
    pages, entries, blocks = [], [], []

    def flush():
        nonlocal entries, blocks
        if entries:
            pages.append(dict(index=len(pages), items=entries, context="\n\n".join(blocks)))
            entries, blocks = [], []

    def append(entry, block):
        if entries and (len(entries) >= settings.aggregate_page_items or counter.count("\n\n".join(blocks + [block])) > budget):
            flush()
        entries.append(entry)
        blocks.append(block)

    for candidate in result.ranked:
        item = result.items[candidate.key]
        if isinstance(item, Fact):
            parts = [("derived", fact_text(item))]
            day = item.when().label()
        else:
            parts = [("user", item.user_text), ("assistant", item.assistant_text)]
            day = item.at.date().isoformat()
        for speaker, text in parts:
            start = 0
            while start < len(text):
                header = "[%s] date=%s status=%s speaker=%s range=%d:" % (item.key, day, item.status, speaker, start)
                if isinstance(item, Fact):
                    header = ("[%s] noted=%s event=%s memory_kind=%s status=%s speaker=%s range=%d:" %
                        (item.key, item.recorded_at.date().isoformat(), item.event.label() if item.event else 'unknown',
                         item.kind, item.status, speaker, start))
                available = budget-counter.count(header)-80
                if available < 1:
                    raise ValueError("context budget cannot fit an evidence page header")
                low, high = 1, len(text)-start
                while low < high:
                    mid = (low+high+1)//2
                    if counter.count(text[start:start+mid]) <= available:
                        low = mid
                    else:
                        high = mid-1
                end = start+low
                block = header+str(end)+"\n"+text[start:end]
                if counter.count(block) > budget:
                    raise ValueError("one source character exceeds page budget")
                entry = dict(key=item.key, kind=candidate.kind, date=day, status=item.status, speaker=speaker,
                             source_start=start, source_end=end, text=text[start:end],
                             sources=["E%d" % i for i in origins.get(item.key, [])])
                if isinstance(item, Fact):
                    entry.update(confidence=item.confidence, uncertain=item.uncertain,
                                 replaced_by="F%d" % item.superseded_by if item.superseded_by else None,
                                 value=item.value, semantics=item.semantics, subject=item.subject,
                                 attribute=item.attribute, memory_kind=item.kind)
                    entry.update(recorded_at=item.recorded_at.isoformat(), event_date=item.event.label() if item.event else None,
                                 event_status=item.semantics.get('event_status', 'unknown'))
                append(entry, block)
                start = end
    flush()
    return pages
