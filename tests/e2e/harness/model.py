"""The model and the embedding endpoint, scripted.

Behind the same ports the production adapters implement, so nothing between
the edges knows it is talking to a fake. The script is deliberately dumb: it
recognises each stage by the schema it asks for, and grounds an answer in
whatever evidence the prompt actually carried. That last part is the useful
property -- a window that should never have reached the model shows up in
the reply, so a leak cannot hide behind a fake that ignores its input.

What this cannot tell you is whether a real model follows the language rule or
picks the right tool. The live tests remain the check on model behaviour.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping, Sequence

from chatmemory.app.reasoning.ports import (
    JsonCompletion,
    TextCompletion,
    ToolCall,
    ToolCompletion,
    ToolDefinition,
)

ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")
EVIDENCE = re.compile(
    r"<<<EVIDENCE window_id=(?P<id>\d+) [^>]*>>>\n(?P<text>.*?)\n<<<END EVIDENCE", re.S
)
QUESTION = re.compile(r"^Question: (?P<q>.*)$", re.M)
_WORD = re.compile(r"[^\W_]{4,}", re.UNICODE)
_STOPWORDS = frozenset(
    {
        *("what", "when", "where", "which", "will", "with", "from", "that", "this"),
        *("have", "about", "there", "their", "they", "your", "does", "show", "details"),
    }
)
"""Words too common to make a window relevant, so relevance means a topic."""

SUMMARY = "summary"
"""What the conversation summariser is told. Its content is never asserted."""

ASK_EXTRACTION = "ask_extraction"
"""The schema name the ingest-side ask extractor asks for."""

ANALYSED = "Message to analyse:\n"


class UnscriptedCall(AssertionError):
    """The graph asked the model for something this script does not know."""


def _words(text: str) -> set[str]:
    return {w.casefold() for w in _WORD.findall(text)} - _STOPWORDS


def _question(prompt: str) -> str:
    """The question line: the last one before the evidence.

    Memory and asker blocks are prepended to the prompt and quote earlier
    questions, so the first match would be the wrong one.
    """
    head = prompt.split("<<<EVIDENCE", 1)[0]
    found = QUESTION.findall(head)
    return found[-1] if found else prompt


def _relevant(prompt: str) -> list[tuple[int, str]]:
    """Windows shown in `prompt` that share a content word with its question."""
    asked = _words(_question(prompt))
    return [
        (int(m.group("id")), m.group("text"))
        for m in EVIDENCE.finditer(prompt)
        if asked & _words(m.group("text"))
    ]


class ScriptedChat:
    """A `ToolCapableChat` whose every answer is derived from its prompt.

    `calls` records `(schema or "text" or "tools", user prompt)` for each
    request, in order, so a turn can say which stages ran.

    Ask extraction is the one stage a scenario scripts by hand, through
    `script_asks`: whether a message creates an obligation is a judgement,
    not something to derive from its words.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self._asks: dict[str, list[dict[str, object]]] = {}

    def script_asks(self, message: str, *asks: Mapping[str, object]) -> None:
        """What extraction reports for the message whose text holds `message`.

        Each ask is the model's JSON entry (`kind`, `text`, `addressee`,
        `addressee_is_group`, `confidence`), so the real parser reads it. An
        unscripted message extracts nothing.
        """
        self._asks[message] = [dict(a) for a in asks]

    async def complete_json(
        self, system: str, user: str, schema: Mapping[str, object], schema_name: str
    ) -> JsonCompletion:
        self.calls.append((schema_name, user))
        if schema_name == "evidence_verdict":
            verdict = "sufficient" if _relevant(user) else "irrelevant"
            return JsonCompletion({"verdict": verdict, "score": 0.9, "suggested_query": None})
        if schema_name == "question_plan":
            return JsonCompletion({"sub_questions": [_question(user)]})
        if schema_name == "grounded_answer":
            return JsonCompletion(self._grounded(user))
        if schema_name == ASK_EXTRACTION:
            return JsonCompletion({"asks": self._extracted(user)})
        raise UnscriptedCall(f"no script for schema {schema_name!r}")

    @staticmethod
    def _grounded(prompt: str) -> dict[str, object]:
        """Quote the most relevant window shown, and cite every relevant one."""
        relevant = _relevant(prompt)
        if not relevant:
            return {"text": "", "cited_window_ids": []}
        _, quoted = relevant[0]
        return {
            "text": f"From what was said: {quoted}",
            "cited_window_ids": [window for window, _ in relevant],
        }

    def _extracted(self, prompt: str) -> list[dict[str, object]]:
        """The asks scripted for the message under analysis, and only it.

        Matched against that one line, not the whole prompt: a scripted
        message quoted as context or as a reply parent must not be extracted
        a second time from the message after it.
        """
        analysed = prompt.split(ANALYSED, 1)[-1].split("\n", 1)[0]
        for message, asks in self._asks.items():
            if message in analysed:
                return asks
        return []

    async def complete_text(self, system: str, user: str) -> TextCompletion:
        self.calls.append(("text", user))
        return TextCompletion(text=SUMMARY)

    async def complete_with_tools(
        self, system: str, user: str, tools: Sequence[ToolDefinition]
    ) -> ToolCompletion:
        """Call the one offered tool whose arguments the prompt can fill.

        The routes narrow the offer, so the script never chooses between
        tools; it only copies an address the way the proposal prompt asks a
        model to. Nothing to copy, or nothing offered, is a plain answer.
        """
        self.calls.append(("tools", user))
        found = list(dict.fromkeys(ADDRESS.findall(user)))
        for tool in tools:
            properties = tool.input_schema.get("properties")
            if not found or not isinstance(properties, Mapping):
                continue
            # The portfolio takes every address the question holds; the other
            # chain tools take one.
            if "addresses" in properties:
                arguments = {"addresses": " ".join(found)}
            elif "address" in properties:
                arguments = {"address": found[0]}
            else:
                continue
            return ToolCompletion(call=ToolCall(tool.name, arguments, call_id="scripted"))
        return ToolCompletion(text="")

    def tool_caller(self) -> ScriptedChat:
        return self


class HashEmbeddings:
    """Deterministic hashed bag-of-words vectors, as wide as the schema's column.

    Every vector carries a constant component, so none is all zeros: the
    cosine distance pgvector ranks by is undefined for a zero vector.
    """

    def __init__(self, dimensions: int) -> None:
        self._dimensions = dimensions

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def vector(self, text: str) -> list[float]:
        values = [0.0] * self._dimensions
        values[0] = 1.0
        for word in _words(text):
            digest = hashlib.sha256(word.encode()).digest()
            values[1 + int.from_bytes(digest[:4], "big") % (self._dimensions - 1)] += 1.0
        norm = math.sqrt(sum(v * v for v in values))
        return [v / norm for v in values]

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self.vector(t) for t in texts]
