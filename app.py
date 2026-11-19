"""Pathfinder AI student and teacher workspaces."""

from contextlib import closing, contextmanager
from datetime import date
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4
import sqlite3

from PIL import Image, UnidentifiedImageError
import streamlit as st

from diagnose import Pipeline
from eval import load_samples
from llm import (
    Config,
    LLMAuthError,
    LLMError,
    LLMNetworkError,
    LLMNotConfiguredError,
    LLMQuotaError,
    LLMResponseError,
    VisionUnavailableError,
)
from store import (
    class_heatmap,
    completed_practice,
    connect,
    gap_profile,
    init_db,
    list_classes,
    mistake_details,
    mistake_history,
    practice_on,
    record_practice,
    register_student,
    repeat_error_rate,
    review_cases,
    save_kp_override,
    save_review,
)
import ui
from vision import from_sample, from_text, parse_photo

ROOT = Path(__file__).resolve().parent
STUDENT_PAGES = {"diagnose": ("诊断台", "target"), "path": ("复习路径", "route"), "history": ("错题档案", "book_2")}
TEACHER_PAGES = {"class": ("班级学情", "grid_view"), "review": ("复核队列", "fact_check")}
SAMPLE_NAMES = {
    "alg_distributive": "数学 · 分配律漏乘",
    "alg_fraction_add": "数学 · 异分母分数相加",
    "alg_sign_drop": "数学 · 移项符号错误",
    "alg_square_of_sum": "数学 · 完全平方公式",
    "che_unbalanced": "化学 · 方程式未配平",
    "phy_unit_conversion": "物理 · 质量单位换算",
    "unreadable_photo": "拍照示例 · 图片无法辨认",
}
PROVIDER_ERRORS = {
    LLMAuthError: ("模型鉴权失败", "请检查 .env 中的 LLM_API_KEY 和服务端点。"),
    LLMQuotaError: ("模型额度或频率受限", "请检查服务额度，稍后再试。"),
    LLMNetworkError: ("暂时连不上模型服务", "请检查网络和 LLM_BASE_URL，再重新提交。"),
    LLMNotConfiguredError: ("模型配置不完整", "请检查 .env 中的模型与端点配置。"),
    VisionUnavailableError: ("照片转录不可用", "请检查 VISION_MODEL，或手动录入题目和步骤。"),
    LLMResponseError: ("模型返回了无法读取的结果", "请重试，或在 .env 中更换兼容的模型。"),
}
REVIEW_LABELS = {"confirmed": "已确认", "rejected": "退回补充"}


@st.cache_data
def samples():
    return {sample["id"]: from_sample(sample) for sample in load_samples()}


def navigate(page: str) -> None:
    st.session_state.page = page


def navigation(conn) -> None:
    with st.sidebar:
        ui.brand()
        for label, page_group, key in (
            ("学生工作台", STUDENT_PAGES, "student_nav"),
            ("教师工作台", TEACHER_PAGES, "teacher_nav"),
        ):
            st.html(f'<div class="pf-side-label">{label}</div>')
            with st.container(key=key):
                for route, (name, icon) in page_group.items():
                    st.button(name, key=f"nav_{route}", icon=f":material/{icon}:",
                              type="primary" if st.session_state.page == route else "secondary",
                              on_click=navigate, args=(route,), width="stretch")

        student_id = st.session_state.student_id
        student = conn.execute("SELECT class_id FROM students WHERE id=?", (student_id,)).fetchone()
        current_class = (student["class_id"] if student else None) or ""
        st.html(
            '<div class="pf-side-student">当前学生'
            f'<strong>{ui.e(student_id)}</strong>{ui.e(current_class or "未分班")}</div>'
        )
        with st.expander("学生资料"):
            with st.form(f"switch_student_{student_id}", border=False):
                student_value = st.text_input("学生编号", value=student_id, max_chars=50)
                if st.form_submit_button("切换学生", width="stretch"):
                    selected = student_value.strip()
                    if not selected:
                        st.error("请填写学生编号。")
                    else:
                        if selected != student_id:
                            # Never let one student's photo/transcript/result leak into another's workspace.
                            st.session_state.pop("transcript", None)
                            st.session_state.pop("diagnosis", None)
                            for key in list(st.session_state):
                                if isinstance(key, str) and (
                                    key.startswith("photo_upload_") or key.startswith("retake_")
                                ):
                                    st.session_state.pop(key, None)
                        st.session_state.student_id = selected
                        st.rerun()
            with st.form(f"student_class_{student_id}", border=False):
                class_value = st.text_input("当前学生的班级", value=current_class, max_chars=50)
                if st.form_submit_button("保存班级", width="stretch"):
                    register_student(conn, student_id, class_value.strip() or None)
                    st.session_state.workspace_notice = "班级已更新。"
                    st.rerun()


