"""사용자 표현을 숫자·선택지 코드로 바꾸는 보조 함수.

LLM이 값을 숫자나 코드로 주면 그대로 쓰고, 문자열로 남기면 여기서 해석한다.
  - to_number: 정확한 숫자 표현 ("3", "세 번", "1갑", "1시간 반")
  - parse_quantity: 범위·어림·기간까지 포함한 해석 (명세 8.2~8.4)
해석할 수 없으면 사유 코드를 돌려주고, 호출한 쪽이 재질문으로 넘긴다.
"""
import re
from dataclasses import dataclass
from typing import Optional

from .mappings import (
    APPROX_PREFIX,
    APPROX_SUFFIX,
    DAY_COUNT_WORDS,
    AT_MOST_SUFFIX,
    BELOW_SUFFIX,
    KOREAN_NUMBERS,
    KOREAN_RANGES,
    LOWER_BOUND_SUFFIX,
    OPTION_SYNONYMS,
    PERIOD_PREFIX,
    UNIT_CONVERSION,
    VAGUE_WORDS,
    WEEK_DAY_WORDS,
    WEEK_DAY_WORDS_APPROX,
    YEAR_WORDS,
    YES_WORDS,
)

# 숫자 뒤에 붙어도 값이 바뀌지 않는 세는 말
_COUNTERS = {"일", "번", "회", "개", "잔", "병", "캔", "명", "주", "번씩", "회씩", "일씩", "일간"}
_NUM_WORDS = sorted(KOREAN_NUMBERS, key=len, reverse=True)
_TOKEN = re.compile(
    r"(\d+(?:\.\d+)?|(?<![가-힣])(?:" + "|".join(_NUM_WORDS) + r"))\s*([가-힣A-Za-z]+)?"
)
# 숫자 뒤 단위 뒤에 붙는 조사·어미 (예: "3일이요", "1갑정도")
_TAIL = re.compile(r"(이요|요|이에요|예요|입니다|정도|쯤|씩|가량|이상|이하|이|가|은|는|을|를)+$")
_ENDING = re.compile(r"\s*(이에요|예요|이요|입니다|요)?[.!?\s]*$")
_RANGE_SPLIT = re.compile(r"\s*(?:~|∼|-|에서|내지)\s*")
_HAS_NUMBER = re.compile(r"\d|(?<![가-힣])(" + "|".join(_NUM_WORDS) + r")(?![가-힣])")


def _clean(text):
    return re.sub(r"\s+", " ", str(text)).strip()


def to_option(fdef, value):
    """선택지 표현 -> DB 코드. DB 라벨, 추가 동의어 순으로 찾고 없으면 None."""
    if value is None or isinstance(value, bool):
        return None
    text = _clean(value)
    if text in fdef.options:
        return text
    if text in fdef.option_labels:
        return fdef.option_labels[text]
    synonyms = OPTION_SYNONYMS.get(fdef.field_id, {})
    if text in synonyms:
        return synonyms[text]
    compact = text.replace(" ", "")
    for table in (fdef.option_labels, synonyms):
        for label, code in table.items():
            if label.replace(" ", "") == compact:
                return code
    return None


def _num(token):
    return float(token) if token[0].isdigit() else float(KOREAN_NUMBERS[token])


