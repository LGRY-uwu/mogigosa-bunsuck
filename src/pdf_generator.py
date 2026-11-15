from __future__ import annotations

import html
import io
from datetime import datetime
from typing import Any

import pymupdf
from PIL import Image

from src.models import ConceptWhitelist, QuestionRecord, RewrittenQuestion

CIRCLED_NUMS = ["①", "②", "③", "④", "⑤"]

EXAM_CSS = """
body {
    font-family: sans-serif;
    font-size: 9.6pt;
    line-height: 1.48;
    color: #111827;
    margin: 0;
    padding: 0;
}
.q-header {
    margin-bottom: 4pt;
    font-size: 9.8pt;
    color: #0F172A;
}
.q-num {
    font-weight: bold;
    font-size: 11pt;
    color: #1D4ED8;
}
.badge {
    background-color: #DBEAFE;
    color: #1E40AF;
    font-size: 7.8pt;
    padding: 1pt 4pt;
    border-radius: 3pt;
    font-weight: bold;
}
.meta {
    color: #475569;
    font-size: 8pt;
}
.stem {
    margin-top: 3pt;
    margin-bottom: 5pt;
    word-break: keep-all;
}
.choices {
    margin-top: 4pt;
    margin-left: 6pt;
}
.choice-item {
    margin-bottom: 2.5pt;
}
.ref-note {
    margin-top: 4pt;
    color: #64748B;
    font-size: 7.8pt;
}
.ans-table {
    width: 100%;
    border-collapse: collapse;
    margin-top: 6pt;
    margin-bottom: 12pt;
    font-size: 9pt;
}
.ans-table th, .ans-table td {
    border: 0.7pt solid #CBD5E1;
    padding: 4pt 6pt;
    text-align: center;
}
.ans-table th {
    background-color: #EFF6FF;
    color: #1E3A8A;
}
.exp-card {
    border: 0.7pt solid #E2E8F0;
    background-color: #F8FAFC;
    padding: 6pt 8pt;
    margin-bottom: 8pt;
    border-radius: 4pt;
}
.exp-title {
    font-weight: bold;
    font-size: 9.8pt;
    color: #0F172A;
    margin-bottom: 3pt;
}
.exp-ans {
    color: #DC2626;
    font-weight: bold;
}
.exp-sub {
    font-size: 8.4pt;
    color: #1D4ED8;
    margin-bottom: 3pt;
}
"""


def _esc_nl(text: str) -> str:
    return html.escape(text or "").replace("\n", "<br/>")


def _format_answer_badge(ans: str) -> str:
    cleaned = (ans or "").strip()
    mapping = {"1": "①", "2": "②", "3": "③", "4": "④", "5": "⑤"}
    return mapping.get(cleaned, cleaned)


def _measure_html_height(html_str: str, width: float, max_h: float = 760.0) -> float:
    """스크래치 페이지에서 HTML 박스의 비축소 실제 높이를 측정한다."""
    scratch_doc = pymupdf.open()
    try:
        sp = scratch_doc.new_page(width=max(600.0, width + 40.0), height=max_h + 40.0)
        spare_h, scale = sp.insert_htmlbox(
            pymupdf.Rect(0, 0, width, max_h),
            html_str,
            css=EXAM_CSS,
        )
        if scale <= 0:
            return max_h
        used = max_h - max(0.0, spare_h)
        return max(16.0, used + 4.0)
    finally:
        scratch_doc.close()


def _draw_page_footer(page: pymupdf.Page, page_num: int, two_column: bool) -> None:
    w, h = page.rect.width, page.rect.height
    if two_column:
        page.draw_line(
            pymupdf.Point(w / 2.0, 42.0),
            pymupdf.Point(w / 2.0, h - 38.0),
            color=(0.82, 0.85, 0.90),
            width=0.6,
        )
    page.insert_htmlbox(
        pymupdf.Rect(30, h - 30, w - 30, h - 10),
        f"<div style='text-align:center; font-size:8.5pt; color:#64748B;'>- {page_num} -</div>",
        css=EXAM_CSS,
    )


