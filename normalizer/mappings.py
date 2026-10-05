"""정규화 규칙표: 라벨(사용자 표현) -> DB 저장 값.

선택지 코드·타입·범위의 원본은 DB(question_fields, field_options)이다.
DB 선택지 라벨(예: week='주', glass='잔')은 catalog가 자동으로 역매핑하므로,
이 파일에는 DB 라벨에 없는 '사용자 표현'과 정규화 담당이 정한 규칙만 둔다.
각 규칙 옆의 (n.n)은 「자연어 응답 정규화 규칙 명세」의 절 번호다.

규칙 버전: normalizer-rules-0.4 (2026-10-05, 신민지)
"""

# ------------------------------------------------------------ 1) 필드 이름 (8.6)
# LoRA-A가 쓴 필드 이름 -> DB 필드명. 문항별 별칭. 여기에 없으면 이름이 같다.
FIELD_ALIASES = {
    "Q8_1": {"answer": "days", "day_count": "days", "usage_days": "days"},
    "Q9_1": {"answer": "days", "day_count": "days", "usage_days": "days"},
    "Q10": {"answer": "days", "day_count": "days", "usage_days": "days"},
    "Q8_2": {"minutes": "duration_minutes", "duration": "duration_minutes", "time": "duration_minutes"},
    "Q9_2": {"minutes": "duration_minutes", "duration": "duration_minutes", "time": "duration_minutes"},
    "Q6_1": {"usage_days": "use_pattern", "answer": "use_pattern", "days": "use_pattern"},
    "Q7": {"count": "frequency", "times": "frequency", "period": "unit"},
}

DURATION_ITEMS = {"Q8_2", "Q9_2"}          # hours + minutes -> duration_minutes
AMOUNT_ITEMS = {"Q7_1", "Q7_2"}            # amounts[i].beverage/amount/unit
USAGE_PATTERN_FIELD = "Q6_1.use_pattern"

# ------------------------------------------------------------ 2) 주종 (4.1~4.3)
# DB repeat_key: soju, beer, spirits, makgeolli, wine
# 4.1 문진표 5주종이거나 같은 술의 다른 이름 -> 그 칸 (도수와 무관)
BEVERAGE_CODE = {
    "소주": "soju", "참이슬": "soju", "처음처럼": "soju", "진로": "soju", "새로": "soju",
    "전통소주": "soju", "안동소주": "soju", "과일소주": "soju",
    "맥주": "beer", "생맥주": "beer", "병맥주": "beer", "캔맥주": "beer", "라거": "beer",
    "에일": "beer", "IPA": "beer", "흑맥주": "beer", "수입맥주": "beer", "수제맥주": "beer",
    "하이트": "beer", "카스": "beer", "테라": "beer",
    "양주": "spirits", "위스키": "spirits", "보드카": "spirits", "데킬라": "spirits",
    "진": "spirits", "럼": "spirits", "코냑": "spirits", "브랜디": "spirits",
    "막걸리": "makgeolli", "동동주": "makgeolli", "탁주": "makgeolli",
    "와인": "wine", "포도주": "wine", "레드와인": "wine", "화이트와인": "wine",
    "샴페인": "wine", "스파클링와인": "wine",
}

# 4.3 혼합주·기타 술 -> 추정 도수(%). 칸은 도수로 정한다 (4.2).
ABV_BY_NAME = {
    "하이볼": 7.0,
    "소맥": 8.25, "폭탄주": 8.25,        # 소주 17% 3 : 맥주 4.5% 7 가정 (4.5)
    "매실주": 14.0,
    "사케": 15.0, "청주": 15.0, "정종": 15.0, "복분자주": 15.0,
    "고량주": 50.0, "백주": 50.0,
}

# 4.2 칸별 대표 도수와 넣는 도수 범위 [최소, 최대)
ABV_BY_BEVERAGE = {"beer": 4.5, "makgeolli": 6.0, "wine": 12.5, "soju": 17.0, "spirits": 40.0}
ABV_BANDS = [
    ("beer", 0, 5), ("makgeolli", 5, 9), ("wine", 9, 15), ("soju", 15, 28), ("spirits", 28, 101),
]


