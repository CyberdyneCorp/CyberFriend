"""Answering "what can you do?" from configuration, never from the corpus.

The corpus is other people's conversations. Searching it for the assistant's
own capabilities finds whatever somebody once wrote about some other tool, and
the answer then presents that as fact about itself: asked what it could do,
the bot replied that it calculates cryptocurrency prices, because a colleague
had described a project that did.

So a self-description is assembled from what this deployment is actually
running. Every section is shown only where its feature is on -- crypto where
the wallet tools are registered, alerts where `/alert` is, voice where voice
is heard -- which keeps it honest in the other direction too: a capability
list that promises web search on a deployment without it is the same mistake
inverted.

Written in the language the question was asked in. A person who writes a whole
sentence of Portuguese and is answered in English has been told, accurately,
that the assistant was not really listening.

Laid out as short sections separated by blank lines, because the Discord
adapter splits a long reply at paragraph boundaries: each section stays whole
in one message, and none comes near the 2000-character limit on its own.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from chatmemory.app.egress import (
    CHAIN_BALANCES_PROVIDER,
    DEFI_POSITIONS_PROVIDER,
    MARKET_CRYPTO_PROVIDER,
    MARKET_FX_PROVIDER,
    MARKET_INDEX_PROVIDER,
)
from chatmemory.app.language import Language, detect
from chatmemory.app.reasoning import features
from chatmemory.app.reasoning.contract import (
    RunOutcome,
    TerminalCause,
    answered,
    run_of,
)
from chatmemory.app.routing import self_description_question
from chatmemory.app.tracing_notice import recording_statement
from chatmemory.domain.audience import DeliveryMode
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

CRYPTO_SERVERS = frozenset({CHAIN_BALANCES_PROVIDER, DEFI_POSITIONS_PROVIDER})
"""Servers described in the crypto section rather than as a lookup."""

LOOKUP_DESCRIPTIONS = {
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
    guild_only: bool = False
    """Registered on the guild alone, so Discord does not list it in a DM."""

    def described(self, language: Language) -> str:
        text = self.portuguese if language is Language.PORTUGUESE else self.english
        return f"`/{self.name}` — {text}"


ASK = Command("ask", "Ask about what's been said", "Perguntar sobre o que foi dito")
INDEX = Command(
    "index",
    "Archive a channel so people who can read it can search it",
    "Arquivar um canal para que quem pode lê-lo possa pesquisá-lo",
    guild_only=True,
)
UNINDEX = Command(
    "unindex",
    "Stop archiving a channel and delete what was archived",
    "Parar de arquivar um canal e apagar o que foi arquivado",
    guild_only=True,
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

SUGGEST = Command(
    "suggest",
    "Suggest something I should learn to do",
    "Sugerir algo que eu deveria aprender a fazer",
)
SUGGESTIONS = Command(
    "suggestions",
    "Show your suggestions and their status",
    "Mostrar suas sugestões e o status de cada uma",
)

PRIVACY = Command(
    "privacy",
    "See what I hold about you and what I keep",
    "Ver o que eu guardo sobre você e o que eu mantenho",
)

CHANNELS = Command(
    "channels",
    "List the channels I archive that you can read",
    "Listar os canais que eu arquivo e que você pode ler",
)

SCHEDULE_CREATE = Command(
    "schedule create",
    "Ask me something on a schedule, between hourly and daily",
    "Me pedir algo de forma agendada, de hora em hora até uma vez por dia",
)
SCHEDULE_LIST = Command(
    "schedule list",
    "Show the questions I ask for you, and when each last ran",
    "Mostrar as perguntas que eu faço por você, e quando cada uma rodou pela última vez",
)
SCHEDULE_DELETE = Command(
    "schedule delete",
    "Stop one of your scheduled questions",
    "Parar uma das suas perguntas agendadas",
)

SCHEDULED = (SCHEDULE_CREATE, SCHEDULE_LIST, SCHEDULE_DELETE)
"""Listed only where the feature is on, like `/notifications`.

