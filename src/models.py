from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, Field, field_validator


def _coerce_grade_int(value: Any) -> int:
    if isinstance(value, bool):
        return 1
    if isinstance(value, int):
        return min(3, max(1, value))
    s = str(value or "").strip()
    for d in ("1", "2", "3"):
        if d in s:
            return int(d)
    return 1


class QuestionRecord(BaseModel):
    """저장소에 영구 보관되는 단일 기출 문항 스키마 (2022 개정 교육과정 기준)."""

    id: str = Field(..., description="고유 문항 ID (예: 2026_고3_6월_화학__Q05)")
    exam_name: str = Field(..., description="시험 회차명 (예: 2026_고3_6월_화학)")
    q_num: int = Field(..., ge=1, description="문항 번호")
    subject_category: Literal["수학", "사회", "과학"] = Field(..., description="대분류 과목")
    curriculum_subject: str = Field(..., description="2022 개정 교육과정 세부 과목명")
    unit_name: str = Field(..., description="단원명")
    min_grade: int = Field(..., ge=1, le=3, description="최저 학년 수준 (1=고1, 2=고2, 3=고3)")

    @field_validator("min_grade", mode="before")
    @classmethod
    def _validate_record_grade(cls, value: Any) -> int:
        return _coerce_grade_int(value)
    core_principles: list[str] = Field(
        default_factory=list,
        description="이 문제를 푸는 데 필요한 본질적 풀이 원리·법칙 리스트",
    )
    required_terms: list[str] = Field(
        default_factory=list,
        description="문제 지문과 보기에 등장하는 전문 용어·단위 리스트",
    )
    is_single_concept: bool = Field(
        ...,
        description="단일 핵심 원리만으로 풀리는지 여부",
    )
    answer: str = Field(..., description="공식 정답")
    official_explanation: str = Field(..., description="공식 해설 핵심 풀이 요약")
    image_filename: str = Field(default="", description="크롭된 문항 PNG 파일명")
    source_pdf: str = Field(default="", description="원본 문제지 PDF 파일명")
    answer_pdf: str = Field(default="", description="원본 해설지 PDF 파일명")
    created_at: str = Field(default="", description="생성 시각 (ISO 8601)")


class QuestionExtractionOutput(BaseModel):
    """Gemini가 크롭 이미지 + 해설지 PDF로부터 추출하는 구조화 메타데이터."""

    curriculum_subject: str = Field(
        ...,
        description="2022 개정 교육과정 세부 과목명 (예: 공통수학1, 공통수학2, 대수, 미적분1, 통합사회1, 통합사회2, 경제, 통합과학1, 통합과학2, 화학, 물리학 등)",
    )
    unit_name: str = Field(..., description="단원명")
    min_grade: int = Field(
        ...,
        ge=1,
        le=3,
        description="이 문항의 본질적 풀이 원리를 처음 배우는 최저 학년 (반드시 1, 2, 3 중 하나의 정수: 1=고1, 2=고2, 3=고3)",
    )

    @field_validator("min_grade", mode="before")
    @classmethod
    def _validate_extracted_grade(cls, value: Any) -> int:
        return _coerce_grade_int(value)
    core_principles: list[str] = Field(
        ...,
        description="표면적인 상위 용어를 배제하고 문제를 푸는 데 필요한 본질적 풀이 원리·법칙 리스트 (예: 질량 보존 법칙, 비례식, 수요·공급 이동)",
    )
    required_terms: list[str] = Field(
        ...,
        description="실제 문제 지문, 그림, 보기에 등장하는 전문 용어·기호·단위 목록 (예: ['mol', '몰농도', 'pH'])",
    )
    is_single_concept: bool = Field(
        ...,
        description="단일 핵심 원리만으로 풀리면 True, 여러 원리가 복합되면 False",
    )
    answer: str = Field(..., description="공식 정답 (객관식은 '1'~'5' 숫자 문자열, 주관식은 정확한 값)")
    official_explanation: str = Field(..., description="공식 해설의 핵심 풀이 요약")


class ConceptWhitelist(BaseModel):
    """업로드된 개념서·교과서 PDF 지정 페이지에서 추출한 지식 화이트리스트."""

    subject_guess: str = Field(..., description="PDF 내용 기반 추정 과목명")
    pdf_summary: str = Field(..., description="지정 페이지 범위의 핵심 학습 내용 요약")
    pdf_concepts: list[str] = Field(
        ...,
        description="이 PDF에 실제로 설명되어 있는 개념·원리·법칙 목록",
    )
    pdf_vocabulary: list[str] = Field(
        ...,
        description="이 PDF에 실제로 등장하여 학생이 이해할 수 있는 허용 용어·기호·단위 목록",
    )