def cell_for_abv(abv):
    """도수(%) -> 문진표 주종 칸 (4.2)."""
    for key, low, high in ABV_BANDS:
        if low <= abv < high:
            return key
    return None


# ------------------------------------------------------------ 3) 음주량 단위 (4.4, 8.5)
# 사용자 단위 -> (DB 단위 코드, 곱할 값)
DRINK_UNIT = {
    "잔": ("glass", 1), "글라스": ("glass", 1), "샷": ("glass", 1), "컵": ("glass", 1),
    "병": ("bottle", 1), "보틀": ("bottle", 1),
    "캔": ("can", 1),
    "cc": ("cc", 1), "CC": ("cc", 1), "ml": ("cc", 1), "mL": ("cc", 1), "㎖": ("cc", 1),
    "L": ("cc", 1000), "l": ("cc", 1000), "리터": ("cc", 1000),
}
# "n개" -> 병 (8.5). 병·캔 차이가 2배 미만이고 위험을 낮게 잡지 않는 큰 쪽.
PIECE_UNIT = {"개": {"beer": "bottle", "soju": "bottle", "makgeolli": "bottle"}}
# 막걸리 "한 사발" -> 1잔 (8.5)
BOWL_UNIT = {"사발": {"makgeolli": "glass"}}
# 피처 (8.5): 크기를 말하지 않으면 실측 1,700~2,700cc의 중앙값, 말하면 실측값
PITCHER_DEFAULT_CC = 2200
PITCHER_MEASURED_CC = {2000: 1700, 3000: 2700}   # 표기 용량 -> 실측 (한국소비자원, 2013)
# 한 자리 양으로 보기 어렵고 인원 정보가 필요한 단위 -> 재질문 (6.2 UNKNOWN_UNIT)
UNKNOWN_VOLUME_UNITS = {"짝", "박스", "통"}

# 같은 주종·다른 단위: 소주만 잔으로 (1병 = 7잔), 그 밖은 cc로 합산 (8.5)
BOTTLE_TO_GLASS = {"soju": 7}

# 7장 용량 기본값 (mL). None = 기준 없음 -> cc 합산 불가, 그 단위만 재질문 (8.9)
VOLUME_ML = {
    "soju":      {"glass": 50,  "bottle": 360, "can": None},
    "beer":      {"glass": 200, "bottle": 500, "can": 355},
    "makgeolli": {"glass": 250, "bottle": 750, "can": None},
    "wine":      {"glass": 150, "bottle": 750, "can": None},
    "spirits":   {"glass": 30,  "bottle": 750, "can": None},
}
VOLUME_BY_NAME = {
    "하이볼": {"glass": 300, "bottle": None, "can": 355},
    "소맥": {"glass": 200}, "폭탄주": {"glass": 200},
}


def volume_ml(beverage, unit, name=None):
    if unit == "cc":
        return 1
    by_name = VOLUME_BY_NAME.get(name or "", {})
    if unit in by_name:
        return by_name[unit]
    return VOLUME_ML.get(beverage, {}).get(unit)


# "조금·약간" (8.5): 다른 술을 숫자로 말한 뒤 곁들인 경우만 1잔
SMALL_AMOUNT_WORDS = {"조금", "약간", "살짝", "조금씩"}
# 최대 음주량 "평소랑 같아 / 두 배" (8.5)
SAME_AS_USUAL_WORDS = ("평소랑 같", "평소와 같", "똑같", "그 정도", "비슷")
DOUBLE_OF_USUAL_WORDS = ("두 배", "2배", "두배")

