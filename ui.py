"""Presentation components for the Streamlit workspaces."""

from html import escape
from pathlib import Path

import streamlit as st

ERRORS = {
    "conceptual": "概念理解",
    "procedural": "解题过程",
    "careless": "计算疏漏",
    "unreadable": "无法辨认",
}
LEVELS = {"easy": "基础", "medium": "进阶", "applied": "应用"}
SOURCES = {"sample": "内置示例", "photo": "照片转录", "text": "手动录入"}
TOPICS = {
    "ALG-7.1": "合并同类项",
    "ALG-7.2": "一元一次方程",
    "ALG-7.3": "乘法分配律",
    "ALG-7.4": "异分母分数运算",
    "ALG-8.1": "指数运算",
    "ALG-8.3": "完全平方公式",
    "PHY-9.1": "牛顿第二定律",
    "PHY-9.2": "国际单位换算",
    "CHE-8.1": "化学方程式配平",
    "ENG-7.1": "主谓一致",
}
RETAKE = {
    "ambiguous": "题目或步骤缺失。请拍进完整题目和每一步作答，也可以手动录入。",
    "rotated": "请将纸面摆正，让镜头正对作业后重新上传。",
    "unreadable": "请在光线充足处正对纸面拍摄，确保题目和作答步骤完整清晰。",
}
OFFLINE = "offline-rule-baseline"
MARK = '<span class="pf-mark" aria-hidden="true"><i></i><i></i><i></i></span>'


def e(value) -> str:
    return escape(str(value)) if value is not None else ""


def topic(kp: str) -> str:
    return TOPICS.get(kp, kp) if kp else "暂未映射"


def provider_name(provider: str) -> str:
    return {
        OFFLINE: "离线规则基线",
        "openai-compatible": "在线模型",
        "openai-compatible+local-hash": "在线模型 · 本地检索",
    }.get(provider, provider or "来源未知")


def setup() -> None:
    st.html(Path(__file__).with_name("ui.css"))


def brand() -> None:
    st.html(f'<div class="pf-brand">{MARK}<div><strong>Pathfinder</strong><small>错题导航</small></div></div>')


def topbar(area: str, page: str, provider: str) -> None:
    st.html(
        f'<div class="pf-topbar"><div>{e(area)} <span class="pf-slash">/</span> '
        f'<strong>{e(page)}</strong></div><span class="pf-provider"><i></i>{e(provider_name(provider))}</span></div>'
    )


def hero() -> None:
    st.html(
        '<section class="pf-hero">'
        '<div class="pf-hero-copy"><p class="pf-hero-label"><span></span> 错题诊断</p>'
        '<h1>回到出错的那一步<span>。</span></h1>'
        '<p>提交题目和你的解题过程，定位思路断点。'
        '<br class="pf-hero-break">再用第 1、3、7 天的练习巩固对应知识点。</p></div>'
        '<div class="pf-orbit" aria-hidden="true">'
        '<i class="pf-orbit-ring pf-ring-1"></i><i class="pf-orbit-ring pf-ring-2"></i>'
        '<i class="pf-orbit-ring pf-ring-3"></i><i class="pf-orbit-ring pf-ring-4"></i>'
        '<i class="pf-route pf-route-1"></i><i class="pf-route pf-route-2"></i><i class="pf-route pf-route-3"></i>'
        '<span class="pf-orbit-node pf-node-1"><b></b><small>01</small></span>'
        '<span class="pf-orbit-node pf-node-2"><b></b><small>03</small></span>'
        '<span class="pf-orbit-node pf-node-3"><b></b><small>07</small></span></div></section>'
    )


def heading(title: str, description: str = "", *, panel: bool = False, icon: str = "", anchor: str = "") -> None:
    level = "h2" if panel else "h1"
    suffix = f"<p>{e(description)}</p>" if description else ""
    symbol = f'<span class="pf-panel-icon" aria-hidden="true">{e(icon)}</span>' if icon else ""
    ident = f' id="{e(anchor)}"' if anchor else ""
    st.html(f'<header class="pf-heading"{ident}>{symbol}<{level}>{e(title)}</{level}>{suffix}</header>')


def empty(title: str, description: str, *, symbol: str = "◎") -> None:
    st.html(
        '<div class="pf-empty">'
        f'<span class="pf-empty-icon" aria-hidden="true">{e(symbol)}</span>'
        f'<h3>{e(title)}</h3><p>{e(description)}</p></div>'
    )


