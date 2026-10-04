"""설문 DB(SQLite)에서 필드 정의와 선택지를 읽어오는 모듈.

선택지 코드·타입·제약조건은 코드에 직접 적지 않고 항상 DB에서 읽는다.
그래야 문진표 버전이 바뀌어도 정규화 코드를 고치지 않아도 된다.
"""
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_DB = (
    Path(__file__).resolve().parent.parent
    / "dataset" / "db" / "questionnaire_reference.sqlite3"
)


@dataclass(frozen=True)
class FieldDef:
    field_id: str            # 예: "Q7_1.amount"
    item_id: str             # 예: "Q7_1"
    field_name: str          # 예: "amount"
    data_type: str           # boolean / integer / number / option
    constraints: dict        # 예: {"minimum": 0, "maximum": 7}
    repeat_keys: tuple       # 예: ("soju", "beer", ...) 또는 ("",)
    options: frozenset = field(default_factory=frozenset)  # 선택지 코드


class Catalog:
    def __init__(self, db_path=DEFAULT_DB, version_id="national_health_2026_v1"):
        self.version_id = version_id
        self.fields = {}
        with sqlite3.connect(db_path) as conn:
            options = {}
            for field_id, code in conn.execute(
                "SELECT field_id, option_code FROM field_options WHERE version_id=?",
                (version_id,),
            ):
                options.setdefault(field_id, set()).add(code)
            for field_id, item_id, name, dtype, cons, keys in conn.execute(
                "SELECT field_id, item_id, field_name, data_type, constraints_json,"
                " repeat_keys_json FROM question_fields WHERE version_id=?",
                (version_id,),
            ):
                self.fields[field_id] = FieldDef(
                    field_id=field_id,
                    item_id=item_id,
                    field_name=name,
                    data_type=dtype,
                    constraints=json.loads(cons or "{}"),
                    repeat_keys=tuple(json.loads(keys or '[""]')),
                    options=frozenset(options.get(field_id, ())),
                )
        if not self.fields:
            raise ValueError(f"DB에 {version_id} 필드 정의가 없습니다: {db_path}")

    def get(self, item_id, field_name):
        return self.fields.get(f"{item_id}.{field_name}")