# ------------------------------------------------------------ 4) 선택지 표현 (3, 6.2)
# DB 라벨(주·월·년, 잔·병·캔, 예·아니요·모름, 매일 등)은 자동 인식된다.
OPTION_SYNONYMS = {
    "Q7.unit": {
        "일주일": "week", "한 주": "week", "주당": "week", "매주": "week", "week": "week",
        "한 달": "month", "달": "month", "매달": "month", "한달": "month", "month": "month",
        "1년": "year", "일 년": "year", "일년": "year", "연": "year", "한 해": "year", "year": "year",
    },
    "Q3.answer": {
        "네": "yes", "맞아요": "yes", "보유": "yes", "보유자": "yes", "있음": "yes",
        "아니오": "no", "아니요": "no", "없음": "no", "비보유": "no",
        "몰라요": "unknown", "모르겠어요": "unknown", "모름": "unknown",
        "검사 안 함": "unknown", "검사 안 해봤어": "unknown", "검사 안 해봤어요": "unknown",
        "검사 안 해 봤어요": "unknown", "검사한 적 없어요": "unknown",
    },
    "Q4_1.status": {
        "피움": "current", "피워요": "current", "흡연 중": "current", "현재": "current",
        "끊음": "former", "끊었어요": "former", "금연": "former", "과거": "former",
    },
    "Q5_1.status": {
        "피움": "current", "피워요": "current", "사용 중": "current", "현재": "current",
        "끊음": "former", "끊었어요": "former", "금연": "former", "과거": "former",
    },
    "Q6_1.use_pattern": {
        "안 씀": "no", "사용 안 함": "no", "없음": "no",
        "하루도 안 빠지고": "daily", "날마다": "daily", "한 달 내내": "daily",
        "거의 매일": "days_10_29",                                   # 8.3
    },
}

# ------------------------------------------------------------ 5) Q6_1 구간 (5, 8.2)
USAGE_PATTERN_RANGES = [            # (선택지 코드, 최소 일수, 최대 일수)
    ("no", 0, 0),
    ("days_1_2", 1, 2),
    ("days_3_9", 3, 9),
    ("days_10_29", 10, 29),
    ("daily", 30, 31),
]
MONTH_MAX_DAYS = 31

# ------------------------------------------------------------ 6) 숫자·어림 표현 (8.2, 8.3)
KOREAN_NUMBERS = {
    "한": 1, "하나": 1, "두": 2, "둘": 2, "세": 3, "셋": 3, "네": 4, "넷": 4,
    "다섯": 5, "여섯": 6, "일곱": 7, "여덟": 8, "아홉": 9, "열": 10, "반": 0.5,
}
# 범위를 뜻하는 수 관형사 -> (최소, 최대)
KOREAN_RANGES = {
    "한두": (1, 2), "두세": (2, 3), "서너": (3, 4), "너댓": (4, 5), "네다섯": (4, 5),
    "대여섯": (5, 6), "예닐곱": (6, 7),
}
# 숫자를 떠올리지 못한 말 -> 재질문 (6.2 VAGUE_QUANTITY)
VAGUE_WORDS = ("가끔", "자주", "종종", "여러 번", "여러번", "몇 번", "몇번", "때때로", "많이", "조금씩")
# 숫자 칸에 대답만 한 말 -> 숫자만 재질문 (6.2 MISSING_NUMBER)
YES_WORDS = {"네", "예", "응", "어", "해요", "합니다", "하고 있어요", "있어요"}
# 숫자 앞뒤 어림 표지
APPROX_PREFIX = ("대략", "약", "한 ", "거의 ")
APPROX_SUFFIX = ("쯤", "정도", "가량", "남짓")
LOWER_BOUND_SUFFIX = ("이상", "넘게", "넘는", "넘어", "넘")
BELOW_SUFFIX = ("미만", "도 안 돼", "도 안 되", "안 돼", "안 되게", "안 됨", "안되게")
AT_MOST_SUFFIX = ("이하",)

