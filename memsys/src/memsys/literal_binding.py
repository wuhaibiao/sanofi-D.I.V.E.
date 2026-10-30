"""Prove a literal label/value association inside one structured fact value."""
import json
import re
import unicodedata


def _label(value):
    return re.sub(r'[\s_*]+','',unicodedata.normalize('NFKC',str(value))).casefold()


def field_association(value, quote):
    parts=re.split(r'\s*[:：]\s*|\s+[—–-]\s+',quote,maxsplit=1)
    if len(parts)!=2 or not all(p.strip() for p in parts):return False
    label,expected=_label(parts[0]),parts[1].strip()
    if isinstance(value,str):
        try:value=json.loads(value)
        except (ValueError,TypeError,RecursionError):return False
    if not isinstance(value,(dict,list)):return False
    matches=set();visited=0;complete=True
    def scalar(v):return v if isinstance(v,str) else json.dumps(v,ensure_ascii=False,separators=(',',':'))
    def emit(labels,v,path):
        nonlocal visited,complete
        visited+=1
        if visited>2048:complete=False;return
        if scalar(v)==expected and label in {_label(l) for l in labels if l}:matches.add(path)
    def walk(node,path=(),context=(),depth=0):
        nonlocal visited,complete
        visited+=1
        if visited>2048 or depth>8:complete=False;return
        if isinstance(node,list):
            for i,child in enumerate(node):
                if visited>=2048:complete=False;break
                walk(child,path+(i,),context,depth+1)
        elif isinstance(node,dict):
            name=next((node[k] for k in ('name','label','title') if isinstance(node.get(k),str)),None)
            if node.get('type') in ('table','sequence'):
                columns,rows=node.get('columns'),node.get('rows')
                if isinstance(columns,list) and all(isinstance(c,str) for c in columns) and isinstance(rows,list):
                    for ri,row in enumerate(rows):
                        if not isinstance(row,list) or len(row)!=len(columns):complete=False;continue
                        for ci,v in enumerate(row):
                            if isinstance(v,(dict,list)):continue
                            row_label=scalar(row[0]) if row and not isinstance(row[0],(dict,list)) else ''
                            emit([columns[ci],row_label+columns[ci]],v,path+('rows',ri,ci))
                return
            for key,v in node.items():
                if visited>=2048:complete=False;break
                if key in ('name','label','title','type','position','coverage','headers','omitted_positions'):continue
                if isinstance(v,(dict,list)):
                    label_name=name or (key if key not in ('items','children') else None)
                    walk(v,path+(key,),context+((label_name,) if label_name else ()),depth+1)
                else:
                    labels=[key]
                    if name:
                        labels.append(name+key)
                        if key in ('details','value','statement'):labels.append(name)
                    if context:labels.append(''.join(context)+(name or '')+key)
                    if context and name and key in ('details','value','statement'):labels.append(''.join(context)+name)
                    emit(labels,v,path+(key,))
    walk(value)
    # Ambiguous rows/scopes and unsupported depth/size are not proof. Never join
    # a label from one item to a value from a different item.
    return complete and len(matches)==1