Naming a command Discord will not show is worse than omitting one, and this is
the feature where that matters most: somebody told they can schedule a question
and then unable to will reasonably conclude the assistant is broken."""

ALERT_LIST = Command(
    "alert list",
    "Show your alerts (LP range and range edge, Aave health factor, BTC/ETH price)",
    "Mostrar seus alertas (faixa e borda de LP, health factor do Aave, preço de BTC/ETH)",
)
ALERT_DELETE = Command(
    "alert delete",
    "Stop one of your alerts",
    "Parar um dos seus alertas",
)

ALERTS = (ALERT_LIST, ALERT_DELETE)
"""Listed only where alerts are on, like `SCHEDULED`. An alert is created by
asking ("tell me when my LP goes out of range") and pressing Confirm; these are
how somebody sees and stops theirs."""

ALWAYS_AVAILABLE = (
    ASK, CHANNELS, INDEX, UNINDEX, FORGET, RESOLVE, SUGGEST, SUGGESTIONS, PRIVACY,
)
"""Commands the bot registers unconditionally.

`notifications` is not here: it is only registered when the feature is on, and
listing a command that does not exist is worse than omitting one that does."""


_TEXT: dict[Language, dict[str, Any]] = {
    Language.ENGLISH: {
        "intro": (
            "I'm CyberFriend. I answer questions about what has been said in "
            "the channels you're allowed to read, and I cite the messages I "
            "used. Mention me, use `/ask`, or send me a direct message."
        ),
        "conversations_heading": "Conversations",
        "conversations": (
            "Ask about a channel: `what's the status of the release?`",
            "Catch up: `what did I miss in #infra?`",
            "What someone said: `what did Ana say about pricing yesterday?`",
            "Decisions: `what did we decide about the deploy?`",
        ),
        "obligations_heading": "What's asked of you",
        "obligations": (
            "`what do I need to do?`",
            "`what did people ask me today?`",
        ),
        "facts_heading": "About you",
        "facts": (
            "Tell me your name or what to call you, your full name, email, "
            "phone, home address, birth date, preferred language, a currency to "
            "see amounts in besides US dollars, or your ETH and BTC wallets "
            "(several of each), and I'll remember it",
            "Several in one message: `call me Leo, my email is leo@example.com`",
            "`what do you know about me?` shows them; `forget my phone` deletes one",
        ),
        "facts_note": (
            "Your contact details, address, birth date and wallets are only shown "
            "to you, in a direct message."
        ),
        "crypto_heading": "Crypto",
        "balances": "Balances on Ethereum, Base and Arbitrum: `what's in 0x…?`",
        "defi": (
            "Uniswap v3/v4 liquidity positions: `show my Uniswap positions`",
            "Aave supplies, borrows and health factor: `what's my Aave health factor?`",
            "Everything together: `how much do I have in total?`",
            "What a wallet did, up to 30 days: `what did my wallet do this week?`",
        ),
        "crypto_note": "Ask about an address you type, or about your saved wallets.",
        "market_heading": "Markets",
        "market_ask": "`what's the BTC price?`",
        "market_note": (
            "Live sources only, never a price somebody mentioned in a channel. "
            "Each figure says how current it is, and is also shown in your "
            "preferred currency when you have told me one (`my currency is "
            "euro`). I don't recommend buying, selling or holding anything."
        ),
        "alerts_heading": "Alerts",
        "alerts": (
            "`tell me when my LP goes out of range`",
            "`warn me when my LP is within 10% of the range edge`",
            "`alert me if my health factor drops below 1.3`",
            "`tell me when BTC goes above 100k`",
        ),
        "alerts_note": (
            "Ask in words and press Confirm; I send you a direct message when one fires."
        ),
        "scheduled_heading": "Scheduled questions",
        "voice_heading": "Voice",
        "voice": (
            "In a direct message, send me a voice message instead of typing; "
            "I show what I understood above the answer.",
        ),
        "lookup_heading": "Looking things up",
        "web": "the web",
        "lookup_note": (
            "When the conversations don't have the answer I can look it up; "
            "anything from outside this server is labelled as such."
        ),
        "privacy_heading": "Privacy",
        "commands_heading": "Commands",
        "audience": "In a channel, I only cite what everyone there can read.",
        "audience_dm_hint": "Ask me in a direct message for your full view.",
    },
    Language.PORTUGUESE: {
        "intro": (
            "Eu sou o CyberFriend. Respondo perguntas sobre o que foi dito nos "
            "canais que você pode ler, e cito as mensagens que usei. Me "
            "mencione, use `/ask` ou me mande uma mensagem direta."
        ),
        "conversations_heading": "Conversas",
        "conversations": (
            "Sobre um canal: `qual o status do release?`",
            "Pôr o papo em dia: `o que eu perdi no #general?`",
            "O que alguém disse: `o que o João disse sobre o preço ontem?`",
            "Decisões: `o que decidimos sobre o deploy?`",
        ),
        "facts_heading": "Sobre você",
        "facts": (
            "Me diga seu nome ou como quer ser chamado, seu nome completo, "
            "e-mail, telefone, endereço, data de nascimento, idioma preferido, "
            "uma moeda para ver os valores além do dólar, ou suas carteiras ETH "
            "e BTC (várias de cada), e eu guardo",
            "Várias de uma vez: `pode me chamar de Leo, meu email é leo@exemplo.com`",
            "`o que você sabe sobre mim?` mostra tudo; `esqueça meu telefone` apaga um",
        ),
        "facts_note": (
            "Seus contatos, endereço, data de nascimento e carteiras só são "
            "mostrados para você, numa mensagem direta."
        ),
        "crypto_heading": "Cripto",
        "balances": "Saldos na Ethereum, Base e Arbitrum: `quanto tem em 0x…?`",
        "defi": (
            "Posições de liquidez na Uniswap v3/v4: `mostre minhas posições na Uniswap`",
            "Depósitos, empréstimos e health factor no Aave: `qual o meu health factor no Aave?`",
            "Tudo junto: `quanto eu tenho no total?`",
            "O que uma carteira fez, até 30 dias: `o que minha carteira fez essa semana?`",
        ),
        "crypto_note": (
            "Pergunte sobre um endereço que você digitar, ou sobre suas carteiras salvas."
        ),
        "market_heading": "Mercado",
        "market_ask": "`qual o preço do BTC?`",
        "market_note": (
            "Só fontes ao vivo, nunca um preço que alguém mencionou num canal. "
            "Cada número diz o quão atual ele é, e aparece também na sua moeda "
            "preferida se você me disser uma (`prefiro ver em reais`); não "
            "recomendo comprar, vender nem manter nada."
        ),
        "alerts_heading": "Alertas",
        "alerts": (
            "`me avisa quando meu LP ficar fora do range`",
            "`me avisa quando meu LP estiver a 10% da borda do range`",
            "`me avisa se o health factor cair abaixo de 1,3`",
            "`avisa quando o BTC passar de 100k`",
        ),
        "alerts_note": (
            "Peça com palavras e aperte Confirmar; eu te mando uma mensagem direta "
            "quando um disparar."
        ),
        "scheduled_heading": "Perguntas agendadas",
        "voice_heading": "Voz",
        "voice": (
            "Numa mensagem direta, me mande um áudio em vez de digitar; eu "
            "mostro o que entendi acima da resposta.",
        ),
        "lookup_heading": "Buscas externas",
        "web": "a web",
        "lookup_note": (
            "Quando as conversas não têm a resposta eu posso buscar; o que vem "
            "de fora deste servidor é marcado como tal."
        ),
        "privacy_heading": "Privacidade",
        "commands_heading": "Comandos",
        "audience": "Num canal, eu só cito o que todos ali podem ler.",
        "audience_dm_hint": (
            "Me pergunte numa mensagem direta para buscar em tudo o que você pode ler."
        ),
    },
}
"""Every sentence the description can say, per language. Every quoted example
is sent to its route by a test, so the reply does not suggest a question the
bot then handles some other way.

