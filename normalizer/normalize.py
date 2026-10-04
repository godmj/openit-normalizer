"""③ 규칙 처리: A 추출 라벨 -> DB 저장 형식으로 정규화·매핑·검증.

입력:  A 추출 JSON {"facts", "intents", "relations", "unmapped_facts"}
출력:  NormalizationResult
        - records: DB answer_revisions 한 행에 대응하는 AnswerRecord 목록
          (CONFIRMED면 값 1개, 아니면 값 없이 상태와 사유만)
        - intents / unmapped_facts: 다음 단계(LangGraph)로 그대로 전달

원칙 (DB 운영 정책)
  never_guess              모름·거부·미응답을 false/0으로 바꾸지 않는다
  preserve_period_unit     기간 단위를 임의로 환산하지 않는다
  raw_range_before_option  범위 전체가 한 선택지에 들어갈 때만 매핑한다
  one_unit_per_beverage    같은 주종은 한 단위로만 기록, 섞이면 확인한다
  other_beverage_mapping   기타 술은 임의 분류하지 않는다 (PENDING)
"""
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from .catalog import Catalog, FieldDef
from .mappings import (
    AMOUNT_ITEMS,
    BEVERAGE_CODE,
    DRINK_UNIT_CODE,
    DURATION_ITEMS,
    FIELD_RENAME,
    INTENT_TO_RESOLUTION,
    SEMANTIC_TO_RESOLUTION,
    USAGE_PATTERN_ITEM,
    USAGE_PATTERN_RANGES,
)

# 사용자에게 다시 물어봐야 하는 사유 (그 외 REFUSED/DEFERRED/UNCERTAIN은 보류)
CLARIFY_STATUSES = {"PARTIAL", "AMBIGUOUS", "CONFLICT"}

_AMOUNT_FIELD = re.compile(r"^amounts\[(\d+)\]\.(beverage|amount|unit)$")


@dataclass
class AnswerRecord:
    item_id: str                       # 문항 ID, 예: "Q7_1"
    field_id: str                      # DB field_id, 예: "Q7_1.amount"
    repeat_key: Optional[str]          # 주종 등 반복 키, 없으면 "" / 정할 수 없으면 None
    resolution_status: str             # CONFIRMED / PARTIAL / AMBIGUOUS / ...
    value_boolean: Optional[int] = None
    value_number: Optional[float] = None
    value_option: Optional[str] = None
    value_text: Optional[str] = None
    precision: str = "exact"           # DB 허용값: exact / approximate / unspecified
    change_reason: str = "NEW_INFORMATION"   # 또는 CORRECTION
    reason: Optional[str] = None       # 미확정 사유 코드
    raw_value: Any = None              # 라벨 원래 값 (사후 검증용)
    sources: list = field(default_factory=list)   # 라벨 키, 예: "Q7_1/amounts[0].amount"
    evidence: list = field(default_factory=list)  # 사용자 원문 근거

    @property
    def confirmed(self):
        return self.resolution_status == "CONFIRMED"

    @property
    def needs_clarify(self):
        return self.resolution_status in CLARIFY_STATUSES

    @property
    def value(self):
        for v in (self.value_boolean, self.value_number, self.value_option, self.value_text):
            if v is not None:
                return v
        return None


@dataclass
class NormalizationResult:
    records: list = field(default_factory=list)
    intents: list = field(default_factory=list)
    unmapped_facts: list = field(default_factory=list)

    def confirmed(self):
        return [r for r in self.records if r.confirmed]

    def unresolved(self):
        return [r for r in self.records if not r.confirmed]

    def find(self, field_id, repeat_key=""):
        for r in self.records:
            if r.field_id == field_id and r.repeat_key == repeat_key:
                return r
        return None


# ---------------------------------------------------------------- 공개 함수

