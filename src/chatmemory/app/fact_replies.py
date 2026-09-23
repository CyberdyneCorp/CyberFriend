"""What the assistant says about personal facts, in the language asked in.

These replies are fixed text, not model output, so the rule that an answer is
written in the question's language had to be applied here by hand. It was not:
"Oque voce sabe sobre mim?" was answered "Here's what you've asked me to
remember:".

English is the fallback for anything `detect` cannot place, and every English
string is unchanged from before this module existed.
"""

from __future__ import annotations

from collections.abc import Sequence

from chatmemory.app.facts import DIRECT_ONLY_KINDS, FactOutcome, FactResult
from chatmemory.app.language import Language
from chatmemory.ports.facts import (
    MAX_FULL_NAME_CHARS,
    MAX_PREFERRED_NAME_CHARS,
    FactKind,
    FactRejection,
    PersonalFacts,
)

EN, PT = Language.ENGLISH, Language.PORTUGUESE


def _lang(language: Language) -> Language:
    return PT if language is PT else EN


LABELS: dict[Language, dict[FactKind, str]] = {
    EN: {
        FactKind.PREFERRED_NAME: "preferred name",
        FactKind.EMAIL: "email address",
        FactKind.PREFERRED_LANGUAGE: "preferred language",
        FactKind.PHONE: "phone number",
        FactKind.ETH_WALLET: "Ethereum wallet",
        FactKind.BTC_WALLET: "Bitcoin wallet",
        FactKind.FULL_NAME: "full name",
    },
    PT: {
        FactKind.PREFERRED_NAME: "nome preferido",
        FactKind.EMAIL: "e-mail",
        FactKind.PREFERRED_LANGUAGE: "idioma preferido",
        FactKind.PHONE: "telefone",
        FactKind.ETH_WALLET: "carteira Ethereum",
        FactKind.BTC_WALLET: "carteira Bitcoin",
        FactKind.FULL_NAME: "nome completo",
    },
}
FACT_LABELS = LABELS[EN]

_FEMININE_PT = frozenset({FactKind.ETH_WALLET, FactKind.BTC_WALLET})


def possessive(kind: FactKind, language: Language) -> str:
    """"your phone number" / "seu telefone" / "sua carteira Ethereum"."""
    if _lang(language) is PT:
        return ("sua " if kind in _FEMININE_PT else "seu ") + LABELS[PT][kind]
    return "your " + LABELS[EN][kind]

REMEMBERABLE_TEXT = {
    EN: (
        "I can remember these about you, if you tell me yourself:\n"
        "- your full name (`my name is Leonardo Araujo`)\n"
        "- the name you'd like me to call you (`call me Leo`)\n"
        "- your email address (`my email is ...`)\n"
        "- your phone number (`my phone is ...`)\n"
        "- the language you'd like answers in (`reply to me in Portuguese`)\n"
        "- your Ethereum wallet (`my wallet is 0x...`) and your Bitcoin wallet "
        "(`my btc wallet is ...`)\n"
        "You can tell me several at once. "
        "Ask `what do you know about me?` to see them, or `forget my email` to "
        "delete one. Once I have your wallet, `what's my balance?` uses it.\n"
        "Your email, phone and wallets I only ever show you, in a direct message."
    ),
    PT: (
        "Posso guardar estas informações sobre você, se você mesmo me disser:\n"
        "- seu nome completo (`meu nome é Leonardo Araujo`)\n"
        "- como quer ser chamado (`pode me chamar de Leo`)\n"
        "- seu e-mail (`meu email é ...`)\n"
        "- seu telefone (`meu telefone é ...`)\n"
        "- o idioma das respostas (`responda em português`)\n"
        "- sua carteira Ethereum (`minha carteira é 0x...`) e sua carteira "
        "Bitcoin (`minha carteira btc é ...`)\n"
        "Pode me dizer várias de uma vez. "
        "Pergunte `o que você sabe sobre mim?` para vê-las, ou `esqueça meu email` "
        "para apagar uma. Com sua carteira salva, `qual o saldo da minha carteira?` "
        "usa ela.\n"
        "Seu e-mail, telefone e carteiras eu só mostro para você, em mensagem direta."
    ),
}
REMEMBERABLE = REMEMBERABLE_TEXT[EN]

