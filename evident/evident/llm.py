"""LLM access for baselines. Reuses STRIDE's LLMClient (auth, endpoints, retries) -- loaded from the
STRIDE repo by file path so nothing is copied and no credentials live in this project.

Model ids follow STRIDE's routing: "gemini-2.5-flash" -> Endor; "openai:gpt-4o" / "aws:..." / "gemini:..."
-> Floodgate; bare "gpt-*" -> OpenAI. "mock:<name>" -> offline test double (no network).

Auth (same as STRIDE): export ENDOR_TOKEN (Endor) or TOKEN (Floodgate), or let `appleconnect` fetch one.

Every call is cached on disk by sha1(model + prompt) so reruns are free and exactly reproducible.
Token counts: STRIDE's client returns text only, so tokens are counted locally with tiktoken's o200k_base (GPT-4o's
tokenizer; a consistent approximation for other models) when tiktoken and its encoding file are available
(TIKTOKEN_CACHE_DIR defaults to evident/models/tiktoken), otherwise estimated as chars/4. Every result records which
(`tokenizer`). Cached responses are re-counted on read, so old runs gain exact counts too. Latency is the client's own
"successful-call compute" figure (retry/rate-limit sleeps and auth time subtracted), as in STRIDE's eval.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import threading
import time
from pathlib import Path

from . import paths

STRIDE_REPO = Path(os.environ.get("STRIDE_REPO", paths.DATASETS.parent))   # .../stride
CACHE_DIR = paths.DATA / "cache" / "llm"
_local = threading.local()
_module = None


def _client_module():
    global _module
    if _module is None:
        f = STRIDE_REPO / "retrieval" / "llm_client.py"
        if not f.exists():
            raise FileNotFoundError(f"STRIDE llm_client not found at {f}; set STRIDE_REPO")
        spec = importlib.util.spec_from_file_location("stride_llm_client", f)
        _module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_module)
    return _module


_ENC = None


def tokenizer():
    """-> (count function, name). o200k_base if available offline, else chars/4."""
    global _ENC
    if _ENC is None:
        os.environ.setdefault("TIKTOKEN_CACHE_DIR", str(paths.MODELS / "tiktoken"))
        try:
            import tiktoken
            enc = tiktoken.get_encoding("o200k_base")
            _ENC = (lambda t: len(enc.encode(t, disallowed_special=())), "o200k_base")
        except Exception:                                           # noqa: BLE001  not installed or no encoding file
            _ENC = (lambda t: max(1, len(t) // 4), "chars/4")
    return _ENC


def est_tokens(text: str) -> int:
    return tokenizer()[0](text)


class LLM:
    def __init__(self, model: str, max_tokens: int = 1024, use_cache: bool = True):
        self.model, self.max_tokens, self.use_cache = model, max_tokens, use_cache
        self.is_mock = model.startswith("mock:")
        self.dir = CACHE_DIR / model.replace(":", "_").replace("/", "_")
        self.dir.mkdir(parents=True, exist_ok=True)

    def _client(self):
        c = getattr(_local, "client", None)
        if c is None or getattr(_local, "model", None) != self.model:
            mod = _client_module()
            mod.MAX_TOKENS = self.max_tokens        # module-level knob read at call time (Endor path)
            c = mod.LLMClient(self.model)
            _local.client, _local.model = c, self.model
        return c

    def complete(self, prompt: str, meta: dict | None = None) -> dict:
        """-> {text, input_tokens, output_tokens, latency_s, cached, error}"""
        key = hashlib.sha1((self.model + "\n" + prompt).encode()).hexdigest()
        f = self.dir / f"{key}.json"
        if self.use_cache and not self.is_mock and f.exists():
            d = json.load(open(f))
            return {**d, "input_tokens": est_tokens(prompt), "output_tokens": est_tokens(d.get("text") or ""),
                    "tokens_estimated": tokenizer()[1] == "chars/4", "tokenizer": tokenizer()[1], "cached": True}
        t0 = time.time()
        err, text = None, ""
        try:
            if self.is_mock:
                text = _mock(self.model, prompt, meta or {})
                lat = 0.0
            else:
                client = self._client()
                client.reset_sleep_counter()
                for attempt, wait in enumerate((0, 2, 6)):          # transient failures only
                    time.sleep(wait)
                    try:
                        text = client._call(prompt)
                        err = None
                        break
                    except Exception as e:                           # noqa: BLE001
                        err = f"{type(e).__name__}: {str(e)[:200]}"
                lat = max(0.0, time.time() - t0 - client._sleep_total)
        except Exception as e:                                       # noqa: BLE001
            err, lat = f"{type(e).__name__}: {str(e)[:200]}", time.time() - t0
        out = {"text": text or "", "input_tokens": est_tokens(prompt), "output_tokens": est_tokens(text or ""),
               "latency_s": lat, "error": err, "tokens_estimated": tokenizer()[1] == "chars/4", "tokenizer": tokenizer()[1]}
        if self.use_cache and not self.is_mock and not err:
            json.dump(out, open(f, "w"))
        return {**out, "cached": False}


def _mock(model: str, prompt: str, meta: dict) -> str:
    """Offline test doubles. 'mock:gold' returns the gold SQL (needs meta['gold_sql']); 'mock:bad' is invalid
    SQL; 'mock:wrong' is valid but wrong; 'mock:slow' sleeps (timeout testing)."""
    kind = model.split(":", 1)[1]
    if kind == "gold":
        return "```sql\n" + meta["gold_sql"] + "\n```"
    if kind == "gold_values":
        return json.dumps({"answer": meta["gold_values"]}, default=str)
    if kind == "bad":
        return "```sql\nSELECT FROM WHERE\n```"
    if kind == "wrong":
        return "```sql\nSELECT 'wrong'\n```"
    raise ValueError(model)