# 날 수를 뜻하는 말
DAY_COUNT_WORDS = {"열흘": "10일", "보름": "15일"}
# 주 단위 일수 표현 (8.3). 정수 칸이면 8.2 방향 규칙을 따른다.
WEEK_DAY_WORDS = {
    "매일": 7, "날마다": 7, "거의 매일": 6, "평일": 5, "평일마다": 5, "주말": 2, "주말마다": 2,
    "격일": 3.5, "이틀에 한 번": 3.5, "하루 걸러": 3.5,
    "안 함": 0, "안 해요": 0, "안 해": 0, "없음": 0, "전혀 안 해요": 0,
}
WEEK_DAY_WORDS_APPROX = {"거의 매일", "격일", "이틀에 한 번", "하루 걸러", "주말", "주말마다",
                         "평일", "평일마다"}
# Q7 음주 빈도 표현 -> (횟수, 기간)
FREQUENCY_WORDS = {"매일": (7, "week"), "날마다": (7, "week"), "거의 매일": (6, "week")}
# 기간 앞말 (8.4): "주 3번", "한 달에 8번"
PERIOD_PREFIX = [
    ("일주일에", "week"), ("한 주에", "week"), ("주에", "week"), ("주당", "week"), ("매주", "week"),
    ("주", "week"),
    ("한 달에", "month"), ("한달에", "month"), ("달에", "month"), ("매달", "month"), ("월", "month"),
    ("1년에", "year"), ("일 년에", "year"), ("일년에", "year"), ("연", "year"),
]
DAYS_PER = {"week": 7, "month": 30}            # 기간 환산 (8.4)

# 기간 단위별 최대 횟수 (Q7 frequency) -> 넘으면 COUNT_EXCEEDS_DAYS (6.1 R2)
PERIOD_MAX = {"week": 7, "month": 31, "year": 366}
MAX_MINUTES_PER_DAY = 1440                      # 6.2 OUT_OF_RANGE

# 단위 환산: DB 단위 -> {사용자 단위: 곱할 값}
UNIT_CONVERSION = {
    "개비": {"개비": 1, "개피": 1, "개": 1, "대": 1, "스틱": 1, "갑": 20},   # 8.9 가열담배 스틱
    "년": {"년": 1, "해": 1, "개월": 1 / 12, "달": 1 / 12},
    "minute": {"분": 1, "시간": 60},
}
# 금연 후 연수 표현 (8.5)
YEAR_WORDS = {"작년": 1, "재작년": 2, "올해": 0.5, "올해 초": 0.5}

# .5를 맞추는 방향 (8.2): 위험을 낮게 잡지 않는 쪽
ROUND_UP_ITEMS = {"Q4_1", "Q5_1", "Q6_1"}       # 흡연·전자담배
ROUND_DOWN_ITEMS = {"Q8_1", "Q8_2", "Q9_1", "Q9_2", "Q10"}   # 신체활동
SMOKING_YEARS_FIELDS = {"Q4_1.total_years", "Q5_1.total_years"}   # 1년 미만 -> 1년

# ------------------------------------------------------------ 7) 항목별 추론 (8.5)
QUIT_WORDS = ("끊었", "끊은", "끊고", "금연", "끊음")
FEW_SMOKES_WORDS = ("몇 대", "한두 대", "한두 번", "몇 개비", "한두 개비", "몇 번 피워", "호기심")
PRE_DISEASE_WORDS = ("전단계", "경계", "주의 단계", "주의단계")
NO_EXERCISE_PATTERN = r"운동\S*\s*(을|은|는)?\s*(전혀|아예|하나도|통)\s*안"
# 질환 동의어 -> 문진표 행 (Q1 D01~D11, Q2 D01~D05)
DISEASE_SYNONYMS = {
    "뇌졸중": "D01", "중풍": "D01", "뇌경색": "D01", "뇌출혈": "D01",
    "심근경색": "D02", "협심증": "D02",
    "고혈압": "D03", "혈압이 높": "D03", "혈압 높": "D03", "혈압약": "D03",
    "당뇨": "D04", "당뇨병": "D04", "혈당약": "D04",
    "이상지질혈증": "D05", "고지혈증": "D05", "콜레스테롤": "D05",
}
Q2_ROWS = {"D01": "D01", "D02": "D02", "D03": "D03", "D04": "D04"}   # 그 밖은 Q2 D05 기타
TARGET_FAMILY_WORDS = ("부모", "아버지", "어머니", "아빠", "엄마", "형", "누나", "언니", "오빠",
                       "동생", "형제", "자매", "남매")
