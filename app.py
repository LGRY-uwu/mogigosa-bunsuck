from __future__ import annotations

import concurrent.futures
import hashlib
import html as _html
import json
import re
import time
from datetime import datetime
from typing import Any

import streamlit as st

from src.auth import (
    FREE_DAILY_LIMIT,
    TIER_LABELS,
    VALID_TIERS,
    authenticate_user,
    batch_update_user_tiers,
    can_access_shared_concept_db,
    can_use_premium_features,
    clear_user_custom_api_key,
    consume_extraction_quota,
    get_remaining_free_quota,
    get_user_by_username,
    mask_api_key,
    register_user,
    resolve_active_gemini_api_key,
    save_user_custom_api_key,
)
from src.config import AVAILABLE_MODELS, DEFAULT_MODEL, MODEL_LABELS, resolve_credentials
from src.ebsi_fetcher import attempt_ebsi_pdf_fetch
from src.exporters import build_exam_html, build_exam_markdown
from src.gemini_service import (
    analyze_question_with_gemini,
    build_gemini_client,
    chat_followup_with_pdf,
    detect_scanned_question_boxes,
    extract_concept_book_units,
    extract_concept_whitelist,
    generate_pdf_grounded_explanation,
    match_questions_to_whitelist,
    prepare_pdf_part,
    rewrite_question_for_pdf,
)
from src.models import (
    ConceptBookRecord,
    ConceptUnit,
    ConceptWhitelist,
    QuestionRecord,
    RewrittenQuestion,
    build_whitelist_from_concept_units,
    infer_subject_category,
    resolve_question_concept_reference,
)
from src.pdf_generator import generate_custom_exam_pdf
from src.pdf_parser import (
    CroppedQuestion,
    crop_normalized_box,
    extract_answer_snippets_from_pdf,
    extract_concept_pages_by_ranges,
    extract_exam_metadata_fast,
    extract_pdf_slice,
    get_page_text_lengths,
    get_pdf_page_count,
    parse_exam_pdf,
    parse_page_ranges_spec,
    render_pdf_page_png,
)
from src.storage import StorageManager
from src.ui_styles import inject_responsive_css, render_chips

st.set_page_config(
    page_title="2022 개정 교육과정 맞춤형 기출 플랫폼",
    page_icon="📘",
    layout="wide",
    initial_sidebar_state="auto",
)

inject_responsive_css()

CIRCLED_MAP = {"1": "①", "2": "②", "3": "③", "4": "④", "5": "⑤"}
REVERSE_CIRCLED_MAP = {v: k for k, v in CIRCLED_MAP.items()}


def normalize_answer(ans: str) -> str:
    s = (ans or "").strip().replace(" ", "")
    if s in REVERSE_CIRCLED_MAP:
        return REVERSE_CIRCLED_MAP[s]
    if len(s) == 2 and s[0] in CIRCLED_MAP and s[1] == "번":
        return s[0]
    return s.lower()


def is_multiple_choice_question(q: QuestionRecord, rewritten: RewrittenQuestion | None) -> bool:
    if rewritten and len(rewritten.rewritten_choices) >= 2:
        return True
    return normalize_answer(q.answer) in {"1", "2", "3", "4", "5"}


def sanitize_id_part(text: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z가-힣_\-]", "_", text.strip())
    return re.sub(r"_+", "_", cleaned).strip("_") or "exam"