def show_provider_error(exc: LLMError) -> None:
    title, message = PROVIDER_ERRORS.get(type(exc), ("诊断暂时不可用", "请稍后重试。"))
    ui.notice(title, message, tone="danger")


def valid_extension(data: bytes) -> str:
    if len(data) > 12 * 1024 * 1024:
        raise ValueError("请上传 12 MB 以内的 JPG 或 PNG。")
    try:
        with Image.open(BytesIO(data), formats=("JPEG", "PNG")) as image:
            if image.width * image.height > 24_000_000:
                raise ValueError("图片像素过多，请缩小图片后重试。")
            suffix = ".png" if image.format == "PNG" else ".jpg"
            image.load()
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, SyntaxError) as exc:
        raise ValueError("无法读取这张图片，请重新选择有效的 JPG 或 PNG。") from exc
    return suffix


@contextmanager
def photo_for_diagnosis(data: bytes | None, suffix: str | None, db_path: Path):
    """Keep an upload only once a saved mistake references it."""
    if data is None:
        yield None
        return

    folder = db_path.parent / "uploads"
    folder.mkdir(exist_ok=True, parents=True)
    path = folder / f"{uuid4().hex}{suffix}"
    saved = False
    try:
        path.write_bytes(data)
        yield path
        saved = True
    finally:
        if not saved:
            path.unlink(missing_ok=True)


def reset_transcript(student_id: str, upload_key: str) -> None:
    """Clear a photo transcription so the student can retake or re-read it."""
    st.session_state.pop("transcript", None)
    st.session_state.pop(upload_key, None)
    st.session_state.pop("diagnosis", None)


def photo_input(pipe, student_id: str):
    if pipe.provider.name == ui.OFFLINE:
        ui.notice("离线模式支持示例和文字", "使用照片转录时，请在 .env 中配置视觉模型后重启应用。")
        return None, None, None, None
    if not pipe.cfg.vision_model:
        ui.notice("照片转录不可用", "请配置 VISION_MODEL，或手动录入题目和步骤。", tone="warn")
        return None, None, None, None

    upload_key = f"photo_upload_{student_id}"
    st.caption("点击转录后，照片会发送到当前配置的视觉模型服务。")
    upload = st.file_uploader("照片", type=["jpg", "jpeg", "png"], key=upload_key, label_visibility="collapsed")
    if upload is None:
        return None, None, None, None

    data = upload.getvalue()
    digest = sha256(data).hexdigest()
    try:
        suffix = valid_extension(data)
    except ValueError as exc:
        ui.notice("图片格式有误", str(exc), tone="warn")
        return None, None, None, None

    st.image(data, caption=upload.name, width="stretch")
    # Scope the cache to this student AND this exact photo. A failed transcript
    # is cached too, but never disables the retake button.
    state = st.session_state.get("transcript")
    parsed = state[2] if state and state[0] == student_id and state[1] == digest else None
    if st.button("转录照片", disabled=parsed is not None and parsed.ok,
                 icon=":material/document_scanner:", width="stretch"):
        try:
            with TemporaryDirectory() as folder, st.spinner("正在转录题目和你的解题步骤…"):
                temporary = Path(folder) / f"exercise{suffix}"
                temporary.write_bytes(data)
                parsed = parse_photo(pipe.provider, str(temporary))
            st.session_state.transcript = (student_id, digest, parsed)
        except LLMError as exc:
            show_provider_error(exc)
        except OSError:
            ui.notice("读取照片失败", "请检查本地临时目录，再重新上传。", tone="danger")

    if parsed is not None:
        if parsed.ok:
            st.caption("请核对转录。若有遗漏，可切到手动录入。")
            ui.question(parsed.question, parsed.steps)
        else:
            ui.notice("需要重新拍摄", ui.RETAKE[parsed.status], tone="warn")
            if st.button("重新拍照 / 重传", key=f"retake_{student_id}",
                         icon=":material/restart_alt:", width="stretch"):
                reset_transcript(student_id, upload_key)
                st.rerun()
    return parsed if parsed and parsed.ok else None, digest, data, suffix