_TEXT: dict[str, dict[Language, str]] = {
    "unsupported": {EN: "I haven't saved that. ", PT: "Não guardei isso. "},
    "about_someone_else": {
        EN: (
            "I only remember what people tell me about themselves, so I haven't "
            "saved that. They can tell me directly."
        ),
        PT: (
            "Só guardo o que as pessoas me contam sobre elas mesmas, então não "
            "guardei isso. A pessoa pode me dizer diretamente."
        ),
    },
    "others_refused": {
        EN: (
            "I don't share anything people have told me about themselves, and I "
            "can't say whether they have. Ask them directly."
        ),
        PT: (
            "Não compartilho nada que as pessoas me contaram sobre elas, nem posso "
            "dizer se contaram. Pergunte diretamente a elas."
        ),
    },
    "unavailable": {
        EN: "I can't remember personal details on this deployment.",
        PT: "Não consigo guardar dados pessoais nesta instalação.",
    },
    "email_note": {
        EN: "-# I only show an email address in a direct message to its owner.",
        PT: "-# Só mostro um e-mail em mensagem direta para o próprio dono.",
    },
    "not_stored": {
        EN: "I didn't save that: you've opted out, so I don't keep personal details for you.",
        PT: "Não guardei: você optou por sair, então não guardo dados pessoais seus.",
    },
    "call_you": {EN: "Got it, I'll call you **{v}**.", PT: "Certo, vou te chamar de **{v}**."},
    "full_name": {
        EN: "Got it, your full name is **{v}**.",
        PT: "Certo, seu nome completo é **{v}**.",
    },
    "answer_in": {
        EN: "Got it, I'll answer you in **{v}**.",
        PT: "Certo, vou te responder em **{v}**.",
    },
    "saved_direct": {
        EN: "Got it, I've saved {p} as `{v}`. I only show it to you, in a direct message.",
        PT: "Certo, salvei {p} como `{v}`. Só mostro para você, em mensagem direta.",
    },
    "saved_channel": {
        EN: (
            "Got it, I've saved {p}. I only show it to you in a direct "
            "message, so I won't repeat it here."
        ),
        PT: (
            "Certo, salvei {p}. Só mostro em mensagem direta para você, então "
            "não vou repetir aqui."
        ),
    },
    "not_saved_as": {
        EN: "I didn't save that as {p}: {r}.",
        PT: "Não salvei isso como {p}: {r}.",
    },
    "at_most": {EN: " (at most {n} characters)", PT: " (no máximo {n} caracteres)"},
    "saved_head": {EN: "Here's what I saved:", PT: "Aqui está o que salvei:"},
    "saved_hidden": {
        EN: "✓ {l}: saved (shown only in a direct message)",
        PT: "✓ {l}: salvo (mostrado só em mensagem direta)",
    },
    "not_saved_line": {EN: "✗ {l}: not saved, {r}", PT: "✗ {l}: não salvo, {r}"},
    "not_kept_line": {EN: "✗ {l}: I don't keep that", PT: "✗ {l}: não guardo isso"},
    "nothing_direct": {
        EN: "You haven't asked me to remember anything about you.\n\n",
        PT: "Você ainda não me pediu para guardar nada sobre você.\n\n",
    },
    "shown_head": {
        EN: "Here's what you've asked me to remember:",
        PT: "Aqui está o que você me pediu para guardar:",
    },
    "shown_here": {
        EN: "Here's what I can show you here:",
        PT: "Aqui está o que posso mostrar aqui:",
    },
    "nothing_here": {
        EN: "I have no preferred name or language saved for you.",
        PT: "Não tenho nome preferido nem idioma salvos para você.",
    },
    "one_missing": {
        EN: "You haven't told me {p}.",
        PT: "Você ainda não me disse {p}.",
    },
    "one_direct_only": {
        EN: "I only show {p} in a direct message. Ask me there.",
        PT: "Só mostro {p} em mensagem direta. Me pergunte lá.",
    },
    "forgot_all": {
        EN: "Done. I don't have a preferred name, email address or preferred language "
        "for you any more.",
        PT: "Pronto. Não tenho mais nenhum dado pessoal seu guardado.",
    },
    "forgot_one": {
        EN: "Done. I don't have a {l} for you any more.",
        PT: "Pronto. Não tenho mais {p}.",
    },
}

NOT_KEPT_LABELS = {
    "where you live": {EN: "Where you live", PT: "Onde você mora"},
}