def normalize(labels: dict, catalog: Catalog) -> NormalizationResult:
    result = NormalizationResult(
        intents=list(labels.get("intents", [])),
        unmapped_facts=list(labels.get("unmapped_facts", [])),
    )
    corrections, conflicts = _read_relations(labels.get("relations", []))

    simple, durations, amounts = [], {}, {}
    for fact in labels.get("facts", []):
        item, name = fact["item_id"], fact["field"]
        m = _AMOUNT_FIELD.match(name)
        if item in AMOUNT_ITEMS and m:
            amounts.setdefault((item, int(m.group(1))), {})[m.group(2)] = fact
        elif item in DURATION_ITEMS and name in ("hours", "minutes"):
            durations.setdefault(item, {})[name] = fact
        else:
            simple.append(fact)

    for fact in simple:
        result.records.append(_normalize_simple(fact, catalog))
    for item, parts in durations.items():
        result.records.append(_normalize_duration(item, parts, catalog))
    result.records.extend(_normalize_amounts(amounts, catalog))
    result.records.extend(_records_from_intents(result.intents, catalog))

    for rec in result.records:
        if rec.item_id in conflicts and rec.resolution_status not in ("REFUSED", "DEFERRED"):
            _unresolve(rec, "CONFLICT", "CONFLICT_WITH_PREVIOUS")
        if any(src in corrections for src in rec.sources):
            rec.change_reason = "CORRECTION"
    return result


# ---------------------------------------------------------------- 단계별 처리

def _read_relations(relations):
    corrections, conflicts = set(), set()
    for rel in relations:
        if rel.get("relation") == "CORRECTION":
            tgt = rel.get("correction_target") or {}
            corrections.add(f"{tgt.get('item_id', rel['item_id'])}/{tgt.get('field', '')}")
        elif rel.get("relation") == "CONFLICT":
            conflicts.add(rel["item_id"])
    return corrections, conflicts


def _normalize_simple(fact, catalog):
    item, name = fact["item_id"], fact["field"]
    db_name = FIELD_RENAME.get((item, name), name)
    fdef = catalog.get(item, db_name)
    rec = _base_record(fact, f"{item}.{db_name}", "")

    if not _is_answered(fact, rec):
        return rec
    if fdef is None:
        return _unresolve(rec, "AMBIGUOUS", "UNKNOWN_FIELD")
    if (item, name) == USAGE_PATTERN_ITEM:
        return _map_usage_pattern(rec, fact, fdef)
    return _store(rec, fdef, fact["value"], fact.get("precision", "exact"))


def _map_usage_pattern(rec, fact, fdef):
    """Q6_1: 사용 일수(숫자/범위) -> use_pattern 선택지."""
    value, precision = fact["value"], fact.get("precision", "exact")

    if isinstance(value, str):                      # 사용자가 '매일', '아니요'를 직접 말한 경우
        return _store(rec, fdef, value, "exact")
    if precision == "approximate":                  # '2일쯤' -> 경계 판단 불가
        return _unresolve(rec, "AMBIGUOUS", "APPROXIMATE_VALUE")

    bounds = _as_range(value) if precision == "range" else (value, value)
    if bounds is None or not all(_is_number(b) for b in bounds):
        return _unresolve(rec, "AMBIGUOUS", "TYPE_MISMATCH")

    low, high = bounds
    opt_low, opt_high = _usage_option(low), _usage_option(high)
    if opt_low is None or opt_high is None:
        return _unresolve(rec, "AMBIGUOUS", "NO_MATCHING_OPTION")      # 예: 30일
    if opt_low != opt_high:
        return _unresolve(rec, "AMBIGUOUS", "RANGE_SPANS_OPTIONS")     # 예: 8~12일
    return _store(rec, fdef, opt_low, "exact")


def _usage_option(days):
    for code, low, high in USAGE_PATTERN_RANGES:
        if low <= days <= high:
            return code
    return None


