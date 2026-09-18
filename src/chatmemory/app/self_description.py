"""Answering "what can you do?" from configuration, never from the corpus.

The corpus is other people's conversations. Searching it for the assistant's
own capabilities finds whatever somebody once wrote about some other tool, and
the answer then presents that as fact about itself: asked what it could do,
the bot replied that it calculates cryptocurrency prices, because a colleague
had described a project that did.

So a self-description is assembled from what this deployment is actually
running. It names the external tools only when they are registered, which
keeps it honest in the other direction too -- a capability list that promises
web search on a deployment without it is the same mistake inverted.

Written in the language the question was asked in. A person who writes a whole
sentence of Portuguese and is answered in English has been told, accurately,
that the assistant was not really listening.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from chatmemory.app.egress import (
    CHAIN_BALANCES_PROVIDER,
    MARKET_CRYPTO_PROVIDER,
    MARKET_FX_PROVIDER,
    MARKET_INDEX_PROVIDER,
)
from chatmemory.app.language import Language, detect
from chatmemory.app.routing import self_description_question
from chatmemory.ports.answers import Answer, AnswerService, Question

MARKET_DESCRIPTIONS = {
    MARKET_CRYPTO_PROVIDER: {
        Language.ENGLISH: "Bitcoin and Ether prices",
        Language.PORTUGUESE: "preços do Bitcoin e do Ether",
    },
    MARKET_INDEX_PROVIDER: {
        Language.ENGLISH: "the S&P 500",
        Language.PORTUGUESE: "o S&P 500",
    },
    MARKET_FX_PROVIDER: {
        Language.ENGLISH: "currency conversion (daily reference rates)",
        Language.PORTUGUESE: "conversão de moedas (taxas de referência diárias)",
    },
}
"""What each market server lets a person ask, in the order they are listed.

Keyed by server, so a deployment without a SerpApi key -- and therefore
without the S&P 500 -- does not promise it."""

WEB_SERVERS = {"wikipedia": "Wikipedia", "serpapi": "Google"}
"""Sources that are "the web", and what to call each one.

Grouped rather than listed separately because "the web (Wikipedia, Google)"
is how a person thinks of them, and because a deployment with neither must not
claim the web at all."""

LOOKUP_DESCRIPTIONS = {
    CHAIN_BALANCES_PROVIDER: {
        Language.ENGLISH: "wallet balances on Ethereum and Base",
        Language.PORTUGUESE: "saldos de carteiras na Ethereum e na Base",
    },
    "context7": {
        Language.ENGLISH: "library documentation (Context7)",
        Language.PORTUGUESE: "documentação de bibliotecas (Context7)",
    },
}
"""What an external source is *for*, rather than what it is called.

A reply naming `chain_balances` has told somebody the name of a server, which
is not a thing they can ask for. Anything unlisted falls back to its server
name, because an honest identifier beats omitting a capability entirely."""


@dataclass(frozen=True, slots=True)
class Command:
    """One slash command, and what it does, in each language.

    Held here rather than read from the Discord surface because this module
    must not depend on an adapter -- but the two are asserted equal by a test,
    so a command added there and forgotten here fails the build rather than
    quietly going unmentioned.
    """

    name: str
    english: str
    portuguese: str

    def described(self, language: Language) -> str:
        text = self.portuguese if language is Language.PORTUGUESE else self.english
        return f"`/{self.name}` — {text}"


ASK = Command("ask", "Ask about what's been said", "Perguntar sobre o que foi dito")
INDEX = Command(
    "index",
    "Archive a channel so people who can read it can search it",
    "Arquivar um canal para que quem pode lê-lo possa pesquisá-lo",
)
UNINDEX = Command(
    "unindex",
    "Stop archiving a channel and delete what was archived",
    "Parar de arquivar um canal e apagar o que foi arquivado",
)
NOTIFICATIONS = Command(
    "notifications",
    "Turn direct messages about things asked of you on or off",
    "Ligar ou desligar as mensagens diretas sobre o que pedem a você",
)
FORGET = Command(
    "forget",
    "Erase what I remember of our conversation",
    "Apagar o que eu lembro da nossa conversa",
)
RESOLVE = Command(
    "resolve",
    "Close or dismiss something I said was asked of you",
    "Fechar ou descartar algo que eu disse que pediram a você",
)

CHANNELS = Command(
    "channels",
    "List the channels I archive that you can read",
    "Listar os canais que eu arquivo e que você pode ler",
)

ALWAYS_AVAILABLE = (ASK, CHANNELS, INDEX, UNINDEX, FORGET, RESOLVE)
"""Commands the bot registers unconditionally.

