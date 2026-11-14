from __future__ import annotations

from typing import Optional

from .transcript_watcher import SessionLedger


def _fmt_tok(val: int) -> str:
    if val >= 1_000_000:
        return f"{val / 1_000_000:.2f}M"
    if val >= 1_000:
        return f"{val / 1_000:.1f}k"
    return str(val)


def build_handoff_prompt(ledger: Optional[SessionLedger], alias: str = "") -> str:
    if ledger is None:
        return "# Context Handoff Summary\n\nChưa có phiên nào được chọn."

    title = alias or ledger.title or ledger.session_id
    last_step = ledger.steps[-1] if ledger.steps else None
    ctx = last_step.context_tokens if last_step else 0
    total_prompt = ledger.total_cache_read + ledger.total_fresh_input
    cache_pct = (ledger.total_cache_read / total_prompt * 100.0) if total_prompt > 0 else 0.0

    lines = [
        f"# Context Handoff — {title}",
        "",
        "## 1. Thông tin Phiên & Quota",
        f"- **Session ID:** `{ledger.session_id}`",
        f"- **Model:** `{ledger.current_model}`",
        f"- **Ngữ cảnh trước khi cắt phiên:** `{_fmt_tok(ctx)}` tokens (`{len(ledger.steps)}` bước LLM)",
        f"- **Hiệu suất Cache cũ:** `{cache_pct:.1f}%` Cache Read (`{_fmt_tok(ledger.total_cache_read)}`) | Fresh Input (`{_fmt_tok(ledger.total_fresh_input)}`) | Rớt cache: `{ledger.cache_miss_count}` lần",
        "",
        "## 2. Mục tiêu & Yêu cầu Gần nhất",
    ]
    if ledger.first_user_prompt:
        lines.append(f"- **Yêu cầu gốc:** {ledger.first_user_prompt}")
    if ledger.last_user_prompt and ledger.last_user_prompt != ledger.first_user_prompt:
        lines.append(f"- **Yêu cầu gần nhất:** {ledger.last_user_prompt}")
    if not ledger.first_user_prompt and not ledger.last_user_prompt:
        lines.append("- *(Không có tóm tắt yêu cầu đầu vào trong log)*")

    lines.extend(["", "## 3. Tệp & Công cụ Liên quan Gần đây"])
    if ledger.recent_files:
        for fp in ledger.recent_files[-8:]:
            lines.append(f"- `{fp}`")
    else:
        lines.append("- *(Chưa ghi nhận đường dẫn tệp trong tool_calls)*")

    if last_step:
        tools_str = ", ".join(last_step.tool_calls) if last_step.tool_calls else "Trả lời người dùng"
        lines.extend(
            [
                "",
                "## 4. Điểm Dừng Cuối Cùng",
                f"- **Bước cuối (`#{last_step.step_index}`):** `{tools_str}` | Output: `{last_step.output_tokens}` tokens",
                "- **Chỉ dẫn tiếp tục:** Tiếp tục công việc từ trạng thái hiện tại trên ổ đĩa, không đọc lại các file đã hoàn tất trừ khi cần sửa trực tiếp.",
            ]
        )

    return "\n".join(lines)