OTHER_FAMILY_WORDS = ("할아버지", "할머니", "삼촌", "고모", "이모", "외삼촌", "사촌", "큰아버지",
                      "작은아버지", "외할", "친척")

# ------------------------------------------------------------ 8) 신체활동 문항 (8.9)
# 문진표 Q8·Q9·Q10에 적힌 예시 그대로. 목록에 없는 활동은 강도를 정하지 않는다.
ACTIVITY_ITEM = {
    "달리기": "Q8", "에어로빅": "Q8", "빠른 속도로 자전거": "Q8", "건설현장 노동": "Q8",
    "계단으로 물건 나르기": "Q8",
    "빠르게 걷기": "Q9", "복식 테니스": "Q9", "보통 속도로 자전거": "Q9",
    "가벼운 물건 나르기": "Q9", "청소": "Q9",
    "팔굽혀펴기": "Q10", "윗몸일으키기": "Q10", "아령": "Q10", "역기": "Q10", "철봉": "Q10",
}

# ------------------------------------------------------------ 9) LoRA-A 출력 보정 (8.6)
REFUSAL_WORDS = ("답하지 않겠", "답변하지 않겠", "응답하지 않겠", "답 안 할", "대답 안 할", "말하기 싫",
                 "패스", "비밀")
DEFER_WORDS = ("나중에", "넘기")
CORRECTION_PATTERN = r"아니(라|고|[,\s])|잘못 말|말고|정정|고칠게|고쳐"

# ------------------------------------------------------------ 10) 상태
SEMANTIC_TO_RESOLUTION = {          # MISSING -> PARTIAL (DB에 MISSING 상태가 없음)
    "MISSING": "PARTIAL",
    "AMBIGUOUS": "AMBIGUOUS",
    "UNCERTAIN": "UNCERTAIN",
}
INTENT_TO_RESOLUTION = {
    "REFUSED": "REFUSED",
    "REQUEST_SKIP": "DEFERRED",
}

# ------------------------------------------------------------ 11) 재질문 문장 예 (6장)
# LoRA-B가 사유 코드로 질문을 만들 때 참고하는 문장. {choices}는 선택지 목록.
CLARIFY_QUESTIONS = {
    "COUNT_EXCEEDS_DAYS": "술 마신 날을 기준으로 일주일에 며칠인가요?",
    "CONFLICT_DOES_NOT_DRINK": "지난 1년 동안 드신 적이 있다면 몇 번쯤인가요?",
    "CONFLICT_MAX_BELOW_USUAL": "가장 많이 드신 날이 평소보다 적게 나왔어요. 그날은 얼마나 드셨나요?",
    "CONFLICT_WITH_PREVIOUS": "아까 말씀하신 값과 달라요. 어느 쪽이 맞을까요?",
    "OUT_OF_RANGE": "기간 기준으로 다시 알려 주시겠어요? (예: 일주일 기준 며칠)",
    "VAGUE_QUANTITY": "숫자로 알려 주시겠어요? 대략적인 값도 괜찮아요.",
    "MISSING_NUMBER": "며칠(몇 번)인지 숫자로 알려 주시겠어요?",
    "INVALID_OPTION": "{choices} 중 어디에 해당하나요?",
    "RANGE_SPANS_OPTIONS": "{choices} 중 어디에 가까우세요?",
    "BEVERAGE_MISSING": "어떤 술이었나요?",
    "OTHER_BEVERAGE": "맥주, 소주, 와인 중 어떤 술과 비슷한가요? 도수를 아시면 알려 주세요.",
    "UNKNOWN_UNIT": "본인이 드신 양은 몇 병(잔)쯤인가요?",
    "MISSING": "빠진 정보를 알려 주시겠어요?",
}
