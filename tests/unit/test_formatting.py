"""Rich formatting: markdown that quoted content cannot turn against the reader.

Answers are markdown now, and the text of an answer is written by a model that
has just read channel messages and web pages. These tests hold the renderer to
what the model cannot be trusted with: a masked link from anywhere but a
citation shows its destination, quoted excerpts carry no formatting, a long
answer never splits a code block, and nothing in an answer notifies anyone.
"""

from __future__ import annotations

import re

from chatmemory.adapters.discord.bot import _citation_line, _messages, _render
from chatmemory.adapters.discord.formatting import (
    DISCORD_MESSAGE_LIMIT,
    FENCE,
    escape_markdown,
    sanitize_answer,
    split_message,
    unmask_links,
)
from chatmemory.app.disclosure import ScopedAnswer
from chatmemory.domain.identity import ChannelRef
from chatmemory.ports.answers import Answer, Citation

GENERAL = ChannelRef("discord", 100)
CITED = "https://discord.com/channels/1/100/7"

# The joint of Discord's masked-link grammar: `](` straight into a URL. A
# label of any shape, nested brackets included, needs this to hide a target,
# so a reply without it has no masked link in it.
MASKED = re.compile(r"\]\(\s*<?https?:", re.IGNORECASE)


def scoped(text: str, *citations: Citation) -> ScopedAnswer:
    return ScopedAnswer(answer=Answer(text, citations), withheld_from_audience=frozenset())


def a_citation(excerpt: str = "the freeze holds until Tuesday", author: str = "sam") -> Citation:
    return Citation(GENERAL, 7, author, excerpt, CITED)


# --- 3.6: only citation links may be masked ----------------------------


def test_a_masked_link_reproduced_from_a_message_shows_its_destination() -> None:
    rendered = _render(
        scoped("Per the pinned note, verify [your account](https://evil.example/login) today.")
    )

    assert "https://evil.example/login" in rendered
    assert not MASKED.search(rendered)


def test_the_citation_link_the_adapter_built_stays_masked() -> None:
    rendered = _render(scoped("Held until **Tuesday**.", a_citation()))

    assert f"[sam]({CITED})" in rendered
    assert len(MASKED.findall(rendered)) == 1


def test_a_link_pointing_at_a_cited_url_in_prose_is_still_unmasked() -> None:
    """The allowlist is structural, not a URL comparison: prose cannot earn a
    masked link by aiming it at something that was also cited."""
    rendered = _render(scoped(f"See [the official notice]({CITED}).", a_citation()))

    assert rendered.count(f"[sam]({CITED})") == 1
    assert len(MASKED.findall(rendered)) == 1


def test_nested_titled_and_angle_bracketed_links_are_all_unmasked() -> None:
    hostile = [
        "[[inner](https://a.example)](https://evil.example)",
        "[a [nested] label](https://evil.example)",
        '[login](https://evil.example "Your bank")',
        "[login](<https://evil.example/a path>)",
        "[login](https://evil.example/wiki/Dune_(novel))",
        "[login](  HTTPS://evil.example)",
    ]
    for text in hostile:
        out = unmask_links(text)
        assert not MASKED.search(out), (text, out)
        assert "evil.example" in out


def _discord_unescape(text: str) -> str:
    """What Discord's link grammar does to a target: a backslash before
    punctuation is dropped, so `https\\://` reaches the renderer as `https://`."""
    return re.sub(r"\\([^0-9A-Za-z\s])", r"\1", text)


def test_links_with_a_backslash_escaped_scheme_are_unmasked() -> None:
    """The escapes are invisible once Discord renders the link, so the check
    runs on the text as Discord will read it, not as it was written."""
    hostile = [
        r"[Verify your account](https\://evil.example/login)",
        r"[x](<https\://evil.example>)",
        r"[x](\<https\://evil.example>)",
        r"[x](https\:\/\/evil.example)",
        r"[x](htt\ps://evil.example)",
        r"[x](svn\+ssh\://evil.example)",
        r"[a [nested] label](https\://evil.example)",
        r'[x](https\://evil.example "title")',
    ]
    for text in hostile:
        out = sanitize_answer(text)
        assert not MASKED.search(_discord_unescape(out)), (text, out)
        assert "evil.example" in out


def test_code_indexing_is_not_mistaken_for_a_link() -> None:
    assert unmask_links("handlers[name](event)") == "handlers[name](event)"