REJECTIONS: dict[Language, dict[FactRejection, str]] = {
    EN: {
        FactRejection.EMPTY: "it was empty",
        FactRejection.TOO_LONG: "it's too long",
        FactRejection.MALFORMED: "it isn't the right shape",
        FactRejection.DISALLOWED_CHARACTERS: (
            "it can only use letters, numbers, spaces and simple punctuation"
        ),
    },
    PT: {
        FactRejection.EMPTY: "estava vazio",
        FactRejection.TOO_LONG: "é longo demais",
        FactRejection.MALFORMED: "não está no formato certo",
        FactRejection.DISALLOWED_CHARACTERS: (
            "só pode ter letras, números, espaços e pontuação simples"
        ),
    },
}
FACT_REJECTIONS = REJECTIONS[EN]

MALFORMED: dict[Language, dict[FactKind, str]] = {
    EN: {
        FactKind.EMAIL: "it isn't a well-formed email address",
        FactKind.PHONE: "it doesn't look like a phone number",
        FactKind.ETH_WALLET: (
            "it isn't a well-formed address - I expect `0x` and 40 hex characters"
        ),
        FactKind.BTC_WALLET: (
            "it isn't a well-formed Bitcoin address - I expect one starting `1`, "
            "`3` or `bc1`"
        ),
    },
    PT: {
        FactKind.EMAIL: "não é um e-mail válido",
        FactKind.PHONE: "não parece um número de telefone",
        FactKind.ETH_WALLET: (
            "não é um endereço válido - espero `0x` e 40 caracteres hexadecimais"
        ),
        FactKind.BTC_WALLET: (
            "não é um endereço Bitcoin válido - espero um começando com `1`, "
            "`3` ou `bc1`"
        ),
    },
}
MALFORMED_BY_KIND = MALFORMED[EN]

FACT_UNSUPPORTED = _TEXT["unsupported"][EN] + REMEMBERABLE
FACT_ABOUT_SOMEONE_ELSE = _TEXT["about_someone_else"][EN]
FACT_OTHERS_REFUSED = _TEXT["others_refused"][EN]
FACTS_UNAVAILABLE = _TEXT["unavailable"][EN]
EMAIL_CHANNEL_NOTE = _TEXT["email_note"][EN]
FACT_NOT_STORED = _TEXT["not_stored"][EN]


def text(key: str, language: Language, **values: object) -> str:
    return _TEXT[key][_lang(language)].format(**values)


def label(kind: FactKind, language: Language) -> str:
    return LABELS[_lang(language)][kind]


def rememberable(language: Language) -> str:
    return REMEMBERABLE_TEXT[_lang(language)]


def unsupported(language: Language) -> str:
    return text("unsupported", language) + rememberable(language)


def _stored_reply(result: FactResult, direct: bool, language: Language) -> str:
    fact = result.fact
    assert fact is not None
    if fact.kind is FactKind.PREFERRED_NAME:
        return text("call_you", language, v=fact.value)
    if fact.kind is FactKind.FULL_NAME:
        return text("full_name", language, v=fact.value)
    if fact.kind is FactKind.PREFERRED_LANGUAGE:
        return text("answer_in", language, v=fact.value)
    # Named by its own label: a phone or wallet was once confirmed as "your
    # email address". Confirmed without the value in a channel.
    key = "saved_direct" if direct else "saved_channel"
    return text(key, language, p=possessive(fact.kind, language), v=fact.value)


def fact_set_reply(result: FactResult, direct: bool, language: Language = EN) -> str:
    """What the person is told after asking to set a fact.

    Every stored fact is confirmed back, so a misrecognised "call me ..." is
    visible the moment it happens. A refused value is never repeated: an
    almost-email is still personal data.
    """
    if result.outcome is FactOutcome.STORED:
        return _stored_reply(result, direct, language)
    if result.outcome is FactOutcome.NOT_STORED:
        return text("not_stored", language)
    return text(
        "not_saved_as", language,
        p=possessive(result.kind, language), r=rejection_reason(result, language),
    )