def render_diagnosis(pipe, conn, student_id: str, db_path: Path) -> None:
    ui.hero()
    result = failure = None
    with st.container(key="diagnosis_workflow"):
        left, right = st.columns([1.1, 1], gap="medium")
        with left, st.container(key="input_panel"):
            ui.heading("提交错题", "带上你的解题步骤，才能定位断点。", panel=True, icon="↗")
            modes = {"sample": "示例错题", "photo": "拍照上传", "text": "手动录入"}
            source = st.radio("录入方式", list(modes), format_func=modes.get, horizontal=True, label_visibility="collapsed")
            data = suffix = None
            if source == "sample":
                examples = samples()
                choice = st.selectbox("选择示例", list(examples), format_func=lambda key: SAMPLE_NAMES.get(key, key))
                parsed, identity = examples[choice], choice
                if parsed.ok:
                    ui.question(parsed.question, parsed.steps)
                else:
                    st.caption("此示例演示照片无法转录时的重拍提示。")
            elif source == "text":
                with st.form("typed_submission", border=False):
                    question_text = st.text_area("题目", placeholder="求 2(x+3) = 4x - 6 中 x 的值", max_chars=4000)
                    steps_text = st.text_area("我的解题步骤", placeholder="每行填写一个步骤", max_chars=8000)
                    submitted = st.form_submit_button("提交诊断", type="primary", icon=":material/arrow_forward:",
                                                      width="stretch")
                steps = [line.strip() for line in steps_text.splitlines() if line.strip()]
                parsed = from_text(question_text.strip(), steps) if question_text.strip() and steps else None
                identity = (question_text, steps_text)
            else:
                parsed, identity, data, suffix = photo_input(pipe, student_id)

            key = (student_id, source, identity)
            # Stable, content-derived key: a refresh or repeated click cannot add
            # a second mistake row for the same student + input.
            submission_key = sha256(
                json.dumps([student_id, source, identity], ensure_ascii=False, sort_keys=True, default=str).encode()
            ).hexdigest()
            last = st.session_state.get("diagnosis")
            if last and last[0] == key:
                result = last[1]
            if source != "text":
                # Always render submit so students can diagnose several wrong
                # answers in a row without switching samples or photos.
                submitted = st.button("提交诊断", type="primary", icon=":material/arrow_forward:", width="stretch",
                                      disabled=parsed is None)
                if result is not None:
                    if st.button("诊断下一题", key="next_problem", icon=":material/add_circle:", width="stretch"):
                        st.session_state.pop("diagnosis", None)
                        st.rerun()
            if result is not None:
                st.html('<a class="pf-result-link" href="#diagnosis-result">查看诊断结果 ↓</a>')
            if submitted and parsed is None:
                ui.notice("题目和步骤都需要填写", "请把每一步作答单独写在一行。", tone="warn")
            if submitted and parsed is not None and result is None:
                try:
                    with st.spinner("正在定位断点并匹配课程知识点…"):
                        # Unreadable photo transcripts are filtered at the input stage.
                        with photo_for_diagnosis(data, suffix, db_path) as path:
                            result = pipe.run(parsed, student_id=student_id,
                                              image_path=str(path) if path else None, source=source,
                                              submission_key=submission_key)
                            if path and not result.get("mistake_id"):
                                path.unlink(missing_ok=True)
                    st.session_state.diagnosis = (key, result)
                    st.rerun()
                except (LLMError, sqlite3.Error, OSError) as exc:
                    failure = exc

        with right, st.container(key="output_panel"):
            ui.heading("诊断结果", "查看首次出错的步骤和对应知识点。", panel=True, icon="⌖",
                       anchor="diagnosis-result")
            if isinstance(failure, LLMError):
                show_provider_error(failure)
            elif failure is not None:
                ui.notice("记录保存失败", "请检查本地数据库或磁盘权限，再重新提交。", tone="danger")
            elif result is None:
                ui.waiting()
            else:
                row = mistake_details(conn, result["mistake_id"], pipe.cfg.confidence_review) if result.get("mistake_id") else None
                render_result(pipe, result, row)


