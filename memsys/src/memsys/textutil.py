"""文本小工具：token 估算、关键词、全文检索用的文本归一化。"""

from __future__ import annotations

import re
from typing import Iterable, List, Optional

_CJK_CHAR = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
_CJK_RUN = re.compile(r"[㐀-䶿一-鿿豈-﫿]+")
_WORD = re.compile(r"[A-Za-z0-9]+(?:['’][A-Za-z]+)?")

STOPWORDS = frozenset("""
a about above after again against all am an and any are as at be because been before being below between
both but by can could did do does doing down during each few for from further had has have having he her here
hers herself him himself his how i if in into is it its itself just me more most my myself no nor not now of off
on once only or other our ours ourselves out over own same she should so some such than that the their theirs them
themselves then there these they this those through to too under until up very was we were what when where which
while who whom why will with would you your yours yourself yourselves also ago did does many much what's i'm i've
i'd you're it's let me tell remind know think recall remember previous previously last time times day days week
weeks month months year years currently current now
""".split())


def estimate_tokens(text: str) -> int:
    """粗估 token 数：中日韩字符每字约 1 个 token，其余字符约 4 个 1 个 token。

    只用来控制上下文预算和出报告，不追求和具体分词器一致。
    """
    cjk = len(_CJK_CHAR.findall(text))
    return cjk + (len(text) - cjk + 3) // 4


def _cjk_bigrams(run: str) -> List[str]:
    if len(run) == 1:
        return [run]
    return [run[i:i + 2] for i in range(len(run) - 1)]


def fts_document(text: str) -> str:
    """写进 FTS5 索引前的文本：把连续的中文切成相邻双字，英文原样交给 porter 分词。

    FTS5 自带的 unicode61 分词器会把一整串中文当成一个词，中文关键词就搜不到了。
    """
    return _CJK_RUN.sub(lambda m: " " + " ".join(_cjk_bigrams(m.group(0))) + " ", text)


def terms(text: str) -> List[str]:
    """切出检索词：英文按单词（去停用词、去太短的），中文按双字。保持出现顺序、去重。"""
    out: List[str] = []
    seen = set()
    for word in _WORD.findall(text):
        word = word.lower().replace("’", "'")
        if len(word) < 3 and not word.isdigit():
            continue
        if word in STOPWORDS:
            continue
        if word not in seen:
            seen.add(word)
            out.append(word)
    for run in _CJK_RUN.findall(text):
        for gram in _cjk_bigrams(run):
            if gram not in seen:
                seen.add(gram)
                out.append(gram)
    return out


def fts_query(phrases: Iterable[str], limit: int = 24) -> Optional[str]:
    """把若干关键词或短语拼成 FTS5 的 OR 查询；每个词加引号，避免特殊字符被当成语法。"""
    words: List[str] = []
    for phrase in phrases:
        for term in terms(phrase):
            if term not in words:
                words.append(term)
    words = [w.replace('"', "") for w in words if w.replace('"', "")][:limit]
    if not words:
        return None
    return " OR ".join('"%s"' % w for w in words)


def truncate_tokens(text: str, max_tokens: int, count=estimate_tokens) -> str:
    """按估算的 token 数截断，截断处加省略标记。"""
    if count(text) <= max_tokens:
        return text
    suffix = " …[truncated]"
    if max_tokens < count(suffix):
        return ""
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if count(text[:mid].rstrip() + suffix) <= max_tokens:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo].rstrip() + suffix


def split_tokens(text: str, max_tokens: int, count=estimate_tokens) -> List[str]:
    """无损分段，优先在段落或词边界切开；每段按所选计数器核验。"""
    if max_tokens <= 0:
        raise ValueError("split token budget must be positive")
    chunks: List[str] = []
    while text:
        if count(text) <= max_tokens:
            chunks.append(text)
            break
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if count(text[:mid]) <= max_tokens:
                lo = mid
            else:
                hi = mid - 1
        # 不 strip：空白、列表编号和段落分隔符也是原文的一部分。
        if lo == 0:
            raise ValueError("split budget cannot fit one character under the selected counter")
        end = lo
        for separator in ("\n\n", "\n", " "):
            boundary = text.rfind(separator, lo // 2, lo)
            if boundary >= 0:
                end = boundary + len(separator)
                break
        while end and count(text[:end]) > max_tokens:
            end -= 1
        if not end:
            raise ValueError("split budget cannot fit one character under the selected counter")
        chunks.append(text[:end])
        text = text[end:]
    return chunks


def slug(text: str) -> str:
    """属性名归一化成 snake_case，方便同一属性的新旧事实对上号。"""
    text = re.sub(r"[^0-9a-zA-Z㐀-鿿]+", "_", text.strip().lower())
    return text.strip("_")[:60]