def _init_session_defaults() -> None:
    defaults: dict[str, Any] = {
        "parsed_crops": [],
        "parsed_exam_meta": {},
        "ebsi_problem_bytes": None,
        "ebsi_answer_bytes": None,
        "ebsi_problem_name": "",
        "ebsi_answer_name": "",
        "concept_whitelist": None,
        "concept_sliced_text": "",
        "concept_source_label": "",
        "concept_pdf_hash": "",
        "matched_items": [],
        "match_diagnostics": [],
        "generated_exam_pdf": None,
        "user_answers": {},
        "graded": False,
        "ai_explanations": {},
        "ai_chats": {},
        "drive_sync_key": "",
        "drive_sync_result": None,
        "logged_in_username": "",
        "tab1_last_loaded_sig": "",
        "tab1_meta_status": "",
        "meta_year": "",
        "meta_month": "",
        "meta_grade": "",
        "meta_subject": "",
        "meta_title": "",
        "tab1_subject_cat": "과학",
        "stage2_exam_name": "",
        "stage2_saved_q_nums": [],
        "stage2_failed_q_nums": [],
        "stage2_failed_errors": [],
        "stage2_sync_err": "",
        "trigger_retry_failed": False,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


_init_session_defaults()

# ---------------------------------------------------------------------------
# 백그라운드 인증 및 저장소 초기화 (수동 입력창은 UI에서 숨김)
# ---------------------------------------------------------------------------
creds = resolve_credentials()
storage = StorageManager(
    local_root="./exam_db",
    service_account_info=creds.service_account_info,
    drive_folder_id=creds.drive_folder_id,
    oauth_info=creds.oauth_credentials_info,
)

auth_payload = creds.oauth_credentials_info or creds.service_account_info
sa_fingerprint = (
    hashlib.sha256(json.dumps(auth_payload, sort_keys=True).encode()).hexdigest()[:12]
    if auth_payload
    else "none"
)
sync_fingerprint = f"{creds.drive_folder_id}:{storage.drive_auth_mode}:{sa_fingerprint}"
if storage.drive_connected and st.session_state["drive_sync_key"] != sync_fingerprint:
    sync_out = storage.sync_on_startup()
    st.session_state["drive_sync_result"] = sync_out
    if not sync_out.get("error"):
        st.session_state["drive_sync_key"] = sync_fingerprint

current_user = (
    get_user_by_username(storage, st.session_state["logged_in_username"])
    if st.session_state["logged_in_username"]
    else None
)
if st.session_state["logged_in_username"] and current_user is None:
    st.session_state["logged_in_username"] = ""

is_admin = bool(current_user is not None and current_user.tier == "ADMIN")
selected_model = DEFAULT_MODEL
active_gemini_key = ""
active_key_label = ""

# ---------------------------------------------------------------------------
# 사이드바: 비로그인 시 [로그인 / 회원가입] 탭만 표시, 로그인 시 프로필·개인키·설정 표시
# ---------------------------------------------------------------------------
with st.sidebar:
    st.markdown("## 👤 계정 & 멤버십")
    if current_user is None:
        auth_tab_login, auth_tab_signup = st.tabs(["🔑 로그인", "📝 회원가입"])
        with auth_tab_login:
            with st.form("sidebar_login_form"):
                login_id = st.text_input("아이디 (학번 또는 영문 ID)", placeholder="예: 2026101")
                login_pw = st.text_input("비밀번호", type="password")
                login_submit = st.form_submit_button("로그인", type="primary", use_container_width=True)
            if login_submit:
                ok, msg, logged_user = authenticate_user(storage, login_id, login_pw)
                if ok and logged_user is not None:
                    st.session_state["logged_in_username"] = logged_user.username
                    st.success(msg)
                    st.rerun()
                else:
                    st.error(msg)

        with auth_tab_signup:
            with st.form("sidebar_signup_form"):
                reg_id = st.text_input("아이디 (영문·숫자 3~30자)", placeholder="예: student01")
                reg_nick = st.text_input("닉네임", placeholder="예: 김민준")
                reg_pw = st.text_input("비밀번호 (4자 이상)", type="password")
                reg_pw2 = st.text_input("비밀번호 확인", type="password")
                reg_submit = st.form_submit_button("회원가입", type="primary", use_container_width=True)
            if reg_submit:
                if reg_pw != reg_pw2:
                    st.error("비밀번호 확인이 일치하지 않습니다.")
                else:
                    ok, msg, new_user = register_user(storage, reg_id, reg_nick, reg_pw)
                    if ok and new_user is not None:
                        st.session_state["logged_in_username"] = new_user.username
                        st.success(msg)
                        st.rerun()
                    else:
                        st.error(msg)
    else:
        remaining_q = get_remaining_free_quota(current_user)
        tier_code = current_user.tier
        tier_kor = TIER_LABELS.get(tier_code, tier_code)
        active_gemini_key, active_key_label = resolve_active_gemini_api_key(
            current_user, creds.gemini_api_key
        )

        if tier_code == "FREE":
            status_line = (
                f"<div style='font-size:0.84rem; color:#334155; margin-top:0.45rem;'>"
                f"오늘 남은 무료 추출: <b>{remaining_q} / {FREE_DAILY_LIMIT}회</b>"
                f"<br/><span style='font-size:0.75rem; color:#64748B;'>매일 자정(00:00 KST) 자동 초기화</span></div>"
            )
        elif tier_code == "CUSTOM":
            masked = _html.escape(mask_api_key(current_user.custom_api_key))
            status_line = (
                f"<div style='font-size:0.84rem; color:#166534; margin-top:0.45rem;'>"
                f"등록된 개인 키: <code>{masked}</code>"
                f"<br/><b>무제한 추출 · 문제 개조 · PDF 병합</b></div>"
            )
        else:
            status_line = (
                "<div style='font-size:0.84rem; color:#1D4ED8; font-weight:600; margin-top:0.45rem;'>"
                "✨ 서버 API 키 무제한 이용 중</div>"
            )

        st.markdown(
            f"""
            <div class="account-card">
              <div style="display:flex; justify-content:space-between; align-items:center; gap:6px;">
                <div>
                  <div style="font-size:1rem; font-weight:800; color:#0F172A;">{_html.escape(current_user.nickname)}</div>
                  <div style="font-size:0.78rem; color:#64748B;">@{_html.escape(current_user.username)}</div>
                </div>
                <span class="tier-badge tier-{tier_code}">{tier_kor} ({tier_code})</span>
              </div>
              {status_line}
            </div>
            """,
            unsafe_allow_html=True,
        )

        if remaining_q is not None:
            st.progress(remaining_q / FREE_DAILY_LIMIT)

        # CUSTOM 개인 API 키 관리 UI
        if tier_code == "FREE":
            show_custom_form = st.toggle(
                "🔑 개인 API 키 등록하여 무제한 쓰기",
                key="sidebar_toggle_custom_key",
            )
            if show_custom_form:
                with st.form("sidebar_custom_key_form", clear_on_submit=True):
                    user_api_input = st.text_input(
                        "내 Gemini API 키 (AIza...)",
                        type="password",
                        placeholder="AIzaSy...",
                    )
                    save_key_btn = st.form_submit_button(
                        "💾 개인 키 저장 & 무제한 모드 전환",
                        type="primary",
                        use_container_width=True,
                    )
                if save_key_btn:
                    ok_k, msg_k, updated_u = save_user_custom_api_key(
                        storage, current_user.username, user_api_input
                    )
                    if ok_k:
                        st.success(msg_k)
                        st.rerun()
                    else:
                        st.error(msg_k)
        elif tier_code == "CUSTOM" or bool(current_user.custom_api_key):
            st.caption(f"저장된 개인 키: `{mask_api_key(current_user.custom_api_key)}`")
            if st.button(
                "🗑️ 개인 키 삭제 / 무료 회원으로 복귀",
                key="clear_custom_key_btn",
                use_container_width=True,
            ):
                ok_c, msg_c, _ = clear_user_custom_api_key(storage, current_user.username)
                if ok_c:
                    st.info(msg_c)
                    st.rerun()
                else:
                    st.error(msg_c)

        if st.button("🚪 로그아웃", key="logout_btn", use_container_width=True):
            st.session_state["logged_in_username"] = ""
            st.session_state["matched_items"] = []
            st.session_state["generated_exam_pdf"] = None
            st.rerun()

        st.markdown("---")
        selected_model = st.selectbox(
            "🤖 Gemini 모델 선택",
            options=AVAILABLE_MODELS,
            index=AVAILABLE_MODELS.index(DEFAULT_MODEL),
            format_func=lambda m: MODEL_LABELS.get(m, m),
            help="기본 추천 모델은 Gemini 3.5 Flash 입니다.",
        )

        # 저장소 & DB 요약
        all_records = storage.load_db()
        all_concept_books = storage.load_concept_books()
        st.markdown("### 📊 기출 · 개념서 DB & 저장소")
        c1, c2, c3 = st.columns(3)
        c1.metric("기출 문항", f"{len(all_records)}개")
        unique_exams = sorted({r.exam_name for r in all_records})
        c2.metric("등록 회차", f"{len(unique_exams)}회")
        c3.metric("공용 개념서", f"{len(all_concept_books)}권")
        if storage.drive_connected:
            mode_tag = "OAuth" if storage.drive_auth_mode == "oauth" else "서비스 계정"
            st.caption(f"☁️ Google Drive 연동됨 ({storage.drive_folder_name} · {mode_tag})")
            sync_res = st.session_state.get("drive_sync_result") or {}
            if sync_res.get("error"):
                st.warning(f"Drive 동기화 오류: {sync_res['error'][:160]}")
        else:
            st.caption("💾 로컬 영구 저장소 (`./exam_db`, `./concept_db`)")
            if storage.drive_error:
                st.caption(f"⚠️ Drive 연결 오류: {storage.drive_error[:140]}")

# ---------------------------------------------------------------------------
# 메인 헤더 & 2개 탭
# ---------------------------------------------------------------------------
st.markdown(
    """
    <div class="app-hero">
      <h1>📘 2022 개정 교육과정 맞춤형 기출 추출 & 상위 문제 개조 플랫폼</h1>
      <p>개념서·교과서 PDF에 실제로 나온 원리와 용어만 남기고, 원리는 같지만 용어가 어려운 상위 기출은 내 개념서 언어로 자동 개조합니다.</p>
    </div>
    """,
    unsafe_allow_html=True,
)


def _render_admin_panel() -> None:
    st.subheader("🛡️ 관리자 회원 관리 (등급 설정 & 사용량 모니터링)")
    st.caption(
        "가입한 회원 목록은 `./exam_db/users.json`에 영구 저장되며 Google Drive 연동 시 자동 동기화됩니다."
    )

    col_top1, col_top2 = st.columns([2, 1])
    with col_top1:
        search_q = st.text_input(
            "회원 검색 (아이디 또는 닉네임)",
            placeholder="검색어를 입력하세요...",
            key="admin_user_search",
        ).strip().lower()
    with col_top2:
        st.markdown("<div style='height: 1.65rem;'></div>", unsafe_allow_html=True)
        if st.button("🔄 회원 목록 새로고침", use_container_width=True, key="admin_refresh_btn"):
            if storage.drive_connected:
                storage.sync_on_startup()
            st.rerun()

    users_list = storage.load_users()
    if not users_list:
        st.info("아직 가입한 회원이 없습니다.")
        return

    tier_counts = {t: sum(1 for u in users_list if u.tier == t) for t in VALID_TIERS}
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("전체 회원", f"{len(users_list)}명")
    m2.metric("무료 회원 (FREE)", f"{tier_counts['FREE']}명")
    m3.metric("개인 키 (CUSTOM)", f"{tier_counts['CUSTOM']}명")
    m4.metric("VIP 회원 (PRO)", f"{tier_counts['PRO']}명")
    m5.metric("최고 관리자 (ADMIN)", f"{tier_counts['ADMIN']}명")

    filtered_users = [
        u
        for u in users_list
        if not search_q or search_q in u.username.lower() or search_q in u.nickname.lower()
    ]
    filtered_users.sort(key=lambda u: u.created_at or u.username, reverse=True)

    table_rows = [
        {
            "아이디": u.username,
            "닉네임": u.nickname,
            "현재 등급": u.tier,
            "등급 설명": TIER_LABELS.get(u.tier, u.tier),
            "개인 키": mask_api_key(u.custom_api_key) or "-",
            "오늘 사용량": u.daily_count,
            "최근 사용일": u.last_used_date or "-",
        }
        for u in filtered_users
    ]

    edited_rows = st.data_editor(
        table_rows,
        column_config={
            "아이디": st.column_config.TextColumn("아이디", disabled=True),
            "닉네임": st.column_config.TextColumn("닉네임", disabled=True),
            "현재 등급": st.column_config.SelectboxColumn(
                "현재 등급 (변경 가능)",
                options=list(VALID_TIERS),
                required=True,
            ),
            "등급 설명": st.column_config.TextColumn("등급명", disabled=True),
            "개인 키": st.column_config.TextColumn("개인 키", disabled=True),
            "오늘 사용량": st.column_config.NumberColumn("오늘 사용량", disabled=True),
            "최근 사용일": st.column_config.TextColumn("최근 사용일", disabled=True),
        },
        hide_index=True,
        use_container_width=True,
        key="admin_users_editor",
    )

    if st.button("💾 회원 등급 변경사항 저장", type="primary", key="admin_save_tiers_btn"):
        updates = {
            row["아이디"]: row["현재 등급"]
            for row in edited_rows
            if row.get("아이디") and row.get("현재 등급") in VALID_TIERS
        }
        changed_cnt, err = batch_update_user_tiers(storage, updates)
        if err:
            st.warning(f"{changed_cnt}명 변경됨 / 저장 경고: {err}")
        elif changed_cnt > 0:
            st.success(f"✅ {changed_cnt}명의 회원 등급을 즉시 변경하여 `users.json`에 저장했습니다!")
            st.rerun()
        else:
            st.info("변경된 등급이 없습니다.")


if current_user is None:
    st.info("👈 좌측 사이드바 상단의 **[로그인 / 회원가입]** 탭에서 로그인하시면 기출 수집·추출·문제 개조 기능을 이용하실 수 있습니다.")
    t_col1, t_col2, t_col3, t_col4 = st.columns(4)
    with t_col1:
        with st.container(border=True):
            st.markdown("<span class='tier-badge tier-FREE'>무료 회원 (FREE)</span>", unsafe_allow_html=True)
            st.markdown("#### 기본 가입 등급")
            st.write("- 서버 API 키 사용")
            st.write("- 하루 **최대 5문제** 추출")
            st.write("- 모드 B 웹 풀기 & AI 해설")
    with t_col2:
        with st.container(border=True):
            st.markdown("<span class='tier-badge tier-CUSTOM'>개인 키 모드 (CUSTOM)</span>", unsafe_allow_html=True)
            st.markdown("#### 본인 API 키 등록")
            st.write("- **승인 없이 즉시 전환**")
            st.write("- 한도 차감 없이 **무제한**")
            st.write("- 문제 개조 & PDF 병합 허용")
    with t_col3:
        with st.container(border=True):
            st.markdown("<span class='tier-badge tier-PRO'>VIP 회원 (PRO)</span>", unsafe_allow_html=True)
            st.markdown("#### 관리자 승급 등급")
            st.write("- 서버 API 키 사용 · 추출 **무제한**")
            st.write("- **공용 개념서·교과서 DB** 구축 & 불러오기")
            st.write("- 문제 개조 & PDF 병합 무제한")
    with t_col4:
        with st.container(border=True):
            st.markdown("<span class='tier-badge tier-ADMIN'>최고 관리자 (ADMIN)</span>", unsafe_allow_html=True)
            st.markdown("#### 관리자 계정")
            st.write("- 서버 API 키 **무제한**")
            st.write("- **공용 개념서·교과서 DB** 구축 & 관리")
            st.write("- **회원 등급 관리** 패널 접근")
    st.stop()


def _render_concept_book_builder_section(key_prefix: str = "tab3") -> None:
    """관리자(ADMIN) 및 Pro(VIP) 등급용 개념서·교과서 PDF 단원별 공용 DB 구축 & 관리 UI."""
    st.subheader("📚 개념서·교과서 PDF 단원별 공용 DB 구축 (관리자 · Pro 전용)")
    st.caption(
        "지정한 개념 페이지 구간만 PyMuPDF로 추출해 목차·정답지·문제 풀이를 제외하고, "
        "사이드바에서 선택한 Gemini 모델로 단원별 핵심 개념과 허용 용어·단위를 구조화하여 "
        "로컬(`./concept_db`) 및 Google Drive 공용 폴더에 자동 동기화합니다."
    )

    if not can_access_shared_concept_db(current_user):
        st.warning(
            "🔒 **공용 개념서·교과서 DB 구축 및 공유 기능은 Pro(VIP) · ADMIN 등급 전용입니다.**\n\n"
            f"현재 로그인 등급: **{TIER_LABELS.get(current_user.tier, current_user.tier)} ({current_user.tier})** — "
            "관리자에게 Pro(VIP) 승급을 요청하시거나, [🎯 탭 2]의 **개별 개념서 PDF 직접 업로드** 기능을 이용해 주세요."
        )
        return

    with st.container(border=True):
        st.markdown("#### ➕ 새 개념서·교과서 PDF 업로드 및 개념 구간 지정")
        cb_col1, cb_col2 = st.columns(2)
        with cb_col1:
            book_title_in = st.text_input(
                "교재명",
                placeholder="예: 완자 통합과학, 개념원리 공통수학1",
                key=f"{key_prefix}_book_title",
            )
        with cb_col2:
            subject_grade_in = st.text_input(
                "과목 및 학년",
                placeholder="예: 고1 통합과학",
                key=f"{key_prefix}_subject_grade",
            )

        page_ranges_in = st.text_input(
            "추출할 개념 페이지 범위 입력 (쉼표 `,`와 하이픈 `-`으로 개념 설명 구간만 여러 개 지정 가능)",
            placeholder="예: 12-25, 34-48, 60-75",
            key=f"{key_prefix}_page_ranges",
        )
        cb_pdf_file = st.file_uploader(
            "교재 / 개념서 PDF 파일 업로드",
            type=["pdf"],
            key=f"{key_prefix}_pdf_uploader",
        )

        cb_pdf_bytes: bytes | None = cb_pdf_file.getvalue() if cb_pdf_file is not None else None
        parsed_intervals: list[tuple[int, int]] = []
        if cb_pdf_bytes is not None:
            total_cb_pages = get_pdf_page_count(cb_pdf_bytes)
            st.caption(f"📄 업로드된 PDF: `{cb_pdf_file.name}` (전체 **{total_cb_pages}쪽**)")
            if page_ranges_in.strip():
                try:
                    parsed_intervals = parse_page_ranges_spec(page_ranges_in, total_cb_pages)
                    total_sel_pages = sum(e - s + 1 for s, e in parsed_intervals)
                    interval_str = ", ".join(
                        f"p.{s}~p.{e}" if s != e else f"p.{s}" for s, e in parsed_intervals
                    )
                    st.info(
                        f"✅ 파싱된 개념 구간: **{interval_str}** (총 {len(parsed_intervals)}개 구간 · {total_sel_pages}쪽)"
                    )
                    with st.expander("👀 첫 구간 시작/끝 페이지 미리보기", expanded=False):
                        first_s, first_e = parsed_intervals[0][0], parsed_intervals[-1][1]
                        pv1, pv2 = st.columns(2)
                        with pv1:
                            st.image(
                                render_pdf_page_png(cb_pdf_bytes, first_s, dpi=130),
                                caption=f"첫 시작 페이지 (p.{first_s})",
                                use_container_width=True,
                            )
                        with pv2:
                            if first_e != first_s:
                                st.image(
                                    render_pdf_page_png(cb_pdf_bytes, first_e, dpi=130),
                                    caption=f"마지막 끝 페이지 (p.{first_e})",
                                    use_container_width=True,
                                )
                except ValueError as ve:
                    st.error(str(ve))

        build_db_btn = st.button(
            "🧠 PyMuPDF 개념 구간 추출 & Gemini 단원별 공용 DB 저장",
            type="primary",
            use_container_width=True,
            key=f"{key_prefix}_build_btn",
        )

        if build_db_btn:
            if not cb_pdf_bytes:
                st.error("교재/개념서 PDF 파일을 먼저 업로드해 주세요.")
            elif not book_title_in.strip():
                st.error("교재명을 입력해 주세요. (예: 완자 통합과학)")
            elif not subject_grade_in.strip():
                st.error("과목 및 학년을 입력해 주세요. (예: 고1 통합과학)")
            elif not page_ranges_in.strip():
                st.error("추출할 개념 페이지 범위를 입력해 주세요. (예: 12-25, 34-48, 60-75)")
            elif not active_gemini_key:
                st.error(f"사용 가능한 Gemini API 키({active_key_label})가 설정되지 않았습니다.")
            else:
                try:
                    total_cb_pages = get_pdf_page_count(cb_pdf_bytes)
                    intervals = parse_page_ranges_spec(page_ranges_in, total_cb_pages)
                except ValueError as ve:
                    st.error(str(ve))
                    intervals = []

                if intervals:
                    with st.spinner(
                        "1/2단계: PyMuPDF로 지정 페이지에서 목차·정답지·문제 풀이를 제외하고 순수 개념 텍스트와 실제 페이지 번호를 매핑 중..."
                    ):
                        sliced_bytes, mapped_text, stats = extract_concept_pages_by_ranges(
                            cb_pdf_bytes, intervals
                        )

                    skipped_info = stats.get("skipped_pages") or []
                    if skipped_info:
                        skip_summary = ", ".join(
                            f"p.{sp['page']}({sp['reason']})" for sp in skipped_info[:10]
                        )
                        st.caption(
                            f"🧹 불필요 페이지 자동 제외 ({len(skipped_info)}쪽): {skip_summary}"
                        )

                    model_disp = MODEL_LABELS.get(selected_model, selected_model)
                    with st.spinner(
                        f"2/2단계: 사이드바 선택 모델({model_disp})로 단원별 핵심 개념·허용 용어·페이지 범위를 구조화하고 공용 DB에 동기화 중..."
                    ):
                        try:
                            extracted_book = extract_concept_book_units(
                                api_key=active_gemini_key,
                                model_name=selected_model,
                                book_title=book_title_in.strip(),
                                subject_and_grade=subject_grade_in.strip(),
                                page_ranges_display=stats["ranges_display"],
                                sliced_pdf_bytes=sliced_bytes,
                                mapped_concept_text=mapped_text,
                            )
                            grade_match = re.search(r"(고\s*[123])", subject_grade_in)
                            grade_str = (
                                grade_match.group(1).replace(" ", "")
                                if grade_match
                                else (extracted_book.grade or "고1")
                            )
                            book_id = f"{sanitize_id_part(book_title_in)}__{sanitize_id_part(subject_grade_in)}"
                            record = ConceptBookRecord(
                                id=book_id,
                                book_title=book_title_in.strip(),
                                subject=subject_grade_in.strip(),
                                grade=grade_str,
                                page_ranges_spec=page_ranges_in.strip(),
                                units=extracted_book.units,
                                created_by=current_user.username,
                                created_at=datetime.now().isoformat(timespec="seconds"),
                            )
                            save_err = storage.save_concept_book(record)
                            if save_err:
                                st.warning(save_err)
                            else:
                                st.success(
                                    f"🎉 공용 개념서 DB 저장 완료! (`{record.book_title}` · 총 {len(record.units)}개 단원 구조화 · "
                                    + (
                                        "로컬 ./concept_db 및 Google Drive 동기화 완료)"
                                        if storage.drive_connected
                                        else "로컬 ./concept_db 저장 완료)"
                                    )
                                )
                        except Exception as exc:
                            st.error(f"개념서 단원별 구조화 추출 실패: {exc}")

    # 등록된 공용 개념서 DB 목록 조회 및 관리
    st.markdown("---")
    st.markdown("#### 🗂️ 등록된 공용 개념서·교과서 DB 목록 (`./concept_db` & Google Drive)")
    saved_books = storage.load_concept_books()
    if not saved_books:
        st.info("아직 등록된 공용 개념서 DB가 없습니다. 위에서 개념서 PDF를 업로드해 첫 공용 DB를 구축해 보세요.")
    else:
        for b_idx, book in enumerate(saved_books):
            with st.container(border=True):
                b_col1, b_col2, b_col3 = st.columns([3, 1.2, 0.8])
                with b_col1:
                    st.markdown(f"##### 📗 {book.book_title} (`{book.subject}`)")
                    st.caption(
                        f"단원 수: **{len(book.units)}개** · 추출 페이지 구간: `{book.page_ranges_spec}` · "
                        f"등록자: @{book.created_by or 'admin'}"
                    )
                with b_col2:
                    schema_dict = {
                        "book_title": book.book_title,
                        "subject": book.subject,
                        "grade": book.grade,
                        "units": [u.model_dump() for u in book.units],
                    }
                    st.download_button(
                        "📦 단원별 JSON 다운로드",
                        data=json.dumps(schema_dict, ensure_ascii=False, indent=2).encode("utf-8"),
                        file_name=f"{sanitize_id_part(book.book_title)}_concept_db.json",
                        mime="application/json",
                        key=f"{key_prefix}_dl_cb_{b_idx}_{book.id}",
                        use_container_width=True,
                    )
                with b_col3:
                    if st.button(
                        "🗑️ 삭제",
                        key=f"{key_prefix}_del_cb_{b_idx}_{book.id}",
                        use_container_width=True,
                    ):
                        del_err = storage.delete_concept_book(book.id)
                        if del_err:
                            st.warning(del_err)
                        st.rerun()

                with st.expander(f"📋 `{book.book_title}` 단원별 개념 및 허용 용어 상세 보기 ({len(book.units)}개 단원)", expanded=False):
                    for u_idx, unit in enumerate(book.units, start=1):
                        st.markdown(f"**{u_idx}. {unit.unit_name}** (`{unit.page_range}`)")
                        uc1, uc2 = st.columns(2)
                        with uc1:
                            st.caption("핵심 원리 및 정의 요약 (`core_concepts`)")
                            render_chips(unit.core_concepts, variant="blue", limit=60)
                        with uc2:
                            st.caption("허용 용어·공식·단위 (`allowed_terms_and_units`)")
                            render_chips(unit.allowed_terms_and_units, variant="green", limit=80)
                        st.markdown("---")


if is_admin:
    tab1, tab2, tab3, admin_tab = st.tabs(
        [
            "📥 탭 1: 모의고사 수집 & 2022 원리·용어 분해",
            "🎯 탭 2: 개념서 범위 추출 & 상위 문제 개조",
            "📚 탭 3: 공용 개념서·교과서 DB 구축",
            "🛡️ 관리자: 회원 등급 관리",
        ]
    )
else:
    tab1, tab2, tab3 = st.tabs(
        [
            "📥 탭 1: 모의고사 수집 & 2022 원리·용어 분해",
            "🎯 탭 2: 개념서 범위 추출 & 상위 문제 개조",
            "📚 탭 3: 공용 개념서·교과서 DB 구축",
        ]
    )
    admin_tab = None

# ===========================================================================
# TAB 1: 모의고사 수집 & 2022 개정 교육과정 원리·용어 분해 태깅
# ===========================================================================
with tab1:
    st.subheader("1️⃣ 기출 PDF 수집 및 고해상도 자동 크롭 (250 DPI)")

    collect_mode = st.radio(
        "수집 방식 선택",
        options=["📤 수동 업로드 (기본)", "🔗 EBSi 링크 수집 (보조)"],
        horizontal=True,
    )

    if collect_mode == "🔗 EBSi 링크 수집 (보조)":
        with st.container(border=True):
            ebsi_url = st.text_input(
                "EBSi 기출 문제지 URL 입력",
                placeholder="https://www.ebsi.co.kr/...",
            )
            if st.button("🌐 EBSi 링크에서 공개 PDF 추출 시도", use_container_width=False):
                with st.spinner("EBSi 페이지에서 공개 PDF 링크 확인 중..."):
                    ebsi_res = attempt_ebsi_pdf_fetch(ebsi_url)
                if ebsi_res.success and ebsi_res.problem_pdf_bytes:
                    st.session_state["ebsi_problem_bytes"] = ebsi_res.problem_pdf_bytes
                    st.session_state["ebsi_problem_name"] = ebsi_res.problem_filename
                    if ebsi_res.answer_pdf_bytes:
                        st.session_state["ebsi_answer_bytes"] = ebsi_res.answer_pdf_bytes
                        st.session_state["ebsi_answer_name"] = ebsi_res.answer_filename
                    st.success(ebsi_res.message)
                else:
                    st.warning(ebsi_res.message)
                    st.info("👉 아래 [수동 업로드] 영역에 내려받은 문제지·해설지 PDF를 직접 올려주세요.")

    with st.container(border=True):
        col_up1, col_up2 = st.columns(2)
        with col_up1:
            prob_file = st.file_uploader("문제지 PDF 업로드 (올리는 즉시 1페이지 자동 인식)", type=["pdf"], key="tab1_prob_pdf")
        with col_up2:
            ans_file = st.file_uploader(
                "해설지 PDF 업로드 (권장)", type=["pdf"], key="tab1_ans_pdf"
            )

        active_prob_bytes: bytes | None = (
            prob_file.getvalue() if prob_file is not None else st.session_state.get("ebsi_problem_bytes")
        )
        active_prob_name: str = (
            prob_file.name if prob_file is not None else st.session_state.get("ebsi_problem_name", "problem.pdf")
        )
        active_ans_bytes: bytes | None = (
            ans_file.getvalue() if ans_file is not None else st.session_state.get("ebsi_answer_bytes")
        )
        active_ans_name: str = (
            ans_file.name if ans_file is not None else st.session_state.get("ebsi_answer_name", "answer.pdf")
        )

        if st.session_state.get("ebsi_problem_bytes") and prob_file is None:
            st.caption(f"🔗 EBSi에서 가져온 문제지 대기 중: `{active_prob_name}`")
        if st.session_state.get("ebsi_answer_bytes") and ans_file is None:
            st.caption(f"🔗 EBSi에서 가져온 해설지 대기 중: `{active_ans_name}`")

        # 업로드 즉시 fitz + re 기반 0.05초 로컬 파싱 (API 호출 없음, file.name + file.size 변경 감지 시 강제 덮어쓰기)
        file_sig = (
            f"{prob_file.name}:{prob_file.size}"
            if prob_file is not None
            else (f"{active_prob_name}:{len(active_prob_bytes)}" if active_prob_bytes else "")
        )

        if not file_sig:
            st.session_state["tab1_last_loaded_sig"] = ""
            st.session_state["tab1_meta_status"] = ""
        elif file_sig != st.session_state.get("tab1_last_loaded_sig"):
            parsed_local: dict[str, str] = {
                "year": "",
                "month": "",
                "grade": "",
                "subject": "",
                "exam_title": "",
            }
            has_p1_text = False
            if active_prob_bytes:
                try:
                    parsed_local, has_p1_text = extract_exam_metadata_fast(
                        active_prob_bytes,
                        filename=active_prob_name,
                    )
                except Exception:
                    parsed_local = {
                        "year": "",
                        "month": "",
                        "grade": "",
                        "subject": "",
                        "exam_title": "",
                    }
                    has_p1_text = False

            st.session_state["meta_year"] = parsed_local.get("year", "")
            st.session_state["meta_month"] = parsed_local.get("month", "")
            st.session_state["meta_grade"] = parsed_local.get("grade", "")
            st.session_state["meta_subject"] = parsed_local.get("subject", "")
            st.session_state["meta_title"] = parsed_local.get("exam_title", "")
            st.session_state["tab1_subject_cat"] = infer_subject_category(parsed_local.get("subject", ""))
            st.session_state["tab1_last_loaded_sig"] = file_sig

            if not has_p1_text:
                st.session_state["tab1_meta_status"] = "scanned"
            elif parsed_local.get("exam_title"):
                st.session_state["tab1_meta_status"] = "parsed"
            else:
                st.session_state["tab1_meta_status"] = "partial_empty"

        meta_status = st.session_state.get("tab1_meta_status", "")
        if file_sig:
            if meta_status == "parsed":
                st.success(
                    f"⚡ 1페이지 즉시 자동 인식 완료: **{st.session_state.get('meta_title', '')}** (필요 시 아래에서 바로 수정하세요)"
                )
            elif meta_status == "scanned":
                st.info("🖼️ 텍스트가 없는 스캔 PDF입니다. 아래 [시행 년도 / 시행 월 / 학년 / 과목 / 회차명]을 직접 입력해 주세요.")
            elif meta_status == "partial_empty":
                st.info("📄 1페이지 헤더 정보가 일부만 확인되어 빈칸으로 두었습니다. 아래 입력칸을 직접 채워 주세요.")

        # 분석 성공 여부와 관계없이 5개 입력칸(+대분류)은 항상 즉시 렌더링
        col_m1, col_m2, col_m3, col_m4 = st.columns(4)
        with col_m1:
            exam_year_input = st.text_input("시행 년도", key="meta_year", placeholder="예: 2026")
        with col_m2:
            exam_month_input = st.text_input("시행 월", key="meta_month", placeholder="예: 9월")
        with col_m3:
            exam_grade_input = st.text_input("학년", key="meta_grade", placeholder="예: 고1")
        with col_m4:
            exam_subject_input = st.text_input(
                "과목",
                key="meta_subject",
                placeholder="예: 통합사회",
            )

        col_meta1, col_meta2 = st.columns([1, 2])
        with col_meta1:
            subject_cat = st.selectbox("과목 (대분류)", options=["과학", "수학", "사회"], key="tab1_subject_cat")
        with col_meta2:
            exam_name_input = st.text_input(
                "회차명 (자동 조합)",
                key="meta_title",
                placeholder="예: 2026년 9월 고1 통합사회",
            )

        parse_col1, parse_col2 = st.columns([1, 1])
        with parse_col1:
            run_crop_btn = st.button("✂️ 1단계: 2단 편집 분석 및 고해상도 문항 크롭", type="primary", use_container_width=True)
        with parse_col2:
            vision_fallback_chk = st.checkbox(
                "스캔(이미지) PDF인 경우 Gemini Vision 좌표 탐지 보조 사용",
                value=True,
            )

        if run_crop_btn:
            if not active_prob_bytes:
                st.error("문제지 PDF를 업로드하거나 EBSi에서 먼저 추출해 주세요.")
            elif not exam_name_input.strip():
                st.error("시험 회차명을 입력해 주세요.")
            else:
                with st.spinner("PyMuPDF 2단 편집(좌→우 칼럼) 분석 및 250 DPI PNG 크롭 중..."):
                    crops, has_text = parse_exam_pdf(active_prob_bytes, dpi=250)
                    total_p = get_pdf_page_count(active_prob_bytes)

                    # 스캔 PDF 또는 일부 페이지만 이미지(텍스트 부족)인 경우 1페이지씩 순차 Gemini Vision 보완
                    if vision_fallback_chk and active_gemini_key:
                        page_text_lens = get_page_text_lengths(active_prob_bytes)
                        cropped_pages = {c.page_num for c in crops}
                        if not crops or not has_text:
                            target_vision_pages = list(range(1, total_p + 1))
                        else:
                            target_vision_pages = [
                                idx + 1
                                for idx, tlen in enumerate(page_text_lens)
                                if tlen < 40 and (idx + 1) not in cropped_pages
                            ]

                        if target_vision_pages:
                            st.info(
                                f"텍스트가 부족한 {len(target_vision_pages)}개 페이지를 "
                                "Gemini Vision으로 1페이지씩 순차 탐지합니다..."
                            )
                            existing_by_q: dict[int, CroppedQuestion] = {c.q_num: c for c in crops}
                            prog = st.progress(0.0)
                            for step_i, p_num in enumerate(target_vision_pages, start=1):
                                page_png = render_pdf_page_png(active_prob_bytes, p_num, dpi=180)
                                boxes = detect_scanned_question_boxes(
                                    active_gemini_key, selected_model, page_png
                                )
                                # 왼쪽 단(xmin < 500) 위->아래, 오른쪽 단(xmin >= 500) 위->아래 순 정렬
                                boxes.sort(key=lambda b: (0 if b.xmin < 500 else 1, b.ymin, b.xmin))
                                for b in boxes:
                                    if b.q_num in existing_by_q:
                                        continue
                                    png_b, pw, ph = crop_normalized_box(
                                        active_prob_bytes,
                                        p_num,
                                        b.ymin,
                                        b.xmin,
                                        b.ymax,
                                        b.xmax,
                                        dpi=250,
                                    )
                                    existing_by_q[b.q_num] = CroppedQuestion(
                                        q_num=b.q_num,
                                        page_num=p_num,
                                        png_bytes=png_b,
                                        width=pw,
                                        height=ph,
                                        extracted_text="",
                                    )
                                prog.progress(step_i / max(1, len(target_vision_pages)))
                            crops = sorted(existing_by_q.values(), key=lambda x: x.q_num)

                if not crops:
                    st.error("문항 번호를 탐지하지 못했습니다. PDF 형식을 확인하거나 Gemini API 키 설정 후 Vision 보조를 활성화해 주세요.")
                else:
                    st.session_state["parsed_crops"] = crops
                    st.session_state["parsed_exam_meta"] = {
                        "exam_name": exam_name_input.strip(),
                        "subject_category": subject_cat,
                        "prob_bytes": active_prob_bytes,
                        "prob_name": active_prob_name,
                        "ans_bytes": active_ans_bytes,
                        "ans_name": active_ans_name,
                    }
                    st.session_state["stage2_exam_name"] = ""
                    st.session_state["stage2_saved_q_nums"] = []
                    st.session_state["stage2_failed_q_nums"] = []
                    st.session_state["stage2_failed_errors"] = []
                    st.session_state["stage2_sync_err"] = ""
                    st.session_state["trigger_retry_failed"] = False
                    found_nums = sorted({c.q_num for c in crops})
                    min_q, max_q = found_nums[0], found_nums[-1]
                    subj_txt = (exam_subject_input or "").strip()
                    if any(k in subj_txt for k in ("국어", "영어")):
                        expected_max = 45
                    elif any(k in subj_txt for k in ("수학", "미적분", "기하", "확률")):
                        expected_max = 30
                    elif any(k in subj_txt for k in ("사회", "과학", "물리", "화학", "생명", "지구", "한국사", "윤리", "지리", "경제", "정치", "역사")):
                        expected_max = 20
                    else:
                        expected_max = max_q

                    range_end = max(max_q, expected_max) if total_p >= 3 else max_q
                    missing_in_found = [n for n in range(1, max_q + 1) if n not in set(found_nums)]
                    missing_tail = [n for n in range(max_q + 1, range_end + 1)]

                    st.success(f"✅ 총 {len(crops)}개 문항 크롭 완료 ({min_q}번 ~ {max_q}번)")
                    if missing_in_found or missing_tail:
                        missing_all = missing_in_found + missing_tail
                        st.warning(
                            f"⚠️ 누락된 문항 번호 ({len(missing_all)}개, 1~{range_end}번 기준): "
                            + ", ".join(f"{n}번" for n in missing_all)
                        )

    parsed_crops: list[CroppedQuestion] = st.session_state.get("parsed_crops") or []
    if parsed_crops:
        meta = st.session_state["parsed_exam_meta"]
        st.markdown(f"### 2️⃣ 크롭 문항 확인 & 2022 원리·용어 분해 태깅 (`{meta['exam_name']}`)")

        with st.expander(f"🖼️ 크롭된 문항 미리보기 ({len(parsed_crops)}문항)", expanded=False):
            cols_per_row = 3
            for idx in range(0, len(parsed_crops), cols_per_row):
                row_cols = st.columns(cols_per_row)
                for col_i, cq in enumerate(parsed_crops[idx : idx + cols_per_row]):
                    with row_cols[col_i]:
                        st.image(
                            cq.png_bytes,
                            caption=f"{cq.q_num}번 ({cq.page_num}쪽 · {cq.width}x{cq.height}px)",
                            use_container_width=True,
                        )

        q_nums_all = [c.q_num for c in parsed_crops]
        selected_q_nums = st.multiselect(
            "태깅 및 DB 저장할 문항 선택 (기본: 전체)",
            options=q_nums_all,
            default=q_nums_all,
        )

        def _run_stage2_extraction(target_q_nums: list[int], is_retry: bool = False) -> None:
            # 등급별 API 키 전달 경로 즉시 검증 (ADMIN/PRO/FREE -> st.secrets, CUSTOM -> custom_api_key)
            verified_key, verified_key_label = resolve_active_gemini_api_key(
                current_user, creds.gemini_api_key
            )
            if not verified_key:
                st.error(
                    f"API 키가 설정되지 않았습니다 ({verified_key_label}). "
                    "ADMIN/PRO/FREE 등급은 `.streamlit/secrets.toml`의 `GEMINI_API_KEY`를, "
                    "CUSTOM 등급은 사이드바의 개인 Gemini API 키를 확인해 주세요."
                )
                return
            if not target_q_nums:
                st.warning("태깅할 문항을 최소 1개 이상 선택해 주세요.")
                return

            safe_exam = sanitize_id_part(meta["exam_name"])
            sync_notice = st.warning(
                "☁️ **\"Google Drive 동기화 완료\"** 문구가 뜰 때까지 브라우저 창을 닫지 말고 유지해 주세요. "
                "(매 문항은 서버 로컬 `./exam_db`에 즉시 저장되고, 5문항마다 중간 백업 + 완료 직후 일괄 동기화됩니다.)"
            )
            status_placeholder = st.empty()
            log_placeholder = st.empty()
            live_logs: list[str] = []

            def _push_status(msg: str) -> None:
                status_placeholder.info(msg)

            def _append_log(line: str) -> None:
                live_logs.append(line)
                log_placeholder.code("\n".join(live_logs[-15:]), language="text")

            mode_label = "실패 문항 재분석" if is_retry else "전체 문항 분석"
            _push_status(f"🔑 API 키 검증 완료 ({verified_key_label}) · {mode_label} 준비 중...")
            pending_pdf_files: list[str] = []
            pending_crop_files: list[str] = []
            if not is_retry:
                if meta.get("prob_bytes"):
                    prob_fn = f"{safe_exam}__문제지.pdf"
                    storage.save_pdf_file(
                        prob_fn,
                        meta["prob_bytes"],
                        subject_category=meta["subject_category"],
                        exam_name=meta["exam_name"],
                        sync_drive=False,
                    )
                    pending_pdf_files.append(prob_fn)
                if meta.get("ans_bytes"):
                    ans_fn = f"{safe_exam}__해설지.pdf"
                    storage.save_pdf_file(
                        ans_fn,
                        meta["ans_bytes"],
                        subject_category=meta["subject_category"],
                        exam_name=meta["exam_name"],
                        sync_drive=False,
                    )
                    pending_pdf_files.append(ans_fn)

            # 해설지 경량화: 텍스트 PDF면 문항별 스니펫만 추출(0.05초), 스캔 PDF일 때만 30초 타임아웃 클라이언트로 1회 첨부
            answer_snippets: dict[int, str] = {}
            answer_pdf_part = None
            if meta.get("ans_bytes"):
                _push_status("📄 해설지 PDF 분석 중 (텍스트 스니펫 우선 추출)...")
                try:
                    answer_snippets, ans_has_text = extract_answer_snippets_from_pdf(meta["ans_bytes"])
                    if ans_has_text:
                        _append_log(f"📄 해설지 텍스트 스니펫 추출 완료 ({len(answer_snippets)}개 문항 매칭 · 경량 전송)")
                    else:
                        tmp_client = build_gemini_client(verified_key, timeout_seconds=30)
                        answer_pdf_part = prepare_pdf_part(
                            tmp_client,
                            meta["ans_bytes"],
                            display_name=f"{safe_exam}_answer.pdf",
                        )
                        _append_log("📄 스캔 해설지 PDF 파트 준비 완료 (30초 타임아웃)")
                except Exception as exc:
                    st.warning(f"해설지 첨부 준비 중 경고 (문제 이미지만으로 진행): {exc}")
                    _append_log(f"⚠️ 해설지 준비 경고: {exc}")

            target_set = set(target_q_nums)
            target_crops = sorted(
                [c for c in parsed_crops if c.q_num in target_set],
                key=lambda c: c.q_num,
            )
            existing_records = storage.load_db()
            rec_map = {r.id: r for r in existing_records}

            prog_bar = st.progress(0.0)
            newly_saved: list[QuestionRecord] = []
            failed_q_nums: list[int] = []
            errors: list[str] = []

            max_workers = 4
            batch_t0 = time.perf_counter()
            _push_status(
                f"⚡ 최대 {min(max_workers, max(1, len(target_crops)))}문항 동시 병렬 분석 시작 "
                f"(총 {len(target_crops)}문항 · 모델: {MODEL_LABELS.get(selected_model, selected_model)})..."
            )

            def _analyze_worker(cq_item: CroppedQuestion) -> tuple[CroppedQuestion, Any, float]:
                t_start = time.perf_counter()
                extracted_out = analyze_question_with_gemini(
                    api_key=verified_key,
                    model_name=selected_model,
                    crop_png_bytes=cq_item.png_bytes,
                    q_num=cq_item.q_num,
                    subject_category=meta["subject_category"],
                    exam_name=meta["exam_name"],
                    answer_pdf_part=answer_pdf_part,
                    answer_text_hint=answer_snippets.get(cq_item.q_num, ""),
                    status_callback=None,
                )
                return cq_item, extracted_out, time.perf_counter() - t_start

            done_count = 0
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_cq = {
                    executor.submit(_analyze_worker, cq): cq for cq in target_crops
                }
                for fut in concurrent.futures.as_completed(future_to_cq):
                    cq = future_to_cq[fut]
                    done_count += 1
                    prefix = f"[{done_count}/{len(target_crops)} · {cq.q_num}번]"
                    try:
                        _, extracted, elapsed_s = fut.result()
                        q_id = f"{safe_exam}__Q{cq.q_num:02d}"
                        img_filename = f"{q_id}.png"
                        storage.save_crop_png(
                            img_filename,
                            cq.png_bytes,
                            subject_category=meta["subject_category"],
                            exam_name=meta["exam_name"],
                            sync_drive=False,
                        )
                        pending_crop_files.append(img_filename)

                        record = QuestionRecord(
                            id=q_id,
                            exam_name=meta["exam_name"],
                            q_num=cq.q_num,
                            subject_category=meta["subject_category"],
                            curriculum_subject=extracted.curriculum_subject,
                            unit_name=extracted.unit_name,
                            min_grade=extracted.min_grade,
                            core_principles=extracted.core_principles,
                            required_terms=extracted.required_terms,
                            is_single_concept=extracted.is_single_concept,
                            answer=extracted.answer,
                            official_explanation=extracted.official_explanation,
                            image_filename=img_filename,
                            source_pdf=f"{safe_exam}__문제지.pdf",
                            answer_pdf=f"{safe_exam}__해설지.pdf" if meta.get("ans_bytes") else "",
                            created_at=datetime.now().isoformat(timespec="seconds"),
                        )
                        rec_map[q_id] = record
                        newly_saved.append(record)

                        # 매 완료 문항 로컬 즉시 체크포인트 (Drive 호출 없음)
                        storage.save_db(
                            list(rec_map.values()),
                            sync_organized_drive=False,
                            sync_db_drive=False,
                            sync_local_organized=False,
                        )
                        _push_status(f"{prefix} ✅ 분석 완료 ({elapsed_s:.1f}초) · 병렬 진행 중...")
                        _append_log(
                            f"✅ {cq.q_num}번 완료 ({elapsed_s:.1f}초) · {record.curriculum_subject} / {record.unit_name} / 정답 {record.answer}"
                        )

                        # 5문항 완료마다 Google Drive 중간 백업 동기화 1회 실행
                        if len(newly_saved) % 5 == 0 and done_count < len(target_crops):
                            _push_status(
                                f"☁️ 중간 백업 동기화 중 ({len(newly_saved)}문항 완료) · "
                                "'Google Drive 동기화 완료'가 뜰 때까지 창을 유지해 주세요..."
                            )
                            mid_err = storage.sync_exam_batch(
                                subject_category=meta["subject_category"],
                                exam_name=meta["exam_name"],
                                records=sorted(rec_map.values(), key=lambda r: (r.exam_name, r.q_num)),
                                crop_filenames=pending_crop_files,
                                pdf_filenames=pending_pdf_files,
                                include_organized_exports=False,
                            )
                            if mid_err:
                                _append_log(f"⚠️ 중간 백업 경고: {mid_err}")
                            else:
                                pending_crop_files.clear()
                                pending_pdf_files.clear()
                                _append_log(f"☁️ 5문항 중간 백업 동기화 완료 (누적 {len(newly_saved)}문항)")
                    except Exception as exc:
                        err_msg = f"{cq.q_num}번 오류 (건너뜀): {exc}"
                        failed_q_nums.append(cq.q_num)
                        errors.append(err_msg)
                        _push_status(f"{prefix} ❌ 실패 → 다른 병렬 문항 진행 중 ({exc})")
                        _append_log(f"❌ {err_msg}")

                    prog_bar.progress(done_count / len(target_crops))

            # 모든 병렬 작업 완료 후 문항 번호순(1번~끝번) 정렬 및 로컬·Drive 최종 일괄 동기화
            newly_saved.sort(key=lambda r: r.q_num)
            sorted_all_records = sorted(rec_map.values(), key=lambda r: (r.exam_name, r.q_num))
            pending_crop_files.sort()

            _push_status(
                "☁️ 문항 번호순 정렬 및 Google Drive 최종 일괄 동기화 중... "
                "('Google Drive 동기화 완료'가 뜰 때까지 창을 유지해 주세요)"
            )
            sync_err = storage.sync_exam_batch(
                subject_category=meta["subject_category"],
                exam_name=meta["exam_name"],
                records=sorted_all_records,
                crop_filenames=pending_crop_files,
                pdf_filenames=pending_pdf_files,
                include_organized_exports=True,
            )
            total_batch_s = time.perf_counter() - batch_t0
            _append_log(f"🏁 전체 병렬 분석 및 일괄 동기화 완료 (총 {total_batch_s:.1f}초)")
            sync_notice.empty()
            status_placeholder.empty()

            prev_saved = (
                st.session_state.get("stage2_saved_q_nums", [])
                if is_retry and st.session_state.get("stage2_exam_name") == meta["exam_name"]
                else []
            )
            merged_saved = sorted(set(prev_saved) | {r.q_num for r in newly_saved})
            st.session_state["stage2_exam_name"] = meta["exam_name"]
            st.session_state["stage2_saved_q_nums"] = merged_saved
            st.session_state["stage2_failed_q_nums"] = sorted(set(failed_q_nums))
            st.session_state["stage2_failed_errors"] = errors
            st.session_state["stage2_sync_err"] = sync_err or ""

        if st.button("🧠 2단계: Gemini 원리·용어 분리 태깅 및 영구 저장", type="primary"):
            _run_stage2_extraction(selected_q_nums, is_retry=False)
        elif st.session_state.pop("trigger_retry_failed", False):
            pending_retry = [
                n for n in st.session_state.get("stage2_failed_q_nums", []) if isinstance(n, int)
            ]
            if pending_retry and st.session_state.get("stage2_exam_name") == meta["exam_name"]:
                _run_stage2_extraction(pending_retry, is_retry=True)

        if st.session_state.get("stage2_exam_name") == meta["exam_name"]:
            saved_q_nums = st.session_state.get("stage2_saved_q_nums") or []
            failed_q_nums_state = st.session_state.get("stage2_failed_q_nums") or []
            failed_errors_state = st.session_state.get("stage2_failed_errors") or []
            sync_err_state = st.session_state.get("stage2_sync_err") or ""

            if saved_q_nums:
                st.success(
                    f"🎉 `{meta['exam_name']}` 총 {len(saved_q_nums)}개 문항 태깅 및 영구 저장 완료!"
                    + (" (Google Drive 동기화 완료)" if storage.drive_connected else " (로컬 ./exam_db 저장)")
                )
            if sync_err_state:
                st.warning(sync_err_state)
            for err in failed_errors_state:
                st.error(err)

            if failed_q_nums_state:
                preview_nums = ", ".join(f"{n}번" for n in failed_q_nums_state[:8])
                suffix = " ..." if len(failed_q_nums_state) > 8 else ""
                st.button(
                    f"🔄 실패한 {len(failed_q_nums_state)}개 문항만 다시 분석하기 ({preview_nums}{suffix})",
                    type="primary",
                    key="retry_failed_questions_btn",
                    on_click=lambda: st.session_state.update({"trigger_retry_failed": True}),
                )

            if saved_q_nums:
                saved_set = set(saved_q_nums)
                saved_records = sorted(
                    [
                        r
                        for r in storage.load_db()
                        if r.exam_name == meta["exam_name"] and r.q_num in saved_set
                    ],
                    key=lambda r: r.q_num,
                )
                for rec in saved_records:
                    with st.expander(
                        f"✅ {rec.q_num}번 · [{rec.curriculum_subject}] {rec.unit_name} (고{rec.min_grade} · 정답: {rec.answer})"
                    ):
                        c_img, c_info = st.columns([1, 1.3])
                        with c_img:
                            b = storage.get_crop_bytes(
                                rec.image_filename,
                                subject_category=rec.subject_category,
                                exam_name=rec.exam_name,
                            )
                            if b:
                                st.image(b, use_container_width=True)
                        with c_info:
                            st.markdown("**본질적 풀이 원리 (`core_principles`)**")
                            render_chips(rec.core_principles, variant="blue")
                            st.markdown("**표면 전문 용어·단위 (`required_terms`)**")
                            render_chips(rec.required_terms, variant="amber")
                            st.write(f"**단일 개념 여부:** {'예 (단일 원리)' if rec.is_single_concept else '아니오 (복합 원리)'}")
                            st.write(f"**공식 해설 요약:** {rec.official_explanation}")

    # 저장된 DB 조회 및 과목·회차별 관리
    st.markdown("---")
    st.subheader("3️⃣ 과목별 · 모의고사 회차별 기출 DB 탐색 & 파일 내보내기")
    if storage.drive_connected:
        st.caption(
            f"☁️ Google Drive `{storage.drive_folder_name}/<과목>/<회차명>/` 폴더에 "
            "`기출정리.html`, `기출정리.md`, `문제목록.json`, 크롭 PNG, 원본 PDF가 자동 정리됩니다."
        )
    else:
        st.caption(
            "💾 로컬 `./exam_db/과목별_기출DB/<과목>/<회차명>/` 폴더에 "
            "`기출정리.html`, `기출정리.md`, `문제목록.json`, 크롭 PNG, 원본 PDF가 자동 정리됩니다."
        )

    db_records = storage.load_db()

    if is_admin:
        with st.expander("🧹 관리자: 누적 기출 DB 전체 초기화 (회원 계정은 유지)", expanded=False):
            st.warning("로컬(`./exam_db`) 및 Google Drive에 누적된 기출 문항·크롭 이미지·PDF 파일을 모두 삭제합니다. (`users.json` 회원 정보는 유지됩니다.)")
            confirm_wipe = st.checkbox("정말 모든 기출 DB 파일을 초기화합니다.", key="confirm_db_wipe")
            if st.button("🗑️ 기출 DB 전체 초기화 실행", disabled=not confirm_wipe, key="exec_db_wipe"):
                wipe_res = storage.reset_exam_database(delete_users=False)
                if wipe_res.get("drive_error"):
                    st.error(f"Drive 초기화 중 오류: {wipe_res['drive_error']}")
                else:
                    st.success(
                        f"초기화 완료! (로컬 항목 {wipe_res['removed_local']}개, Drive 항목 {wipe_res['trashed_drive']}개 정리)"
                    )
                st.rerun()

    if not db_records:
        st.info("아직 저장된 기출 문항이 없습니다. 위에서 모의고사 PDF를 수집·태깅해 보세요.")
    else:
        subject_order = ["전체", "수학", "사회", "과학"]
        subj_counts = {
            s: (len(db_records) if s == "전체" else sum(1 for r in db_records if r.subject_category == s))
            for s in subject_order
        }
        subj_tabs = st.tabs([f"{s} ({subj_counts[s]})" for s in subject_order])

        for tab_obj, subj_label in zip(subj_tabs, subject_order):
            with tab_obj:
                subj_recs = (
                    list(db_records)
                    if subj_label == "전체"
                    else [r for r in db_records if r.subject_category == subj_label]
                )
                if not subj_recs:
                    st.info(f"'{subj_label}' 과목에 저장된 모의고사가 없습니다.")
                    continue

                exam_groups: dict[tuple[str, str], list[QuestionRecord]] = {}
                for rec in subj_recs:
                    exam_groups.setdefault((rec.subject_category, rec.exam_name), []).append(rec)

                for (subj_cat, exam_name) in sorted(
                    exam_groups.keys(), key=lambda x: (x[0], x[1]), reverse=True
                ):
                    exam_recs = sorted(exam_groups[(subj_cat, exam_name)], key=lambda r: r.q_num)
                    safe_fn_prefix = sanitize_id_part(exam_name)

                    with st.container(border=True):
                        head_col1, head_col2 = st.columns([3, 1])
                        with head_col1:
                            units_preview = sorted({r.unit_name for r in exam_recs if r.unit_name})
                            st.markdown(f"#### 📘 {exam_name}")
                            st.caption(
                                f"과목: **{subj_cat}** · 저장 문항: **{len(exam_recs)}문항** · 단원: {', '.join(units_preview[:4])}"
                                + (" 등" if len(units_preview) > 4 else "")
                            )
                        with head_col2:
                            if st.button(
                                "🗑️ 회차 삭제",
                                key=f"del_exam_{subj_label}_{subj_cat}_{safe_fn_prefix}",
                                use_container_width=True,
                            ):
                                cnt = storage.delete_exam(exam_name)
                                st.success(f"'{exam_name}' ({cnt}문항) 삭제 완료")
                                st.rerun()

                        # 회차별 정리 파일 & 원본 PDF 다운로드 바
                        dl_c1, dl_c2, dl_c3, dl_c4, dl_c5 = st.columns(5)
                        with dl_c1:
                            html_str = build_exam_html(
                                exam_name,
                                subj_cat,
                                exam_recs,
                                crop_loader=lambda fn, s=subj_cat, e=exam_name: storage.get_crop_bytes(
                                    fn, subject_category=s, exam_name=e
                                ),
                            )
                            st.download_button(
                                "🌐 웹페이지(HTML)",
                                data=html_str.encode("utf-8"),
                                file_name=f"{safe_fn_prefix}_기출정리.html",
                                mime="text/html",
                                key=f"dl_html_{subj_label}_{subj_cat}_{safe_fn_prefix}",
                                use_container_width=True,
                            )
                        with dl_c2:
                            md_str = build_exam_markdown(exam_name, subj_cat, exam_recs)
                            st.download_button(
                                "📝 정리본(MD)",
                                data=md_str.encode("utf-8"),
                                file_name=f"{safe_fn_prefix}_기출정리.md",
                                mime="text/markdown",
                                key=f"dl_md_{subj_label}_{subj_cat}_{safe_fn_prefix}",
                                use_container_width=True,
                            )
                        with dl_c3:
                            json_str = json.dumps(
                                [r.model_dump() for r in exam_recs],
                                ensure_ascii=False,
                                indent=2,
                            )
                            st.download_button(
                                "📦 JSON",
                                data=json_str.encode("utf-8"),
                                file_name=f"{safe_fn_prefix}_문제목록.json",
                                mime="application/json",
                                key=f"dl_json_{subj_label}_{subj_cat}_{safe_fn_prefix}",
                                use_container_width=True,
                            )

                        prob_pdf_name = next((r.source_pdf for r in exam_recs if r.source_pdf), "")
                        ans_pdf_name = next((r.answer_pdf for r in exam_recs if r.answer_pdf), "")
                        with dl_c4:
                            prob_bytes = (
                                storage.get_pdf_bytes(prob_pdf_name, subject_category=subj_cat, exam_name=exam_name)
                                if prob_pdf_name
                                else None
                            )
                            if prob_bytes:
                                st.download_button(
                                    "📕 문제지 PDF",
                                    data=prob_bytes,
                                    file_name=prob_pdf_name,
                                    mime="application/pdf",
                                    key=f"dl_prob_{subj_label}_{subj_cat}_{safe_fn_prefix}",
                                    use_container_width=True,
                                )
                        with dl_c5:
                            ans_bytes = (
                                storage.get_pdf_bytes(ans_pdf_name, subject_category=subj_cat, exam_name=exam_name)
                                if ans_pdf_name
                                else None
                            )
                            if ans_bytes:
                                st.download_button(
                                    "📗 해설지 PDF",
                                    data=ans_bytes,
                                    file_name=ans_pdf_name,
                                    mime="application/pdf",
                                    key=f"dl_ans_{subj_label}_{subj_cat}_{safe_fn_prefix}",
                                    use_container_width=True,
                                )

                        with st.expander(f"📋 '{exam_name}' 문항 상세 보기 ({len(exam_recs)}문항)", expanded=False):
                            for rec in exam_recs:
                                st.markdown(
                                    f"**{rec.q_num}번** · `{rec.curriculum_subject}` · {rec.unit_name} "
                                    f"(고{rec.min_grade}, {'단일 원리' if rec.is_single_concept else '복합 원리'}, 정답 `{rec.answer}`)"
                                )
                                col_a, col_b = st.columns([1, 1.3])
                                with col_a:
                                    img_b = storage.get_crop_bytes(
                                        rec.image_filename,
                                        subject_category=rec.subject_category,
                                        exam_name=rec.exam_name,
                                    )
                                    if img_b:
                                        st.image(img_b, use_container_width=True)
                                with col_b:
                                    st.markdown("**본질 원리 (`core_principles`)**")
                                    render_chips(rec.core_principles, variant="blue")
                                    st.markdown("**표면 용어 (`required_terms`)**")
                                    render_chips(rec.required_terms, variant="amber")
                                    st.write(f"**공식 해설:** {rec.official_explanation}")
                                    if st.button(
                                        "이 문항 삭제",
                                        key=f"del_q_{subj_label}_{rec.id}",
                                    ):
                                        storage.delete_question(rec.id)
                                        st.rerun()
                                st.markdown("---")


# ===========================================================================
# TAB 2: 개념서·교과서 PDF 분석 → 엄격한 범위 내 추출 & 상위 문제 개조
# ===========================================================================
with tab2:
    st.subheader("1️⃣ 기준 개념서 범위 설정 (공용 개념서 DB 선택 또는 PDF 직접 업로드)")

    source_tab_db, source_tab_pdf = st.tabs(
        [
            "📚 공용 개념서·교과서 DB에서 단원 선택 (Pro · ADMIN 전용)",
            "📄 개념서·교과서 PDF 1회성 직접 업로드",
        ]
    )

    with source_tab_db:
        has_concept_db_access = can_access_shared_concept_db(current_user)
        shared_books = storage.load_concept_books()

        if not has_concept_db_access:
            st.warning(
                "🔒 **공용 개념서·교과서 DB 불러오기는 Pro(VIP) · ADMIN 등급 전용 기능입니다.**\n\n"
                f"현재 등급(**{TIER_LABELS.get(current_user.tier, current_user.tier)}**)에서는 잠금 처리되어 있으며, "
                "우측 **[📄 개념서·교과서 PDF 1회성 직접 업로드]** 탭에서 PDF를 직접 올려 이용하실 수 있습니다."
            )
            locked_options = (
                [f"🔒 {b.book_title} ({b.subject})" for b in shared_books]
                if shared_books
                else ["🔒 등록된 공용 개념서 DB (Pro 이상 전용)"]
            )
            st.selectbox(
                "공용 개념서·교과서 선택 (잠금)",
                options=locked_options,
                disabled=True,
                key="locked_shared_book_select",
            )
        else:
            if not shared_books:
                st.info(
                    "아직 Google Drive / `./concept_db`에 저장된 공용 개념서 DB가 없습니다. "
                    "상단 **[📚 탭 3: 공용 개념서·교과서 DB 구축]** 또는 아래 패널에서 교재 PDF를 먼저 등록해 주세요."
                )
            else:
                book_labels = [
                    f"{b.book_title} ({b.subject} · {len(b.units)}개 단원)" for b in shared_books
                ]
                sel_book_idx = st.selectbox(
                    "📖 공용 개념서·교과서 DB 선택",
                    options=list(range(len(shared_books))),
                    format_func=lambda idx: book_labels[idx],
                    key="tab2_shared_book_idx",
                )
                chosen_book = shared_books[sel_book_idx]
                st.caption(
                    f"선택 교재: **{chosen_book.book_title}** | 과목/학년: `{chosen_book.subject}` | "
                    f"추출 페이지 구간: `{chosen_book.page_ranges_spec}`"
                )

                st.markdown("**✅ 출제 및 개조 기준으로 사용할 단원을 체크하세요:**")
                select_all_units = st.checkbox(
                    "전체 단원 일괄 선택",
                    value=True,
                    key=f"sel_all_units_{chosen_book.id}",
                )
                selected_units_list: list[ConceptUnit] = []
                for u_i, unit_obj in enumerate(chosen_book.units):
                    checked = st.checkbox(
                        f"{unit_obj.unit_name} ({unit_obj.page_range}) — 핵심개념 {len(unit_obj.core_concepts)}개 · 허용용어 {len(unit_obj.allowed_terms_and_units)}개",
                        value=select_all_units,
                        key=f"unit_chk_{chosen_book.id}_{u_i}",
                    )
                    if checked:
                        selected_units_list.append(unit_obj)

                if st.button(
                    "📚 선택한 단원으로 지식 화이트리스트 즉시 적용",
                    type="primary",
                    use_container_width=True,
                    key="apply_shared_concept_units_btn",
                ):
                    if not selected_units_list:
                        st.error("최소 1개 이상의 단원을 체크해 주세요.")
                    else:
                        wl_from_db, ctx_text = build_whitelist_from_concept_units(
                            book_title=chosen_book.book_title,
                            subject=chosen_book.subject,
                            grade=chosen_book.grade,
                            selected_units=selected_units_list,
                        )
                        unit_names_short = ", ".join(
                            f"{u.unit_name}({u.page_range})" for u in selected_units_list[:3]
                        )
                        if len(selected_units_list) > 3:
                            unit_names_short += f" 외 {len(selected_units_list) - 3}개 단원"
                        st.session_state["concept_whitelist"] = wl_from_db
                        st.session_state["concept_sliced_text"] = ctx_text
                        st.session_state["concept_source_label"] = (
                            f"{chosen_book.book_title} - {unit_names_short}"
                        )
                        st.session_state["matched_items"] = []
                        st.session_state["generated_exam_pdf"] = None
                        st.success(
                            f"✅ 공용 개념서 `{chosen_book.book_title}`의 {len(selected_units_list)}개 단원이 출제·개조 기준으로 적용되었습니다!"
                        )

            with st.expander("➕ 새 개념서·교과서 PDF를 공용 DB에 바로 추가하기", expanded=not bool(shared_books)):
                _render_concept_book_builder_section(key_prefix="tab2_inline")

    with source_tab_pdf:
        concept_file = st.file_uploader(
            "통합과학·통합사회·공통수학 등 개념서/교과서 PDF 업로드",
            type=["pdf"],
            key="tab2_concept_pdf",
        )

        concept_pdf_bytes: bytes | None = concept_file.getvalue() if concept_file is not None else None
        if concept_pdf_bytes is not None:
            cur_pdf_hash = hashlib.sha256(concept_pdf_bytes).hexdigest()[:16]
            if st.session_state["concept_pdf_hash"] and st.session_state["concept_pdf_hash"] != cur_pdf_hash:
                st.session_state["concept_whitelist"] = None
                st.session_state["matched_items"] = []
                st.session_state["generated_exam_pdf"] = None
            st.session_state["concept_pdf_hash"] = cur_pdf_hash

            total_pages = get_pdf_page_count(concept_pdf_bytes)
            default_book_name = re.sub(r"\.pdf$", "", concept_file.name, flags=re.IGNORECASE)
            up_meta_c1, up_meta_c2 = st.columns([1, 1.4])
            with up_meta_c1:
                direct_book_title = st.text_input(
                    "교재명 (결과 출처 표기용)",
                    value=default_book_name,
                    placeholder="예: 완자 통합과학",
                    key="tab2_direct_book_title",
                )
            with up_meta_c2:
                multi_range_str = st.text_input(
                    "개념 페이지 범위 입력 (예: 12-25, 34-48 처럼 여러 구간 지정 가능)",
                    value=f"1-{min(total_pages, 5)}",
                    placeholder="예: 12-25, 34-48, 60-75",
                    key="tab2_direct_page_ranges",
                )

            parsed_direct_ranges: list[tuple[int, int]] = []
            try:
                parsed_direct_ranges = parse_page_ranges_spec(multi_range_str, total_pages)
                range_disp = ", ".join(
                    f"p.{s}~p.{e}" if s != e else f"p.{s}" for s, e in parsed_direct_ranges
                )
                sel_cnt = sum(e - s + 1 for s, e in parsed_direct_ranges)
                st.info(f"전체 {total_pages}쪽 중 **{range_disp}** (총 {sel_cnt}쪽) 선택됨")
            except ValueError as ve:
                st.error(str(ve))

            if parsed_direct_ranges:
                first_page_num = parsed_direct_ranges[0][0]
                last_page_num = parsed_direct_ranges[-1][1]
                with st.expander("👀 선택한 범위 첫 페이지(및 끝 페이지) 미리보기", expanded=True):
                    prev_c1, prev_c2 = st.columns(2)
                    with prev_c1:
                        st.image(
                            render_pdf_page_png(concept_pdf_bytes, first_page_num, dpi=140),
                            caption=f"시작 페이지 ({first_page_num}쪽)",
                            use_container_width=True,
                        )
                    with prev_c2:
                        if last_page_num != first_page_num:
                            st.image(
                                render_pdf_page_png(concept_pdf_bytes, last_page_num, dpi=140),
                                caption=f"끝 페이지 ({last_page_num}쪽)",
                                use_container_width=True,
                            )
                        else:
                            st.caption("단일 페이지가 선택되었습니다.")

                if st.button("🔍 1단계: 업로드된 PDF 지식 화이트리스트 추출", type="primary"):
                    if not active_gemini_key:
                        st.error(f"사용 가능한 Gemini API 키({active_key_label})가 설정되지 않았습니다.")
                    else:
                        with st.spinner(f"PDF ({range_disp})의 순수 개념 본문·도표·용어를 정독해 화이트리스트를 구축 중..."):
                            sliced_bytes, sliced_text, slice_stats = extract_concept_pages_by_ranges(
                                concept_pdf_bytes, parsed_direct_ranges
                            )
                            try:
                                wl = extract_concept_whitelist(
                                    api_key=active_gemini_key,
                                    model_name=selected_model,
                                    sliced_pdf_bytes=sliced_bytes,
                                    sliced_text=sliced_text,
                                    start_page=first_page_num,
                                    end_page=last_page_num,
                                    book_title=direct_book_title.strip() or default_book_name,
                                    range_display=slice_stats["ranges_display"],
                                )
                                st.session_state["concept_whitelist"] = wl
                                st.session_state["concept_sliced_text"] = sliced_text
                                st.session_state["concept_source_label"] = (
                                    f"{direct_book_title.strip() or concept_file.name} ({slice_stats['ranges_display']})"
                                )
                                st.session_state["matched_items"] = []
                                st.session_state["generated_exam_pdf"] = None
                                st.success("✅ PDF 지식 화이트리스트 추출이 완료되었습니다!")
                            except Exception as exc:
                                st.error(f"화이트리스트 추출 실패: {exc}")

    # 추출된 화이트리스트 표시
    whitelist: ConceptWhitelist | None = st.session_state.get("concept_whitelist")
    if whitelist is not None:
        st.markdown("### 📋 현재 적용된 개념서 지식 화이트리스트 (이 범위만 학습한 것으로 간주)")
        with st.container(border=True):
            book_badge = f"**기준 교재:** `{whitelist.book_title}` | " if whitelist.book_title else ""
            st.write(f"{book_badge}**추정 과목:** `{whitelist.subject_guess}` | **범위 요약:** {whitelist.pdf_summary}")
            if whitelist.units:
                unit_badges = [f"{u.unit_name} ({u.page_range})" for u in whitelist.units]
                st.caption("📖 **적용 단원 및 페이지:** " + " · ".join(unit_badges))
            w_c1, w_c2 = st.columns(2)
            with w_c1:
                st.markdown(f"**이 범위에 실제로 설명된 개념·원리 (`pdf_concepts`, {len(whitelist.pdf_concepts)}개)**")
                render_chips(whitelist.pdf_concepts, variant="blue", limit=100)
            with w_c2:
                st.markdown(f"**이 범위에 실제로 등장하는 허용 용어 (`pdf_vocabulary`, {len(whitelist.pdf_vocabulary)}개)**")
                render_chips(whitelist.pdf_vocabulary, variant="green", limit=120)

            with st.expander("✏️ 필요 시 허용 개념/용어 수동 보완하기"):
                edited_concepts = st.text_area(
                    "허용 개념 (쉼표 또는 줄바꿈 구분)",
                    value="\n".join(whitelist.pdf_concepts),
                    height=100,
                )
                edited_vocab = st.text_area(
                    "허용 용어 (쉼표 또는 줄바꿈 구분)",
                    value=", ".join(whitelist.pdf_vocabulary),
                    height=100,
                )
                if st.button("화이트리스트 수정 적용"):
                    whitelist.pdf_concepts = [
                        s.strip() for s in re.split(r"[\n,]+", edited_concepts) if s.strip()
                    ]
                    whitelist.pdf_vocabulary = [
                        s.strip() for s in re.split(r"[\n,]+", edited_vocab) if s.strip()
                    ]
                    st.session_state["concept_whitelist"] = whitelist
                    st.success("화이트리스트를 업데이트했습니다.")

        # -------------------------------------------------------------------
        # 2단계 & 3단계: 기출 매칭·필터링 & 상위 과목 문제 자동 개조
        # -------------------------------------------------------------------
        st.markdown("---")
        st.subheader("2️⃣~3️⃣ 엄격한 범위 내 기출 매칭 & 상위 문제 자동 개조")

        is_premium = can_use_premium_features(current_user)
        remaining_quota = get_remaining_free_quota(current_user)

        if not is_premium:
            if remaining_quota == 0:
                st.warning(
                    f"🆓 **무료 회원 (FREE)**: 오늘의 무료 추출 한도({FREE_DAILY_LIMIT}문제)를 모두 사용했습니다. "
                    "자정(00:00 KST)에 초기화되며, 사이드바에서 **개인 API 키 등록(CUSTOM)** 또는 **VIP 승급(PRO)** 시 무제한 이용 가능합니다."
                )
            else:
                st.info(
                    f"🆓 **무료 회원 (FREE)**: 오늘 남은 무료 추출 **{remaining_quota} / {FREE_DAILY_LIMIT}문제** "
                    "(상위 문제 자동 개조 및 PDF 병합은 CUSTOM · PRO · ADMIN 전용)"
                )

        with st.container(border=True):
            opt_c1, opt_c2, opt_c3 = st.columns(3)
            with opt_c1:
                filter_subject = st.selectbox("과목 필터", ["전체", "수학", "사회", "과학"], index=0)
            with opt_c2:
                filter_grade = st.selectbox("학년 수준 필터", ["전체", "고1 이하", "고2 이하"], index=0)
            with opt_c3:
                count_mode = st.radio("추출 문항 수", ["전체", "N문제 지정"], horizontal=True)
                max_allowed_n = 100 if is_premium else max(1, remaining_quota or 1)
                default_n = min(5, max_allowed_n)
                target_n = default_n
                if count_mode == "N문제 지정":
                    target_n = int(
                        st.number_input(
                            "지정 문항 수 (N)",
                            min_value=1,
                            max_value=max_allowed_n,
                            value=default_n,
                            step=1,
                        )
                    )
                elif not is_premium and remaining_quota is not None:
                    st.caption(f"무료 회원(FREE)은 오늘 최대 {remaining_quota}문제까지만 추출됩니다.")

            chk_c1, chk_c2 = st.columns(2)
            with chk_c1:
                only_single = st.checkbox("단일 개념 문항만 보기 (`is_single_concept`)", value=False)
            with chk_c2:
                if is_premium:
                    auto_rewrite_mode = st.checkbox(
                        "✨ 상위 과목 문제 자동 개조 모드 (PDF-Grounded Rewriter)",
                        value=True,
                        help="풀이 원리는 PDF로 해결되지만 범위 외 상위 용어(mol, 엔탈피, 한계효용 등)가 포함된 문항을 PDF 내부 용어만으로 자동 개조합니다.",
                    )
                else:
                    st.checkbox(
                        "✨ 상위 과목 문제 자동 개조 모드 (🔒 CUSTOM · PRO 전용)",
                        value=False,
                        disabled=True,
                        help="사이드바에서 개인 API 키를 등록(CUSTOM)하거나 PRO 등급으로 승급하면 상위 과목 용어를 개념서 용어로 자동 치환해 출제합니다.",
                    )
                    auto_rewrite_mode = False

            if st.button("🚀 기출 매칭 & 개념서 맞춤 문제 추출·개조 실행", type="primary"):
                db_list = storage.load_db()
                if not is_premium and (remaining_quota or 0) <= 0:
                    st.error(
                        f"오늘의 무료 추출 한도({FREE_DAILY_LIMIT}문제)를 모두 소진했습니다. "
                        "사이드바에서 개인 API 키를 등록(CUSTOM)하면 즉시 무제한 이용할 수 있습니다."
                    )
                elif not active_gemini_key:
                    st.error(f"사용 가능한 Gemini API 키({active_key_label})가 설정되지 않았습니다.")
                elif not db_list:
                    st.warning("저장된 기출 DB가 비어 있습니다. 먼저 [탭 1]에서 모의고사를 수집·태깅해 주세요.")
                else:
                    # 1차 메타데이터 필터링
                    candidates: list[QuestionRecord] = []
                    for r in db_list:
                        if filter_subject != "전체" and r.subject_category != filter_subject:
                            continue
                        if filter_grade == "고1 이하" and r.min_grade > 1:
                            continue
                        if filter_grade == "고2 이하" and r.min_grade > 2:
                            continue
                        if only_single and not r.is_single_concept:
                            continue
                        candidates.append(r)

                    if not candidates:
                        st.warning("선택한 과목/학년/단일개념 조건에 맞는 후보 문항이 DB에 없습니다.")
                    else:
                        with st.spinner(f"후보 {len(candidates)}개 문항의 핵심 원리를 개념서 범위와 대조 중..."):
                            try:
                                judgments = match_questions_to_whitelist(
                                    api_key=active_gemini_key,
                                    model_name=selected_model,
                                    candidates=candidates,
                                    whitelist=whitelist,
                                )
                            except Exception as exc:
                                st.error(f"원리 매칭 중 오류가 발생했습니다: {exc}")
                                judgments = []

                        j_map = {j.question_id: j for j in judgments}
                        diagnostics = []
                        selected_pairs: list[tuple[QuestionRecord, Any]] = []
                        skipped_needs_rewrite = 0

                        for q in candidates:
                            j = j_map.get(q.id)
                            if not j:
                                continue
                            diagnostics.append((q, j))
                            if not j.is_solvable_by_pdf_only:
                                continue
                            if j.needs_rewriting and not auto_rewrite_mode:
                                skipped_needs_rewrite += 1
                                continue
                            selected_pairs.append((q, j))

                        if count_mode == "N문제 지정":
                            selected_pairs = selected_pairs[:target_n]

                        truncated_by_free = False
                        if not is_premium and remaining_quota is not None and len(selected_pairs) > remaining_quota:
                            selected_pairs = selected_pairs[:remaining_quota]
                            truncated_by_free = True

                        st.session_state["match_diagnostics"] = diagnostics

                        if not selected_pairs:
                            st.warning(
                                "선택한 개념서 범위의 개념만으로 풀 수 있는 문항이 없습니다."
                                + (
                                    f" (단, CUSTOM/PRO 등급에서 '상위 과목 문제 자동 개조 모드'를 켜면 원리가 일치하는 {skipped_needs_rewrite}문항을 개조해 출제할 수 있습니다.)"
                                    if skipped_needs_rewrite > 0
                                    else ""
                                )
                            )
                            st.session_state["matched_items"] = []
                        else:
                            assembled: list[dict[str, Any]] = []
                            prog = st.progress(0.0)
                            stat = st.empty()

                            for idx, (q, j) in enumerate(selected_pairs, start=1):
                                crop_bytes = storage.get_crop_bytes(q.image_filename)
                                rewritten_obj: RewrittenQuestion | None = None
                                concept_ref = resolve_question_concept_reference(
                                    question=q,
                                    judgment=j,
                                    whitelist=whitelist,
                                    fallback_source_label=st.session_state.get("concept_source_label", ""),
                                )

                                if j.needs_rewriting and auto_rewrite_mode:
                                    stat.info(
                                        f"상위 문제 자동 개조 중 ({idx}/{len(selected_pairs)}): {q.exam_name} {q.q_num}번 "
                                        f"(치환 대상: {', '.join(j.out_of_scope_terms)})"
                                    )
                                    try:
                                        rewritten_obj = rewrite_question_for_pdf(
                                            api_key=active_gemini_key,
                                            model_name=selected_model,
                                            question=q,
                                            crop_png_bytes=crop_bytes,
                                            whitelist=whitelist,
                                            out_of_scope_terms=j.out_of_scope_terms,
                                            sliced_text=st.session_state.get("concept_sliced_text", ""),
                                        )
                                    except Exception as exc:
                                        st.error(f"{q.id} 개조 실패 (원본 제외): {exc}")
                                        continue

                                assembled.append(
                                    {
                                        "question": q,
                                        "judgment": j,
                                        "crop_png_bytes": crop_bytes,
                                        "rewritten": rewritten_obj,
                                        "concept_reference": concept_ref,
                                    }
                                )
                                prog.progress(idx / len(selected_pairs))

                            stat.empty()
                            updated_u = consume_extraction_quota(
                                storage, current_user.username, len(assembled)
                            )
                            st.session_state["matched_items"] = assembled
                            st.session_state["generated_exam_pdf"] = None
                            st.session_state["user_answers"] = {}
                            st.session_state["graded"] = False
                            st.session_state["ai_explanations"] = {}
                            st.session_state["ai_chats"] = {}
                            rem_msg = ""
                            if updated_u is not None:
                                new_rem = get_remaining_free_quota(updated_u)
                                if new_rem is not None:
                                    rem_msg = f" | 오늘 남은 무료 추출: {new_rem}/{FREE_DAILY_LIMIT}문제"
                            if truncated_by_free:
                                st.info(f"FREE 등급 일일 한도 적용으로 {len(assembled)}문항만 추출되었습니다.")
                            st.success(
                                f"🎯 총 {len(assembled)}문항 추출 완료! "
                                f"(원문 그대로 {sum(1 for it in assembled if it['rewritten'] is None)}문항 · "
                                f"개념서 기준 개조 {sum(1 for it in assembled if it['rewritten'] is not None)}문항){rem_msg}"
                            )

    # -----------------------------------------------------------------------
    # 결과 활용 2가지 모드 (모드 A: PDF 다운로드 / 모드 B: 웹에서 풀기 + AI 해설)
    # -----------------------------------------------------------------------
    matched_items: list[dict[str, Any]] = st.session_state.get("matched_items") or []
    if st.session_state.get("match_diagnostics"):
        with st.expander("🔎 후보 문항 매칭/제외 판정 근거 상세 보기", expanded=False):
            for q, j in st.session_state["match_diagnostics"]:
                icon = "🟢" if j.is_solvable_by_pdf_only and not j.needs_rewriting else ("🔵" if j.is_solvable_by_pdf_only else "⚪")
                status_lbl = (
                    "원문 그대로 매칭"
                    if (j.is_solvable_by_pdf_only and not j.needs_rewriting)
                    else ("원리 일치 → 상위 용어 개조 대상" if j.is_solvable_by_pdf_only else "범위 외 원리 포함 (제외)")
                )
                ref_preview = ""
                if j.is_solvable_by_pdf_only:
                    ref_preview = " | " + resolve_question_concept_reference(
                        q, j, whitelist, fallback_source_label=st.session_state.get("concept_source_label", "")
                    )
                st.write(
                    f"{icon} **{q.exam_name} {q.q_num}번** [{status_lbl}] — {j.matching_reason}"
                    + (f" | 범위 외 용어: `{', '.join(j.out_of_scope_terms)}`" if j.out_of_scope_terms else "")
                    + ref_preview
                )

    if matched_items:
        st.markdown("---")
        mode_tab_a, mode_tab_b = st.tabs(
            [
                "📄 모드 A: 맞춤형 시험지 PDF 생성 & 다운로드",
                "✏️ 모드 B: 웹에서 바로 풀기 & 자동 채점 + AI 자동 해설",
            ]
        )

        # ----- 모드 A: 맞춤형 시험지 PDF 생성 & 다운로드 -----
        with mode_tab_a:
            st.markdown("#### 📄 모의고사 스타일 맞춤형 시험지 PDF 제작")
            if not can_use_premium_features(current_user):
                st.warning(
                    "🔒 **맞춤형 시험지 PDF 병합 및 다운로드는 CUSTOM · PRO · ADMIN 등급 전용 기능입니다.**\n\n"
                    "사이드바에서 **개인 API 키 등록(CUSTOM)** 시 즉시 이용 가능하며, 무료 회원은 우측 **[모드 B: 웹에서 바로 풀기]**를 계속 이용하실 수 있습니다."
                )
            else:
                with st.container(border=True):
                    pdf_title = st.text_input(
                        "시험지 제목",
                        value="2022 개정 교육과정 개념서 맞춤형 기출 모의고사",
                    )
                    pa_c1, pa_c2 = st.columns(2)
                    with pa_c1:
                        layout_choice = st.radio(
                            "시험지 레이아웃",
                            options=["2단 모의고사 스타일", "1단 태블릿/인쇄 스타일"],
                            horizontal=True,
                        )
                    with pa_c2:
                        include_orig_fig = st.checkbox(
                            "개조 문항 아래 참고용 원본 도표/이미지 함께 수록",
                            value=True,
                        )

                    if st.button("🛠️ 맞춤형 시험지 PDF 생성하기", type="primary"):
                        with st.spinner("PDF 조판 및 뒷장 정답·해설 페이지 생성 중..."):
                            pdf_bytes = generate_custom_exam_pdf(
                                items=matched_items,
                                exam_title=pdf_title.strip() or "맞춤형 기출 모의고사",
                                source_label=st.session_state.get("concept_source_label", "업로드 개념서 PDF"),
                                whitelist=whitelist,
                                two_column=(layout_choice == "2단 모의고사 스타일"),
                                include_original_for_rewritten=include_orig_fig,
                            )
                            st.session_state["generated_exam_pdf"] = pdf_bytes
                        st.success("✅ 시험지 PDF(뒷장 정답·개념서 맞춤 해설 포함)가 생성되었습니다!")

                    if st.session_state.get("generated_exam_pdf"):
                        dl_bytes = st.session_state["generated_exam_pdf"]
                        st.download_button(
                            label=f"⬇️ 맞춤형 시험지 PDF 다운로드 ({len(dl_bytes) // 1024} KB)",
                            data=dl_bytes,
                            file_name=f"{sanitize_id_part(pdf_title)}.pdf",
                            mime="application/pdf",
                            type="primary",
                            use_container_width=True,
                        )

        # ----- 모드 B: 웹에서 바로 풀기 & 자동 채점 + AI 자동 해설 -----
        with mode_tab_b:
            st.markdown("#### ✏️ 실전 풀이 & 자동 채점 + 개념서 기반 AI 자동 해설")

            # 채점 완료 시 상단 성적표 배너
            if st.session_state.get("graded"):
                correct_cnt = 0
                for item in matched_items:
                    q: QuestionRecord = item["question"]
                    rw: RewrittenQuestion | None = item.get("rewritten")
                    truth = rw.preserved_answer if rw else q.answer
                    user_val = st.session_state["user_answers"].get(q.id, "")
                    if normalize_answer(user_val) == normalize_answer(truth) and normalize_answer(user_val) != "":
                        correct_cnt += 1
                total_q = len(matched_items)
                score_pct = round((correct_cnt / max(1, total_q)) * 100, 1)
                st.success(f"📊 **채점 결과: {total_q}문항 중 {correct_cnt}문항 정답 ({score_pct}점)**")

            for seq_num, item in enumerate(matched_items, start=1):
                q = item["question"]
                j = item["judgment"]
                crop_b = item.get("crop_png_bytes")
                rw = item.get("rewritten")
                truth_ans = rw.preserved_answer if rw else q.answer
                concept_ref_str = (
                    item.get("concept_reference")
                    or resolve_question_concept_reference(
                        q, j, whitelist, fallback_source_label=st.session_state.get("concept_source_label", "")
                    )
                )

                with st.container(border=True):
                    # 카드 상단 헤더 + 배지
                    hdr_col1, hdr_col2 = st.columns([3, 1])
                    with hdr_col1:
                        st.markdown(f"### 문제 {seq_num}번")
                        st.caption(
                            f"출처: {q.exam_name} {q.q_num}번 · {q.curriculum_subject} ({q.unit_name}) · 본질 원리: {', '.join(q.core_principles)}"
                        )
                    with hdr_col2:
                        if rw is not None:
                            st.markdown("<span class='chip chip-blue'>✨ 개념서 기준 개조 문항</span>", unsafe_allow_html=True)
                        else:
                            st.markdown("<span class='chip chip-green'>📌 원문 그대로 출제</span>", unsafe_allow_html=True)

                    # 문제 본문 표시
                    if rw is not None:
                        stem_html = _html.escape(rw.rewritten_stem).replace("\n", "<br/>")
                        choices_html = "".join(
                            f"<div class='rewritten-choice'><b>{CIRCLED_MAP.get(str(ci + 1), f'{ci+1})')}</b> "
                            f"{_html.escape(rw.rewritten_choices[ci]).replace(chr(10), '<br/>')}</div>"
                            for ci in range(len(rw.rewritten_choices))
                        )
                        st.markdown(
                            f"""
                            <div class="rewritten-card">
                              <div class="rewritten-stem">{stem_html}</div>
                              {choices_html}
                            </div>
                            """,
                            unsafe_allow_html=True,
                        )
                        if rw.replaced_terms_map:
                            render_chips(rw.replaced_terms_map, variant="purple")

                        with st.expander("🖼️ 원본 문제 이미지 & 개조 검증 내역 보기", expanded=False):
                            if crop_b:
                                st.image(crop_b, caption=f"원본 ({q.exam_name} {q.q_num}번)", use_container_width=True)
                            st.info(f"**자가 검증:** {rw.self_verification_note}")
                    else:
                        if crop_b:
                            st.image(crop_b, use_container_width=True)
                        else:
                            st.warning("문항 이미지를 불러올 수 없습니다.")

                    # 문항 카드 하단: 참고 개념서 단원 및 페이지 번호 표기
                    st.markdown(
                        f"<div style='background:#EFF6FF; border-left:4px solid #2563EB; padding:8px 12px; "
                        f"border-radius:6px; margin:8px 0; font-size:0.9rem; font-weight:600; color:#1E3A8A;'>"
                        f"{_html.escape(concept_ref_str)}</div>",
                        unsafe_allow_html=True,
                    )

                    # 답안 입력 UI (객관식 1~5 터치 버튼 또는 주관식 입력)
                    st.markdown("**내 답안 선택 / 입력:**")
                    current_ans = st.session_state["user_answers"].get(q.id, "")
                    is_mc = is_multiple_choice_question(q, rw)

                    if is_mc:
                        st.markdown("<div class='omr-row'>", unsafe_allow_html=True)
                        omr_cols = st.columns(5)
                        for opt_num in ("1", "2", "3", "4", "5"):
                            with omr_cols[int(opt_num) - 1]:
                                selected = normalize_answer(current_ans) == opt_num
                                if st.button(
                                    f"{CIRCLED_MAP[opt_num]} ({opt_num}번)",
                                    key=f"omr_{q.id}_{opt_num}",
                                    type="primary" if selected else "secondary",
                                    use_container_width=True,
                                ):
                                    st.session_state["user_answers"][q.id] = opt_num
                                    st.rerun()
                        st.markdown("</div>", unsafe_allow_html=True)
                    else:
                        typed = st.text_input(
                            "주관식 답안 입력",
                            value=current_ans,
                            key=f"short_{q.id}",
                            placeholder="정답을 입력하세요",
                        )
                        st.session_state["user_answers"][q.id] = typed

                    # 채점 상태 표시
                    if st.session_state.get("graded"):
                        is_correct = (
                            normalize_answer(st.session_state["user_answers"].get(q.id, ""))
                            == normalize_answer(truth_ans)
                            and normalize_answer(st.session_state["user_answers"].get(q.id, "")) != ""
                        )
                        disp_truth = CIRCLED_MAP.get(normalize_answer(truth_ans), truth_ans)
                        disp_user = CIRCLED_MAP.get(normalize_answer(current_ans), current_ans) or "미입력"
                        if is_correct:
                            st.success(f"⭕ **정답입니다!** (내 답: {disp_user} / 정답: {disp_truth})")
                        else:
                            st.error(f"❌ **오답입니다.** (내 답: {disp_user} / 정답: {disp_truth})")

                        with st.expander("📖 빠른 정답 해설 보기", expanded=not is_correct):
                            if rw is not None:
                                st.markdown(rw.pdf_grounded_explanation)
                            else:
                                st.markdown(q.official_explanation)
                            st.caption(concept_ref_str)

                    # ✨ AI 자동 해설 버튼 & 추가 질문 채팅
                    ai_col1, ai_col2 = st.columns([1, 2])
                    with ai_col1:
                        if st.button("✨ AI 자동 해설", key=f"ai_btn_{q.id}", use_container_width=True):
                            if not active_gemini_key:
                                st.error(f"사용 가능한 Gemini API 키({active_key_label})가 설정되지 않았습니다.")
                            elif whitelist is None:
                                st.error("개념서 화이트리스트가 필요합니다.")
                            else:
                                with st.spinner("개념서 범위 내 용어만 사용하여 맞춤 해설 작성 중..."):
                                    try:
                                        exp_md = generate_pdf_grounded_explanation(
                                            api_key=active_gemini_key,
                                            model_name=selected_model,
                                            question=q,
                                            crop_png_bytes=crop_b,
                                            whitelist=whitelist,
                                            user_answer=st.session_state["user_answers"].get(q.id, ""),
                                            rewritten=rw,
                                            sliced_text=st.session_state.get("concept_sliced_text", ""),
                                            concept_reference=concept_ref_str,
                                        )
                                        st.session_state["ai_explanations"][q.id] = exp_md
                                    except Exception as exc:
                                        st.error(f"AI 해설 생성 실패: {exc}")

                    if q.id in st.session_state["ai_explanations"]:
                        st.info("✨ **개념서 범위 기준 AI 맞춤 해설**")
                        st.markdown(st.session_state["ai_explanations"][q.id])
                        if concept_ref_str not in st.session_state["ai_explanations"][q.id]:
                            st.markdown(f"**{_html.escape(concept_ref_str)}**")

                        # 문항별 추가 질문 채팅
                        st.markdown("💬 **추가 질문 채팅 (개념서 범위 내 1:1 질의응답)**")
                        q_chat_history: list[dict[str, str]] = st.session_state["ai_chats"].get(q.id, [])
                        for msg in q_chat_history:
                            with st.chat_message(msg["role"]):
                                st.markdown(msg["content"])

                        with st.form(key=f"chat_form_{q.id}", clear_on_submit=True):
                            followup_q = st.text_input(
                                "이 문제에 대해 궁금한 점을 질문하세요",
                                placeholder="예: 왜 이 식에서 질량비가 이렇게 적용되나요?",
                                label_visibility="collapsed",
                            )
                            send_chat = st.form_submit_button("질문 보내기")

                        if send_chat and followup_q.strip():
                            if not active_gemini_key or whitelist is None:
                                st.error(f"사용 가능한 Gemini API 키({active_key_label})와 개념서 화이트리스트가 필요합니다.")
                            else:
                                q_chat_history.append({"role": "user", "content": followup_q.strip()})
                                with st.spinner("개념서 범위 내에서 답변 생성 중..."):
                                    try:
                                        reply = chat_followup_with_pdf(
                                            api_key=active_gemini_key,
                                            model_name=selected_model,
                                            question=q,
                                            crop_png_bytes=crop_b,
                                            whitelist=whitelist,
                                            chat_history=q_chat_history[:-1],
                                            user_question=followup_q.strip(),
                                            rewritten=rw,
                                            sliced_text=st.session_state.get("concept_sliced_text", ""),
                                        )
                                        q_chat_history.append({"role": "assistant", "content": reply})
                                        st.session_state["ai_chats"][q.id] = q_chat_history
                                        st.rerun()
                                    except Exception as exc:
                                        st.error(f"답변 생성 오류: {exc}")

            # 하단 채점하기 버튼
            grade_c1, grade_c2 = st.columns([2, 1])
            with grade_c1:
                if st.button("📝 채점하기", type="primary", use_container_width=True):
                    st.session_state["graded"] = True
                    st.rerun()
            with grade_c2:
                if st.button("🔄 답안 초기화", use_container_width=True):
                    st.session_state["user_answers"] = {}
                    st.session_state["graded"] = False
                    st.rerun()

# ===========================================================================
# TAB 3: 관리자 / Pro 전용 공용 개념서·교과서 DB 구축 및 관리
# ===========================================================================
with tab3:
    _render_concept_book_builder_section(key_prefix="tab3")

if admin_tab is not None:
    with admin_tab:
        _render_admin_panel()