def render_result(pipe, result: dict, row) -> None:
    flags = result["flags"]
    if result["status"] != "ok":
        ui.notice("需要重新拍摄", ui.RETAKE.get(result["status"], ui.RETAKE["unreadable"]), tone="warn")
        st.caption(ui.provider_name(flags["provider"]))
        return

    decision = row["decision"] if row else None
    # Prefer the effective knowledge point: a teacher may have assigned one to an
    # originally unmapped mistake. Fall back to the diagnosis-time value.
    knowledge_point = (row["effective_kp"] if row else None) or result["knowledge_point"]
    node = pipe.node_by_id(knowledge_point) if knowledge_point else None
    ui.diagnosis_summary(
        result["error_type"], knowledge_point, result["confidence"], flags["provider"],
        provisional=flags["needs_teacher_review"] and decision != "confirmed",
    )
    has_items = bool(node and node.get("items"))
    can_practice = has_items and (
        decision == "confirmed" or (decision is None and not flags["needs_teacher_review"])
    )
    if decision:
        ui.notice(f"教师意见 · {REVIEW_LABELS[decision]}", row["review_note"],
                  tone="warn" if decision == "rejected" else "neutral")
    elif flags["needs_teacher_review"]:
        message = ("当前结果无法引用一条确切的原始步骤。"
                   if not result["broken_step"] else "当前诊断置信度较低。")
        ui.notice("等待教师复核", message + "教师确认后可解锁已映射的练习。", tone="warn")
    if flags["unmapped"]:
        score, threshold = flags["grounding_score"], flags["threshold"]
        ui.notice("暂未映射到课程知识点",
                  f"匹配分数 {score:.2f}，阈值 {threshold:.2f}。此错题已进入教师复核队列，"
                  "教师会在复核时指定对应知识点，之后练习会自动出现，无需你操作。", tone="warn")

    ui.question(result["question"], result["steps"], result["broken_step"])
    if result["explanation"]:
        ui.notice("断点分析", result["explanation"])
    if can_practice and node:
        ui.notice("订正提示", node.get("remediation_zh") or result["remediation"] or "")

    if result["mistake_id"]:
        st.caption(f"已保存到错题档案 · #{result['mistake_id']}")
    if can_practice:
        st.button("查看复习路径", icon=":material/route:", on_click=navigate, args=("path",), width="stretch")


def render_path(pipe, conn, student_id: str) -> None:
    ui.heading("复习路径", "从最近一次诊断或教师确认起算，按薄弱点优先级安排第 1、3、7 天的练习。")
    today = date.today()
    blocks = pipe.review_plan(conn, student_id)
    pending = sum(len(block["items"]) for block in blocks)
    completed = completed_practice(conn, student_id)
    max_per_day = pipe.cfg.max_practice_per_day
    studied = practice_on(conn, student_id, today)
    ui.stats([
        ("本轮待练", str(pending), "来自课程题库"),
        ("累计已掌握", str(sum(map(len, completed.values()))), "自评记录"),
        ("今日已练", f"{len(studied)} / {max_per_day}", "每天的练习限量"),
    ])
    if not pending:
        ui.empty("当前没有待练习题", "提交新的错题或等待教师确认，关联的课程练习会出现在这里。", symbol="↗")
        st.button("返回诊断台", on_click=navigate, args=("diagnose",))
        return

    st.caption("到期后可记录掌握情况。选择「再练一次」会保留该题。同一天做同一题只计一次。")
    for block in blocks:
        day = block["day"]
        due = date.fromisoformat(block["date"])
        date_label = f"{due.isoformat()} · 尚未到期" if due > today else due.isoformat()
        with st.container(key=f"study_day_{day}"):
            ui.day_header(day, date_label, len(block["items"]))
            if not block["items"]:
                st.caption("当天暂无待练。")
            for item in block["items"]:
                ui.practice_header(item)
                if item.get("solution"):
                    with st.expander("查看参考解法"):
                        st.markdown(item["solution"])
                assignment = (item["mistake_id"], item["item_id"])
                key = f"{assignment[0]}_{assignment[1]}_{day}"
                available = due <= today and (len(studied) < max_per_day or assignment in studied)
                with st.container(horizontal=True):
                    mastered = st.button("已掌握", key=f"pass_{key}", icon=":material/check:", disabled=not available)
                    retry = st.button("再练一次", key=f"again_{key}", disabled=not available)
                if mastered or retry:
                    recorded = record_practice(conn, *assignment, day, "pass" if mastered else "retry",
                                               max_per_day=max_per_day, not_before=due)
                    if recorded:
                        message = "已记录掌握情况。" if mastered else "已记录，题目保留在待练路径中。"
                    else:
                        message = "练习状态已更新，请查看今天的进度。"
                    st.session_state.workspace_notice = message
                    st.rerun()