def _normalize_duration(item, parts, catalog):
    """Q8_2, Q9_2: hours + minutes -> duration_minutes."""
    fdef = catalog.get(item, "duration_minutes")
    facts = list(parts.values())
    rec = _base_record(facts[0], f"{item}.duration_minutes", "")
    rec.sources = [f"{f['item_id']}/{f['field']}" for f in facts]
    rec.evidence = [f.get("evidence") for f in facts]
    rec.raw_value = {k: f.get("value") for k, f in parts.items()}

    for f in facts:
        if not _is_answered(f, rec):
            return rec
    if any(f.get("precision") == "range" for f in facts):
        return _unresolve(rec, "AMBIGUOUS", "RANGE_NOT_STORABLE")

    hours = parts.get("hours", {}).get("value", 0) or 0
    minutes = parts.get("minutes", {}).get("value", 0) or 0
    if not (_is_number(hours) and _is_number(minutes)):
        return _unresolve(rec, "AMBIGUOUS", "TYPE_MISMATCH")
    precision = "approximate" if any(f.get("precision") == "approximate" for f in facts) else "exact"
    return _store(rec, fdef, hours * 60 + minutes, precision)


def _normalize_amounts(groups, catalog):
    """Q7_1, Q7_2: amounts[i].{beverage, amount, unit} -> 주종별 amount/unit 2행."""
    records = []
    for (item, _idx), parts in sorted(groups.items()):
        bev_fact = parts.get("beverage")
        repeat_key, bev_reason, bev_status = None, None, None
        if bev_fact is None or bev_fact.get("semantic_status") != "ANSWERED" \
                or bev_fact.get("value") is None:
            bev_status = SEMANTIC_TO_RESOLUTION.get(
                (bev_fact or {}).get("semantic_status"), "PARTIAL")
            bev_reason = "BEVERAGE_MISSING"
        else:
            repeat_key = BEVERAGE_CODE.get(str(bev_fact["value"]).strip())
            if repeat_key is None:
                bev_status, bev_reason = "AMBIGUOUS", "OTHER_BEVERAGE"   # 정책 PENDING

        for name in ("amount", "unit"):
            fdef = catalog.get(item, name)
            fact = parts.get(name)
            rec = _base_record(fact or bev_fact or {"item_id": item, "field": name},
                               f"{item}.{name}", repeat_key)
            if bev_fact is not None:
                rec.sources.append(f"{item}/{bev_fact['field']}")
                rec.evidence.append(bev_fact.get("evidence"))
            if bev_reason:
                rec.raw_value = {"beverage": (bev_fact or {}).get("value"),
                                 name: (fact or {}).get("value")}
                records.append(_unresolve(rec, bev_status, bev_reason))
                continue
            if fact is None:
                records.append(_unresolve(rec, "PARTIAL", "MISSING"))
                continue
            if not _is_answered(fact, rec):
                records.append(rec)
                continue
            if repeat_key not in fdef.repeat_keys:
                records.append(_unresolve(rec, "AMBIGUOUS", "INVALID_REPEAT_KEY"))
                continue

            value = fact["value"]
            if name == "unit":
                code = DRINK_UNIT_CODE.get(str(value).strip())
                if code is None:
                    records.append(_unresolve(rec, "AMBIGUOUS", "UNKNOWN_UNIT"))
                    continue
                value = code
            records.append(_store(rec, fdef, value, fact.get("precision", "exact")))

    _check_one_unit_per_beverage(records)
    return records


def _check_one_unit_per_beverage(records):
    """같은 문항에서 같은 주종이 두 번 나오면 (예: 소주 1병이랑 2잔) 확인 대상으로."""
    seen = {}
    for rec in records:
        if rec.repeat_key:
            seen.setdefault((rec.field_id, rec.repeat_key), []).append(rec)
    for recs in seen.values():
        if len(recs) > 1:
            for rec in recs:
                _unresolve(rec, "AMBIGUOUS", "MIXED_UNIT")