def to_number(value, fdef=None):
    """정확한 숫자 표현 -> 숫자. 단위가 있으면 DB 단위로 환산한다. 실패하면 None.

    예) "3" -> 3, "세 번" -> 3, "1갑" -> 20 (개비), "반 갑" -> 10,
        "6개월" -> 0.5 (년), "1시간 반" -> 90 (분), "매일" -> 7 (주당 일수)
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    text = _ENDING.sub("", _clean(value))
    if not text or text in YES_WORDS:            # "네"는 대답(예)으로 본다
        return None
    db_unit = fdef.unit if fdef is not None else None
    if db_unit == "day/week":
        for word, days in WEEK_DAY_WORDS.items():
            if text.replace(" ", "") == word.replace(" ", ""):
                return days
    table = UNIT_CONVERSION.get(db_unit, {})

    total, last_factor, matched = 0.0, None, False
    pos = 0
    for m in _TOKEN.finditer(text):
        if text[pos:m.start()].strip():         # 숫자 사이에 해석 못 하는 말이 있음
            return None
        pos = m.end()
        number, unit = _num(m.group(1)), m.group(2)
        if unit:
            unit = _TAIL.sub("", unit)
        factor = None
        if unit:
            factor = _unit_factor(unit, table)
            if factor is None and unit in _COUNTERS:
                factor = 1
            if factor is None:
                return None                     # 모르는 단위 -> 추정하지 않음
        elif m.group(1) == "반" and last_factor is not None:
            factor = last_factor                # "1시간 반", "한 갑 반"
        else:
            factor = 1
        total += number * factor
        last_factor = factor
        matched = True
    if not matched or text[pos:].strip():
        return None
    return int(total) if float(total).is_integer() else total


def _unit_factor(unit, table):
    for name in sorted(table, key=len, reverse=True):
        if unit == name:
            return table[name]
    return None


# ================================================================ 범위·어림·기간 (8.2~8.4)

@dataclass
class Quantity:
    value: Optional[float] = None
    approximate: bool = False
    low: Optional[float] = None          # 범위로 답한 경우 최소·최대
    high: Optional[float] = None
    period: Optional[str] = None         # "주 3번" -> week
    reason: Optional[str] = None         # 실패 사유: VAGUE_QUANTITY / MISSING_NUMBER / UNPARSED_VALUE

    @property
    def ok(self):
        return self.reason is None and self.value is not None

    @property
    def is_range(self):
        return self.low is not None and self.high is not None and self.low != self.high


def parse_quantity(value, fdef=None, integer=None):
    """숫자·범위·어림·기간 표현 -> Quantity.

    - 범위("2~3일", "두세 번", {"min":2,"max":3})는 중앙값 + approximate (8.2)
    - "~쯤·정도·대략"은 approximate, "~이상·넘게"는 하한, "~미만"은 정수 칸이면 값-1 (8.3)
    - "주 3번", "한 달에 8번"은 period를 함께 돌려준다 (8.4)
    - "가끔·여러 번"은 VAGUE_QUANTITY, 숫자 칸에 "네"는 MISSING_NUMBER (6.2)
    """
    if isinstance(value, bool):
        if value is False and fdef is not None and fdef.unit == "day/week":
            return Quantity(value=0)
        return Quantity(reason="MISSING_NUMBER" if value else "UNPARSED_VALUE")
    if value is None:
        return Quantity(reason="UNPARSED_VALUE")
    if isinstance(value, (int, float)):
        return Quantity(value=value)
    if isinstance(value, dict) or isinstance(value, (list, tuple)):
        bounds = _as_range(value)
        if bounds is None:
            return Quantity(reason="UNPARSED_VALUE")
        low, high = (parse_quantity(b, fdef) for b in bounds)
        if not (low.ok and high.ok):
            return Quantity(reason="UNPARSED_VALUE")
        return _range(low.value, high.value, low.period or high.period)

    text = _ENDING.sub("", _clean(value))
    for word, number in DAY_COUNT_WORDS.items():          # "열흘" -> 10일
        text = text.replace(word, number)
    if not text:
        return Quantity(reason="UNPARSED_VALUE")
    if text in YES_WORDS:
        return Quantity(reason="MISSING_NUMBER")

    period = None
    for prefix, p in sorted(PERIOD_PREFIX, key=lambda x: len(x[0]), reverse=True):
        rest = text[len(prefix):].strip()
        if text.startswith(prefix) and (_HAS_NUMBER.search(rest) or rest.startswith(tuple(KOREAN_RANGES))):
            period, text = p, rest
            break

    if fdef is not None and fdef.unit == "day/week":
        compact = text.replace(" ", "")
        for word, days in WEEK_DAY_WORDS.items():
            if compact == word.replace(" ", ""):
                return Quantity(value=days, approximate=word in WEEK_DAY_WORDS_APPROX)
    if fdef is not None and fdef.unit == "년":
        for word, years in sorted(YEAR_WORDS.items(), key=lambda x: len(x[0]), reverse=True):
            if text.startswith(word):
                return Quantity(value=years, approximate=True)

    if not _HAS_NUMBER.search(text) and not any(text.startswith(k) for k in KOREAN_RANGES):
        if any(w in text for w in VAGUE_WORDS):
            return Quantity(reason="VAGUE_QUANTITY")
        return Quantity(reason="UNPARSED_VALUE")

    approximate, bound = False, None
    for prefix in APPROX_PREFIX:
        rest = text[len(prefix):]
        if text.startswith(prefix) and rest and (rest[0].isdigit() or rest.startswith(tuple(KOREAN_RANGES))):
            text, approximate = rest.strip(), True
            break
    text, approximate, bound = _strip_suffix(text, approximate)

    q = _parse_range_text(text, fdef)
    if q is None:
        number = to_number(text, fdef)
        if number is None:
            reason = "VAGUE_QUANTITY" if any(w in text for w in VAGUE_WORDS) else "UNPARSED_VALUE"
            return Quantity(reason=reason)
        q = Quantity(value=number)
    q.period = q.period or period
    q.approximate = q.approximate or approximate

    if bound == "lower":
        q.approximate = True
    elif bound == "below":
        q.approximate = True
        if (integer or _is_integer_field(fdef)) and float(q.value).is_integer():
            q.value -= 1
    elif bound == "at_most":
        q.approximate = True
    return q


def _strip_suffix(text, approximate):
    bound = None
    changed = True
    while changed:
        changed = False
        for words, kind in ((LOWER_BOUND_SUFFIX, "lower"), (BELOW_SUFFIX, "below"),
                            (AT_MOST_SUFFIX, "at_most"), (APPROX_SUFFIX, "approx")):
            for w in sorted(words, key=len, reverse=True):
                if text.endswith(w):
                    text = text[: -len(w)].strip()
                    if kind == "approx":
                        approximate = True
                    else:
                        bound = kind
                    changed = True
                    break
    return text, approximate, bound


def _parse_range_text(text, fdef):
    for word, (lo, hi) in sorted(KOREAN_RANGES.items(), key=lambda x: len(x[0]), reverse=True):
        if text.startswith(word):
            rest = text[len(word):].strip()
            low, high = to_number(f"{lo}{rest}", fdef), to_number(f"{hi}{rest}", fdef)
            if low is None or high is None:
                return None
            return _range(low, high)
    parts = _RANGE_SPLIT.split(text)
    if len(parts) != 2 or not all(parts):
        return None
    left, right = parts
    unit = re.search(r"[가-힣A-Za-z]+$", right)
    high = to_number(right, fdef)
    low = to_number(left, fdef)
    if unit and re.fullmatch(r"\d+(?:\.\d+)?", left.strip()):
        low = to_number(f"{left}{unit.group(0)}", fdef)       # "1~2시간" -> 1시간~2시간
    if low is None or high is None:
        return None
    return _range(low, high)


def _range(low, high, period=None):
    low, high = min(low, high), max(low, high)
    return Quantity(value=(low + high) / 2, approximate=True, low=low, high=high, period=period)


def _is_integer_field(fdef):
    return fdef is not None and (fdef.data_type == "integer" or fdef.unit == "day/week")


def _as_range(value):
    """범위 표현을 (최소, 최대)로. 지원 형식: {"min","max"}, {"low","high"}, [a, b]."""
    if isinstance(value, dict):
        for lo, hi in (("min", "max"), ("low", "high"), ("start", "end"), ("from", "to")):
            if lo in value and hi in value:
                return value[lo], value[hi]
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return value[0], value[1]
    return None