def review_state(row) -> str:
    if row["decision"]:
        return REVIEW_LABELS[row["decision"]]
    if row["needs_review"] or not row["grounded"]:
        return "待复核"
    return "已诊断"


def show_case(row, *, photo_dir: Path, teacher: bool = False) -> None:
    status = review_state(row)
    source = ui.SOURCES.get(row["source"], "历史或 CLI 记录")
    date_text = row["created_at"][:10]
    st.caption(f"#{row['id']} · {date_text} · {source} · {status}" + (f" · 学生 {row['student_id']}" if teacher else ""))
    ui.diagnosis_summary(
        row["error_type"], row["effective_kp"], row["confidence"], row["provider"],
        provisional=bool(row["needs_review"]) and row["decision"] != "confirmed",
    )
    if row["override_kp"]:
        st.caption(f"知识点由教师指定：{row['override_kp']} · {ui.topic(row['override_kp'])}")
    ui.question(row["question_text"], (row["steps"] or "").splitlines(), row["broken_step"])
    if row["image_path"]:
        try:
            photo = Path(row["image_path"]).resolve()
            stored = photo.is_relative_to(photo_dir.resolve()) and photo.is_file()
        except (OSError, RuntimeError):
            stored = False
        if stored and st.toggle("查看原始照片", key=f"photo_case_{row['id']}"):
            st.image(str(photo), width="stretch")
    if row["explanation"]:
        ui.notice("诊断说明", row["explanation"])
    if row["review_note"]:
        ui.notice(f"教师意见 · {status}", row["review_note"])


def render_history(pipe, conn, student_id: str, photo_dir: Path) -> None:
    ui.heading("错题档案", "查看历史诊断、知识薄弱点和教师复核意见。")
    rows = mistake_history(conn, student_id, pipe.cfg.confidence_review)
    profile = gap_profile(conn, student_id)
    repeat = repeat_error_rate(conn, student_id)
    ui.stats([
        ("已记录错题", str(len(rows)), "当前学生"),
        ("已映射知识点", str(len(profile)), "历史累计"),
        ("14 天重复错误率", f"{repeat['repeat_error_rate']:.0%}" if repeat["observations"] else "—",
         f"{repeat['repeat_knowledge_points']} / {repeat['distinct_knowledge_points']} 个近期知识点"),
    ])
    if not rows:
        ui.empty("还没有错题记录", "从诊断台提交一道错题，结果会保存在这里。", symbol="▤")
        st.button("返回诊断台", on_click=navigate, args=("diagnose",))
        return

    if profile:
        with st.container(key="profile_panel"):
            ui.heading("知识薄弱点", panel=True)
            st.dataframe(
                [{"知识点": ui.topic(row["knowledge_point"]), "编号": row["knowledge_point"],
                  "错题次数": row["repeat_count"], "已掌握练习": row["cleared_count"],
                  "最近错误类型": ui.ERRORS.get(row["error_type"], "—")} for row in profile],
                hide_index=True, width="stretch",
            )
    ui.heading("诊断记录", panel=True)
    selection = st.radio("筛选记录", ["全部", "待复核", "已确认", "退回补充"], horizontal=True, label_visibility="collapsed")
    selected = [row for row in rows if selection == "全部" or review_state(row) == selection]
    if not selected:
        st.caption("没有符合条件的记录。")
    for row in selected[:50]:
        title = " ".join((row["question_text"] or "未记录题目").split())
        with st.expander(f"#{row['id']} · {title[:85]}"):
            show_case(row, photo_dir=photo_dir)
    if len(selected) > 50:
        st.caption("显示最近 50 条符合条件的记录。")


def class_filter(conn, key: str):
    return st.selectbox("班级", [None, *list_classes(conn)], key=key,
                        format_func=lambda value: value if value is not None else "全部班级")