def _records_from_intents(intents, catalog):
    """거절·보류 의도 -> 해당 문항 필드들을 REFUSED/DEFERRED로 기록 (값 없음)."""
    records = []
    for intent in intents:
        status = INTENT_TO_RESOLUTION.get(intent.get("intent"))
        if status is None:            # ASK_PURPOSE, OFF_TOPIC 등은 기록 없이 전달만
            continue
        for fdef in catalog.fields.values():
            if fdef.item_id == intent["item_id"]:
                rec = AnswerRecord(
                    item_id=fdef.item_id, field_id=fdef.field_id, repeat_key="", resolution_status=status,
                    precision="unspecified", reason=intent["intent"],
                    sources=[f"{intent['item_id']}/intent"],
                    evidence=[intent.get("evidence")],
                )
                records.append(rec)
    return records


# ---------------------------------------------------------------- 저장·검증

def _store(rec, fdef, value, precision):
    """타입·선택지·범위를 DB 정의로 검증한 뒤 맞는 칸 하나에 값을 넣는다."""
    if precision == "range":
        return _unresolve(rec, "AMBIGUOUS", "RANGE_NOT_STORABLE")
    if precision not in ("exact", "approximate"):
        precision = "exact"

    dtype = fdef.data_type
    if dtype == "boolean":
        if not isinstance(value, bool):
            return _unresolve(rec, "AMBIGUOUS", "TYPE_MISMATCH")
        rec.value_boolean = int(value)
    elif dtype in ("integer", "number"):
        if not _is_number(value):
            return _unresolve(rec, "AMBIGUOUS", "TYPE_MISMATCH")
        if dtype == "integer" and float(value) != int(value):
            return _unresolve(rec, "AMBIGUOUS", "TYPE_MISMATCH")
        if not _within(value, fdef.constraints):
            return _unresolve(rec, "AMBIGUOUS", "OUT_OF_RANGE")
        rec.value_number = value
    elif dtype == "option":
        if value not in fdef.options:
            return _unresolve(rec, "AMBIGUOUS", "INVALID_OPTION")
        rec.value_option = value
    else:
        if not isinstance(value, str):
            return _unresolve(rec, "AMBIGUOUS", "TYPE_MISMATCH")
        rec.value_text = value

    rec.resolution_status = "CONFIRMED"
    rec.precision = precision
    rec.reason = None
    return rec


def _within(value, cons):
    if "minimum" in cons and value < cons["minimum"]:
        return False
    if "maximum" in cons and value > cons["maximum"]:
        return False
    if "exclusive_minimum" in cons and value <= cons["exclusive_minimum"]:
        return False
    if "exclusive_maximum" in cons and value >= cons["exclusive_maximum"]:
        return False
    return True


# ---------------------------------------------------------------- 보조 함수

def _base_record(fact, field_id, repeat_key):
    return AnswerRecord(
        item_id=fact["item_id"],
        field_id=field_id,
        repeat_key=repeat_key,
        resolution_status="UNASKED",
        raw_value=fact.get("value"),
        sources=[f"{fact['item_id']}/{fact['field']}"],
        evidence=[fact.get("evidence")],
    )


def _is_answered(fact, rec):
    """ANSWERED가 아니거나 값이 없으면 rec을 미확정으로 바꾸고 False."""
    status = fact.get("semantic_status")
    if status == "ANSWERED" and fact.get("value") is not None:
        return True
    _unresolve(rec, SEMANTIC_TO_RESOLUTION.get(status, "PARTIAL"), status or "MISSING")
    return False


def _unresolve(rec, status, reason):
    rec.resolution_status = status
    rec.reason = reason
    rec.precision = "unspecified"
    rec.value_boolean = rec.value_number = rec.value_option = rec.value_text = None
    return rec


def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _as_range(value):
    """범위 표현을 (최소, 최대)로. 지원 형식: {"min","max"}, {"low","high"}, [a, b]."""
    if isinstance(value, dict):
        for lo, hi in (("min", "max"), ("low", "high"), ("start", "end")):
            if lo in value and hi in value:
                return value[lo], value[hi]
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return value[0], value[1]
    return None