def _draw_first_page_banner(
    page: pymupdf.Page,
    exam_title: str,
    source_label: str,
    total_count: int,
) -> float:
    w = page.rect.width
    today = datetime.now().strftime("%Y.%m.%d")
    banner_html = f"""
    <div style="border-bottom: 1.8pt solid #1E3A8A; padding-bottom: 6pt;">
      <div style="font-size: 8.5pt; color: #2563EB; font-weight: bold;">2022 개정 교육과정 개념서 범위 엄격 준수 맞춤형 시험지</div>
      <div style="font-size: 14.5pt; font-weight: bold; color: #0F172A; margin-top: 2pt;">{html.escape(exam_title)}</div>
      <div style="font-size: 8.4pt; color: #475569; margin-top: 3pt;">
        출제 범위: {html.escape(source_label)} &nbsp;|&nbsp; 총 {total_count}문항 &nbsp;|&nbsp; 생성일: {today}
        &nbsp;&nbsp;&nbsp; 성명: ______________ &nbsp; 점수: _____ / {total_count}
      </div>
    </div>
    """
    banner_h = _measure_html_height(banner_html, w - 64.0, max_h=140.0)
    page.insert_htmlbox(pymupdf.Rect(32, 24, w - 32, 24 + banner_h + 6.0), banner_html, css=EXAM_CSS)
    return 24.0 + banner_h + 10.0


