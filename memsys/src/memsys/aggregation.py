"""核验回答模型提交的计算凭据，不从任意原文猜测事件身份或枚举完整性。"""

from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, Dict, Mapping, Optional, Sequence, Set, Tuple

from .llm import ChatModel, ModelError
from .read_budget import is_read_service_error
from . import prompts
from .dateproof import inspect_dates, quoted_duration_days, source_quote
from .daycount import attendance_dates
from .queryops import INTERVAL, is_aggregate, is_attendance_day_count, is_calendar_span, is_duration_total, operation_for, operation_rules
from .semantic_review import retry_completion_tokens, review_response
from .read import parse_answer
from .textutil import estimate_tokens

_CALCULATION = re.compile(r"(?mi)^\s*CALCULATION\s*[:：]\s*")
_ANSWER = re.compile(r"(?mi)^\s*ANSWER\s*[:：]")
_LITERAL = r"[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
_NUMBER = re.compile(r"(?<![A-Za-z0-9_.])" + _LITERAL + r"(?![A-Za-z0-9_]|[.,]\d)")
_AGGREGATE = re.compile(r"\b(how many|how much|total|sum|count)\b|多少|几个|几次|一共|总共|合计|求和", re.I)
_INTERVAL = re.compile(r"how many (?:calendar )?(?:days|weeks|months|years)\b[^?\n]*"
                       r"\b(?:ago|before|after|since|until|between|passed|elapsed|take|taken|been)\b|"
                       r"how long\b[^?\n]*\b(?:ago|before|after|since|until|between)\b|"
                       r"多久以前|多少(?:天|周|个月|年)(?:以前|之前|之后)|"
                       r"(?:相隔|间隔|经过|过去了|用了)(?:多少|几)(?:天|周|个月|年)", re.I)
_SCOPE = "Checks cover quoted evidence, explicit duplicates, units and arithmetic; event qualification and completeness remain model judgments."

