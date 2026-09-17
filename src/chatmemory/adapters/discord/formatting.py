"""Discord markdown that quoted content cannot turn against the reader.

Answers are rendered as markdown so a price can be bold and a snippet can sit
in a code block. The same renderer that makes the model's formatting readable
would make a reproduced `[your account](https://evil.example)` a disguised,
clickable link, and the answer text is written by a model that has just read
channel messages and web pages. The model cannot be trusted to keep that out,
so this module enforces it on the way out:

*   Answer text never carries a masked link. Every `[text](url)` in it is
    unmasked so the destination is visible. Masked links exist only in the
    citation lines this adapter builds itself from structured citations, whose
    URLs came from the evidence ledger rather than from prose.
*   Excerpts and labels quoted from messages are escaped, so they cannot add a
    heading, a link, a spoiler or a code fence to the answer.
*   Long answers are split at paragraph or line boundaries and never inside a
    fenced code block, because a split fence swallows everything after it --
    including the source lines -- into code.
*   `@everyone` and `@here` are defanged in the text as well. The client is
    constructed with `AllowedMentions.none()` and every send repeats it; this
    is the second lock, for a send path somebody adds later without it.

Pure functions only: nothing here talks to Discord, so every guarantee is
testable without a client.
"""

from __future__ import annotations

import re

#: Discord rejects a message over 2000 characters outright.
DISCORD_MESSAGE_LIMIT = 2000

FENCE = "```"
MAX_LANGUAGE_CHARS = 20

# The start of a link target: an optional `<` (_ANGLE), then a URL scheme and
# its colon (_SCHEME). Discord's link grammar drops a backslash before punctuation, so `https\://`
# and `\<https://` render exactly like the unescaped forms; any of these
# characters may be written with a backslash in front and still count.
_ANGLE = r"(?:\\?<)?"
_SCHEME = r"\\?[a-zA-Z](?:\\?[a-zA-Z0-9+.\-])*\\?:"

# A masked link in its plain form. The target must carry a URL scheme because
# Discord only renders a masked link for one; requiring it keeps indexing
# expressions in code (`items[i](x)`) from being rewritten.
_MASKED_LINK = re.compile(
    rf"\[(?P<label>[^\[\]\n]*)\]\(\s*{_ANGLE}(?P<url>{_SCHEME}[^\s()<>]*)>?"
    r"(?:\s+\"[^\"\n]*\")?\s*\)"
)

# The residue the plain form misses: nested brackets in the label, a title, a
# target with parentheses. Discord's link grammar needs `](` with nothing in
# between, so a space there is enough to stop it being a link at all, whatever
# the label looked like.
_LINK_JOINT = re.compile(rf"\]\(\s*{_ANGLE}{_SCHEME}")

_MASS_MENTION = re.compile(r"@(everyone|here)\b")
_ZERO_WIDTH_SPACE = "​"

# Everything Discord's markdown gives meaning to. `#`, `-` and `>` only matter
# at a line start, but an excerpt is collapsed onto one line that is then
# placed after other text, so escaping them everywhere is the simple rule that
# cannot be wrong.
_MARKDOWN_SPECIALS = re.compile(r"([\\*_~|`>#\-\[\]()<])")


def unmask_links(text: str) -> str:
    """Show the destination of every masked link in `text`.

    `[label](url)` becomes `label (url)`: the reader keeps the words and sees
    where the link goes, and Discord autolinks the bare address to itself,
    which is the only place it can then lead.
    """
    previous = None
    # Unmasking an inner link can expose an outer one (`[[a](u)](v)`), so run
    # to a fixed point. Each pass removes at least one `](`, so it terminates.
    while previous != text:
        previous = text
        text = _MASKED_LINK.sub(lambda m: f"{m['label']} ({m['url']})", text)
    return _LINK_JOINT.sub(lambda m: "] " + m.group(0)[1:], text)


def escape_markdown(text: str) -> str:
    """Neutralise markdown in text quoted from a message.

    Used for excerpts and labels only, never for the answer body, whose
    formatting is wanted.
    """
    return defang_mentions(_MARKDOWN_SPECIALS.sub(r"\\\1", text))


def defang_mentions(text: str) -> str:
    """Stop `@everyone` and `@here` from reading as mentions in the text itself."""
    return _MASS_MENTION.sub(lambda m: f"@{_ZERO_WIDTH_SPACE}{m.group(1)}", text)


def sanitize_answer(text: str) -> str:
    """The model's answer text, made safe to render as markdown."""
    return close_open_fence(defang_mentions(unmask_links(text)))


