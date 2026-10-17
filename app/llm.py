"""Gemini (Vertex AI) wrapper, plus a deterministic fake for tests/offline work.

Callers pass a `fake` callable to generate_json: the fake LLM returns its result instead of
calling a model, which keeps the stub logic next to the pipeline that needs it.
"""
import hashlib
import math
import re
import time
from typing import Callable, Literal, Protocol, TypeVar

from pydantic import BaseModel

from app.config import get_settings
from app.tracing import llm_call

T = TypeVar("T", bound=BaseModel)
Turn = tuple[Literal["user", "model"], str]
TaskType = Literal["RETRIEVAL_QUERY", "RETRIEVAL_DOCUMENT", "SEMANTIC_SIMILARITY"]


class LLM(Protocol):
    mode: str

    def generate_json(self, system: str, contents: str | list[Turn], schema: type[T],
                      fake: Callable[[], T], think: bool = False) -> T: ...

    def embed(self, texts: list[str], task_type: TaskType) -> list[list[float]]: ...


def normalize(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    return sum(x * y for x, y in zip(a, b))  # vectors are stored normalized


OPERATIONS = {"AnswerOut": "answer", "AnswerWithDocsOut": "answer", "SummaryOut": "summarize", "PlaybookOut": "compile_playbook"}


def _operation(schema) -> str:
    return OPERATIONS.get(schema.__name__, f"generate:{schema.__name__}")


def _prompt_text(system, contents) -> str:
    body = contents if isinstance(contents, str) else "\n".join(f"{r}: {t}" for r, t in contents)
    return f"[system] {system[:200]}\n{body}"


def _note_output(info: dict, out) -> None:
    info["response"] = out.model_dump_json()
    if hasattr(out, "used_playbook_ids"):
        info["playbook_ids"] = list(out.used_playbook_ids)


def _retry(fn, attempts: int = 4):
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:  # 429 / transient 5xx from Vertex
            msg = str(e)
            if i == attempts - 1 or not any(c in msg for c in ("429", "RESOURCE_EXHAUSTED", "503", "500", "UNAVAILABLE")):
                raise
            time.sleep(2 ** i)


class VertexLLM:
    mode = "vertex"

    def __init__(self):
        from google import genai

        s = get_settings()
        self._s = s
        self._client = genai.Client(vertexai=True, project=s.project, location=s.location)

    def generate_json(self, system, contents, schema, fake, think=False):
        from google.genai import types

        raw = contents
        if isinstance(contents, list):
            contents = [types.Content(role=r, parts=[types.Part(text=t)]) for r, t in contents]
        config = types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_schema=schema,
            temperature=0.0,
            thinking_config=types.ThinkingConfig(thinking_budget=1024 if think else 0),
        )
        with llm_call(_operation(schema), self._s.chat_model, _prompt_text(system, raw)) as info:
            resp = _retry(lambda: self._client.models.generate_content(
                model=self._s.chat_model, contents=contents, config=config))
            usage = getattr(resp, "usage_metadata", None)
            if usage is not None:
                info["input_tokens"] = usage.prompt_token_count
                info["output_tokens"] = (usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0)
            out = resp.parsed if isinstance(resp.parsed, schema) else schema.model_validate_json(resp.text)
            _note_output(info, out)
            return out

    def embed(self, texts, task_type):
        from google.genai import types

        config = types.EmbedContentConfig(task_type=task_type, output_dimensionality=self._s.embed_dim)
        out = []
        for t in texts:  # gemini-embedding-001 on Vertex accepts one input per request
            with llm_call(f"embed:{task_type.lower()}", self._s.embed_model, t) as info:
                resp = _retry(lambda: self._client.models.embed_content(
                    model=self._s.embed_model, contents=t, config=config))
                out.append(normalize(list(resp.embeddings[0].values)))
                info["response"] = f"{len(out[-1])}-dim vector"
        return out


_STOP = set("""a an the and or but if to of in on at for with from by is are was were be been it its this that
i im i'm me my we our you your he she they them do does did not no so can could would should will just get got
what how why when where which who any some there here have has had about into up out as than then too very
please thanks thank hi hey help need trying try tried again still also""".split())


class FakeLLM:
    """Deterministic: hashed bag-of-words embeddings; generate_json returns the caller's fake."""
    mode = "fake"
    dim = 512

    def generate_json(self, system, contents, schema, fake, think=False):
        with llm_call(_operation(schema), "fake", _prompt_text(system, contents)) as info:
            out = fake()
            _note_output(info, out)
            return out

    def embed(self, texts, task_type):
        out = []
        for t in texts:
            with llm_call(f"embed:{task_type.lower()}", "fake", t) as info:
                out.append(self._embed_one(t))
                info["response"] = f"{len(out[-1])}-dim vector"
        return out

    def _embed_one(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        for tok in re.findall(r"[a-z0-9_]+", text.lower()):
            if tok in _STOP or len(tok) < 3:
                continue
            tok = tok.rstrip("s") if len(tok) > 4 else tok
            h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
            v[h % self.dim] += 1.0
        return normalize(v)


_llm: LLM | None = None


def get_llm() -> LLM:
    global _llm
    if _llm is None:
        _llm = VertexLLM() if get_settings().llm == "vertex" else FakeLLM()
    return _llm