_SMALL_WORDS = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen").split()
_TENS = {20: "twenty", 30: "thirty", 40: "forty", 50: "fifty", 60: "sixty", 70: "seventy", 80: "eighty", 90: "ninety"}
_QUANTITY_WORDS = {word: Decimal(number) for number, word in enumerate(_SMALL_WORDS)}
for _number in range(20, 100):
    _word = _TENS[_number // 10 * 10] + (" " + _SMALL_WORDS[_number % 10] if _number % 10 else "")
    _QUANTITY_WORDS[_word] = Decimal(_number)
    _QUANTITY_WORDS[_word.replace(" ", "-")] = Decimal(_number)

_ORDINAL_WORDS = dict(zip(
    "first second third fourth fifth sixth seventh eighth ninth tenth eleventh twelfth thirteenth fourteenth fifteenth sixteenth seventeenth eighteenth nineteenth".split(),
    map(Decimal, range(1, 20))))
_ORDINAL_TENS = {20: "twentieth", 30: "thirtieth", 40: "fortieth", 50: "fiftieth",
                 60: "sixtieth", 70: "seventieth", 80: "eightieth", 90: "ninetieth"}
for _number in range(20, 100):
    _word = (_TENS[_number // 10 * 10] + " " + list(_ORDINAL_WORDS)[_number % 10 - 1]
             if _number % 10 else _ORDINAL_TENS[_number])
    _ORDINAL_WORDS[_word] = Decimal(_number)
    _ORDINAL_WORDS[_word.replace(" ", "-")] = Decimal(_number)


def _group_quantities(quote, label, ordinal=False):
    quantities = _ORDINAL_WORDS if ordinal else _QUANTITY_WORDS
    words = "|".join(re.escape(word) for word in sorted(quantities, key=len, reverse=True))
    literals = r"\d{1,2}(?:st|nd|rd|th)" if ordinal else "a|an|" + _LITERAL
    pattern = re.compile(r"(?<![\w.-])(" + words + "|" + literals + r")\s+" + re.escape(label) + r"(?!\w)", re.I)
    values = []
    for match in pattern.finditer(quote):
        token = match.group(1).casefold()
        number = (Decimal(1) if token in ("a", "an") else quantities[token] if token in quantities else
                  _decimal(re.sub(r"(st|nd|rd|th)$", "", token) if ordinal else token))
        if number >= 0 and number == number.to_integral_value():
            values.append(number)
    if ordinal:
        # A completed sequence can use a noun followed by its cardinal index.
        # The caller still requires ordinal_progress=true/value_type=cumulative;
        # event identity and completed cumulative progress need semantic review.
        cardinals = "|".join(re.escape(word) for word in sorted(_QUANTITY_WORDS, key=len, reverse=True))
        indexed = re.compile(r"(?<!\w)" + re.escape(label) + r"\s+(" + cardinals + "|" + _LITERAL +
                             r")(?![A-Za-z0-9_]|[.,]\d)", re.I)
        for match in indexed.finditer(quote):
            token = match.group(1).casefold()
            number = _QUANTITY_WORDS[token] if token in _QUANTITY_WORDS else _decimal(token)
            if number >= 0 and number == number.to_integral_value():
                values.append(number)
    return values




def wants_calculation(question: str, operation=None) -> bool:
    return is_aggregate(question, operation)


def is_temporal_interval(question: str) -> bool:
    return is_calendar_span(question) or (bool(_INTERVAL.search(question)) and not is_duration_total(question)
            and not re.search(r"\b(total|sum|across)\b|总共|合计", question, re.I))


def _clean(value: str) -> str:
    return " ".join(value.casefold().split())


def _decimal(value: Any) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError("value must be a numeric literal")
    text = str(value).strip()
    if len(text) > 80 or not _NUMBER.fullmatch(text):
        raise ValueError("value must be a finite decimal literal")
    try:
        number = Decimal(text.replace(",", ""))
    except InvalidOperation:
        raise ValueError("invalid number") from None
    if not number.is_finite():
        raise ValueError("non-finite number")
    return number


def _quantity_decimal(value):
    """Normalize bounded English cardinals without accepting arbitrary prose."""
    if isinstance(value, str) and _clean(value) in _QUANTITY_WORDS:
        return _QUANTITY_WORDS[_clean(value)]
    return _decimal(value)


def _week_answer_matches(answer,audit):
    """Check a verified week presentation, including any literal day remainder."""
    quantities='|'.join(re.escape(word) for word in sorted(_QUANTITY_WORDS,key=len,reverse=True))
    value='(?:'+quantities+'|a|an|'+_LITERAL+')'
    weeks=re.compile(r'(?<![\w.])('+value+r')\s*(?:(?:complete|whole|full|calendar)\s+|(?:个)?完整\s*)?'
                     r'(?:weeks?\b|周|星期)',re.I)
    days=re.compile(r'(?<![\w.])('+value+r')\s*(?:days?\b|天)',re.I)
    number=lambda match: Decimal(1) if match.group(1).casefold() in ('a','an') else _quantity_decimal(match.group(1))
    week_matches=list(weeks.finditer(answer))
    day_matches=list(days.finditer(answer))
    week_values=[number(m) for m in week_matches]
    complete=Decimal(audit['complete_units'])
    remainder=Decimal(audit['remainder_days'])
    if day_matches:
        if not week_values or any(n!=complete for n in week_values):
            return False
        measurements=sorted([(m.start(),m.end(),'weeks',m) for m in week_matches]+
                            [(m.start(),m.end(),'days',m) for m in day_matches])
        for index,(start,end,kind,match) in enumerate(measurements):
            if kind!='days':
                continue
            previous_end=measurements[index-1][1] if index else 0
            prefix=answer[previous_end:start]
            suffix=answer[end:measurements[index+1][0] if index+1<len(measurements) else len(answer)]
            # Adjacent addition denotes the remainder; an explicitly labelled
            # equivalence or parenthesis denotes the whole elapsed-day span.
            component=bool(index and measurements[index-1][2]=='weeks' and
                re.fullmatch(r'\s*(?:(?:and|plus|&|又|加)\s*)?',prefix,re.I))
            equivalent=bool(re.search(r'[（(]|\b(?:total|overall|elapsed|equivalent|equals|equal to|'
                r'i\.e\.|that is|or)\b|共|合计|总共',prefix+' '+suffix,re.I))
            if component:
                if number(match)!=remainder:
                    return False
            elif not equivalent or number(match)!=Decimal(audit['elapsed_days']):
                return False
        return True
    if week_values:
        return all(n==complete for n in week_values)
    if audit.get('unit_conversion'):
        return bool(re.fullmatch(r'\s*'+_LITERAL+r'\s*[.。]?\s*',answer)) and _answer_numbers(answer)==[complete]
    return bool(_answer_numbers(answer)) and _answer_numbers(answer)[0]==_decimal(audit['computed'])


def _answer_numbers(text):
    words = "|".join(re.escape(word) for word in sorted(_QUANTITY_WORDS, key=len, reverse=True))
    pattern = re.compile(r"(?<![A-Za-z0-9_.])(?:" + _LITERAL + "|" + words + r")(?![A-Za-z0-9_]|[.,]\d)", re.I)
    return [_quantity_decimal(match.group()) for match in pattern.finditer(text)]


def _numbers(text: str) -> Sequence[Decimal]:
    out = []
    for match in _NUMBER.finditer(text):
        try:
            out.append(_decimal(match.group()))
        except ValueError:
            continue
    return out


def _unit(value: str) -> str:
    text = _clean(value)
    return {"$": "usd", "dollar": "usd", "dollars": "usd", "us dollars": "usd",
            "€": "eur", "euro": "eur", "euros": "eur", "£": "gbp", "pounds": "gbp",
            "day": "days", "week": "weeks", "hour": "hours", "minute": "minutes", "second": "seconds"}.get(text, text)


_TIME_SECONDS = {"seconds": Decimal(1), "minutes": Decimal(60), "hours": Decimal(3600),
                 "days": Decimal(86400), "weeks": Decimal(604800)}
_MEASUREMENT_UNITS = set(_TIME_SECONDS) | {"month", "months", "year", "years", "usd", "eur", "gbp"}


def _count_unit_variants(unit):
    """Regular English noun forms; no category or measurement aliases."""
    if re.search(r"(?:ches|shes|xes|zes|sses)$", unit):
        return {unit, unit[:-2]}
    if unit.endswith("s") and len(unit) > 1:
        return {unit, unit[:-1]}
    return {unit, unit + ("es" if re.search(r"(?:ch|sh|s|x|z)$", unit) else "s")}


def _unit_amounts(unit: str, quote: str) -> Sequence[Decimal]:
    patterns = {"usd": r"\$|\b(?:usd|dollars?)\b", "eur": r"€|\b(?:eur|euros?)\b",
                "gbp": r"£|\b(?:gbp|pounds?)\b"}
    pattern = patterns.get(unit)
    if pattern is None:
        variants = _count_unit_variants(unit)
        pattern = "(?:%s)" % "|".join(re.escape(v) for v in sorted(variants, key=len, reverse=True) if v)
        if re.search(r"[A-Za-z]", unit):
            pattern = r"(?<![A-Za-z])" + pattern + r"(?![A-Za-z])"
    words = "|".join(re.escape(word) for word in sorted(_QUANTITY_WORDS, key=len, reverse=True))
    literal = _LITERAL if unit in ("usd", "eur", "gbp") else "(?:" + _LITERAL + "|" + words + "|a|an)"
    # Adjacency includes literal word quantities and adjectival forms (4-day).
    # A year or another currency elsewhere in the quote still cannot contribute.
    pair = re.compile("(?:" + pattern + r")\s*(?P<before>" + literal + r")(?![A-Za-z0-9_]|[.,]\d)|"
                      r"(?<![A-Za-z0-9_.])(?P<after>" + literal + r")(?:\s+|[-–]|(?=[^A-Za-z]))(?:" + pattern + ")", re.I)
    values = []
    fractional_spans = []
    if unit in _TIME_SECONDS:
        # Exclude the component 'one week' inside 'one week and a half'.
        fractional = re.compile(r"(?<![\w.-])(?P<amount>" + literal + r")\s+(?:" + pattern +
                                r")\s+and\s+(?:a\s+)?half\b", re.I)
        for match in fractional.finditer(quote):
            token = match.group('amount').casefold()
            number = (Decimal(1) if token in ('a', 'an') else
                      _QUANTITY_WORDS[token] if token in _QUANTITY_WORDS else _decimal(token))
            values.append(number + Decimal('0.5'))
            fractional_spans.append(match.span())
    for match in pair.finditer(quote):
        if any(start <= match.start() and match.end() <= end for start, end in fractional_spans):
            continue
        try:
            token = (match.group("before") or match.group("after")).casefold()
            values.append(Decimal(1) if token in ("a", "an") else
                          _QUANTITY_WORDS[token] if token in _QUANTITY_WORDS else _decimal(token))
        except ValueError:
            continue
    return values


def _verified_sum_value(item, unit, quote, source_text):
    value = _quantity_decimal(item.get("value"))
    target = _unit(unit)
    if not isinstance(item.get("unit"), str) or _unit(item["unit"]) != target:
        raise ValueError("mixed or missing unit")
    if "ordinal_progress" in item:
        if item["ordinal_progress"] is not True or item.get("value_type") != "cumulative":
            raise ValueError("ordinal progress needs an explicit cumulative-count claim")
        if target in _MEASUREMENT_UNITS or "duration" in item or "source_unit" in item:
            raise ValueError("ordinal progress cannot verify measurements or conversions")
        quantities = [number for noun in _count_unit_variants(target)
                      for number in _group_quantities(quote, noun, ordinal=True)]
        if value not in quantities:
            raise ValueError("ordinal quantity and count unit are not adjacent in the quote")
    elif "duration" in item:
        days = Decimal(quoted_duration_days(item["duration"], quote, source_text))
        if target not in _TIME_SECONDS or value * _TIME_SECONDS[target] != days * _TIME_SECONDS["days"]:
            raise ValueError("duration value differs from its quoted date range")
    elif "source_unit" in item:
        source_unit = item["source_unit"]
        if not isinstance(source_unit, str) or target not in _TIME_SECONDS or _unit(source_unit) not in _TIME_SECONDS:
            raise ValueError("only exact seconds/minutes/hours/days/weeks conversions are supported")
        try:
            original = _quantity_decimal(item.get("source_value"))
        except ValueError as exc:
            raise ValueError('source_value: '+str(exc)+
                             '; put only the source number here, without units or qualifiers; '
                             'keep the original duration wording in quote') from None
        if original not in _unit_amounts(_unit(source_unit), quote):
            raise ValueError("source value and unit are not adjacent in the quote")
        with localcontext() as context:
            context.prec = 200
            if value * _TIME_SECONDS[target] != original * _TIME_SECONDS[_unit(source_unit)]:
                raise ValueError("converted value differs from exact unit conversion")
    elif value not in _unit_amounts(target, quote):
        raise ValueError("value and unit are not adjacent in the quote")
    return value


def inspect_calculation(output: str, evidence: Mapping[str, str],
                        origins: Optional[Mapping[str, Set[int]]] = None,
                        max_items: int = 100, today: Optional[str] = None,
                        question: str = "") -> Tuple[str, Dict[str, Any]]:
    """返回去掉内部 JSON 的输出和审计结果。只接受实际送给回答模型的原文片段。"""
    matches = list(_CALCULATION.finditer(output))
    if not matches:
        return output, {"status": "not_provided", "scope": _SCOPE}
    start = matches[0].start()
    answer_markers = list(_ANSWER.finditer(output, matches[0].end()))
    end = answer_markers[0].start() if answer_markers else len(output)
    stripped = output[:start].rstrip() + "\n" + output[end:].lstrip()
    audit: Dict[str, Any] = {"status": "invalid", "scope": _SCOPE, "errors": []}
    errors = audit["errors"]
    if len(matches) != 1 or not answer_markers:
        errors.append("expected one CALCULATION object before ANSWER")
        return stripped, audit
    raw = output[matches[0].end():end].strip()
    try:
        spec = json.loads(raw)
    except (ValueError, RecursionError):
        errors.append("calculation is not a JSON object")
        return stripped, audit
    if not isinstance(spec, dict):
        errors.append("calculation must be an object")
        return stripped, audit
    operation = spec.get("operation")
    audit["operation"] = operation
    if operation == "date_difference":
        audit = inspect_dates(spec, evidence, today, question)
        audit['calculation'] = spec
        if audit["status"] == "checks_passed":
            _, answer = parse_answer(stripped)
            try:
                if _quantity_decimal(spec.get("result")) != _decimal(audit["computed"]):
                    audit["errors"].append("declared date interval differs from the computed result")
                matches = (_week_answer_matches(answer,audit) if 'elapsed_days' in audit
                    and (audit.get('requested_unit',audit.get('unit'))=='weeks') else
                    bool(_answer_numbers(answer)) and _answer_numbers(answer)[0]==_decimal(audit['computed']))
                if not matches:
                    audit["errors"].append("ANSWER differs from the computed date interval")
                if audit.get("rounding") == "nearest" and audit.get("remainder_days") and not re.search(
                        r"\b(?:about|approximately|roughly|rounded)\b|大约|约|近似", answer + " " + question, re.I):
                    audit["errors"].append("a rounded calendar answer must state its approximation")
                if audit.get("precision") == "year" and not re.search(
                        r"\b(?:about|approximately|roughly)\b|大约|约|近似", answer + " " + question, re.I):
                    audit["errors"].append("year-only endpoints require an explicit approximate answer")
                if audit.get('bound') == 'at_least' and not re.search(
                        r'\bat least\b|\b(?:a )?minimum(?: of)?\b|\bno fewer than\b|\bor more\b|至少|不低于|≥|>=', answer, re.I):
                    audit['errors'].append('a bounded interval answer must state its at-least qualification')
            except ValueError as exc:
                audit["errors"].append(str(exc))
            if audit["errors"]:
                audit["status"] = "invalid"
        return stripped, audit
    if operation not in ("sum", "count_unique", "count_groups", "count_dates"):
        audit.update(status="unsupported", errors=["unsupported operation"])
        return stripped, audit
    items, unit = spec.get("items"), spec.get("unit")
    audit['calculation'] = spec
    if not isinstance(items, list) or not items or len(items) > max_items:
        errors.append("items must contain 1 to %d evidence entries" % max_items)
        return stripped, audit
    if not isinstance(unit, str) or not unit.strip() or len(unit) > 80:
        errors.append("unit is missing or invalid")
        return stripped, audit
    audit["unit"] = unit
    if operation == 'count_dates' and (not is_attendance_day_count(question) or _unit(unit) != 'days'):
        errors.append('count_dates requires a requested attendance calendar-day count')
    if is_duration_total(question) and operation != 'count_dates':
        requested = re.search(r"\bhow many (days|weeks|hours|minutes|seconds)\b", question, re.I)
        if operation != 'sum' or requested and _unit(unit) != requested.group(1).lower():
            errors.append('duration total requires a sum in the requested unit')
    seen, entities, values = [], set(), []
    literal_date_count = 0
    for index, item in enumerate(items):
        label = "item %d: " % (index + 1)
        if not isinstance(item, dict):
            errors.append(label + "not an object")
            continue
        source, quote = item.get("source"), item.get("quote")
        if not isinstance(source, str) or source not in evidence:
            errors.append(label + "source was not supplied to the answer model")
            continue
        if operation == 'count_dates':
            try:
                quote = source_quote(item, evidence)
            except ValueError as exc:
                errors.append(label + str(exc))
                continue
        if not isinstance(quote, str) or not quote.strip() or quote not in evidence[source]:
            errors.append(label + "quote is not an exact contiguous source excerpt")
            continue
        origin = (origins or {}).get(source, set())
        named = operation == 'count_groups' and isinstance(item.get('entity'), str)
        for previous, previous_quote, previous_origin, previous_named in seen:
            if ((source == previous or bool(origin & previous_origin)) and _clean(quote) == previous_quote):
                if operation == 'sum' or operation == 'count_groups' and not (named and previous_named):
                    errors.append(label + "duplicate contribution")
            if source[0] != previous[0] and origin & previous_origin:
                errors.append(label + "a fact and its original episode are both used as contributions")
        seen.append((source, _clean(quote), origin, named))
        if operation == 'count_dates':
            try:
                dates = attendance_dates(item, evidence)
                literal_date_count += len(dates)
                entities.update(dates)
            except ValueError as exc:
                errors.append(label + str(exc))
        elif operation == "sum":
            try:
                value = _verified_sum_value(item, unit, quote, evidence[source])
            except ValueError as exc:
                errors.append(label + str(exc))
                continue
            values.append(value)
        elif operation == "count_groups":
            try:
                value = _quantity_decimal(item.get("value"))
            except ValueError as exc:
                errors.append(label + str(exc))
                continue
            noun, entity = item.get("quantity_label"), item.get('entity')
            if entity is not None:
                if noun is not None or 'ordinal_progress' in item:
                    errors.append(label + 'a named singleton cannot also claim a group quantity')
                elif (value != 1 or not isinstance(entity, str) or not entity.strip() or
                      not re.search(r'(?<!\w)' + re.escape(entity) + r'(?!\w)', quote)):
                    errors.append(label + 'a named singleton needs value=1 and a literal identified entity')
            elif not isinstance(noun, str) or not noun.strip() or len(noun) > 80 or noun not in quote:
                errors.append(label + "quantity_label must be copied from the quote")
            elif item.get("ordinal_progress") is not None and (
                    item["ordinal_progress"] is not True or item.get("value_type") != "cumulative"):
                errors.append(label + "ordinal progress needs an explicit cumulative-count claim")
            elif value not in _group_quantities(quote, noun, ordinal=item.get("ordinal_progress") is True):
                errors.append(label + "integer quantity must immediately precede its group label")
            item_unit = item.get("unit")
            if not isinstance(item_unit, str) or _unit(item_unit) != _unit(unit):
                errors.append(label + "mixed or missing count unit")
            if _unit(unit) in _MEASUREMENT_UNITS:
                errors.append(label + "count_groups cannot verify currency or time measurements")
            if entity is not None:
                key = _clean(entity) if isinstance(entity, str) else ''
                if key in entities:
                    continue
                entities.add(key)
            values.append(value)
        else:
            entity = item.get("entity")
            if not isinstance(entity, str) or not entity.strip() or entity not in quote:
                errors.append(label + "entity must be copied from the quote")
                continue
            if re.search(r"[A-Za-z]", entity) and not re.search(r"(?<!\w)" + re.escape(entity) + r"(?!\w)", quote):
                errors.append(label + "entity is only a substring of a different name")
                continue
            entities.add(_clean(entity))
    if errors:
        # 有引用或单位错误时，部分合法条目的小计不能充当可修正的总数。
        return stripped, audit
    # 大数字也使用十进制精确累加，避免浮点误差和 Decimal 默认精度舍入。
    with localcontext() as context:
        context.prec = 200
        computed = sum(values, Decimal(0)) if operation in ("sum", "count_groups") else Decimal(len(entities))
    audit["computed"] = format(computed, "f")
    audit["entries"] = len(items)
    if operation == "count_unique":
        audit["duplicates_removed"] = len(items) - len(entities)
    elif operation == 'count_dates':
        audit.update(dates=sorted(entities), basis='distinct_attendance_dates',
                     duplicates_removed=literal_date_count-len(entities))
    try:
        if _quantity_decimal(spec.get("result")) != computed:
            errors.append("declared result differs from the computed result")
    except ValueError as exc:
        errors.append("result: " + str(exc))
    _, answer = parse_answer(stripped)
    answer_numbers = _answer_numbers(answer)
    if not answer_numbers or answer_numbers[0] != computed:
        errors.append("the first numeric result in ANSWER differs from the computed result")
    audit["status"] = "checks_passed" if not errors else "invalid"
    return stripped, audit


def _inspect_read_answer(output, evidence, origins=None, *, today=None, question=''):
    """Qualify a bare date result only after its source and value checks pass."""
    stripped, audit = inspect_calculation(output, evidence, origins, today=today, question=question)
    presentation_errors = {
        'a rounded calendar answer must state its approximation',
        'year-only endpoints require an explicit approximate answer',
        'a bounded interval answer must state its at-least qualification',
    }
    errors = audit.get('errors', [])
    complete_weeks = (audit.get('status') == 'checks_passed' and audit.get('requested_unit',audit.get('unit')) == 'weeks'
        and audit.get('rounding') == 'complete' and bool(audit.get('remainder_days')))
    if (audit.get('operation') != 'date_difference' or not (errors or complete_weeks) or
            not set(errors) <= presentation_errors or
            re.search(r'\b(?:only|just) (?:a single |a |one |the )?(?:number|numeral|integer)\b|\b(?:number|integer)[ -]only\b|只(?:回复|回答|输出)(?:一个)?数字', question, re.I)):
        return stripped, audit
    _, answer = parse_answer(stripped)
    unit = audit.get('requested_unit',audit.get('unit'))
    if unit not in ('days', 'weeks', 'months', 'years'):
        return stripped, audit
    aliases = {'days':'天', 'weeks':'周', 'months':'个月', 'years':'年'}
    bare = re.fullmatch(r'\s*('+_LITERAL+r')\s*(?:'+unit[:-1]+r's?|'+aliases[unit]+r')?\s*[.。]?\s*', answer, re.I)
    markers = list(_ANSWER.finditer(output))
    if not bare or not markers:
        return stripped, audit
    amount = bare.group(1)
    chinese = bool(re.search(r'[\u4e00-\u9fff]', question))
    display_unit = unit[:-1] if abs(_decimal(amount)) == 1 else unit
    if complete_weeks:
        qualified = (amount+' 个完整周。' if chinese else amount+' complete '+display_unit+'.')
    elif audit.get('bound') == 'at_least':
        qualified = ('至少 '+amount+' '+aliases[unit]+'；确切间隔未记录。' if chinese else
                     'At least '+amount+' '+display_unit+'; the exact interval was not recorded.')
    else:
        qualified = ('大约 '+amount+' '+aliases[unit]+'。' if chinese else 'About '+amount+' '+display_unit+'.')
    repaired = output[:markers[-1].end()]+' '+qualified
    checked, final = inspect_calculation(repaired, evidence, origins, today=today, question=question)
    if final.get('status') != 'checks_passed':
        return stripped, audit
    final['presentation_repair'] = dict(original_answer=answer, qualified_answer=qualified,
        reason=errors or ['a complete-week result has a nonzero day remainder'],
        scope='Only a bare result qualifier and unit formatting were changed; source evidence and numeric value are unchanged.')
    return checked, final


def _generate_complete(model, messages, max_tokens, tags, counter=None, input_limit=None):
    """One bounded retry for an unfinished or empty answer; never publish a clipped answer."""
    attempts = []
    current = list(messages)
    request_tokens = max_tokens
    for attempt in range(2):
        if counter and input_limit is not None and counter.messages(current) > input_limit:
            raise ModelError("answer generation retry exceeds input budget")
        reply = model.chat(current, purpose="answer", max_tokens=request_tokens,
                           tags={**dict(tags or {}), **({"answer_format_retry": "1"} if attempt else {})})
        markers = list(_ANSWER.finditer(reply.text))
        has_answer = bool(reply.text.strip())
        if markers:
            has_answer = bool(reply.text[markers[-1].end():].strip())
        elif re.match(r"\s*NOTES\s*[:：]", reply.text, re.I):
            has_answer = False
        attempts.append({"finish_reason": reply.finish_reason, "has_answer": has_answer,
                         "max_tokens": request_tokens})
        if reply.finish_reason == "content_filter" or (reply.finish_reason == "refusal" and not has_answer):
            raise ModelError("answer generation was blocked: " + reply.finish_reason)
        if reply.finish_reason not in ("length", "incomplete") and has_answer:
            return reply.text, attempts
        if reply.finish_reason in ("length", "incomplete"):
            request_tokens = retry_completion_tokens(model, max_tokens)
        # An unfinished draft is not a completed assistant message to replay.
        current = list(messages) + [{"role": "user", "content":
            "The previous response was empty or ended before completion. Return a complete response with concise NOTES and ANSWER. Keep notes brief and finish within the output budget; retain required calculation evidence."}]
    raise ModelError("answer remained empty or incomplete after one generation retry")


def quantity_repair_rules(errors):
    """Explain the count schema when a sum failed literal measurement checks."""
    if not any("unit is not adjacent" in str(error) or "unit are not adjacent" in str(error)
               or "mixed or missing unit" in str(error)
               or "quantity must immediately precede" in str(error)
               or "quantity_label must be copied" in str(error)
               or "ordinal progress" in str(error)
               or "numeric aggregate recovery requires" in str(error)
               for error in errors):
        return ""
    return ("\nFor object counts whose literal source nouns differ from the shared count unit, use "
            "operation=count_groups, not sum with mixed units. Set every item.unit exactly to the top-level "
            "unit. Put each source noun in quantity_label, not in a different item.unit. "
            "Copy the whole noun phrase directly after its quantity, including adjectives. For a completed "
            "sequence index, the literal noun may precede the number; it needs ordinal_progress=true and "
            "value_type=cumulative. A rank, planned "
            "occurrence or list position is not completed progress. Keep all required items; do not use "
            "count_groups for money, time measurements or unsupported category membership. "
            "For example, completed 'my fourth practice round' requires quantity_label='practice round', "
            "value='4', ordinal_progress=true and value_type='cumulative'; 'a new badge' requires "
            "quantity_label='new badge' and value='1'. These examples illustrate fields, not supplied evidence. "
            "A supported numeric total must include CALCULATION before ANSWER.")


def _normalize_literal_count_schema(output, audit, evidence, origins, today, question):
    """Normalize only unambiguous literal count labels before mandatory review."""
    if audit.get('status') != 'invalid' or not audit.get('errors'):
        return None
    if any(not re.fullmatch(r'item \d+: (?:value and unit are not adjacent in the quote|ordinal quantity and count unit are not adjacent in the quote|integer quantity must immediately precede its group label)', error)
           for error in audit['errors']):
        return None
    spec = audit.get('calculation', {})
    if spec.get('operation') not in ('sum','count_groups') or not isinstance(spec.get('unit'), str):
        return None
    measurement = re.compile(r'\b(?:seconds?|minutes?|hours?|days?|weeks?|months?|years?|usd|eur|gbp|jpy|cny|dollars?|euros?|pounds?|yuan|cents?|miles?|kilomet(?:er|re)s?|met(?:er|re)s?|feet|foot|inches|yards?|lit(?:er|re)s?|gallons?|ounces?|grams?|kilograms?|lbs?|kg|km|cm|mm|ml|oz|watts?|volts?|percent(?:age)?)\b|[$€£%]', re.I)
    if measurement.search(spec['unit']):
        return None
    allowed_labels = {variant for word in re.findall(r'[A-Za-z]+',spec['unit'])
                      for variant in _count_unit_variants(word.casefold())}
    allowed_fields={'source','quote','value','unit','ordinal_progress','value_type'}
    if spec['operation'] == 'count_groups':
        allowed_fields.add('quantity_label')
    items=[]
    for item in spec.get('items', []):
        if (set(item) - allowed_fields
                or not str(item.get('source','')).startswith('E')):
            return None
        quote, source = item['quote'], evidence.get(item['source'], '')
        if 'User: ' not in source:
            return None
        user = source.split('User: ',1)[1].split('\nAssistant:',1)[0]
        if quote not in user:
            return None
        value = _quantity_decimal(item['value'])
        if value < 0 or value != value.to_integral_value():
            return None
        words=list(re.finditer(r"[A-Za-z0-9]+(?:[-'][A-Za-z0-9]+)*",quote))
        labels=set()
        blocked={'of','and','or','per','for','in','with','to','from','at','by','as',
                 'is','was','are','were','i','we','a','an','the','each'}
        for index,head in enumerate(words):
            if head.group().casefold() not in allowed_labels:
                continue
            for start in range(max(0,index-3),index+1):
                prefix=[m.group().casefold() for m in words[start:index]]
                if any(word in blocked or word in _QUANTITY_WORDS or word in _ORDINAL_WORDS
                       or re.fullmatch(r'\d+(?:st|nd|rd|th)?',word) for word in prefix):
                    continue
                label=quote[words[start].start():head.end()]
                if not re.fullmatch(r"[A-Za-z0-9]+(?:[-'][A-Za-z0-9]+)*(?:\s+[A-Za-z0-9]+(?:[-'][A-Za-z0-9]+)*)*",label):
                    continue
                if value in _group_quantities(quote,label,ordinal=item.get('ordinal_progress') is True):
                    labels.add(label)
        if len(labels) != 1:
            return None
        label = next(iter(labels))
        if spec['operation'] == 'count_groups':
            old_label = item.get('quantity_label')
            if (not isinstance(old_label,str) or not old_label.strip()
                    or not re.search(r'(?<!\w)'+re.escape(old_label)+r'$',label)):
                return None
        if measurement.search(label):
            return None
        items.append(dict(item,quantity_label=label))
    if not items:
        return None
    normalized = dict(spec,operation='count_groups',items=items)
    match=list(_CALCULATION.finditer(output))[0]
    marker=list(_ANSWER.finditer(output,match.end()))[0]
    revised=output[:match.end()]+' '+json.dumps(normalized,ensure_ascii=False)+'\n'+output[marker.start():]
    stripped,checked=_inspect_read_answer(revised,evidence,origins,today=today,question=question)
    if checked['status'] != 'checks_passed':
        return None
    checked['schema_normalization']=dict(from_operation=spec['operation'],to_operation='count_groups',
        original_errors=list(audit['errors']),original_calculation=spec,
        scope='Literal labels and count schema only; values, quotes, units, result and ANSWER retained. Qualification still requires semantic review.')
    return stripped,checked


def _semantic_finish(model, messages, notes, answer, audit, evidence, origins, question,
                     operation, qualifiers, max_tokens, tags, counter, input_limit, today, enabled,
                     require_calculation=False):
    """One review and at most one grounded repair; every attempt remains auditable."""
    if audit.get("status") == "invalid" or "rejected_answer" in audit:
        return notes, answer, audit
    operation = operation or operation_for(question)
    reviews = []
    try:
        checked_math = {k:audit[k] for k in ("operation", "unit", "requested_unit", "unit_conversion", "computed", "basis", "entries", "dates", "duplicates_removed", "rounding", "complete_units", "elapsed_days", "remainder_days", "endpoints", "precision", "years", "bound", "endpoint_roles") if k in audit} if audit.get("status") == "checks_passed" else None
        review = review_response(model, question, answer, evidence, operation, qualifiers, counter, input_limit, tags, today,
                                 verified_calculation=checked_math)
        reviews.append(review)
        if not (review["responsive"] and review["supported"]):
            audit["semantic_rejected_answer"] = answer
            correction = ("The answer review found a problem: " + str(review.get("reason", "")) +
                "\nAnswer the original question using the SAME supplied evidence. " + operation_rules(operation, qualifiers,question) +
                "\nReturn complete concise NOTES and ANSWER. " +
                ("Include required CALCULATION JSON using the original schema. "
                 if is_temporal_interval(question) or is_aggregate(question, operation) else
                 "Omit CALCULATION for this lookup or state answer; do not emit a placeholder. ") +
                "When original terminology differs, state what was actually supplied and explain that difference.")
            if operation == 'advice':
                correction += ("\nCorrect the identified unsupported personal claim while retaining supported, responsive "
                    "reasoning and useful conditional advice from the draft. Phrase new suggestions as suggestions. "
                    "Keep the answer focused on the user's current decision or problem; do not turn it into a report "
                    "about which old recommendations were confirmed or unconfirmed unless that is what was asked.")
            if is_temporal_interval(question):
                correction += ("\nDate proof: quote literal body substrings for event_text and relative_text; "
                    "anchor relative wording to recorded_at. Weeks use complete seven-day periods, "
                    "not a fractional result; state any remaining days separately.")
            if audit.get("calculation") and audit.get("status") == "checks_passed":
                correction += ("\nPreviously verified literal evidence and arithmetic (qualification still needs review):\n" +
                    json.dumps(audit["calculation"], ensure_ascii=False) +
                    "\nRetain this proof only if its selected events still answer the question. Values and result "
                    "must remain plain numbers; put about/approximately/at least only in ANSWER, never in numeric JSON fields. "
                    "Omit ordinal_progress unless it is an actual completed ordinal count, never a false placeholder.")
            retry = list(messages) + [{"role": "user", "content": correction}]
            if counter and input_limit is not None and counter.messages(retry) > input_limit:
                rules = prompts.DATE_CALCULATION_RULES if is_temporal_interval(question) else (
                    prompts.calculation_rules(question) if is_aggregate(question, operation) else "")
                retry = [{"role": "system", "content": prompts.ANSWER_SYSTEM}, {"role": "user", "content":
                    "Today: %s\nQuestion: %s\nSupplied sources:\n%s\n%s\n%s" % (
                        today, question, "\n\n".join("[%s]\n%s" % (k, v) for k, v in evidence.items()), rules, correction)}]
            revised, generations = _generate_complete(model, retry, max_tokens,
                {**dict(tags or {}), "semantic_retry": "1"}, counter, input_limit)
            audit["generation_attempts"].extend(generations)
            if enabled:
                stripped, calculation = _inspect_read_answer(revised, evidence, origins, today=today, question=question)
                audit["semantic_repair_calculation"] = calculation
                if (is_temporal_interval(question) and _answer_numbers(parse_answer(stripped)[1]) and
                        calculation.get("operation") != "date_difference"):
                    raise ValueError("semantic repair omitted required date evidence")
                if calculation["status"] == "invalid":
                    raise ValueError("semantic repair calculation did not validate")
                if require_calculation and _answer_numbers(parse_answer(stripped)[1]) and calculation.get("operation") not in ("sum", "count_unique", "count_groups", "count_dates"):
                    raise ValueError("semantic repair omitted required aggregate evidence")
                audit.update(calculation)
            else:
                stripped = revised
            notes, answer = parse_answer(stripped)
            checked_math = {k:audit[k] for k in ("operation", "unit", "requested_unit", "unit_conversion", "computed", "basis", "entries", "dates", "duplicates_removed", "rounding", "complete_units", "elapsed_days", "remainder_days", "endpoints", "precision", "years", "bound", "endpoint_roles") if k in audit} if audit.get("status") == "checks_passed" else None
            review = review_response(model, question, answer, evidence, operation, qualifiers, counter, input_limit, tags, today,
                                     verified_calculation=checked_math)
            reviews.append(review)
            if not (review["responsive"] and review["supported"]):
                raise ValueError("answer still fails responsiveness or support review: " + str(review.get("reason", "")))
    except (ValueError, ModelError) as exc:
        if isinstance(exc, ModelError) and (exc.account_problem or is_read_service_error(exc)):
            raise
        audit.update(status="incomplete", semantic_error=str(exc), semantic_rejected_answer=answer)
        answer = ("目前无法根据已有记忆核实这个问题的完整答案。" if re.search(r"[\u4e00-\u9fff]", question)
                  else "I don't have enough verified information to answer this question.")
    audit["semantic_reviews"] = reviews
    return notes, answer, audit


def answer_with_validation(model: ChatModel, messages: Sequence[Mapping[str, str]], *,
                           evidence: Mapping[str, str], origins: Optional[Mapping[str, Set[int]]] = None,
                           enabled: bool = True, max_tokens: int = 1024,
                           question: str = "",
                           tags: Optional[Mapping[str, str]] = None, counter=None,
                           input_limit: Optional[int] = None, today: Optional[str] = None,
                           semantic_enabled: bool = False, operation: Optional[str] = None,
                           qualifiers=(), review_policy=None, review_context=(), require_calculation=False) -> Tuple[str, str, Dict[str, Any]]:
    if counter and input_limit is not None and counter.messages(messages) > input_limit:
        raise ValueError("answer prompt exceeds input budget")
    if require_calculation and not enabled:
        raise ValueError("aggregate recovery requires calculation validation")
    output, generation_attempts = _generate_complete(model, messages, max_tokens, tags, counter, input_limit)
    policy = review_policy or ("always" if semantic_enabled else "off")
    if policy not in ("always", "on_risk", "off"):
        raise ValueError("invalid answer review policy")
    require_calculation = require_calculation or is_duration_total(question)
    reasons = list(review_context)
    selected_operation = operation or operation_for(question)
    if selected_operation != "lookup" or is_temporal_interval(question):
        reasons.append("numeric_temporal_or_state_operation")
    if qualifiers:
        reasons.append("conditional_scope")
    if len(evidence) > 1:
        reasons.append("multiple_sources_or_conflict")
    if re.search(r"\b(?:if|only|except|before|after|same|other|their|latest)\b|如果|仅|除了|之前|之后|它|那个", question, re.I):
        reasons.append("scope_or_dependency")
    review_enabled = policy == "always" or policy == "on_risk" and bool(reasons)
    review_trace = {"policy":policy,"triggered":review_enabled,"reasons":reasons}
    if not enabled:
        notes, answer = parse_answer(output)
        audit = {"status": "disabled", "generation_attempts": generation_attempts, "model_review":review_trace}
        if review_enabled:
            return _semantic_finish(model, messages, notes, answer, audit, evidence, origins, question,
                operation, qualifiers, max_tokens, tags, counter, input_limit, today, enabled, require_calculation)
        return notes, answer, audit
    stripped, audit = _inspect_read_answer(output, evidence, origins, today=today, question=question)
    if is_temporal_interval(question) and _answer_numbers(parse_answer(stripped)[1]) and audit.get("operation") != "date_difference":
        audit = {"status": "invalid", "errors": ["a numeric date interval requires source-backed date_difference evidence"]}
    if require_calculation and _answer_numbers(parse_answer(stripped)[1]) and audit.get("operation") not in ("sum", "count_unique", "count_groups", "count_dates"):
        audit = {"status":"invalid","errors":["a numeric aggregate recovery requires source-backed calculation evidence"]}
    attempts = [audit]
    if review_enabled:
        normalized = _normalize_literal_count_schema(output,audit,evidence,origins,today,question)
        if normalized is not None:
            stripped,audit=normalized
            attempts.append(audit)
            audit={**audit,'attempts':attempts}
    if audit["status"] == "invalid":
        correction = ("The program found errors in your calculation evidence: " + json.dumps(audit, ensure_ascii=False) +
                      "\nRecheck against the supplied memory. Return complete NOTES, CALCULATION and ANSWER again. "
                      "Do not invent evidence or merely copy the computed value when source validation failed. "
                      "If an operation is unsupported or evidence is insufficient, omit CALCULATION and explain the limitation.")
        correction += quantity_repair_rules(audit.get("errors", ()))
        if is_temporal_interval(question):
            correction += ('\nDates: date_text must occur in quote; headers may be quoted with the body. '
                           'For relative dates use anchor=recorded_at and copy event_text/relative_text as '
                           'exact contiguous BODY substrings, never paraphrases. TODAY only needs source=TODAY '
                           'and its ISO caller date. Numeric date answers require CALCULATION.')
        # The original source remains in messages. Replaying the rejected draft
        # duplicates large quotes and can crowd out the bounded repair itself.
        retry = list(messages) + [{"role": "user", "content": correction}]
        repair_context = "original_messages"
        if counter and input_limit is not None and counter.messages(retry) > input_limit:
            # Remove generic answer framing, never source evidence. The same
            # quote/date/unit validator still checks the complete new response.
            systems = [dict(message) for message in messages if message["role"] == "system"]
            if not systems:
                systems = [{"role": "system", "content": prompts.ANSWER_SYSTEM}]
            sources = "\n\n".join("Supplied source %s:\n%s" % (key, text) for key, text in evidence.items())
            rules = prompts.DATE_CALCULATION_RULES if is_temporal_interval(question) else prompts.calculation_rules(question)
            retry = systems + [{"role": "user", "content": prompts.CALCULATION_REPAIR_USER.format(
                today=today or "not supplied", question=question, sources=sources, rules=rules, correction=correction)}]
            repair_context = "source_evidence"
        try:
            if counter and input_limit is not None and counter.messages(retry) > input_limit:
                raise ModelError("calculation repair skipped: input budget exceeded")
            revised, repair_generation = _generate_complete(model, retry, max_tokens,
                {**dict(tags or {}), "aggregation_retry": "1"}, counter, input_limit)
            generation_attempts.extend(repair_generation)
            stripped, audit = _inspect_read_answer(revised, evidence, origins, today=today, question=question)
            if is_temporal_interval(question) and _answer_numbers(parse_answer(stripped)[1]) and audit.get("operation") != "date_difference":
                audit = {"status": "invalid", "errors": ["a numeric date interval requires source-backed date_difference evidence"]}
            if require_calculation and _answer_numbers(parse_answer(stripped)[1]) and audit.get("operation") not in ("sum", "count_unique", "count_groups", "count_dates"):
                audit = {"status":"invalid","errors":["a numeric aggregate recovery requires source-backed calculation evidence"]}
            attempts.append(audit)
            if review_enabled:
                normalized = _normalize_literal_count_schema(revised,audit,evidence,origins,today,question)
                if normalized is not None:
                    stripped,audit=normalized
                    attempts.append(audit)
        except ModelError as exc:
            if exc.account_problem or is_read_service_error(exc):
                raise
            audit = {**audit, "repair_error": str(exc)}
        audit = {**audit, "attempts": attempts,
                 "repair_context": repair_context,
                 "repair_prompt_budget_count": counter.messages(retry) if counter else None,
                 "repair_prompt_tokens_estimate": sum(estimate_tokens(m["content"]) for m in retry)}
    notes, answer = parse_answer(stripped)
    if audit["status"] == "invalid" or (len(attempts) > 1 and audit["status"] == "not_provided" and _numbers(answer)):
        # 已发现的错误在一次修正后仍未得到核验，不把未核实数字作为最终结论。
        audit["rejected_answer"] = answer
        if is_temporal_interval(question):
            answer = ("目前无法根据引用日期和当前日期核实这个时间间隔。" if re.search(r"[\u4e00-\u9fff]", question)
                      else "I don't know; I couldn't verify this date interval against the cited dates and current date.")
        else:
            answer = ("目前无法根据引用证据核实这个合计。" if re.search(r"[\u4e00-\u9fff]", question)
                      else "I don't know; I couldn't verify this total against the cited evidence.")
    audit["generation_attempts"] = generation_attempts
    if audit.get("status") == "invalid" or "rejected_answer" in audit:
        review_enabled = False
        review_trace.update(triggered=False, skipped="invalid_program_evidence")
    audit["model_review"] = review_trace
    if review_enabled:
        return _semantic_finish(model, messages, notes, answer, audit, evidence, origins, question,
            operation, qualifiers, max_tokens, tags, counter, input_limit, today, enabled, require_calculation)
    return notes, answer, audit
