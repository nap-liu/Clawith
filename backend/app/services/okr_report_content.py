"""Pure content-building helpers for OKR company reports."""

from __future__ import annotations

from datetime import date, timedelta

from app.models.okr import CompanyReport


MEMBER_DAILY_CHAR_LIMIT = 2000
BUCKET_SIZE = 20
LLM_PROMPT_CHAR_LIMIT = 1200

RISK_KEYWORDS = (
    "risk", "block", "blocked", "issue", "delay", "delayed",
    "problem", "pending", "stuck", "dependency",
    "风险", "阻塞", "问题", "延期", "卡住", "依赖",
)


def _truncate_report_content(content: str) -> str:
    """Normalize member report content and enforce the character cap."""
    normalized_lines = [
        " ".join(line.split())
        for line in (content or "").replace("\r\n", "\n").split("\n")
        if line.strip()
    ]
    normalized = "\n".join(normalized_lines)
    if len(normalized) <= MEMBER_DAILY_CHAR_LIMIT:
        return normalized
    return normalized[: MEMBER_DAILY_CHAR_LIMIT - 1].rstrip() + "…"


def _truncate_for_prompt(content: str, limit: int = LLM_PROMPT_CHAR_LIMIT) -> str:
    """Trim source text before sending it to the report summarizer."""
    normalized = _truncate_report_content(content)
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1].rstrip() + "…"


def _contains_risk(text: str) -> bool:
    lowered = (text or "").lower()
    return any(keyword in lowered for keyword in RISK_KEYWORDS)


def _period_label(report_type: str, period_start: date, period_end: date) -> str:
    """Build a compact display label for the report period."""
    if report_type == "daily":
        return period_start.isoformat()
    if report_type == "weekly":
        iso_year, iso_week, _ = period_start.isocalendar()
        return f"{iso_year} W{iso_week:02d}"
    return period_start.strftime("%Y-%m")


