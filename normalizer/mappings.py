"""A 추출 라벨 값 -> DB 저장 값 변환 사전.

선택지 코드와 필드 정의의 원본은 DB(question_fields, field_options)이다.
이 파일은 DB에 없는 정보, 즉 '라벨 쪽 이름 -> DB 쪽 이름' 대응만 담는다.
"""

# 1) 필드 이름 바꾸기: (문항 ID, 라벨 필드명) -> DB 필드명
#    여기에 없는 필드는 라벨 이름과 DB 이름이 같다.
FIELD_RENAME = {
    ("Q8_1", "answer"): "days",
    ("Q9_1", "answer"): "days",
    ("Q10", "answer"): "days",
    ("Q8_2", "minutes"): "duration_minutes",
    ("Q9_2", "minutes"): "duration_minutes",
    ("Q6_1", "usage_days"): "use_pattern",
}

# 2) 시간 합산 대상: 라벨의 hours/minutes -> DB duration_minutes (분 단위)
DURATION_ITEMS = {"Q8_2", "Q9_2"}

# 3) 주종: 한국어 표현 -> DB repeat_key
#    DB의 repeat_keys_json: soju, beer, spirits, makgeolli, wine
#    여기에 없는 술(하이볼, 사케 등)은 정책 other_beverage_mapping(PENDING)에 따라
#    임의 분류하지 않고 확인 대상으로 넘긴다.
BEVERAGE_CODE = {
    "소주": "soju",
    "맥주": "beer",
    "생맥주": "beer",
    "양주": "spirits",
    "막걸리": "makgeolli",
    "와인": "wine",
    "포도주": "wine",
}

# 4) 음주량 단위: 한국어 표현 -> DB 선택지 코드 (glass, bottle, can, cc)
DRINK_UNIT_CODE = {
    "잔": "glass",
    "병": "bottle",
    "캔": "can",
    "cc": "cc",
    "CC": "cc",
    "ml": "cc",   # 1 mL = 1 cc, 같은 단위라 환산 없이 코드만 바꾼다
    "mL": "cc",
}

# 5) 주종별 반복 저장 문항 (amounts[i].beverage/amount/unit 구조)
AMOUNT_ITEMS = {"Q7_1", "Q7_2"}

# 6) Q6_1 구간: (선택지 코드, 최소 일수, 최대 일수)
#    daily는 숫자 구간으로 판정하지 않는다.
#    정책 raw_range_before_option: "30일만으로 매일 추정 금지"
#    -> '매일'은 사용자가 직접 말했을 때(value == "daily")만 저장한다.
USAGE_PATTERN_RANGES = [
    ("no", 0, 0),
    ("days_1_2", 1, 2),
    ("days_3_9", 3, 9),
    ("days_10_29", 10, 29),
]
USAGE_PATTERN_ITEM = ("Q6_1", "usage_days")

# 7) 라벨 상태 -> DB resolution_status
#    MISSING -> PARTIAL 대응은 팀 확인 필요 (DB에 MISSING 상태가 없음)
SEMANTIC_TO_RESOLUTION = {
    "MISSING": "PARTIAL",
    "AMBIGUOUS": "AMBIGUOUS",
    "UNCERTAIN": "UNCERTAIN",
}

# 8) 발화 의도 -> DB resolution_status (값 없이 상태만 기록)
INTENT_TO_RESOLUTION = {
    "REFUSED": "REFUSED",
    "REQUEST_SKIP": "DEFERRED",
}