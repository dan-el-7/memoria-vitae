"""Hierarchical Temporal Indexing (HTI): one compact LLM timeline per hour.

A background pass reads the raw observation rows of each CLOSED hour and asks
the reasoning LLM to compress them into a short chronological digest stored in
`hour_index`. Agent queries spanning many hours then read a handful of digests
instead of scanning thousands of raw rows; drill-down stays on the row tools.

Design constraints honored here:
- Memory pillar: this only ADDS an index; raw rows are never merged/deleted.
- Local-first: skipped entirely when the reasoning stage is a cloud provider
  (indexing would ship observation text off-machine) — metric recorded once.
- Never competes with perception: the worker only sweeps when the intake is
  idle, one or two hours per pass.
"""

from __future__ import annotations

from typing import Any

from ..providers.base import ChatMessage
from ..store.db import RunStore
from ..utils import iso

MAX_ROWS_PER_HOUR = 240
MAX_SUMMARY_CHARS = 4000

_PROMPT = """Compress this hour of visual-memory observations into a compact timeline.

Rules:
- 3-12 chronological bullets, each starting with the HH:MM time.
- One line per distinct event/scene; merge consecutive near-identical rows
  (an object seen at 12:01, 12:04, 12:07 is ONE bullet "12:01-12:07 ...").
- Terse, factual, no preamble, no markdown headers.
- Include people/objects/actions that matter; skip camera noise.

Hour (UTC): {hour}
Rows ({n}, format HH:MM:SS [importance] kind: summary):

{rows}"""


class HourlyIndexer:
    def __init__(self, reasoning: Any, reasoning_kind: str, store: RunStore,
                 *, enabled: bool = False) -> None:
        self.reasoning = reasoning  # ReasoningProvider | None
        self.kind = reasoning_kind  # "ollama" | cloud kinds
        self.store = store
        self.enabled = enabled
        self._cloud_skip_noted = False

    async def build_missing(self, *, max_hours: int = 2) -> int:
        """Index up to `max_hours` of the oldest closed, unindexed hours."""
        if not self.enabled or self.reasoning is None:
            return 0
        if self.kind != "ollama":
            # Egress guard: observation text must not leave for a background
            # convenience pass the user may not associate with cloud usage.
            if not self._cloud_skip_noted:
                self._cloud_skip_noted = True
                self.store.add_metric("hour_index_skipped_cloud", 1, {"kind": self.kind})
            return 0
        now_hour = iso()[:13]
        built = 0
        for hour_prefix, n_obs in self.store.unindexed_closed_hours(now_hour, limit=max_hours):
            rows = self.store.observations_for_hour(hour_prefix)
            summary = await self._summarize(hour_prefix, rows)
            if summary:
                self.store.add_hour_index(
                    hour_start=f"{hour_prefix}:00:00Z",
                    summary=summary,
                    model=getattr(self.reasoning, "model", None),
                    provider=self.kind,
                    n_obs=n_obs,
                )
                self.store.add_metric("hour_index_built", 1, {"hour": hour_prefix, "n_obs": n_obs})
                built += 1
        return built

    async def _summarize(self, hour_prefix: str, rows: list[dict[str, Any]]) -> str:
        if not rows:
            return ""
        lines = [
            f"{r['ts'][11:19]} [{r.get('importance', 0)}] {r.get('kind') or 'scene'}: "
            f"{r.get('scene') or ''} {r.get('summary') or ''}".strip()
            for r in rows[:MAX_ROWS_PER_HOUR]
        ]
        rows_text = "\n".join(lines)
        if len(rows) > MAX_ROWS_PER_HOUR:
            rows_text += f"\n(+{len(rows) - MAX_ROWS_PER_HOUR} earlier rows elided)"
        messages = [ChatMessage(role="user", content=_PROMPT.format(
            hour=hour_prefix, n=len(rows), rows=rows_text))]
        result = await self.reasoning.chat(messages, tools=None)
        content = (result.content or "").strip() or (result.thinking or "").strip()
        return content[:MAX_SUMMARY_CHARS]
