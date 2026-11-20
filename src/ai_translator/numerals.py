"""Recognize standalone Chinese integers and common numeric dates and times."""

import re
import unicodedata

DIGITS = {
    **{character: str(index) for index, character in enumerate("零一二三四五六七八九")},
    **{character: str(index) for index, character in enumerate("〇壹贰叁肆伍陆柒捌玖")},
    "两": "2",
}
UNITS = {"十": 10, "拾": 10, "百": 100, "佰": 100, "千": 1000, "仟": 1000,
         "万": 10000, "亿": 100000000}
NUMBER = rf"[0-9{''.join(DIGITS)}{''.join(UNITS)}]+"
DATE_TIME = re.compile(
    rf"(?:(?P<year>{NUMBER})年)?(?:(?P<month>{NUMBER})月)?(?:(?P<day>{NUMBER})[日号])?"
    rf"(?:(?P<period>凌晨|早上|上午|中午|下午|傍晚|晚上)?(?P<hour>{NUMBER})[点时]"
    rf"(?:(?P<minute>{NUMBER})分?|(?P<half>半))?(?:(?P<second>{NUMBER})秒)?)?"
)


def _integer(text: str) -> int:
    if not any(character in UNITS for character in text):
        return int("".join(DIGITS.get(character, character) for character in text))
    result = group = 0
    digit = None
    for character in text:
        if character not in UNITS:
            number = int(DIGITS.get(character, character))
            digit = (digit or 0) * 10 + number
            continue
        unit = UNITS[character]
        if unit < 10000:
            group += (1 if digit is None else digit) * unit
        elif unit == 10000:
            group = (group + (digit or 0) or 1) * unit
        else:
            result += (group + (digit or 0) or 1) * unit
            group = 0
        digit = None
    return result + group + (digit or 0)


def equivalent_numeric(source: str, translation: str) -> bool:
    source = unicodedata.normalize("NFKC", source).strip().rstrip("。！!？?．")
    target = unicodedata.normalize("NFKC", translation).strip()
    if len(source) > 120 or len(target) > 120:
        return False
    if re.fullmatch(NUMBER, source):
        # Decimal commas and incorrectly grouped digits change the meaning in Russian.
        if re.fullmatch(r"[0-9]{1,3}(?: [0-9]{3})+", target):
            target = target.replace(" ", "")
        return bool(target.isdecimal() and int(target) == _integer(source))

    match = DATE_TIME.fullmatch(source)
    if not match or not source:
        return False
    parts = match.groupdict()
    date_patterns = {
        ("year", "month", "day"): (("day.month.year", r"\."), ("year-month-day", "-")),
        ("month", "day"): (("day.month", r"\."),),
        ("year", "month"): (("month.year", r"\."), ("year-month", "-")),
        ("year",): (("year", r"\."),),
        ("month",): (("month", r"\."),),
        ("day",): (("day", r"\."),),
        (): (("", r"\."),),
    }
    fields = tuple(key for key in ("year", "month", "day") if parts[key])
    date_formats = date_patterns.get(fields, ())
    time = []
    if parts["hour"]:
        hour = _integer(parts["hour"])
        if hour == 12 and parts["period"] in ("凌晨", "晚上"):
            hour = 0
        elif parts["period"] in ("下午", "傍晚", "晚上") and hour < 12:
            hour += 12
        minute = 30 if parts["half"] else _integer(parts["minute"] or "0")
        time = [hour, minute]
        if parts["second"]:
            time.append(_integer(parts["second"]))
    for date_format, separator in date_formats:
        ordering = re.findall(r"[a-z]+", date_format)
        date = [_integer(parts[key]) for key in ordering]
        date_pattern = separator.join(r"[0-9]{1,4}" if key == "year" else r"[0-9]{1,2}" for key in ordering)
        if len(time) == 3:
            time_pattern = r"[0-9]{1,2}:[0-9]{2}:[0-9]{2}"
        elif time:
            time_pattern = r"[0-9]{1,2}(?::[0-9]{2})?" if time[1] == 0 else r"[0-9]{1,2}:[0-9]{2}"
        else:
            time_pattern = ""
        pattern = date_pattern + (r"(?:,? +|T)" if date and time else "") + time_pattern
        if not pattern or not re.fullmatch(pattern, target):
            continue
        numbers = [int(value) for value in re.findall(r"[0-9]+", target)]
        if numbers == date + time or (len(time) == 2 and time[1] == 0 and numbers == date + time[:1]):
            return True
    return False


def looks_numeric(text: str) -> bool:
    normalized = unicodedata.normalize("NFKC", text).strip()
    return bool(re.fullmatch(r"[0-9 .,:/–—+\-]+(?:[Tt][0-9 :.+\-]+[Zz]?)?", normalized)
                and any(character.isdecimal() for character in normalized))