def generate_custom_exam_pdf(
    items: list[dict[str, Any]],
    exam_title: str,
    source_label: str,
    whitelist: ConceptWhitelist | None = None,
    two_column: bool = True,
    include_original_for_rewritten: bool = True,
) -> bytes:
    """
    추출 및 개조된 문항들을 모의고사 스타일 PDF로 병합 생성하고,
    마지막 페이지에 정답 및 개념서 용어 기준 해설 페이지를 부착한다.

    items 각 원소 기대 키:
      - question: QuestionRecord
      - crop_png_bytes: bytes | None
      - rewritten: RewrittenQuestion | None
    """
    doc = pymupdf.open()
    page_w, page_h = 595.0, 842.0  # A4
    margin_x = 30.0
    margin_bottom = 40.0
    col_gap = 20.0

    def get_col_rects(top_y: float) -> list[tuple[float, float, float]]:
        # returns list of (x0, x1, start_y)
        if two_column:
            col_w = (page_w - 2 * margin_x - col_gap) / 2.0
            return [
                (margin_x, margin_x + col_w, top_y),
                (margin_x + col_w + col_gap, page_w - margin_x, top_y),
            ]
        return [(margin_x, page_w - margin_x, top_y)]

    # 1) 문제지 페이지 생성
    page = doc.new_page(width=page_w, height=page_h)
    first_top = _draw_first_page_banner(page, exam_title, source_label, len(items))
    col_rects = get_col_rects(first_top)
    col_idx = 0
    cur_y = col_rects[0][2]

    def advance_column() -> None:
        nonlocal page, col_rects, col_idx, cur_y
        if col_idx + 1 < len(col_rects):
            col_idx += 1
            cur_y = col_rects[col_idx][2]
        else:
            _draw_page_footer(page, doc.page_count, two_column)
            page = doc.new_page(width=page_w, height=page_h)
            col_rects = get_col_rects(36.0)
            col_idx = 0
            cur_y = col_rects[0][2]

    for idx, item in enumerate(items, start=1):
        q: QuestionRecord = item["question"]
        crop_bytes: bytes | None = item.get("crop_png_bytes")
        rewritten: RewrittenQuestion | None = item.get("rewritten")
        concept_ref: str = str(item.get("concept_reference") or "").strip()
        ref_html = (
            f"<div class='ref-note' style='color:#1E40AF; font-weight:bold; margin-top:4pt;'>{html.escape(concept_ref)}</div>"
            if concept_ref
            else ""
        )

        col_x0, col_x1, _ = col_rects[col_idx]
        col_w = col_x1 - col_x0
        max_col_h = (page_h - margin_bottom) - col_rects[col_idx][2]

        if rewritten is not None:
            choices_html = ""
            if rewritten.rewritten_choices:
                rows = []
                for c_idx, c_text in enumerate(rewritten.rewritten_choices[:5]):
                    num_sym = CIRCLED_NUMS[c_idx] if c_idx < len(CIRCLED_NUMS) else f"{c_idx + 1})"
                    rows.append(
                        f"<div class='choice-item'>{num_sym} {_esc_nl(c_text)}</div>"
                    )
                choices_html = f"<div class='choices'>{''.join(rows)}</div>"

            card_html = f"""
            <div>
              <div class="q-header">
                <span class="q-num">{idx}.</span>
                <span class="badge">PDF 맞춤 개조</span>
                <span class="meta">{html.escape(q.curriculum_subject)} · 원본 {html.escape(q.exam_name)} {q.q_num}번</span>
              </div>
              <div class="stem">{_esc_nl(rewritten.rewritten_stem)}</div>
              {choices_html}
              {ref_html}
            </div>
            """
            text_h = _measure_html_height(card_html, col_w, max_h=720.0)

            img_draw_h = 0.0
            img_draw_w = col_w
            if include_original_for_rewritten and crop_bytes:
                with Image.open(io.BytesIO(crop_bytes)) as im:
                    iw, ih = im.size
                ratio = ih / max(1, iw)
                # 개조 문항 아래 참고 원본 도표는 적정 높이로 제한
                img_draw_w = min(col_w, col_w * 0.92)
                img_draw_h = min(img_draw_w * ratio, 175.0)
                if img_draw_w * ratio > 175.0:
                    img_draw_w = 175.0 / max(0.01, ratio)

            note_h = 16.0 if img_draw_h > 0 else 0.0
            total_needed = text_h + note_h + img_draw_h + 14.0

            if cur_y + total_needed > page_h - margin_bottom and cur_y > col_rects[col_idx][2] + 5.0:
                advance_column()
                col_x0, col_x1, _ = col_rects[col_idx]
                col_w = col_x1 - col_x0

            page.insert_htmlbox(
                pymupdf.Rect(col_x0, cur_y, col_x1, min(page_h - margin_bottom, cur_y + text_h + 4.0)),
                card_html,
                css=EXAM_CSS,
            )
            cur_y += text_h + 2.0

            if img_draw_h > 0 and crop_bytes:
                # 남은 공간이 이미지보다 작으면 다음 컬럼으로 이동해 이미지 짤림 방지
                if cur_y + note_h + img_draw_h > page_h - margin_bottom:
                    advance_column()
                    col_x0, col_x1, _ = col_rects[col_idx]
                page.insert_htmlbox(
                    pymupdf.Rect(col_x0, cur_y, col_x1, cur_y + 15.0),
                    "<div class='ref-note'>[참고용 원본 문항 도표/이미지]</div>",
                    css=EXAM_CSS,
                )
                cur_y += 14.0
                page.insert_image(
                    pymupdf.Rect(col_x0, cur_y, col_x0 + img_draw_w, cur_y + img_draw_h),
                    stream=crop_bytes,
                    keep_proportion=True,
                )
                cur_y += img_draw_h + 8.0

            cur_y += 10.0

        else:
            header_html = f"""
            <div class="q-header">
              <span class="q-num">{idx}.</span>
              <span class="meta">[{html.escape(q.curriculum_subject)} · {html.escape(q.unit_name)}] (원본 {html.escape(q.exam_name)} {q.q_num}번)</span>
              {ref_html}
            </div>
            """
            header_h = _measure_html_height(header_html, col_w, max_h=80.0)

            img_draw_w = col_w
            img_draw_h = 40.0
            if crop_bytes:
                with Image.open(io.BytesIO(crop_bytes)) as im:
                    iw, ih = im.size
                ratio = ih / max(1, iw)
                img_draw_h = img_draw_w * ratio
                max_allowed_img_h = max_col_h - header_h - 14.0
                if img_draw_h > max_allowed_img_h:
                    scale_f = max_allowed_img_h / img_draw_h
                    img_draw_h = max_allowed_img_h
                    img_draw_w = img_draw_w * scale_f

            total_needed = header_h + img_draw_h + 14.0
            if cur_y + total_needed > page_h - margin_bottom and cur_y > col_rects[col_idx][2] + 5.0:
                advance_column()
                col_x0, col_x1, _ = col_rects[col_idx]
                col_w = col_x1 - col_x0

            page.insert_htmlbox(
                pymupdf.Rect(col_x0, cur_y, col_x1, cur_y + header_h + 2.0),
                header_html,
                css=EXAM_CSS,
            )
            cur_y += header_h + 2.0
            if crop_bytes:
                page.insert_image(
                    pymupdf.Rect(col_x0, cur_y, col_x0 + img_draw_w, cur_y + img_draw_h),
                    stream=crop_bytes,
                    keep_proportion=True,
                )
                cur_y += img_draw_h + 12.0
            else:
                cur_y += 20.0

    _draw_page_footer(page, doc.page_count, two_column)

    # 2) 맨 뒷장: 정답 및 업로드한 개념서 용어 기준 해설 페이지
    ans_page = doc.new_page(width=page_w, height=page_h)
    ans_header = f"""
    <div style="border-bottom: 1.5pt solid #1D4ED8; padding-bottom: 5pt; margin-bottom: 8pt;">
      <div style="font-size: 13pt; font-weight: bold; color: #0F172A;">정답 및 개념서 용어 기준 맞춤 해설</div>
      <div style="font-size: 8.5pt; color: #475569; margin-top: 2pt;">
        시험지: {html.escape(exam_title)} &nbsp;|&nbsp; 기준 개념서: {html.escape(source_label)}
      </div>
    </div>
    """
    ah = _measure_html_height(ans_header, page_w - 64.0, max_h=120.0)
    ans_page.insert_htmlbox(pymupdf.Rect(32, 28, page_w - 32, 28 + ah + 4.0), ans_header, css=EXAM_CSS)
    ay = 28.0 + ah + 8.0

    # 빠른 정답표 (5개씩 묶어 행 출력)
    table_rows = []
    chunk_size = 5
    for start in range(0, len(items), chunk_size):
        chunk = items[start : start + chunk_size]
        ths = "".join(f"<th>{start + j + 1}번</th>" for j in range(len(chunk)))
        tds = "".join(
            f"<td><b>{html.escape(_format_answer_badge(it['rewritten'].preserved_answer if it.get('rewritten') else it['question'].answer))}</b></td>"
            for it in chunk
        )
        table_rows.append(f"<tr>{ths}</tr><tr>{tds}</tr>")

    ans_table_html = f"<table class='ans-table'>{''.join(table_rows)}</table>"
    th = _measure_html_height(ans_table_html, page_w - 64.0, max_h=400.0)
    ans_page.insert_htmlbox(pymupdf.Rect(32, ay, page_w - 32, ay + th + 4.0), ans_table_html, css=EXAM_CSS)
    ay += th + 10.0

    if whitelist and whitelist.pdf_vocabulary:
        vocab_preview = ", ".join(whitelist.pdf_vocabulary[:28])
        if len(whitelist.pdf_vocabulary) > 28:
            vocab_preview += f" 외 {len(whitelist.pdf_vocabulary) - 28}개"
        scope_note = (
            f"<div style='font-size:8pt; color:#475569; margin-bottom:8pt;'>"
            f"<b>적용된 개념서 허용 용어 일부:</b> {html.escape(vocab_preview)}</div>"
        )
        sh = _measure_html_height(scope_note, page_w - 64.0, max_h=100.0)
        ans_page.insert_htmlbox(pymupdf.Rect(32, ay, page_w - 32, ay + sh + 2.0), scope_note, css=EXAM_CSS)
        ay += sh + 4.0

    # 문항별 해설 카드
    for idx, item in enumerate(items, start=1):
        q = item["question"]
        rewritten = item.get("rewritten")
        concept_ref = str(item.get("concept_reference") or "").strip()
        exp_ref_html = (
            f"<div style='margin-top:4pt; font-size:8.2pt; color:#1E40AF; font-weight:bold;'>{html.escape(concept_ref)}</div>"
            if concept_ref
            else ""
        )

        if rewritten is not None:
            ans_disp = _format_answer_badge(rewritten.preserved_answer)
            swap_info = ""
            if rewritten.replaced_terms_map:
                swap_info = (
                    "<div class='exp-sub'><b>용어 치환:</b> "
                    + html.escape(" / ".join(rewritten.replaced_terms_map))
                    + "</div>"
                )
            exp_body = _esc_nl(rewritten.pdf_grounded_explanation)
            badge = "[PDF 맞춤 개조 해설]"
        else:
            ans_disp = _format_answer_badge(q.answer)
            swap_info = (
                "<div class='exp-sub'><b>핵심 원리:</b> "
                + html.escape(", ".join(q.core_principles))
                + "</div>"
            )
            exp_body = _esc_nl(q.official_explanation)
            badge = "[원본 기출 해설]"

        exp_html = f"""
        <div class="exp-card">
          <div class="exp-title">
            {idx}번 &nbsp; 정답: <span class="exp-ans">{html.escape(ans_disp)}</span>
            &nbsp; <span class="meta">{badge} · {html.escape(q.exam_name)} {q.q_num}번</span>
          </div>
          {swap_info}
          <div>{exp_body}</div>
          {exp_ref_html}
        </div>
        """
        eh = _measure_html_height(exp_html, page_w - 64.0, max_h=720.0)
        if ay + eh > page_h - 42.0 and ay > 60.0:
            _draw_page_footer(ans_page, doc.page_count, False)
            ans_page = doc.new_page(width=page_w, height=page_h)
            ay = 34.0

        ans_page.insert_htmlbox(
            pymupdf.Rect(32, ay, page_w - 32, min(page_h - 38.0, ay + eh + 4.0)),
            exp_html,
            css=EXAM_CSS,
        )
        ay += eh + 6.0

    _draw_page_footer(ans_page, doc.page_count, False)

    try:
        doc.subset_fonts()
    except Exception:
        pass
    out_bytes = doc.tobytes(deflate=True, garbage=3)
    doc.close()
    return out_bytes
