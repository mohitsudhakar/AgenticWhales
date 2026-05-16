# AgenticWhales/graph/reflection.py

from typing import Any, List


class Reflector:
    """Handles reflection on trading decisions."""

    def __init__(self, quick_thinking_llm: Any):
        """Initialize the reflector with an LLM."""
        self.quick_thinking_llm = quick_thinking_llm
        self.log_reflection_prompt = self._get_log_reflection_prompt()
        self.extended_reflection_prompt = self._get_extended_reflection_prompt()

    def _get_log_reflection_prompt(self) -> str:
        """Concise prompt for reflect_on_final_decision (Phase B log entries).

        Produces 2-4 sentences of plain prose — compact enough to be re-injected
        into future agent prompts without bloating the context window.
        """
        return (
            "You are a trading analyst reviewing your own past decision now that the outcome is known.\n"
            "Write exactly 2-4 sentences of plain prose (no bullets, no headers, no markdown).\n\n"
            "Cover in order:\n"
            "1. Was the directional call correct? (cite the alpha figure)\n"
            "2. Which part of the investment thesis held or failed?\n"
            "3. One concrete lesson to apply to the next similar analysis.\n\n"
            "Be specific and terse. Your output will be stored verbatim in a decision log "
            "and re-read by future analysts, so every word must earn its place."
        )

    def _get_extended_reflection_prompt(self) -> str:
        """Prompt for the M-day retrospective (Yu et al. 2023 §3.2.1).

        Synthesizes recent immediate reflections into a higher-order
        lesson that goes into the deep memory layer. The output is
        re-read by future analyses, so it must be terse and
        pattern-oriented rather than episodic.
        """
        return (
            "You are a trading analyst writing a multi-day retrospective. "
            "You will be shown a window of recent (resolved) decisions and "
            "the post-outcome reflections written for each. Your job is "
            "to find the PATTERN across them — the recurring mistakes, "
            "the recurring wins, and the conditions that distinguish them.\n\n"
            "Write 3-5 sentences of plain prose (no bullets, no headers).\n\n"
            "Cover, in order:\n"
            "1. The dominant pattern in correct calls vs. wrong calls "
            "(what conditions or evidence types separate them).\n"
            "2. One mistake you keep making, named concretely.\n"
            "3. One operating heuristic to apply going forward.\n\n"
            "This goes into the deep memory layer and will be retrieved "
            "across many future analyses — every word must earn its place."
        )

    def extended_reflection(self, recent_entries: List[dict]) -> str:
        """Produce an M-day retrospective from a window of resolved entries.

        Used by the periodic extended-reflection pass. ``recent_entries``
        is the list of resolved memory entries (as returned by
        ``TradingMemoryLog.load_entries``) inside the configured window.
        """
        if not recent_entries:
            return ""
        lines = []
        for e in recent_entries:
            tag = (
                f"[{e.get('date', '?')} | {e.get('ticker', '?')} | "
                f"{e.get('rating', '?')} | "
                f"raw {e.get('raw') or 'n/a'} | alpha {e.get('alpha') or 'n/a'}]"
            )
            reflection = e.get("reflection") or "(no reflection)"
            lines.append(f"{tag}\nReflection: {reflection}")
        window = "\n\n".join(lines)
        messages = [
            ("system", self.extended_reflection_prompt),
            (
                "human",
                f"Window of {len(recent_entries)} recent resolved decisions:\n\n{window}",
            ),
        ]
        return self.quick_thinking_llm.invoke(messages).content

    def reflect_on_final_decision(
        self,
        final_decision: str,
        raw_return: float,
        alpha_return: float,
    ) -> str:
        """Single reflection call on the final trade decision with outcome context.

        Used by Phase B deferred reflection. The final_trade_decision already
        synthesises all analyst insights, so no separate market context is needed.
        """
        messages = [
            ("system", self.log_reflection_prompt),
            (
                "human",
                (
                    f"Raw return: {raw_return:+.1%}\n"
                    f"Alpha vs SPY: {alpha_return:+.1%}\n\n"
                    f"Final Decision:\n{final_decision}"
                ),
            ),
        ]
        return self.quick_thinking_llm.invoke(messages).content
