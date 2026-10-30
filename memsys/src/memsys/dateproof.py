"""Validate literal day dates and compute intervals against the caller's clock."""
import re
from datetime import date, datetime, timedelta

from .timeutil import add_months, parse_span

_EXACT_INTERVAL = re.compile(
    r"\b(?:exact|complete|completed|whole|full|calendar)\s+(?:number\s+of\s+)?"
    r"(?:days?|weeks?|months?|years?|interval|duration)\b|\b(?:exactly|precisely)\b|完整|整月|整年|精确", re.I)
_MINIMUM_INTERVAL = re.compile(r'\bminimum\b|\bat least\b|\blower bound\b|\bno fewer than\b|至少|最少|下限', re.I)


def literal_day(text):
    if not isinstance(text, str) or len(text) > 80:
        raise ValueError("date_text must be a literal complete day date")
    span = parse_span(text)
    if span and span.precision == "day":
        return span.start
    normalized = re.sub(r"(?<=\d)(st|nd|rd|th)\b", "", text.strip(), flags=re.I)
    normalized = re.sub(r"\b(Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\.", r"\1", normalized, flags=re.I)
    chinese = re.fullmatch(r"(\d{4})年(\d{1,2})月(\d{1,2})日", normalized)
    if chinese:
        return date(*(int(value) for value in chinese.groups()))
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y", "%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(normalized, fmt).date()
        except ValueError:
            pass
    raise ValueError("date_text must be a literal complete day date; month-only dates are insufficient")