def test_a_citation_author_name_cannot_rewrite_the_link() -> None:
    line = _citation_line(1, a_citation(author="x](https://evil.example) [y"))

    assert "](https://evil.example)" not in line
    assert line.endswith("the freeze holds until Tuesday")
    assert f"]({CITED})" in line


# --- 3.3: quoted excerpts are escaped ----------------------------------


def test_an_excerpt_that_begins_with_a_heading_is_plain_text() -> None:
    line = _citation_line(1, a_citation(excerpt="# URGENT: everyone reset passwords"))

    assert "\\# URGENT" in line
    assert line.startswith("-# 1. ")


def test_an_excerpt_cannot_add_links_spoilers_or_code() -> None:
    excerpt = "||spoiler|| ```py [click](https://evil.example) **bold**"
    line = _citation_line(1, a_citation(excerpt=excerpt))

    assert "](https://evil.example" not in line
    assert "||" not in line
    assert FENCE not in line
    assert "**bold**" not in line


def test_escaping_is_reversible_by_reading() -> None:
    """Every escape is a backslash before the character, so the reader still
    sees the original words."""
    assert escape_markdown("a_b *c*") == "a\\_b \\*c\\*"


# --- 3.4 / 3.7: long answers split cleanly -----------------------------


def fence_balanced(message: str) -> bool:
    return sum(line.count(FENCE) for line in message.split("\n")) % 2 == 0


def test_a_long_answer_with_a_code_block_splits_with_whole_code_blocks() -> None:
    prose = "\n\n".join(f"Paragraph {i}. " + "words " * 60 for i in range(6))
    code = "```python\n" + "\n".join(f"value_{i} = compute({i})" for i in range(40)) + "\n```"
    text = f"{prose}\n\n{code}\n\n{prose}"

    messages = _messages(scoped(text, a_citation()))

    assert len(messages) > 1
    assert all(len(m) <= DISCORD_MESSAGE_LIMIT for m in messages)
    assert all(fence_balanced(m) for m in messages)
    # The code block travelled whole, in one message.
    assert sum(code in m for m in messages) == 1
    # Nothing was lost, and the sources came last.
    assert "value_39 = compute(39)" in "".join(messages)
    assert messages[-1].rstrip().endswith("the freeze holds until Tuesday")


def test_a_code_block_larger_than_a_message_becomes_several_whole_blocks() -> None:
    code = "```sql\n" + "\n".join(f"SELECT {i} FROM t;  -- row {i}" for i in range(200)) + "\n```"

    messages = split_message(code)

    assert len(messages) > 1
    assert all(len(m) <= DISCORD_MESSAGE_LIMIT for m in messages)
    assert all(fence_balanced(m) for m in messages)
    assert all(m.startswith("```sql\n") and m.endswith("\n```") for m in messages)
    joined = "\n".join(messages)
    assert all(f"SELECT {i} FROM t;" in joined for i in range(200))


def test_a_long_list_splits_between_items() -> None:
    items = "\n".join(f"- item {i}: " + "detail " * 10 for i in range(80))

    messages = split_message(items)

    assert len(messages) > 1
    assert all(line.startswith("- item ") for m in messages for line in m.split("\n"))


def test_an_unclosed_fence_is_closed_so_sources_are_not_swallowed() -> None:
    rendered = _render(scoped("Run this:\n```bash\nmake deploy", a_citation()))

    body, _, sources = rendered.partition("-# **From this server:**")
    assert fence_balanced(body)
    assert FENCE not in sources


def test_a_one_line_code_span_is_not_a_fence() -> None:
    assert split_message("use ```x = 1``` here\n\nnext") == ["use ```x = 1``` here\n\nnext"]


def test_a_short_answer_is_one_message() -> None:
    assert _messages(scoped("**$64,210** per BTC.", a_citation())) == [
        _render(scoped("**$64,210** per BTC.", a_citation()))
    ]


# --- 3.8 (text): mass mentions are defanged ----------------------------


def test_everyone_in_answer_text_is_not_a_mention_even_as_text() -> None:
    out = sanitize_answer("Ping @everyone and @here now")

    assert "@everyone" not in out
    assert "@here" not in out
    assert "everyone" in out


def test_everyone_in_an_excerpt_is_not_a_mention_even_as_text() -> None:
    line = _citation_line(1, a_citation(excerpt="@everyone standup moved"))

    assert "@everyone" not in line