def notice(title: str, message: str, *, tone: str = "neutral") -> None:
    st.html(f'<div class="pf-notice pf-{tone}"><strong>{e(title)}</strong><p>{e(message)}</p></div>')


def question(question_text: str, steps: list[str], broken_step: str | None = None) -> None:
    rows = []
    highlighted = False
    for i, step in enumerate(steps, 1):
        hit = broken_step and step.strip() == broken_step.strip() and not highlighted
        if hit:
            highlighted = True
        klass = " class=\"pf-step-hit\"" if hit else ""
        status = '<strong class="pf-step-flag">断点</strong>' if hit else ""
        rows.append(f'<li{klass}><span>{i:02}</span><code>{e(step)}</code>{status}</li>')
    work = f'<ol class="pf-steps">{"".join(rows)}</ol>' if rows else ""
    text = f'<p class="pf-question-text">{e(question_text)}</p>' if question_text else ""
    st.html(f'<div class="pf-question">{text}{work}</div>')


def waiting() -> None:
    st.html(
        '<div class="pf-waiting"><div class="pf-scan" aria-hidden="true"><i></i><i></i><i></i>'
        '<b>⌖</b></div><h3>等待诊断</h3><p>提交后，这里会圈出第一个出错的步骤，并显示对应的知识点。</p>'
        '<div class="pf-waiting-stages"><span>定位断点</span><span>匹配知识点</span><span>安排复习</span></div></div>'
    )


def badge(text: str, tone: str = "green") -> str:
    return f'<span class="pf-badge pf-badge-{tone}">{e(text)}</span>'


def diagnosis_summary(
    error_type: str, knowledge_point: str, confidence: float | None, provider: str, *, provisional: bool = False,
) -> None:
    label = "规则置信分" if provider == OFFLINE else "诊断置信度"
    score = "—"
    if confidence is not None:
        score = f"{confidence:.2f}" if provider == OFFLINE else f"{confidence:.0%}"
    error_label = ERRORS.get(error_type, "待复核")
    if provisional:
        error_label = f"初判 · {error_label}"
    st.html(
        '<div class="pf-result-meta">'
        f'{badge(error_label, "warn" if provisional else "green")}{badge(provider_name(provider), "neutral")}</div>'
        f'<div class="pf-kp"><div><span>课程知识点</span><strong>{e(topic(knowledge_point))}</strong>'
        f'<small>{e(knowledge_point or "")}</small></div><div><span>{label}</span><strong>{e(score)}</strong></div></div>'
    )


def stats(items: list[tuple[str, str, str]]) -> None:
    cells = "".join(
        f'<div><span>{e(label)}</span><strong>{e(value)}</strong><small>{e(detail)}</small></div>'
        for label, value, detail in items
    )
    st.html(f'<div class="pf-stats">{cells}</div>')


def day_header(day: int, date_text: str, count: int) -> None:
    st.html(
        f'<div class="pf-day-header"><span class="pf-day-index">{day:02}</span>'
        f'<div><h2>第 {day} 天</h2><p>{e(date_text)}</p></div><small>{count} 道待练</small></div>'
    )


def practice_header(item: dict) -> None:
    st.html(
        f'<div class="pf-practice">{badge(LEVELS.get(item["difficulty"], item["difficulty"]))}'
        f'<span>{e(topic(item.get("knowledge_point")))}</span><p>{e(item["text"])}</p></div>'
    )


def heatmap(rows: list[dict]) -> None:
    kinds = ["conceptual", "procedural", "careless"]
    peak = max(row["mistakes"] for row in rows)
    header = "".join(f'<th scope="col">{e(ERRORS[kind])}</th>' for kind in kinds)
    body = []
    for row in rows:
        kp = row["knowledge_point"]
        counts = [row["error_types"].get(kind, 0) for kind in kinds] + [row["mistakes"]]
        cells = "".join(
            f'<td class="pf-heat-{min(4, (count * 4 + peak - 1) // peak)}">{count}</td>' for count in counts
        )
        body.append(f'<tr><th scope="row">{e(topic(kp))}<small>{e(kp)}</small></th>{cells}</tr>')
    st.html(
        '<div class="pf-heatmap"><table><thead><tr><th scope="col">知识点</th>'
        f'{header}<th scope="col">合计</th></tr></thead><tbody>{"".join(body)}</tbody></table>'
        '<div class="pf-legend">错题数 <span>少</span><i class="pf-heat-1"></i><i class="pf-heat-2"></i>'
        '<i class="pf-heat-3"></i><i class="pf-heat-4"></i><span>多</span></div></div>'
    )
