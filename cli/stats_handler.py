import threading
from typing import Any, Dict, List, Optional, Union

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult
from langchain_core.messages import AIMessage


def _extract_model_name(serialized: Optional[Dict[str, Any]],
                       kwargs: Dict[str, Any]) -> str:
    """Best-effort extract the underlying model id from the start callback's
    payload. LangChain stuffs it in different places depending on provider.
    Returns 'unknown' as a stable bucket when we can't tell."""
    if serialized:
        # The canonical path is `serialized['kwargs']['model']` or 'model_name'.
        kw = serialized.get("kwargs") or {}
        for key in ("model", "model_name", "deployment_name"):
            v = kw.get(key)
            if v:
                return str(v)
        # Some providers nest under `id` (Bedrock, vertex).
        v = serialized.get("name") or serialized.get("id")
        if v:
            return str(v)
    invocation = kwargs.get("invocation_params") or {}
    for key in ("model", "model_name", "deployment_name"):
        v = invocation.get(key)
        if v:
            return str(v)
    return "unknown"


class StatsCallbackHandler(BaseCallbackHandler):
    """Callback handler that tracks LLM calls, tool calls, and token usage.

    Phase 1.5 cleanup: also tracks per-model usage so `record_fire_cost`
    can bill calls at the *actual* model's rate (quick vs deep) instead of
    attributing every token to the deep model. `model_usage` maps
    model_id → {tokens_in, tokens_out, llm_calls}.

    Run-id correlation: LangChain passes a `run_id` UUID through every
    callback. We stash the per-run model so `on_llm_end` knows which
    bucket to attribute the usage_metadata to even if multiple models run
    interleaved on the same handler instance.
    """

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self.llm_calls = 0
        self.tool_calls = 0
        self.tokens_in = 0
        self.tokens_out = 0
        # Phase 1.5 additions.
        self.model_usage: Dict[str, Dict[str, int]] = {}
        self._run_id_to_model: Dict[str, str] = {}

    def _bump_call(self, model: str, run_id: Optional[str]) -> None:
        with self._lock:
            self.llm_calls += 1
            bucket = self.model_usage.setdefault(
                model, {"tokens_in": 0, "tokens_out": 0, "llm_calls": 0},
            )
            bucket["llm_calls"] += 1
            if run_id is not None:
                self._run_id_to_model[str(run_id)] = model

    def on_llm_start(
        self,
        serialized: Dict[str, Any],
        prompts: List[str],
        **kwargs: Any,
    ) -> None:
        model = _extract_model_name(serialized, kwargs)
        self._bump_call(model, kwargs.get("run_id"))

    def on_chat_model_start(
        self,
        serialized: Dict[str, Any],
        messages: List[List[Any]],
        **kwargs: Any,
    ) -> None:
        model = _extract_model_name(serialized, kwargs)
        self._bump_call(model, kwargs.get("run_id"))

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        """Extract token usage and attribute it to the model that started
        this run. Falls back to the global totals when run_id correlation
        fails (older providers / direct .invoke without the callback chain)."""
        try:
            generation = response.generations[0][0]
        except (IndexError, TypeError):
            return

        usage_metadata = None
        if hasattr(generation, "message"):
            message = generation.message
            if isinstance(message, AIMessage) and hasattr(message, "usage_metadata"):
                usage_metadata = message.usage_metadata

        if not usage_metadata:
            return
        tin = int(usage_metadata.get("input_tokens", 0) or 0)
        tout = int(usage_metadata.get("output_tokens", 0) or 0)

        run_id = kwargs.get("run_id")
        with self._lock:
            self.tokens_in += tin
            self.tokens_out += tout
            model = self._run_id_to_model.pop(str(run_id), "unknown") if run_id else "unknown"
            bucket = self.model_usage.setdefault(
                model, {"tokens_in": 0, "tokens_out": 0, "llm_calls": 0},
            )
            bucket["tokens_in"] += tin
            bucket["tokens_out"] += tout

    def on_tool_start(
        self,
        serialized: Dict[str, Any],
        input_str: str,
        **kwargs: Any,
    ) -> None:
        """Increment tool call counter when a tool starts."""
        with self._lock:
            self.tool_calls += 1

    def get_stats(self) -> Dict[str, Any]:
        """Return current statistics. `model_usage` is the per-model
        breakdown the cost middleware uses for accurate attribution."""
        with self._lock:
            return {
                "llm_calls": self.llm_calls,
                "tool_calls": self.tool_calls,
                "tokens_in": self.tokens_in,
                "tokens_out": self.tokens_out,
                "model_usage": {k: dict(v) for k, v in self.model_usage.items()},
            }
