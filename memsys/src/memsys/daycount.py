"""Count literal attendance dates, without assuming an event lasts 24 hours."""
import re
from datetime import date, datetime

from .dateproof import _recorded_date, literal_day, source_quote

_MONTH = (r"January|February|March|April|May|June|July|August|September|October|November|December|"
          r"Jan\.?|Feb\.?|Mar\.?|Apr\.?|Jun\.?|Jul\.?|Aug\.?|Sep\.?|Oct\.?|Nov\.?|Dec\.?")
_DAY = r"\d{1,2}(?:st|nd|rd|th)?"
_DAYS = _DAY + r"(?:(?:\s*,\s*(?:and\s+)?|\s+and\s+)" + _DAY + r")*"


def attendance_dates(point, evidence):
    """Return only dates stated in one literal body date or date-list phrase.

    An omitted year needs an explicit recorded-date anchor. Event qualification,
    speaker attribution and actual attendance remain semantic-review duties.
    """
    if any(key in point for key in ('date', 'relative_text', 'completed_by_recorded_date')):
        raise ValueError('attendance counts need literal body date_text, not interval or recording-bound fields')
    quote = source_quote(point, evidence)
    body = "\n".join(line for line in quote.splitlines() if not re.match(r"^\[[FE]\d+\]", line))
    event, phrase = point.get('event_text'), point.get('date_text')
    if not isinstance(event, str) or not event.strip() or event not in body:
        raise ValueError('attendance date needs a literal body event phrase')
    if (not isinstance(phrase, str) or not phrase.strip() or len(phrase) > 120 or
            not re.search(r'(?<!\w)' + re.escape(phrase) + r'(?!\w)', body)):
        raise ValueError('attendance date_text must be a literal source-body date phrase')
    date_phrase = re.sub(r'^the\s+', '', phrase.strip(), flags=re.I)
    normalized = re.sub(r'(?<=\d)(st|nd|rd|th)\b', '', date_phrase, flags=re.I)
    normalized = re.sub(r'\s+of\s+', ' ', normalized, flags=re.I)
    try:
        actual = literal_day(normalized)
    except ValueError:
        actual = None
    if actual is not None:
        if point.get('anchor') is not None:
            raise ValueError('a complete attendance date does not need a recorded-year anchor')
        return [actual.isoformat()]
    match = re.fullmatch(r'(?P<days>'+_DAYS+r')\s+(?:of\s+)?(?P<month>'+_MONTH+
                         r')(?:\s*,?\s+(?P<year>\d{4}))?', date_phrase, re.I)
    if not match:
        match = re.fullmatch(r'(?P<month>'+_MONTH+r')\s+(?P<days>'+_DAYS+
                             r')(?:\s*,?\s*(?P<year>\d{4}))?', date_phrase, re.I)
    if not match:
        raise ValueError('attendance needs complete dates or an explicit day list with a month')
    year_text = match.group('year')
    if year_text:
        if point.get('anchor') is not None:
            raise ValueError('an explicit attendance year cannot use a recorded-year anchor')
        year = int(year_text)
    else:
        anchor = point.get('anchor')
        if anchor not in ('recorded_at', 'recorded_at_past'):
            raise ValueError('anchor must be the literal string recorded_at or recorded_at_past, never a date value')
        if re.search(r'\b(?:last|next|previous|following) year\b|去年|明年', quote, re.I):
            raise ValueError('a different attendance year cannot use its recording year')
        recorded = _recorded_date(evidence[point['source']])
        year = recorded.year
        if anchor == 'recorded_at_past':
            source = point['source']
            source_body = evidence[source].split('\n', 1)[-1]
            user = source_body.split('\nAssistant: ', 1)[0] if source_body.startswith('User: ') else ''
            if (not re.fullmatch(r'E\d+', source) or event not in user or phrase not in user
                    or re.search(r'\b(?:18|19|20|21)\d{2}\b', body)):
                raise ValueError('recorded_at_past needs an original user event/date without a stated year')
    month_text = match.group('month').rstrip('.')
    try:
        month = datetime.strptime(month_text, '%B').month
    except ValueError:
        month = datetime.strptime(month_text, '%b').month
    days = [int(day) for day in re.findall(r'\d{1,2}', match.group('days'))]
    if not year_text and point.get('anchor') == 'recorded_at_past' and (month, max(days)) > (recorded.month, recorded.day):
        year -= 1
    return [date(year, month, day).isoformat() for day in days]
