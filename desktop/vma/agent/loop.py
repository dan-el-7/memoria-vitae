"""Agent loop: reasoning model + tools over a run's observation store.

The model operates on structured observations retrieved via tools — never on
the raw image stream. Images re-enter only through the explicit get_observation_image
or inspect_frame tools; nothing attaches images automatically.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..providers.base import ChatMessage, ChatResult, ReasoningProvider, ToolCall
from ..runs.manager import Run
from ..utils import iso
from .tools import ALL_TOOLS, ToolContext, execute_tool

MAX_TOOL_ITERATIONS = 10

DeltaCallback = Callable[[str], Awaitable[None]]
StatusCallback = Callable[[str], Awaitable[None]]


@dataclass
class ChatOutcome:
    answer: str
    tool_trace: list[dict[str, Any]] = field(default_factory=list)
    iterations: int = 0
    total_ms: int = 0
    images_attached: int = 0


def build_system_prompt(run: Run) -> str:
    from ..utils import tz_offset_string

    stats = run.store.stats()
    cfg = run.meta.get("model_config", {})
    return f"""You are the analysis and memory layer of a visual memory system. A phone camera streamed
frames that a vision model already converted into structured observations stored in a database
for this run. You never see images unless you explicitly request one via get_observation_image
or inspect_frame.

Run: "{run.meta.get('name')}" (started {run.meta.get('created_at')})
Vision model: {cfg.get('vision', {}).get('model', '?')} | Reasoning model: {cfg.get('reasoning', {}).get('model', '?')}
Store: {stats['observations']} observations ({stats['embeddings']} semantically embedded),
{stats['frames_accepted']} processed frames of {stats['frames_total']} received,
{stats['frames_nochange']} no-change frames, {stats['frames_error']} processing-error frames,
{stats['media_files']} retained images ({stats['media_bytes']} bytes),
time span {stats['first_ts']} .. {stats['last_ts']}.

Answer the user's questions using the tools:
- search_observations for WHAT happened. mode='hybrid' (default) fuses keyword and semantic
  matching; use mode='semantic' when the user paraphrases ('where was the calculator' may match
  observations never containing the word 'calculator').
- get_observations_in_time_range for WHEN, get_location_history for WHERE.
- get_timeline_index FIRST for broad multi-hour questions: compact per-hour digests;
  fall back to the row tools only for windows the index does not cover.
- get_events for the mid-level view: contiguous scene runs with start/end and a title;
  good for 'when was I at X' and for choosing a window before fetching rows.
- search_all_runs when the question might predate THIS run or mentions another day's
  session; results are tagged with their run_id and are read-only summaries.
- get_observation for full detail of one id; observations are ordered history — semantically
  similar observations are NOT duplicates (e.g. an object at 12:01, 12:04, 12:07 is a story).
- get_observation_image attaches a stored image for visual inspection. Use it only when visual
  detail genuinely matters; images are never attached automatically.
- inspect_frame re-runs the vision model on a stored image (EXPENSIVE) — prefer it when this
  model cannot read images directly.
- get_run_stats for an overview.

Times: stored times are ISO-8601 UTC ('...Z'). The user's local timezone is UTC{tz_offset_string()}.
Every observation carries a 'local_ts' field — the same instant in local time. ALWAYS quote
local_ts for user-facing times, and use it to resolve phrases like '3pm', 'this morning' or
'this afternoon' — never do timezone arithmetic yourself.

Rules:
- Prefer several targeted tool calls over guessing; ground every claim in retrieved observations.
- If the store has no relevant data, say so explicitly instead of inventing.
- Treat `frames_error` as processing failures, never as no-change frames; mention the failure count and error evidence
  when generating a report.
- Be concise and concrete in the final answer; mention times and places from the evidence."""


def _history_messages(run: Run, limit: int = 20) -> list[ChatMessage]:
    msgs: list[ChatMessage] = []
    for row in run.store.chat_history(limit=limit):
        role = row["role"]
        if role in ("user", "assistant"):
            content = row["content"] or ""
            if role == "assistant" and not content.strip():
                continue
            msgs.append(ChatMessage(role=role, content=content))
    return msgs


async def run_chat(
    reasoning: ReasoningProvider,
    ctx: ToolContext,
    user_text: str,
    *,
    on_delta: DeltaCallback | None = None,
    on_status: StatusCallback | None = None,
) -> ChatOutcome:
    run = ctx.run
    run.store.add_chat_message("user", user_text)

    messages: list[ChatMessage] = [ChatMessage(role="system", content=build_system_prompt(run))]
    messages.extend(_history_messages(run))
    messages.append(ChatMessage(role="user", content=user_text))

    tool_trace: list[dict[str, Any]] = []
    t0 = time.monotonic()
    iterations = 0
    images_attached = 0

    for _ in range(MAX_TOOL_ITERATIONS):
        iterations += 1
        if on_status:
            await on_status(f"thinking (step {iterations})...")
        result: ChatResult = await reasoning.chat(messages, tools=ALL_TOOLS, on_stream=on_delta)
        if not result.tool_calls:
            run.store.add_chat_message(
                "assistant", result.content,
                tool_trace={"calls": tool_trace} if tool_trace else None,
                provider="reasoning", model=getattr(reasoning, "model", None),
            )
            return ChatOutcome(result.content, tool_trace, iterations,
                               int((time.monotonic() - t0) * 1000), images_attached)

        # Persist the assistant tool-call turn, execute tools, feed results back.
        messages.append(ChatMessage(role="assistant", content=result.content, tool_calls=result.tool_calls))
        for call in result.tool_calls:
            if on_status:
                await on_status(f"tool:{call.name}")
            tool_result = await execute_tool(ctx, call.name, call.arguments or {})
            # get_observation_image returns `_images_b64`; strip it from the JSON
            # the model reads and attach the image itself to a following turn.
            images: list[bytes] = []
            try:
                parsed = json.loads(tool_result)
                if isinstance(parsed, dict) and isinstance(parsed.get("result"), dict):
                    b64_list = parsed["result"].pop("_images_b64", None)
                    if b64_list:
                        import base64

                        images = [base64.b64decode(b) for b in b64_list]
                        images_attached += len(images)
                        tool_result = json.dumps(parsed, ensure_ascii=False)
            except (json.JSONDecodeError, ValueError):
                pass
            tool_trace.append({
                "ts": iso(), "name": call.name, "arguments": call.arguments,
                "result_excerpt": tool_result[:2000],
            })
            messages.append(ChatMessage(role="tool", content=tool_result, tool_name=call.name))
            if images:
                messages.append(ChatMessage(
                    role="user",
                    content=f"[Image attached by tool {call.name} — inspect it to answer the question]",
                    images=images,
                ))

    # Iteration budget exhausted: ask for a direct answer with what we have.
    if on_status:
        await on_status("synthesizing answer...")
    messages.append(ChatMessage(
        role="user",
        content="Tool budget reached. Answer now using the information gathered so far.",
    ))
    result = await reasoning.chat(messages, tools=None, on_stream=on_delta)
    run.store.add_chat_message(
        "assistant", result.content, tool_trace={"calls": tool_trace, "budget_exhausted": True},
        provider="reasoning", model=getattr(reasoning, "model", None),
    )
    return ChatOutcome(result.content, tool_trace, iterations,
                       int((time.monotonic() - t0) * 1000), images_attached)


def tool_calls_from(outcome: ChatOutcome) -> list[ToolCall]:
    return [ToolCall(id=str(i), name=c["name"], arguments=c["arguments"]) for i, c in enumerate(outcome.tool_trace)]