Portuguese has no obligations section: the obligation route only recognises
English questions, and answers in English, so "o que eu preciso fazer?" is
answered by retrieval rather than from the asks. Offering it would promise a
route the question never reaches."""


@dataclass(frozen=True, slots=True)
class Capabilities:
    """What this deployment runs, as far as describing itself goes.

    Built once from settings in composition and shared by the answer path and
    the Discord surface, so "what can you do?" and a bare mention can never
    disagree about what is on. Alerts and scheduled questions are read off
    `commands`, the one place their switches already decide.
    """

    external_tools: tuple[str, ...] = ()
    commands: tuple[Command, ...] = ALWAYS_AVAILABLE
    personal_facts: bool = False
    """Whether this process keeps personal facts ("call me Leo")."""
    obligations: bool = True
    """Whether asks are extracted, so "what do I need to do?" has rows."""
    voice_questions: bool = False
    trace_retention_days: int | None = None
    """How long questions and answers are recorded; None where nothing is traced."""
    servers: frozenset[str] = field(init=False)

    def __post_init__(self) -> None:
        servers = frozenset(t.split(":")[0] for t in self.external_tools)
        object.__setattr__(self, "servers", servers)

    def offered(self, *, direct_message: bool) -> Capabilities:
        """The same capabilities with only the commands Discord lists there.

        A DM menu has no guild commands, and telling somebody to pick
        `/index` from a menu that does not have it is the same mistake as
        listing a command whose feature is off.
        """
        if not direct_message:
            return self
        return Capabilities(
            self.external_tools,
            tuple(c for c in self.commands if not c.guild_only),
            personal_facts=self.personal_facts,
            obligations=self.obligations,
            voice_questions=self.voice_questions,
            trace_retention_days=self.trace_retention_days,
        )

    def describe(self, language: Language, *, direct_message: bool = False) -> str:
        key = language if language in _TEXT else Language.ENGLISH
        shown = self.offered(direct_message=direct_message)
        words = _TEXT[key]
        parts = [
            words["intro"],
            _section(words["conversations_heading"], words["conversations"]),
        ]
        parts += [s for build in _SECTIONS if (s := build(shown, words, key))]
        # Telling somebody already in a DM to ask in a DM is noise.
        audience = words["audience"]
        if not direct_message:
            audience += " " + words["audience_dm_hint"]
        parts.append(audience)
        return "\n\n".join(parts)


def _section(heading: str, bullets: Sequence[str], note: str = "") -> str:
    lines = [f"**{heading}**", *(f"• {b}" for b in bullets)]
    if note:
        lines.append(note)
    return "\n".join(lines)


Words = dict[str, Any]


def _obligations(caps: Capabilities, words: Words, _: Language) -> str:
    if not caps.obligations or "obligations" not in words:
        return ""
    return _section(words["obligations_heading"], words["obligations"])


def _facts(caps: Capabilities, words: Words, _: Language) -> str:
    if not caps.personal_facts:
        return ""
    return _section(words["facts_heading"], words["facts"], words["facts_note"])


def _crypto(caps: Capabilities, words: Words, _: Language) -> str:
    bullets: list[str] = []
    if CHAIN_BALANCES_PROVIDER in caps.servers:
        bullets.append(words["balances"])
    if DEFI_POSITIONS_PROVIDER in caps.servers:
        bullets += words["defi"]
    if not bullets:
        return ""
    return _section(words["crypto_heading"], bullets, words["crypto_note"])


def _markets(caps: Capabilities, words: Words, language: Language) -> str:
    offered = [
        text[language] for server, text in MARKET_DESCRIPTIONS.items() if server in caps.servers
    ]
    if not offered:
        return ""
    listed = ", ".join(offered)
    bullet = listed[0].upper() + listed[1:]
    if MARKET_CRYPTO_PROVIDER in caps.servers:
        # The example asks a price, so it is offered only where prices are.
        bullet += ": " + words["market_ask"]
    return _section(words["market_heading"], [bullet], words["market_note"])


def _alerts(caps: Capabilities, words: Words, language: Language) -> str:
    commands = [c.described(language) for c in ALERTS if c in caps.commands]
    if not commands:
        return ""
    return _section(words["alerts_heading"], [*words["alerts"], *commands], words["alerts_note"])


def _scheduled(caps: Capabilities, words: Words, language: Language) -> str:
    commands = [c.described(language) for c in SCHEDULED if c in caps.commands]
    return _section(words["scheduled_heading"], commands) if commands else ""


def _voice(caps: Capabilities, words: Words, _: Language) -> str:
    return _section(words["voice_heading"], words["voice"]) if caps.voice_questions else ""


def _lookups(caps: Capabilities, words: Words, language: Language) -> str:
    web = [name for server, name in WEB_SERVERS.items() if server in caps.servers]
    bullets = [f"{words['web']} ({', '.join(web)})"] if web else []
    bullets += [
        LOOKUP_DESCRIPTIONS[server][language] if server in LOOKUP_DESCRIPTIONS else server
        for server in sorted(caps.servers)
        if server not in MARKET_DESCRIPTIONS
        and server not in WEB_SERVERS
        and server not in CRYPTO_SERVERS
    ]
    if not bullets:
        return ""
    return _section(words["lookup_heading"], bullets, words["lookup_note"])


def _privacy(caps: Capabilities, words: Words, language: Language) -> str:
    # Only where runs are exported: saying questions are recorded on a
    # deployment that records nothing is the same mistake as a missing tool.
    if caps.trace_retention_days is None:
        return ""
    return _section(
        words["privacy_heading"], [recording_statement(language, caps.trace_retention_days)]
    )


def _commands(caps: Capabilities, words: Words, language: Language) -> str:
    # Alerts and schedules are listed in their own sections, beside how they
    # are asked for, rather than twice.
    general = [
        c.described(language) for c in caps.commands if c not in ALERTS and c not in SCHEDULED
    ]
    return _section(words["commands_heading"], general) if general else ""


_SECTIONS: tuple[Callable[[Capabilities, Words, Language], str], ...] = (
    _obligations,
    _facts,
    _crypto,
    _markets,
    _alerts,
    _scheduled,
    _voice,
    _lookups,
    _privacy,
    _commands,
)
"""The optional sections, in the order they are shown. Each is empty where
its feature is off."""


def describe_capabilities(
    external_tools: Sequence[str],
    *,
    personal_facts: bool = False,
    commands: Sequence[Command] = ALWAYS_AVAILABLE,
    language: Language = Language.ENGLISH,
    voice_questions: bool = False,
    obligations: bool = True,
    trace_retention_days: int | None = None,
) -> str:
    return Capabilities(
        tuple(external_tools),
        tuple(commands),
        personal_facts=personal_facts,
        obligations=obligations,
        voice_questions=voice_questions,
        trace_retention_days=trace_retention_days,
    ).describe(language)


class SelfDescriptionAnswerService:
    """Answers questions about the assistant itself; delegates the rest."""

    def __init__(self, fallback: AnswerService, capabilities: Capabilities | None = None) -> None:
        self._fallback = fallback
        self.capabilities = capabilities or Capabilities()

    async def answer(self, question: Question) -> Answer:
        return (await self.answer_run(question)).answer

    async def answer_run(self, question: Question) -> RunOutcome:
        if not self_description_question(question.text):
            return await run_of(self._fallback, question)
        # From configuration: nothing is searched, as for any other refusal
        # to consult the corpus.
        return answered(
            self._describe(question),
            features.CAPABILITIES,
            TerminalCause.CONFIGURATION_BLOCKED,
        )

    def _describe(self, question: Question) -> Answer:
        # Rendered per question rather than once at startup, because the
        # language is the question's.
        language = detect(question.text)
        # No citations, deliberately: nothing here came from a message,
        # and inventing a source for a description of configuration would
        # make it look retrieved.
        return Answer(
            self.capabilities.describe(
                language if language.known else Language.ENGLISH,
                direct_message=question.audience.mode is DeliveryMode.DIRECT_MESSAGE,
            ),
            # An empty set, not None: it read no channel. None means
            # "never established", and memory refuses to store that.
            consulted_channels=frozenset(),
        )


_EVERY_COMMAND = (*ALWAYS_AVAILABLE, NOTIFICATIONS, *SCHEDULED, *ALERTS)

_TYPED_COMMAND = {
    Language.ENGLISH: (
        "`/{name}` is a slash command, and typed as a message it doesn't run. "
        "Type `/` and pick **/{name}** from the menu Discord shows."
    ),
    Language.PORTUGUESE: (
        "`/{name}` é um comando de barra, e digitado como mensagem ele não roda. "
        "Digite `/` e escolha **/{name}** no menu que o Discord mostra."
    ),
}


def typed_command(text: str) -> Command | None:
    """A slash command sent as a plain message, or None.

    "/forget" arrived as text -- the command menu was not used -- and went to
    the corpus, which answered that it had no evidence it could forget
    anything. Only real command names count, longest first so "/schedule
    list" is not read as "/schedule".
    """
    stripped = " ".join(text.strip().lower().split())
    if not stripped.startswith("/"):
        return None
    typed = stripped[1:]
    for command in sorted(_EVERY_COMMAND, key=lambda c: -len(c.name)):
        if typed == command.name or typed.startswith(command.name + " "):
            return command
    return None


def typed_command_reply(command: Command, language: Language) -> str:
    """How to run it. Both languages when the message itself has none."""
    if language is Language.UNKNOWN:
        return "\n".join(
            _TYPED_COMMAND[lang].format(name=command.name)
            for lang in (Language.PORTUGUESE, Language.ENGLISH)
        )
    return _TYPED_COMMAND[language].format(name=command.name)