def masked_link(label: str, target: str) -> str:
    """A masked link for a citation this adapter generated.

    The only producer of masked links in a reply. The label is escaped here so
    an author name like `x](https://evil.example) [y` cannot rewrite the link.
    """
    return f"[{escape_markdown(label)}]({target})"


def _is_fence(line: str) -> bool:
    """Whether this line opens or closes a code block.

    An odd number of fences, wherever they sit: "```print(x)```" on one line
    is a complete inline block and changes nothing, while "like this ```py"
    opens one mid-line just as surely as a fence at the start does.
    """
    return line.count(FENCE) % 2 == 1


def close_open_fence(text: str) -> str:
    """Close a code block the model left open.

    Otherwise everything appended after the answer -- the sources -- renders
    as code, and a reader loses the provenance of what they just read.
    """
    fences = sum(1 for line in text.split("\n") if _is_fence(line))
    return text if fences % 2 == 0 else f"{text}\n{FENCE}"


def _blocks(text: str) -> list[list[str]]:
    """Paragraphs and fenced code blocks, as lists of lines.

    A blank line ends a paragraph, a fence line starts or ends a code block,
    and a code block is always one block of its own even without blank lines
    around it, so it is never packed half with the prose next to it.
    """
    blocks: list[list[str]] = []
    current: list[str] = []
    in_code = False
    for line in text.split("\n"):
        if in_code:
            current.append(line)
            if _is_fence(line):
                blocks.append(current)
                current, in_code = [], False
        elif _is_fence(line):
            if current:
                blocks.append(current)
            current, in_code = [line], True
        elif not line.strip():
            if current:
                blocks.append(current)
            current = []
        else:
            current.append(line)
    if current:
        blocks.append(current)
    return blocks


def _hard_cut(line: str, limit: int) -> list[str]:
    return [line[i : i + limit] for i in range(0, len(line), limit)] or [""]


def _code_pieces(block: list[str], limit: int) -> list[str]:
    """An oversized code block as several whole code blocks.

    Each piece reopens the fence with its language tag and closes it, so each
    message still holds well-formed code rather than half a block.
    """
    first, last = block[0], block[-1]
    # Continuations reopen with the language tag only; repeating any prose
    # that shared the opening line would say it twice.
    reopen = FENCE + first[first.rfind(FENCE) + len(FENCE) :].strip()[:MAX_LANGUAGE_CHARS]
    closing_at = last.rfind(FENCE)
    body = block[1:-1] + ([last[:closing_at]] if last[:closing_at].strip() else [])
    trailer = last[closing_at + len(FENCE) :]
    # Room for the longest opening line plus the closing fence and trailer.
    room = max(limit - max(len(first), len(reopen)) - len(FENCE) - len(trailer) - 2, 1)
    pieces: list[str] = []
    lines: list[str] = []
    for line in (cut for raw in body for cut in _hard_cut(raw, room)):
        if lines and len("\n".join([*lines, line])) > room:
            pieces.append("\n".join([reopen if pieces else first, *lines, FENCE]))
            lines = []
        lines.append(line)
    pieces.append("\n".join([reopen if pieces else first, *lines, FENCE + trailer]))
    return pieces


def _pieces(block: list[str], limit: int) -> list[tuple[str, str]]:
    """A block as `(joiner, text)` pieces, each within `limit`.

    The joiner is what goes before the piece when it shares a message with the
    one before: a blank line between paragraphs, a newline between the lines
    of a paragraph that had to be broken up (a list, typically).
    """
    whole = "\n".join(block)
    if len(whole) <= limit:
        return [("\n\n", whole)]
    if _is_fence(block[0]):
        return [("\n\n", piece) for piece in _code_pieces(block, limit)]
    lines = [cut for line in block for cut in _hard_cut(line, limit)]
    return [("\n\n" if i == 0 else "\n", line) for i, line in enumerate(lines)]


def split_message(text: str, limit: int = DISCORD_MESSAGE_LIMIT) -> list[str]:
    """Split `text` into messages of at most `limit` characters.

    Breaks at paragraph boundaries first, then at line boundaries (list
    items), and never inside a fenced code block.
    """
    messages: list[str] = []
    current = ""
    for block in _blocks(close_open_fence(text)):
        for joiner, piece in _pieces(block, limit):
            if current and len(current) + len(joiner) + len(piece) <= limit:
                current += joiner + piece
                continue
            if current:
                messages.append(current)
            current = piece
    if current:
        messages.append(current)
    # Only reachable with a pathological opening line longer than a message.
    # Discord rejects an oversized message outright, so a cut fence is still
    # better than no reply.
    return [cut for message in messages for cut in _hard_cut(message, limit)]
