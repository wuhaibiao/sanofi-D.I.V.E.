"""Literal structured assistant facts, independent of questions and scoring labels."""
from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import List


@dataclass
class LiteralStructure:
    kind: str
    value: dict
    quote: str
    context_quotes: List[str] = field(default_factory=list)


def assistant_fields(text: str) -> List[LiteralStructure]:
    """Retain short explicit numeric fields with their literal heading context.

    This parses labelled data, not propositions from prose. It neither infers a
    user state nor converts units. Unsupported/long values are left to extraction.
    """
    result = []
    headings = {}
    section = None
    fence = None
    for raw in text.splitlines():
        stripped = raw.strip()
        marker = re.match(r'^(`{3,}|~{3,})', stripped)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            continue
        if fence or '|' in stripped:
            continue
        heading = re.fullmatch(r'(#{1,6})\s+(.+?)\s*#*', stripped)
        if heading:
            level = len(heading.group(1))
            headings = {k: v for k, v in headings.items() if k < level}
            headings[level] = (heading.group(2), raw)
            section = None
            continue
        title = re.fullmatch(r'([^:：]{1,80})[:：]\s*', stripped)
        if title and not re.match(r'^(?:[-*+]\s|\d+[.)]\s)', stripped):
            section = (title.group(1).strip(), raw)
            continue
        # A non-field paragraph ends a colon-heading scope. Markdown headings
        # remain in force until another heading at that level replaces them.
        line = re.sub(r'^(?:[-*+]\s+|\d+[.)]\s+)', '', stripped)
        item = re.fullmatch(r'([^:：]{1,80})[:：]\s*(\S.*?)\s*', line)
        if not item:
            if stripped:
                section = None
            continue
        label, value = item.group(1).strip(), item.group(2)
        sep = item.end(1)
        if (not any(c.isalpha() for c in label) or not any(c.isdigit() for c in value)
                or len(value.encode('utf-8')) > 240 or re.search(r'\w+://', line)
                or line[sep - 1].isdigit() and line[sep + 1].isdigit()):
            continue
        context = [headings[k] for k in sorted(headings)] + ([section] if section else [])
        result.append(LiteralStructure('fields', dict(type='fields',
            headers=[title for title, _ in context], items=[dict(label=label, value=value)]),
            raw, [quote for _, quote in context]))
    return result


def _cells(line):
    line=line.strip()
    if line.startswith('|'):line=line[1:]
    if line.endswith('|') and not line.endswith('\\|'):line=line[:-1]
    return [cell.strip().replace('\\|','|') for cell in re.split(r'(?<!\\)\|',line)]


def _list_item(body, position=None):
    """Keep a short literal item whole; never shorten a long detail to a prefix."""
    item = dict(position=position) if position is not None else {}
    bold=re.fullmatch(r'(\*\*|__)(.+?)[:：]\1\s*(.*)',body) or re.fullmatch(r'(\*\*|__)(.+?)\1[:：]\s*(.*)',body)
    pair = [bold[2],bold[3]] if bold else re.split(r'\s[-–—]\s|[:：]\s*(?=\D)', body, maxsplit=1)
    if len(pair) == 2 and not re.search(r'\w+://', body):
        if len(pair[0].encode('utf-8'))>240:return item,False
        item['name'] = pair[0].strip('*_ ')
        if pair[1]:
            if len(pair[1].encode('utf-8')) > 240:
                return item, False
            item['details'] = pair[1]
    elif body.endswith((':', '：')):
        item['name'] = body[:-1].strip('*_ ')
    elif len(body.encode('utf-8')) <= 240:
        item['statement'] = body
    else:
        return item, False
    return item, True