def render_class(pipe, conn) -> None:
    ui.heading("班级学情", "按课程知识点查看错题集中在哪些环节。")
    class_id = class_filter(conn, "class_filter")
    heat = class_heatmap(conn, class_id, by_error=True)
    pending = review_cases(conn, pipe.cfg.confidence_review, class_id)
    ui.stats([
        ("已映射错题", str(sum(row["mistakes"] for row in heat)), "含待确认记录"),
        ("涉及知识点", str(len(heat)), "基于班级历史记录"),
        ("待教师复核", str(len(pending)), "低置信度或未映射"),
    ])
    with st.container(key="heat_panel"):
        ui.heading("知识点热力图", "颜色越深，代表该类错题出现越多。", panel=True, icon="▦")
        if heat:
            ui.heatmap(heat)
        else:
            ui.empty("这个范围还没有已映射错题", "学生提交并匹配到课程知识点后，热力图会自动更新。", symbol="▦")
    st.button("查看复核队列", on_click=navigate, args=("review",), icon=":material/fact_check:")


def render_review(pipe, conn, photo_dir: Path) -> None:
    ui.heading("复核队列", "核对原题和学生步骤，记录确认或退回意见。")
    class_id = class_filter(conn, "review_class_filter")
    category = st.radio("复核状态", ["待处理", "已处理"], horizontal=True)
    resolved = category == "已处理"
    rows = review_cases(conn, pipe.cfg.confidence_review, class_id, resolved=resolved)
    if not rows:
        ui.empty("这个范围暂无复核记录", "置信度较低、缺少可靠断点或未匹配到知识点的错题会进入队列。")
        return

    choices = {row["id"]: row for row in rows}
    selected = st.selectbox(
        "选择错题", list(choices),
        format_func=lambda mid: f"#{mid} · {choices[mid]['student_id']} · {ui.topic(choices[mid]['knowledge_point_id'])}",
    )
    with st.container(key="profile_panel"):
        row = choices[selected]
        show_case(row, photo_dir=photo_dir, teacher=True)
        if resolved:
            return
        knowledge_points = [node["id"] for node in pipe.nodes]
        current_kp = row["effective_kp"]
        with st.form(f"review_{selected}", border=False):
            decision = st.radio("处理意见", ["confirmed", "rejected"], horizontal=True, format_func=REVIEW_LABELS.get)
            kp_choice = st.selectbox(
                "关联知识点（未映射时可在此指定，已映射可修正）",
                [None, *knowledge_points],
                index=(knowledge_points.index(current_kp) + 1) if current_kp in knowledge_points else 0,
                format_func=lambda value: "保持默认（不指定）" if value is None else f"{value} · {ui.topic(value)}",
            )
            note = st.text_area("教师备注", placeholder="写下核对依据，或说明需要学生补充什么", max_chars=2000)
            if st.form_submit_button("保存复核", type="primary"):
                if not note.strip():
                    st.error("请填写教师备注。")
                else:
                    saved = save_review(conn, selected, decision, note)
                    if kp_choice:
                        save_kp_override(conn, selected, kp_choice)
                    st.session_state.workspace_notice = "复核意见已保存。" if saved else "此错题已有复核意见。"
                    st.rerun()


def main() -> None:
    st.set_page_config(page_title="Pathfinder AI · 错题导航", page_icon="🧭", layout="wide")
    ui.setup()
    if "pipeline" not in st.session_state:
        st.session_state.pipeline = Pipeline(Config.from_env())
    pipe = st.session_state.pipeline
    st.session_state.setdefault("page", "diagnose")
    student_id = st.session_state.setdefault("student_id", "student-01")
    page = st.session_state.page
    if page in STUDENT_PAGES:
        ui.topbar("学生工作台", STUDENT_PAGES[page][0], pipe.provider.name)
    else:
        ui.topbar("教师工作台", TEACHER_PAGES[page][0], pipe.provider.name)

    db_path = ROOT / pipe.cfg.db_path
    with closing(connect(str(db_path))) as conn:
        init_db(conn)
        navigation(conn)
        message = st.session_state.pop("workspace_notice", None)
        if message:
            st.toast(message)
        if page == "diagnose":
            render_diagnosis(pipe, conn, student_id, db_path)
        elif page == "path":
            render_path(pipe, conn, student_id)
        elif page == "history":
            render_history(pipe, conn, student_id, db_path.parent / "uploads")
        elif page == "class":
            render_class(pipe, conn)
        else:
            render_review(pipe, conn, db_path.parent / "uploads")


main()