def recorded_year_day(text, source_text):
    """Resolve a quoted month and day under an explicit source-year anchor."""
    normalized = re.sub(r"(?<=\d)(st|nd|rd|th)\b", "", text.strip(), flags=re.I)
    normalized = re.sub(r"\b(Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\.", r"\1", normalized, flags=re.I)
    normalized = re.sub(r"^this year\s*,?\s*|\s*,?\s*this year$", "", normalized, flags=re.I).strip()
    normalized = re.sub(r"^今年\s*", "", normalized)
    year = _recorded_date(source_text).year
    chinese = re.fullmatch(r"(\d{1,2})月(\d{1,2})日", normalized)
    if chinese:
        return date(year, *(int(value) for value in chinese.groups()))
    for fmt in ("%B %d %Y", "%b %d %Y", "%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(normalized + " " + str(year), fmt).date()
        except ValueError:
            pass
    raise ValueError("recorded-year endpoint needs a literal month and day")


def interval(start, end, unit):
    if unit not in ("days", "weeks", "months", "years"):
        raise ValueError("unsupported date interval unit")
    if end < start:
        return -interval(end, start, unit)
    if unit == "days":
        return (end - start).days
    if unit == "weeks":
        return (end - start).days // 7
    months = (end.year - start.year) * 12 + end.month - start.month
    if add_months(datetime.combine(start, datetime.min.time()), months).date() > end:
        months -= 1
    return months if unit == "months" else months // 12


def rounded_calendar_interval(start, end, unit):
    """Nearest calendar unit using progress between clamped anniversaries."""
    if unit not in ("months", "years"):
        raise ValueError("calendar rounding is supported only for months and years")
    sign = -1 if end < start else 1
    if sign < 0:
        start, end = end, start
    complete = interval(start, end, unit)
    multiplier = 1 if unit == "months" else 12
    origin = datetime.combine(start, datetime.min.time())
    anchor = add_months(origin, complete * multiplier).date()
    next_anchor = add_months(origin, (complete + 1) * multiplier).date()
    remainder = (end - anchor).days
    rounded = complete + int(remainder * 2 >= (next_anchor - anchor).days)
    return dict(computed=str(sign * rounded), complete_units=sign * complete,
                remainder_days=sign * remainder, rounding="nearest")


def source_quote(point, evidence):
    source = point.get("source")
    if not isinstance(source, str) or source not in evidence:
        raise ValueError("date source was not supplied to the answer model")
    quote = point.get("quote")
    if 'full_source' in point:
        if point['full_source'] is not True:
            raise ValueError('full_source must be true when supplied')
        quote = evidence[source]
    elif "line_start" in point or "line_end" in point:
        start, end = point.get("line_start"), point.get("line_end")
        lines = evidence[source].splitlines(keepends=True)
        if (not isinstance(start, int) or isinstance(start, bool) or not isinstance(end, int) or isinstance(end, bool)
                or not 1 <= start <= end <= len(lines)):
            raise ValueError("date line citation range is invalid")
        quote = "".join(lines[start - 1:end])
    if not isinstance(quote, str) or not quote.strip() or quote not in evidence[source]:
        raise ValueError("date quote is not an exact contiguous source excerpt")
    return quote


def _recorded_date(text):
    match = re.search(r"(?m)^\[E\d+\]\s+(?:conversation on\s+)?(\d{4}-\d{2}-\d{2})\b", text)
    if not match:
        match = re.search(r"(?m)^\[F\d+\][^\n]*?\bnoted\s+(\d{4}-\d{2}-\d{2})\b", text)
    if not match:
        raise ValueError("relative endpoint needs a supplied recorded-date header")
    return date.fromisoformat(match.group(1))


def _completed_recording_bound(point, evidence):
    """An explicitly completed user event occurred no later than its record.

    Literal provenance and the bound are checked here. Whether the quoted
    wording actually establishes completion remains a semantic-review duty.
    """
    if not isinstance(point, dict) or point.get('completed_by_recorded_date') is not True:
        raise ValueError('recording bound requires completed_by_recorded_date=true')
    source = point.get('source')
    if not isinstance(source, str) or not re.fullmatch(r'E\d+', source) or source not in evidence:
        raise ValueError('recording bound requires an original episode source')
    if any(field in point for field in ('date', 'date_text', 'year_text', 'relative_text', 'anchor')):
        raise ValueError('a recording bound must not assert an exact event date')
    quote = source_quote(point, evidence)
    text = evidence[source]
    if '\n' not in text:
        raise ValueError('recording bound needs an original user turn and dated header')
    _, body = text.split('\n', 1)
    user = body.split('\nAssistant: ', 1)[0] if body.startswith('User: ') else ''
    event = point.get('event_text')
    if (not isinstance(event, str) or not event.strip() or event not in user[len('User: '):]
            or event not in quote):
        raise ValueError('recording bound needs a literal event from the original user statement')
    return _recorded_date(text)


def quoted_duration_days(spec, quote, source_text):
    """Compute a literal date range; shortened endpoints inherit month/year.

    Only an explicit connector between the two quoted endpoints establishes
    a range. Missing years use the supplied source header, never the LLM clock.
    """
    if not isinstance(spec, dict):
        raise ValueError("duration needs quoted start_text and end_text")
    start_text, end_text = spec.get("start_text"), spec.get("end_text")
    if any(not isinstance(t, str) or not t.strip() or len(t) > 80 for t in (start_text, end_text)):
        raise ValueError("duration endpoints must be literal quoted dates")
    connector = re.escape(start_text) + r"\s*(?:to|through|until|[-–—]|到|至)\s*" + re.escape(end_text) + r"(?!\w)"
    if not re.search(connector, quote, re.I):
        raise ValueError("duration endpoints must form an explicit range in the quote")

    def point(text, anchor=None):
        try:
            return literal_day(text)
        except ValueError:
            normalized = re.sub(r"(?<=\d)(st|nd|rd|th)\b", "", text.strip(), flags=re.I)
            if anchor is not None and re.fullmatch(r"\d{1,2}", normalized):
                return date(anchor.year, anchor.month, int(normalized))
            year = anchor.year if anchor else _recorded_date(source_text).year
            for fmt in ("%B %d %Y", "%b %d %Y", "%d %B %Y", "%d %b %Y"):
                try:
                    return datetime.strptime(normalized + " " + str(year), fmt).date()
                except ValueError:
                    pass
            raise ValueError("duration endpoint needs a day and month, not an approximate date")

    start = point(start_text)
    end = point(end_text, start)
    if end < start:
        raise ValueError("duration ends before it starts; supply explicit years for cross-year ranges")
    inclusive = spec.get("inclusive", False)
    if not isinstance(inclusive, bool) or inclusive and not re.search(r"\binclusive\b|含首尾", quote, re.I):
        raise ValueError("inclusive duration requires an explicit inclusive source convention")
    return (end - start).days + int(inclusive)


def _endpoint(point, evidence, today):
    if not isinstance(point, dict):
        raise ValueError("date interval needs start and end objects")
    declared = date.fromisoformat(point["date"]) if point.get("date") else None
    if point.get("source") == "TODAY":
        actual = literal_day(today) if today else None
        if actual is None or declared is not None and declared != actual:
            raise ValueError("TODAY must equal the date supplied by the caller")
        return actual
    quote = source_quote(point, evidence)
    if point.get('full_source'):
        body = '\n'.join(line for line in quote.splitlines() if not re.match(r'^\[[FE]\d+\]', line))
        event = point.get('event_text')
        if not isinstance(event, str) or not event.strip() or event not in body:
            raise ValueError('full-source endpoint requires a literal event phrase from the source body')
    relative = point.get("relative_text")
    if relative is not None:
        event = point.get("event_text")
        body = "\n".join(line for line in quote.splitlines() if not re.match(r"^\[[FE]\d+\]", line))
        if (point.get("anchor") != "recorded_at" or not isinstance(relative, str) or
                not re.search(r"(?<!\w)" + re.escape(relative) + r"(?!\w)", body, re.I) or
                not isinstance(event, str) or not event.strip() or event not in body):
            raise ValueError('relative_text/event_text must be verbatim body substrings; anchor=recorded_at')
        offsets = {"today": 0, "yesterday": -1, "tomorrow": 1, "今天": 0, "昨天": -1, "明天": 1}
        offset = offsets.get(relative.casefold())
        if offset is None:
            m = re.fullmatch(r"(\d+)\s+days?\s+(ago|later)", relative, re.I)
            if not m:
                raise ValueError("unsupported or approximate relative endpoint")
            offset = int(m.group(1)) * (-1 if m.group(2).casefold() == "ago" else 1)
        actual = _recorded_date(evidence[point["source"]]) + timedelta(days=offset)
    else:
        date_text = point.get("date_text")
        if not isinstance(date_text, str) or not re.search(r"(?<!\d)" + re.escape(date_text) + r"(?!\d)", quote):
            raise ValueError('date_text must occur verbatim in quote. If the source body says today/yesterday/tomorrow, '
                             'use relative_text with that literal word, anchor="recorded_at" and literal event_text; '
                             'omit date_text, never substitute a computed/header ISO day')
        try:
            actual = literal_day(date_text)
        except ValueError:
            if point.get("anchor") != "recorded_at":
                raise
            if re.search(r"\b(?:last|next|previous|following) year\b|去年|明年", quote, re.I):
                raise ValueError("an explicit different year cannot use the recorded-year anchor")
            actual = recorded_year_day(date_text, evidence[point["source"]])
    if declared is not None and declared != actual:
        raise ValueError("declared date does not match the literal complete date in its quote")
    return actual


def _year_endpoint(point, evidence, today):
    if not isinstance(point, dict):
        raise ValueError("year interval needs start and end objects")
    if point.get("source") == "TODAY":
        if any(key in point for key in ("year_text", "date", "date_text", "relative_text", "anchor")):
            raise ValueError("TODAY year uses the caller clock, not a declared date or year")
        return literal_day(today).year
    quote = source_quote(point, evidence)
    body = "\n".join(line for line in quote.splitlines() if not re.match(r"^\[[FE]\d+\]", line))
    year_text, event = point.get("year_text"), point.get("event_text")
    if (not isinstance(year_text, str) or not re.fullmatch(r"\d{4}", year_text) or not 1 <= int(year_text) <= 9999
            or not re.search(r"(?<![A-Za-z0-9_$€£])" + re.escape(year_text) + r"(?![A-Za-z0-9_]|[.,]\d)", body)):
        raise ValueError("year_text needs a literal four-digit year in the source body")
    if not isinstance(event, str) or not event.strip() or event not in body:
        raise ValueError("year endpoint needs a literal event phrase from the source body")
    if any(key in point for key in ("date", "date_text", "relative_text", "anchor")):
        raise ValueError("year precision cannot declare a full or inferred day date")
    return int(year_text)


def _check_clock_points(spec, question):
    start, end = spec.get("start"), spec.get("end")
    if not isinstance(start, dict) or not isinstance(end, dict):
        raise ValueError("date interval needs start and end objects")
    if start.get("source") == end.get("source") == "TODAY":
        raise ValueError("date interval needs at least one quoted memory date")
    since_now = (re.search(r"\bsince\b", question, re.I)
                 and not re.search(r"\b(?:when|as of|at that time)\b", question, re.I))
    if (re.search(r"\bago\b|距今|多久以前", question, re.I) or since_now) and end.get("source") != "TODAY":
        raise ValueError("ago/since requires the caller's TODAY as the interval end")
    if re.search(r"how (?:many (?:calendar )?(?:days|weeks|months|years)|long) until\b", question, re.I) and start.get("source") != "TODAY":
        raise ValueError("until requires the caller's TODAY as the interval start")


def _relative_interval(spec, evidence, unit):
    relation = spec["relation"]
    if not isinstance(relation, dict):
        raise ValueError("relative relation must be an object")
    quote = source_quote(relation, evidence)
    start, end = spec.get("start"), spec.get("end")
    if not isinstance(start, dict) or not isinstance(end, dict):
        raise ValueError("relative interval needs two source-backed actions")
    left, right = source_quote(start, evidence), source_quote(end, evidence)
    for point, text in ((start, left), (end, right)):
        event = point.get("event_text")
        if not isinstance(event, str) or not event.strip() or event not in text:
            raise ValueError("relative interval action is not literal in its quote")
    first, anchor, offset = relation.get("from"), relation.get("to"), relation.get("offset_text")
    if any(not isinstance(v, str) or not v.strip() or v not in quote for v in (first, anchor, offset)):
        raise ValueError("relative interval relation anchors are not literal")
    if re.search(r"\b(about|around|approximately|roughly)\s*$|大约$|约$", quote[:quote.index(offset)], re.I):
        raise ValueError("approximate relative relation cannot prove an exact interval")
    words = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4,
             "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
    m = re.fullmatch(r"(\d+|a|an|one|two|three|four|five|six|seven|eight|nine|ten)\s+"
                     r"(days?|weeks?|months?|years?)\s+(before|after|prior to)", offset, re.I)
    if not m:
        raise ValueError("relative interval needs a literal exact amount/unit and before/after relation")
    amount = int(m.group(1)) if m.group(1).isdigit() else words[m.group(1).casefold()]
    quantity_unit, direction = m.group(2).lower().rstrip("s") + "s", m.group(3).lower()
    # The relation must really connect to the two separately cited actions.
    if first in left and anchor in right:
        sign = -1 if direction == "after" else 1
    elif first in right and anchor in left:
        sign = 1 if direction == "after" else -1
    else:
        raise ValueError("relative anchors do not link the two requested action quotes")
    if quantity_unit == unit:
        value = amount
    elif quantity_unit == "weeks" and unit == "days":
        value = amount * 7
    elif quantity_unit == "days" and unit == "weeks":
        value = amount // 7
    elif quantity_unit == "years" and unit == "months":
        value = amount * 12
    elif quantity_unit == "months" and unit == "years":
        value = amount // 12
    else:
        raise ValueError("relative units cannot be converted without absolute dates")
    return sign * value


def inspect_dates(spec, evidence, today, question):
    audit = {"status": "invalid", "operation": "date_difference", "errors": [],
             "scope": "Source dates, calendar-year labels or recording bounds, the caller clock and arithmetic are checked; action/date qualification remains a model judgment."}
    errors = audit["errors"]
    try:
        unit = spec.get("unit")
        if not isinstance(unit, str):
            raise ValueError("date interval unit must be a string")
        unit = unit.lower().replace("calendar_", "").replace("calendar ", "")
        if unit not in ("days", "weeks", "months", "years"):
            raise ValueError("date interval unit must be days, weeks, months or years")
        requested = re.search(r"how many (?:(?:calendar|complete|completed|whole|full|exact)\s+)*"
                              r"(days|weeks|months|years)\b", question, re.I)
        requested_unit = requested.group(1).lower() if requested else unit
        converted_weeks = unit=='days' and requested_unit=='weeks'
        if requested_unit != unit:
            if (not converted_weeks or spec.get('precision','day')!='day'
                    or spec.get('rounding','complete')!='complete' or 'relation' in spec
                    or 'bound' in spec or any(isinstance(spec.get(k),dict) and
                        'completed_by_recorded_date' in spec[k] for k in ('start','end'))):
                raise ValueError("date interval unit does not match the requested unit")
            audit.update(requested_unit=requested_unit,
                         unit_conversion=dict(source_unit='days',answer_unit='weeks',days_per_week=7))
        audit["unit"] = unit
        precision = spec.get("precision", "day")
        if precision not in ("day", "year"):
            raise ValueError("date interval precision must be day or year")
        rounding = spec.get("rounding", "complete")
        if rounding not in ("complete", "nearest"):
            raise ValueError("date rounding must be complete or nearest")
        if rounding == "nearest" and _EXACT_INTERVAL.search(question):
            raise ValueError("an exact or complete-unit question cannot use nearest rounding")
        bounded = 'bound' in spec or any(isinstance(spec.get(key), dict) and
            'completed_by_recorded_date' in spec[key] for key in ('start', 'end'))
        if bounded and (unit not in ('days','weeks') or not _MINIMUM_INTERVAL.search(question)):
            raise ValueError('recording bounds apply only to an explicitly requested minimum day/week interval')
        if bounded and spec.get('bound') != 'at_least':
            raise ValueError('bound=at_least must be a top-level CALCULATION field alongside operation/unit/start/end/result, not inside start/end')
        if bounded and (precision != 'day' or
                        rounding != 'complete' or 'relation' in spec):
            raise ValueError('recording bounds require day-precision endpoints, complete rounding and no relative relation object')
        if precision == "year":
            if unit != "years" or "relation" in spec or rounding != "complete":
                raise ValueError("year-only endpoints establish approximate year-label differences only")
            if _EXACT_INTERVAL.search(question):
                raise ValueError("year-only evidence cannot establish an exact or complete-unit interval")
            _check_clock_points(spec, question)
            years = [_year_endpoint(spec[key], evidence, today) for key in ("start", "end")]
            if years[0] == years[1]:
                raise ValueError("same-year labels cannot establish a zero-duration interval")
            audit.update(status="checks_passed", computed=str(years[1] - years[0]), entries=2,
                         precision="year", years=years, basis="quoted_calendar_years")
            return audit
        if "relation" in spec:
            if rounding != "complete":
                raise ValueError("nearest rounding requires two complete dated endpoints")
            if re.search(r"\bago\b|\bsince\b|\buntil\b|距今", question, re.I):
                raise ValueError("caller-clock intervals need dated endpoints, not an unanchored relative relation")
            computed = _relative_interval(spec, evidence, unit)
            audit.update(status="checks_passed", computed=str(computed), entries=3,
                         basis="explicit_relative_relation")
            return audit
        _check_clock_points(spec, question)
        if bounded:
            if not isinstance(spec.get('end'), dict) or 'completed_by_recorded_date' in spec['end']:
                raise ValueError('a recording bound needs one bounded start and an exact end')
            start = _completed_recording_bound(spec.get('start'), evidence)
            end = _endpoint(spec['end'], evidence, today)
            if end < start:
                raise ValueError('a recording bound cannot prove a positive interval to an earlier end')
            audit.update(status='checks_passed', computed=str(interval(start, end, unit)), entries=2,
                         bound='at_least', basis='completed_by_recorded_date',
                         endpoints=[start.isoformat(), end.isoformat()],
                         endpoint_roles=['latest_possible_start', 'exact_end'])
            return audit
        endpoints = []
        for key in ("start", "end"):
            point = spec.get(key)
            endpoints.append(_endpoint(point, evidence, today))
        computed = interval(endpoints[0], endpoints[1], unit)
        audit.update(computed=str(computed), entries=2, endpoints=[d.isoformat() for d in endpoints])
        if (unit == "weeks" or converted_weeks) and rounding == "complete":
            elapsed_days = (endpoints[1] - endpoints[0]).days
            complete = interval(endpoints[0], endpoints[1], 'weeks')
            audit.update(rounding="complete", complete_units=complete,
                         elapsed_days=elapsed_days, remainder_days=elapsed_days - complete * 7)
        if rounding == "nearest":
            audit.update(rounded_calendar_interval(endpoints[0], endpoints[1], unit))
        audit["status"] = "checks_passed"
    except (ValueError, TypeError, OverflowError) as exc:
        errors.append(str(exc))
    return audit