def assistant_structures(text: str) -> List[LiteralStructure]:
    """Read explicit Markdown tables and literal numbered entries, preserving order.

    Only structured cells/items and short labelled numeric fields are retained,
    never complete conversation turns. Code and ambiguous tables are excluded.
    """
    lines=text.splitlines(keepends=True)
    offsets=[];offset=0
    for line in lines:offsets.append(offset);offset+=len(line)
    result=[];i=0;fenced=False
    headings={};section=None
    numbered=re.compile(r'^\s*(\d{1,6})[.)]\s+(.+?)\s*$')
    separator=re.compile(r'^:?-{3,}:?$')
    while i<len(lines):
        stripped=lines[i].strip()
        if stripped.startswith(('```','~~~')):
            fenced=not fenced;i+=1;continue
        if fenced:i+=1;continue
        heading=re.fullmatch(r'(#{1,6})\s+(.+?)\s*#*',stripped)
        if heading:
            level=len(heading[1]);headings={k:v for k,v in headings.items() if k<level}
            headings[level]=(heading[2],lines[i].rstrip('\r\n'));section=None;i+=1;continue
        title=re.fullmatch(r'([^:：]{1,80})[:：]\s*',stripped)
        if title and not re.match(r'^(?:[-*+]\s|\d+[.)]\s)',stripped):
            section=(title[1],lines[i].rstrip('\r\n'));i+=1;continue
        context=[headings[k] for k in sorted(headings)]+([section] if section else [])
        if ('|' in stripped and i+1<len(lines) and
                all(separator.fullmatch(c) for c in _cells(lines[i+1]))):
            columns=_cells(lines[i]);j=i+2;data=[];valid=len(set(columns))==len(columns)
            while j<len(lines) and lines[j].strip() and '|' in lines[j]:
                row=_cells(lines[j])
                if len(row)!=len(columns):valid=False
                data.append(row);j+=1
            if valid and data:
                columns=[c or 'row_label' for c in columns]
                if len(set(columns))==len(columns):
                    quote=text[offsets[i]:offsets[j] if j<len(lines) else len(text)].rstrip('\r\n')
                    value=dict(type='table',columns=columns,rows=data)
                    if context:value['headers']=[title for title,_ in context]
                    result.append(LiteralStructure('table',value,quote,[q for _,q in context]))
            i=j;continue
        match=numbered.match(lines[i])
        if match:
            begin=i;items=[];omitted=[];last=None
            base_indent=len(lines[i])-len(lines[i].lstrip())
            while i<len(lines):
                match=numbered.match(lines[i])
                if not match:
                    if not lines[i].strip() and i+1<len(lines) and numbered.match(lines[i+1]):i+=1;continue
                    break
                position=int(match.group(1))
                if last is not None and position<=last:break
                body=match.group(2).strip()
                item,complete=_list_item(body,position)
                last=position;i+=1
                # One explicit child level only. Do not attach prose paragraphs,
                # code, or a new numbered section to the preceding parent.
                child_indent=None
                while i<len(lines):
                    if not lines[i].strip() and i+1<len(lines) and re.match(r'^\s*[-*+]\s+',lines[i+1]):
                        i+=1
                    child=re.match(r'^\s*[-*+]\s+(.+?)\s*$',lines[i])
                    if not child:break
                    indent=len(lines[i])-len(lines[i].lstrip())
                    if indent<base_indent:break
                    if child_indent is not None and indent!=child_indent:
                        complete=False
                        break
                    child_indent=indent
                    value,kept=_list_item(child.group(1).strip())
                    if value:item.setdefault('children',[]).append(value)
                    complete=complete and kept
                    i+=1
                if not complete:omitted.append(position)
                if len(item)>1:items.append(item)
            quote=text[offsets[begin]:offsets[i] if i<len(lines) else len(text)].rstrip('\r\n')
            if items:
                value=dict(type='list',items=items,coverage='literal positions, short values and one explicit child level; long prose is left to model extraction')
                if context:value['headers']=[title for title,_ in context]
                if omitted:value['omitted_positions']=omitted
                result.append(LiteralStructure('list',value,quote,[q for _,q in context]))
            continue
        if stripped:section=None
        i+=1
    return result + assistant_fields(text)