def rejection_reason(result: FactResult, language: Language = EN) -> str:
    """Why a value was refused, without repeating it."""
    lang = _lang(language)
    rejection = result.rejection or FactRejection.MALFORMED
    reason = (
        MALFORMED[lang].get(result.kind, REJECTIONS[lang][FactRejection.MALFORMED])
        if rejection is FactRejection.MALFORMED
        else REJECTIONS[lang].get(rejection, "")
    )
    limits = {
        FactKind.PREFERRED_NAME: MAX_PREFERRED_NAME_CHARS,
        FactKind.FULL_NAME: MAX_FULL_NAME_CHARS,
    }
    if result.rejection is FactRejection.TOO_LONG and result.kind in limits:
        reason += text("at_most", language, n=limits[result.kind])
    return reason


def shown_line(kind: FactKind, value: str, language: Language = EN) -> str:
    # Values were validated to carry no markdown, so they are safe to embolden;
    # an email goes in code so its underscores are not read as emphasis.
    shown = f"`{value}`" if kind is FactKind.EMAIL else f"**{value}**"
    return f"• {label(kind, language).capitalize()}: {shown}"


def facts_set_reply(
    results: Sequence[FactResult],
    not_kept: Sequence[str],
    direct: bool,
    language: Language = EN,
) -> str:
    """One reply for an introduction: what was saved, what was not, and why.

    Values follow the same rule as a single fact: shown back in a direct
    message, withheld in a channel for the kinds only ever shown to their
    owner, and never repeated when refused.
    """
    lines: list[str] = []
    for result in results:
        name = label(result.kind, language).capitalize()
        if result.outcome is FactOutcome.NOT_STORED:
            return text("not_stored", language)
        if result.outcome is FactOutcome.REJECTED:
            lines.append(
                text("not_saved_line", language, l=name, r=rejection_reason(result, language))
            )
            continue
        assert result.fact is not None
        if not direct and result.kind in DIRECT_ONLY_KINDS:
            lines.append(text("saved_hidden", language, l=name))
        else:
            line = shown_line(result.kind, result.fact.value, language)
            lines.append("✓ " + line.removeprefix("• "))
    for kept in not_kept:
        shown = NOT_KEPT_LABELS.get(kept, {}).get(_lang(language), kept.capitalize())
        lines.append(text("not_kept_line", language, l=shown))
    return "\n".join([text("saved_head", language), *lines])


def facts_shown_reply(
    facts: PersonalFacts,
    direct: bool,
    language: Language = EN,
    kind: FactKind | None = None,
) -> str:
    """The person's own facts, as they may be shown where they asked.

    `facts` has already been through `visible_facts`, so a channel reply has no
    email to show. The wording must not let "not shown here" read as "not
    stored": the channel version never says there is nothing.

    `kind` answers one question ("what's my phone?") with that fact alone.
    """
    if kind is not None:
        return _one_fact(facts, direct, language, kind)
    lines = [shown_line(s.fact.kind, s.fact.value, language) for s in facts.facts]
    if direct:
        if not lines:
            return text("nothing_direct", language) + rememberable(language)
        return "\n".join([text("shown_head", language), *lines])
    head = text("shown_here", language) if lines else text("nothing_here", language)
    return "\n".join([head, *lines, text("email_note", language)])


def _one_fact(
    facts: PersonalFacts, direct: bool, language: Language, kind: FactKind
) -> str:
    # In a channel a direct-only kind gets the same words whether or not it is
    # stored: anything else would tell the room it exists.
    if not direct and kind in DIRECT_ONLY_KINDS:
        return text("one_direct_only", language, p=possessive(kind, language))
    by_kind = {stored.fact.kind: stored.fact.value for stored in facts.facts}
    if kind in by_kind:
        return shown_line(kind, by_kind[kind], language)
    # "What's my name?" with no full name saved: the name they gave is the
    # answer, not "you haven't told me".
    if kind is FactKind.FULL_NAME and FactKind.PREFERRED_NAME in by_kind:
        return shown_line(FactKind.PREFERRED_NAME, by_kind[FactKind.PREFERRED_NAME], language)
    return text("one_missing", language, p=possessive(kind, language))


def fact_forgotten_reply(kind: FactKind | None, language: Language = EN) -> str:
    """The same words whether or not there was anything to delete.

    In a channel, "you had no email saved" would tell the room something; and
    the person's goal -- that it is gone -- holds either way.
    """
    if kind is None:
        return text("forgot_all", language)
    return text("forgot_one", language, l=label(kind, language), p=possessive(kind, language))
