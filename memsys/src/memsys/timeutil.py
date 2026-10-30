"""时间工具。记忆系统里的"现在"永远由调用方传进来，这里只做解析、格式化和日期计算。"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

# LongMemEval 的时间戳形如 "2023/07/01 (Sat) 23:13"；也接受 ISO 写法
_STAMP = re.compile(
    r"^\s*(\d{4})[/-](\d{1,2})[/-](\d{1,2})(?:\s*\([A-Za-z]+\))?"
    r"(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?\s*$")
_SPAN = re.compile(r"^\s*(\d{4})(?:[/-](\d{1,2})(?:[/-](\d{1,2}))?)?\s*$")
_SHIFT = re.compile(r"^\s*([+-]?)(\d+)\s*([hdwmy])\s*$", re.IGNORECASE)


def parse_datetime(text: str) -> datetime:
    """解析 "2023/07/01 (Sat) 23:13"、"2023-07-01"、"2023-07-01T10:00" 等写法。"""
    match = _STAMP.match(text)
    if not match:
        raise ValueError(f"unrecognized date: {text!r}")
    y, mo, d, h, mi, s = match.groups()
    return datetime(int(y), int(mo), int(d), int(h or 0), int(mi or 0), int(s or 0))


def to_iso(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat()


def from_iso(text: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(text) if text else None


def format_day(value: date) -> str:
    """2023/07/01 (Saturday)：给模型看的日期带上星期几，方便它换算"上周二"这类说法。"""
    return value.strftime("%Y/%m/%d (%A)")


def format_stamp(value: datetime) -> str:
    return value.strftime("%Y/%m/%d (%A) %H:%M")


@dataclass(frozen=True)
class DateSpan:
    """一个日期区间及其精度：day、month 或 year。"""

    start: date
    end: date
    precision: str

    @classmethod
    def day(cls, value: date) -> DateSpan:
        return cls(value, value, "day")

    def label(self) -> str:
        if self.precision == "year":
            return "%04d" % self.start.year
        if self.precision == "month":
            return "%04d-%02d" % (self.start.year, self.start.month)
        return self.start.isoformat()

    def overlaps(self, other: DateSpan, slack_days: int = 0) -> bool:
        slack = timedelta(days=slack_days)
        return self.start - slack <= other.end and other.start - slack <= self.end


def parse_span(text: Optional[str]) -> Optional[DateSpan]:
    """解析 "2023-05-20"、"2023-05"、"2023"（也接受斜杠）；不合法返回 None。"""
    if not text or not isinstance(text, str):
        return None
    match = _SPAN.match(text)
    if not match:
        return None
    y, mo, d = match.groups()
    try:
        year = int(y)
        if d:
            day = date(year, int(mo), int(d))
            return DateSpan(day, day, "day")
        if mo:
            month = int(mo)
            last = calendar.monthrange(year, month)[1]
            return DateSpan(date(year, month, 1), date(year, month, last), "month")
        return DateSpan(date(year, 1, 1), date(year, 12, 31), "year")
    except ValueError:
        return None


def days_between(earlier: date, later: date) -> int:
    return (later - earlier).days


def describe_offset(span: DateSpan, today: date) -> str:
    """相对"今天"的距离。日期差在代码里算好，模型只需要读，不需要自己做日期加减。"""
    if span.precision == "day":
        delta = (today - span.start).days
        if delta == 0:
            return "today"
        if delta > 0:
            return "1 day ago" if delta == 1 else "%d days ago" % delta
        return "in 1 day" if delta == -1 else "in %d days" % -delta
    if span.precision == "month":
        months = (today.year - span.start.year) * 12 + today.month - span.start.month
        if months == 0:
            return "this month"
        if months > 0:
            return "1 month ago" if months == 1 else "about %d months ago" % months
        return "in about %d months" % -months
    years = today.year - span.start.year
    if years == 0:
        return "this year"
    return "about %d years ago" % years if years > 0 else "in about %d years" % -years


def add_months(value: datetime, months: int) -> datetime:
    month_index = value.month - 1 + months
    year, month = value.year + month_index // 12, month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def shift(value: datetime, spec: str) -> datetime:
    """按 "+3w"、"-2d"、"+1m"、"+1y"、"+5h" 推移时间，给模拟时钟用。"""
    match = _SHIFT.match(spec)
    if not match:
        raise ValueError(f"unrecognized time shift: {spec!r} (use e.g. +3d, +2w, +1m, +1y)")
    sign, amount, unit = match.groups()
    n = -int(amount) if sign == "-" else int(amount)
    unit = unit.lower()
    if unit == "h":
        return value + timedelta(hours=n)
    if unit == "d":
        return value + timedelta(days=n)
    if unit == "w":
        return value + timedelta(weeks=n)
    if unit == "m":
        return add_months(value, n)
    return add_months(value, 12 * n)
