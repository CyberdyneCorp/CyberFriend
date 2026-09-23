"""Which language a question was asked in.

Two languages, because two are spoken on this server. Not a general language
identifier: adding a dependency that recognises ninety would buy nothing here
and would have to be reasoned about every time it changed its mind on a short
sentence.

Function words rather than content words. "Deploy", "commit" and "token" are
written the same in both languages and are most of what people type here, so a
detector weighing them would call every technical question English. What
actually differs is the scaffolding -- "o que voce pode" against "what you can"
-- and that is what is counted.

`UNKNOWN` is a real answer and the honest one for "ok", "?" or a bare address.
A caller that gets it should leave the language alone rather than pick one,
because a wrong guess is worse than no instruction: the model answering in the
question's language is the behaviour we already get for free most of the time.
"""

from __future__ import annotations

import re
from enum import StrEnum

_WORD = re.compile(r"[^\W_]+", re.UNICODE)

#: Characters that appear in Portuguese and not in English. Worth more than a
#: single function word: nobody writes "não" by accident.
_PORTUGUESE_LETTERS = frozenset("ãõçáéíóúâêôàü")

_PORTUGUESE = frozenset({
    "que", "qual", "quais", "como", "quando", "onde", "porque", "porquê",
    "voce", "você", "vocês", "voces", "nao", "não", "sim", "para", "pra",
    "com", "sem", "uma", "uns", "umas", "sao", "são", "esta", "está",
    "estao", "estão", "fazer", "faz", "fez", "pode", "podem", "posso",
    "consegue", "tem", "tinha", "foi", "seja", "seu", "sua", "seus", "suas",
    "meu", "minha", "isso", "isto", "aquilo", "mais", "menos", "muito",
    "tambem", "também", "agora", "hoje", "ontem", "amanha", "amanhã",
    "aqui", "ali", "sobre", "pelo", "pela", "dos", "das", "nos", "nas",
    "ele", "ela", "eles", "elas", "nosso", "nossa", "obrigado", "obrigada",
    "ola", "olá", "bom", "boa", "dia", "tarde", "noite", "favor",
    "funcionalidades", "comandos", "ferramentas", "carteira", "saldo",
})

_ENGLISH = frozenset({
    "the", "what", "which", "how", "when", "where", "why", "who", "whose",
    "you", "your", "yours", "and", "but", "for", "with", "without", "this",
    "that", "these", "those", "are", "is", "was", "were", "been", "being",
    "can", "could", "would", "should", "will", "shall", "may", "might",
    "have", "has", "had", "does", "did", "doing", "about", "from", "into",
    "there", "here", "then", "than", "them", "they", "their", "our", "ours",
    "please", "thanks", "thank", "hello", "give", "tell", "show", "make",
    "commands", "features", "tools", "wallet", "balance",
})


class Language(StrEnum):
    """A language an answer may be written in, or the absence of one."""

    ENGLISH = "English"
    PORTUGUESE = "Portuguese"
    UNKNOWN = "unknown"

    @property
    def known(self) -> bool:
        return self is not Language.UNKNOWN


def detect(text: str) -> Language:
    """The language `text` was written in, or UNKNOWN.

    Ties are UNKNOWN rather than a default. A question with one marker each
    way is genuinely ambiguous, and the caller's behaviour for UNKNOWN -- say
    nothing about language -- is the safe one.
    """
    lowered = text.casefold()
    words = {m.group() for m in _WORD.finditer(lowered)}
    portuguese = len(words & _PORTUGUESE)
    english = len(words & _ENGLISH)
    # An accented letter is strong evidence on its own: English borrows very
    # few, and none of them are typed in a chat question by accident. Not in a
    # capitalised word, though: "João's email is ..." is English about a
    # person with a Portuguese name, and was once answered in Portuguese.
    if any(
        _PORTUGUESE_LETTERS & set(word.casefold()) and not word[:1].isupper()
        for word in text.split()
    ):
        portuguese += 2
    if portuguese > english:
        return Language.PORTUGUESE
    if english > portuguese:
        return Language.ENGLISH
    return Language.UNKNOWN