class QuestionMatchJudgment(BaseModel):
    """단일 기출 문항과 PDF 화이트리스트 간 엄격한 범위 일치 판정."""

    question_id: str = Field(..., description="평가 대상 문항 ID")
    is_solvable_by_pdf_only: bool = Field(
        ...,
        description="문항의 core_principles가 오직 pdf_concepts 범위만으로 완전히 해결 가능하면 True",
    )
    out_of_scope_terms: list[str] = Field(
        default_factory=list,
        description="required_terms 중 pdf_vocabulary에 없는 상위/범위 외 용어·단위 목록",
    )
    needs_rewriting: bool = Field(
        ...,
        description="원리는 PDF로 해결 가능(is_solvable_by_pdf_only=True)하지만 out_of_scope_terms가 존재하여 개조가 필요하면 True",
    )
    matching_reason: str = Field(..., description="매칭 또는 제외 사유 간략 설명")


class BatchMatchResult(BaseModel):
    """배치 문항 매칭 판정 결과."""

    judgments: list[QuestionMatchJudgment] = Field(default_factory=list)


class RewrittenQuestion(BaseModel):
    """업로드된 PDF 용어·개념만 사용해 개조한 변형 문제 카드 및 맞춤 해설."""

    rewritten_stem: str = Field(
        ...,
        description="업로드된 PDF의 개념과 허용 용어·일상어만 사용해 자연스럽게 개조한 문제 지문 및 <조건> (원본 논리·수치·정답 100% 유지)",
    )
    rewritten_choices: list[str] = Field(
        default_factory=list,
        description="객관식 5개 선지 텍스트 리스트 (선지 번호 ①~⑤ 제외하고 내용만, 주관식이면 빈 리스트)",
    )
    replaced_terms_map: list[str] = Field(
        default_factory=list,
        description="치환된 상위 용어 → PDF 기반 대체 표현 내역 (예: 'mol → 일정 개수 묶음(입자 수 비)')",
    )
    pdf_grounded_explanation: str = Field(
        ...,
        description="반드시 업로드된 PDF의 개념과 허용 용어만 사용해 작성한 단계별 맞춤 해설",
    )
    preserved_answer: str = Field(..., description="원본과 100% 동일하게 유지된 정답")
    self_verification_note: str = Field(
        ...,
        description="범위 외 용어 제거 및 정답/논리 일치 여부 자가 검증 결과",
    )


class ExamHeaderMetadata(BaseModel):
    year: str = Field(..., description="시행 년도 (예: '2026')")
    month: str = Field(..., description="시행 월 (예: '3월', '6월', '9월', '11월')")
    grade: str = Field(..., description="학년 (예: '고1', '고2', '고3')")
    subject: str = Field(
        ...,
        description="시험지 헤더 기준 세부 과목명 (예: '국어', '수학', '영어', '한국사', '통합사회', '통합과학', '화학', '경제')",
    )
    exam_title: str = Field(
        ...,
        description="표준 회차명 (형식: '{year}년 {month} {grade} {subject}', 예: '2026년 9월 고1 통합사회')",
    )


def infer_subject_category(subject: str) -> SubjectCategory:
    """세부 과목명을 대분류('수학' | '사회' | '과학')로 매핑한다."""
    s = (subject or "").strip()
    if any(k in s for k in ("수학", "미적분", "기하", "확률", "통계", "대수")):
        return "수학"
    if any(k in s for k in ("과학", "물리", "화학", "생명", "지구")):
        return "과학"
    if any(
        k in s
        for k in (
            "사회",
            "한국사",
            "역사",
            "지리",
            "윤리",
            "사상",
            "경제",
            "정치",
            "법",
            "문화",
            "사탐",
        )
    ):
        return "사회"
    return "과학"


TierType = Literal["FREE", "CUSTOM", "PRO", "ADMIN"]
LEGACY_TIER_MAP: dict[str, str] = {
    "BYOK": "CUSTOM",
    "ULTRA": "ADMIN",
}


class UserRecord(BaseModel):
    """회원 계정 및 등급·개인 API 키·일일 사용량 스키마 (users.json 저장용)."""

    username: str = Field(..., description="로그인 아이디 (학번 또는 영문·숫자 ID)")
    nickname: str = Field(..., description="표시 닉네임")
    password_hash: str = Field(..., description="salt$sha256 형식의 암호화 비밀번호")
    tier: TierType = Field(default="FREE", description="회원 등급 (FREE / CUSTOM / PRO / ADMIN)")
    custom_api_key: str = Field(default="", description="CUSTOM 회원의 개인 Gemini API 키")
    daily_count: int = Field(default=0, ge=0, description="금일 사용한 기출 추출 문항 수")
    last_used_date: str = Field(default="", description="마지막 추출 날짜 (YYYY-MM-DD, KST)")
    created_at: str = Field(default="", description="가입 시각 (ISO 8601)")

    @field_validator("tier", mode="before")
    @classmethod
    def _normalize_legacy_tier(cls, value: Any) -> str:
        raw = str(value or "FREE").strip().upper()
        return LEGACY_TIER_MAP.get(raw, raw)