def _monday_of(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _month_start(day: date) -> date:
    return day.replace(day=1)


def _month_end(day: date) -> date:
    if day.month == 12:
        return day.replace(month=12, day=31)
    return day.replace(month=day.month + 1, day=1) - timedelta(days=1)


def _bucket_items(items: list[dict], bucket_size: int = BUCKET_SIZE) -> list[list[dict]]:
    """Split items into deterministic fixed-size buckets."""
    return [items[idx: idx + bucket_size] for idx in range(0, len(items), bucket_size)]


def _summarize_member_bucket(bucket: list[dict], label: str) -> tuple[list[str], list[str]]:
    """Produce lightweight bucket-level progress and risk bullets."""
    updates: list[str] = []
    risks: list[str] = []

    for item in bucket:
        text = item["content"].strip()
        if not text:
            continue
        display_name = item["display_name"]
        sentence = text.replace("\n", " ").strip()
        if _contains_risk(sentence):
            risks.append(f"{display_name}: {sentence}")
        else:
            updates.append(f"{display_name}: {sentence}")

    update_lines = updates[:3]
    risk_lines = risks[:2]
    if update_lines:
        update_lines = [f"{label}: " + " | ".join(update_lines)]
    if risk_lines:
        risk_lines = [f"{label}: " + " | ".join(risk_lines)]
    return update_lines, risk_lines


def _build_company_daily_content(
    period_day: date,
    submitted_count: int,
    missing_members: list[dict],
    submitted_items: list[dict],
) -> str:
    """Build a concise company daily report from member daily reports."""
    lines = [
        "# Company Daily Report",
        f"Date: {period_day.isoformat()}",
        "",
        "## Submission Summary",
        f"- Submitted: {submitted_count}",
        f"- Missing: {len(missing_members)}",
        "",
    ]

    updates: list[str] = []
    risks: list[str] = []
    buckets = _bucket_items(submitted_items)
    for idx, bucket in enumerate(buckets, start=1):
        bucket_updates, bucket_risks = _summarize_member_bucket(bucket, f"Bucket {idx}")
        updates.extend(bucket_updates)
        risks.extend(bucket_risks)

    lines.append("## Key Updates")
    if updates:
        lines.extend(f"- {line}" for line in updates[:8])
    else:
        lines.append("- No major progress updates were submitted.")
    lines.append("")

    lines.append("## Key Risks")
    if risks:
        lines.extend(f"- {line}" for line in risks[:6])
    else:
        lines.append("- No major risks were highlighted.")
    lines.append("")

    lines.append("## Follow-up")
    if missing_members:
        preview = ", ".join(item["display_name"] for item in missing_members[:10])
        suffix = " ..." if len(missing_members) > 10 else ""
        lines.append(f"- Missing reports: {preview}{suffix}")
    else:
        lines.append("- All members submitted their reports.")

    return "\n".join(lines)


def _default_report_headings(report_type: str) -> tuple[str, str]:
    """Return canonical report title metadata."""
    if report_type == "daily":
        return "Company Daily Report", "Date"
    if report_type == "weekly":
        return "Company Weekly Report", "Period"
    return "Company Monthly Report", "Period"


def _sanitize_llm_report_output(
    report_type: str,
    period_start: date,
    period_end: date,
    content: str,
) -> str:
    """Normalize LLM output into markdown while preserving the requested structure."""
    text = (content or "").strip()
    if not text:
        return ""
    if text.startswith("```"):
        parts = text.split("```")
        text = next((part for part in parts if part.strip() and part.strip().lower() != "markdown"), "").strip()
        if text.lower().startswith("markdown"):
            text = text[len("markdown"):].strip()

    title, period_key = _default_report_headings(report_type)
    period_line = (
        f"{period_key}: {period_start.isoformat()}"
        if report_type == "daily"
        else f"{period_key}: {period_start.isoformat()} to {period_end.isoformat()}"
    )

    if not text.startswith("# "):
        text = f"# {title}\n{period_line}\n\n{text}"
    else:
        lines = text.splitlines()
        if lines[0].strip() != f"# {title}":
            lines[0] = f"# {title}"
        text = "\n".join(lines)
        if period_line not in text:
            body = "\n".join(text.splitlines()[1:]).lstrip("\n")
            text = f"# {title}\n{period_line}\n\n{body}".strip()

    return text


def _extract_section_lines(content: str, section: str) -> list[str]:
    """Extract bullet lines from a markdown section title."""
    lines = content.splitlines()
    in_section = False
    collected: list[str] = []
    for line in lines:
        if line.startswith("## "):
            in_section = line.strip() == f"## {section}"
            continue
        if in_section and line.startswith("- "):
            collected.append(line[2:].strip())
    return collected


def _is_placeholder_rollup_line(line: str) -> bool:
    """Return True when a line is just a generated placeholder/noise line."""
    normalized = line.strip().lower()
    placeholder_prefixes = (
        "no major progress updates were submitted.",
        "no major updates were recorded in this period.",
        "no major risks were highlighted.",
        "no sustained risks were identified.",
        "all members submitted their reports.",
        "missing reports:",
    )
    return any(normalized.startswith(prefix) for prefix in placeholder_prefixes)


def _dedupe_preserve_order(items: list[str]) -> list[str]:
    """Remove duplicate lines while preserving the first-seen order."""
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        normalized = item.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def _build_company_rollup_content(
    title: str,
    period_start: date,
    period_end: date,
    source_reports: list[CompanyReport],
    *,
    missing_count: int,
    submitted_count: int,
) -> str:
    """Build a weekly or monthly report from lower-level company reports."""
    lines = [
        f"# {title}",
        f"Period: {period_start.isoformat()} to {period_end.isoformat()}",
        "",
    ]

    aggregated_updates: list[str] = []
    aggregated_risks: list[str] = []
    aggregated_followups: list[str] = []

    for report in source_reports:
        aggregated_updates.extend(_extract_section_lines(report.content, "Key Updates"))
        aggregated_risks.extend(_extract_section_lines(report.content, "Key Risks"))
        aggregated_followups.extend(_extract_section_lines(report.content, "Follow-up"))

    aggregated_updates = _dedupe_preserve_order(
        [item for item in aggregated_updates if not _is_placeholder_rollup_line(item)]
    )
    aggregated_risks = _dedupe_preserve_order(
        [item for item in aggregated_risks if not _is_placeholder_rollup_line(item)]
    )
    aggregated_followups = _dedupe_preserve_order(
        [item for item in aggregated_followups if not _is_placeholder_rollup_line(item)]
    )

    lines.append("## Key Updates")
    if aggregated_updates:
        lines.extend(f"- {item}" for item in aggregated_updates[:10])
    else:
        lines.append("- No major updates were recorded in this period.")
    lines.append("")

    lines.append("## Key Risks")
    if aggregated_risks:
        lines.extend(f"- {item}" for item in aggregated_risks[:8])
    else:
        lines.append("- No sustained risks were identified.")
    lines.append("")

    lines.append("## Follow-up")
    if aggregated_followups:
        lines.extend(f"- {item}" for item in aggregated_followups[:6])
    else:
        lines.append("- No period-level follow-up items were carried over.")

    return "\n".join(lines)