`notifications` is not here: it is only registered when the feature is on, and
listing a command that does not exist is worse than omitting one that does."""


_TEXT: dict[Language, dict[str, Any]] = {
    Language.ENGLISH: {
        "intro": (
            "I'm CyberFriend. I answer questions about what has been said in "
            "the channels you're allowed to read, and I cite the messages I "
            "used."
        ),
        "ask_heading": "Things you can ask me:",
        "asks": (
            "what did people ask me to do today?",
            "what was decided about the deploy last week?",
            "summarise the discussion in #general yesterday",
        ),
        "lookup_heading": (
            "When the conversations don't have the answer, I can also look it up:"
        ),
        "web": "the web",
        "lookup_note": (
            "Answers from outside this server are labelled as such, so you can "
            "always tell what your colleagues said from what I looked up."
        ),
        "market_heading": (
            "Current market data, from live sources and never from a price "
            "somebody mentioned in a channel: "
        ),
        "market_note": (
            "Each figure says how current it is. I report figures; I don't "
            "recommend buying, selling or holding anything."
        ),
        "commands_heading": "Commands:",
        "facts": (
            "Tell me what to call you (`call me Leo`), your email address, or "
            "the language you'd like answers in, and I'll remember it. Ask "
            "`what do you know about me?` to see it; I only show your email to "
            "you, in a direct message."
        ),
        "audience": (
            "In a channel, I only cite what everyone there can read. Ask me in "
            "a direct message for your full view."
        ),
    },
    Language.PORTUGUESE: {
        "intro": (
            "Eu sou o CyberFriend. Respondo perguntas sobre o que foi dito nos "
            "canais que você pode ler, e cito as mensagens que usei."
        ),
        "ask_heading": "Coisas que você pode me perguntar:",
        "asks": (
            "o que me pediram para fazer hoje?",
            "o que foi decidido sobre o deploy na semana passada?",
            "resuma a conversa no #general ontem",
        ),
        "lookup_heading": (
            "Quando as conversas não têm a resposta, eu também posso buscar:"
        ),
        "web": "a web",
        "lookup_note": (
            "Respostas de fora deste servidor são marcadas como tal, então você "
            "sempre sabe o que foi dito por colegas e o que eu fui buscar."
        ),
        "market_heading": (
            "Dados de mercado atuais, de fontes ao vivo e nunca de um preço que "
            "alguém mencionou num canal: "
        ),
        "market_note": (
            "Cada número diz o quão atual ele é. Eu informo números; não "
            "recomendo comprar, vender nem manter nada."
        ),
        "commands_heading": "Comandos:",
        "facts": (
            "Me diga como quer ser chamado (`me chame de Leo`), seu e-mail, ou "
            "o idioma em que prefere as respostas, e eu vou lembrar. Pergunte "
            "`o que você sabe sobre mim?` para ver; eu só mostro seu e-mail "
            "para você, numa mensagem direta."
        ),
        "audience": (
            "Num canal, eu só cito o que todos ali podem ler. Me pergunte numa "
            "mensagem direta para ver tudo o que você pode."
        ),
    },
}


def describe_capabilities(
    external_tools: Sequence[str],
    *,
    personal_facts: bool = False,
    commands: Sequence[Command] = ALWAYS_AVAILABLE,
    language: Language = Language.ENGLISH,
) -> str:
    key = language if language in _TEXT else Language.ENGLISH
    words = _TEXT[key]
    lines = [words["intro"], "", words["ask_heading"]]
    lines += [f"• {a}" for a in words["asks"]]

    servers = {t.split(":")[0] for t in external_tools}
    market = [
        text[key] for server, text in MARKET_DESCRIPTIONS.items() if server in servers
    ]
    web = [name for server, name in WEB_SERVERS.items() if server in servers]
    lookups = [f"{words['web']} ({', '.join(web)})"] if web else []
    lookups += [
        LOOKUP_DESCRIPTIONS[server][key] if server in LOOKUP_DESCRIPTIONS else server
        for server in sorted(servers)
        if server not in MARKET_DESCRIPTIONS and server not in WEB_SERVERS
    ]
    if lookups:
        lines += ["", words["lookup_heading"]]
        lines += [f"• {text}" for text in lookups]
        lines.append(words["lookup_note"])
    if market:
        lines += [
            "",
            words["market_heading"] + ", ".join(market) + ".",
            words["market_note"],
        ]
    if commands:
        lines += ["", words["commands_heading"]]
        lines += [f"• {c.described(key)}" for c in commands]
    if personal_facts:
        lines += ["", words["facts"]]
    lines += ["", words["audience"]]
    return "\n".join(lines)


class SelfDescriptionAnswerService:
    """Answers questions about the assistant itself; delegates the rest."""

    def __init__(
        self,
        fallback: AnswerService,
        external_tools: Sequence[str] = (),
        *,
        personal_facts: bool = False,
        commands: Sequence[Command] = ALWAYS_AVAILABLE,
    ) -> None:
        self._fallback = fallback
        self._tools = tuple(external_tools)
        self._personal_facts = personal_facts
        self._commands = tuple(commands)

    async def answer(self, question: Question) -> Answer:
        if self_description_question(question.text):
            # Rendered per question rather than once at startup, because the
            # language is the question's.
            language = detect(question.text)
            # No citations, deliberately: nothing here came from a message,
            # and inventing a source for a description of configuration would
            # make it look retrieved.
            return Answer(
                describe_capabilities(
                    self._tools,
                    personal_facts=self._personal_facts,
                    commands=self._commands,
                    language=language if language.known else Language.ENGLISH,
                )
            )
        return await self._fallback.answer(question)
