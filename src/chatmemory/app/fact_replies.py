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
from chatmemory.app.routing import AGE, WHERE_YOURE_FROM
from chatmemory.ports.facts import (
    MAX_FULL_NAME_CHARS,
    MAX_HOME_ADDRESS_CHARS,
    MAX_PREFERRED_NAME_CHARS,
    MAX_WALLETS_PER_KIND,
    MULTI_VALUED_KINDS,
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
        FactKind.HOME_ADDRESS: "home address",
        FactKind.BIRTH_DATE: "birth date",
    },
    PT: {
        FactKind.PREFERRED_NAME: "nome preferido",
        FactKind.EMAIL: "e-mail",
        FactKind.PREFERRED_LANGUAGE: "idioma preferido",
        FactKind.PHONE: "telefone",
        FactKind.ETH_WALLET: "carteira Ethereum",
        FactKind.BTC_WALLET: "carteira Bitcoin",
        FactKind.FULL_NAME: "nome completo",
        FactKind.HOME_ADDRESS: "endereço",
        FactKind.BIRTH_DATE: "data de nascimento",
    },
}
FACT_LABELS = LABELS[EN]

PLURAL_LABELS: dict[Language, dict[FactKind, str]] = {
    EN: {FactKind.ETH_WALLET: "Ethereum wallets", FactKind.BTC_WALLET: "Bitcoin wallets"},
    PT: {FactKind.ETH_WALLET: "carteiras Ethereum", FactKind.BTC_WALLET: "carteiras Bitcoin"},
}
"""The kinds a person may hold several of, named in the plural."""

_FEMININE_PT = frozenset({FactKind.ETH_WALLET, FactKind.BTC_WALLET, FactKind.BIRTH_DATE})


def possessive(kind: FactKind, language: Language, *, plural: bool = False) -> str:
    """"your phone number" / "seu endereço" / "sua data de nascimento" /
    "suas carteiras Ethereum"."""
    lang = _lang(language)
    plurals = PLURAL_LABELS[lang]
    name = plurals[kind] if plural and kind in plurals else label(kind, lang)
    if lang is PT:
        feminine = kind in _FEMININE_PT
        return ("sua" if feminine else "seu") + ("s " if plural else " ") + name
    return "your " + name

REMEMBERABLE_TEXT = {
    EN: (
        "I can remember these about you, if you tell me yourself:\n"
        "- your full name (`my name is Leonardo Araujo`)\n"
        "- the name you'd like me to call you (`call me Leo`)\n"
        "- your email address (`my email is ...`)\n"
        "- your phone number (`my phone is ...`)\n"
        "- your home address (`I live in ...`)\n"
        "- your birth date (`I was born on 21/06/1981`)\n"
        "- the language you'd like answers in (`reply to me in Portuguese`)\n"
        "- your Ethereum wallets, up to 5 (`my wallet is 0x...`), and your Bitcoin "
        "wallets (`my btc wallet is ...`)\n"
        "You can tell me several at once. "
        "Ask `what do you know about me?` to see them, `forget my email` to "
        "delete one, or `forget my wallet 0x...` to delete one wallet. Once I "
        "have your wallets, `what's my portfolio?` sums them.\n"
        "Your email, phone, address, birth date and wallets I only ever show you, "
        "in a direct message."
    ),
    PT: (
        "Posso guardar estas informações sobre você, se você mesmo me disser:\n"
        "- seu nome completo (`meu nome é Leonardo Araujo`)\n"
        "- como quer ser chamado (`pode me chamar de Leo`)\n"
        "- seu e-mail (`meu email é ...`)\n"
        "- seu telefone (`meu telefone é ...`)\n"
        "- seu endereço (`moro em ...`)\n"
        "- sua data de nascimento (`nasci em 21/06/1981`)\n"
        "- o idioma das respostas (`responda em português`)\n"
        "- suas carteiras Ethereum, até 5 (`minha carteira é 0x...`), e suas "
        "carteiras Bitcoin (`minha carteira btc é ...`)\n"
        "Pode me dizer várias de uma vez. "
        "Pergunte `o que você sabe sobre mim?` para vê-las, `esqueça meu email` "
        "para apagar uma, ou `esqueça minha carteira 0x...` para apagar uma "
        "carteira. Com suas carteiras salvas, `qual o meu portfólio?` soma todas.\n"
        "Seu e-mail, telefone, endereço, data de nascimento e carteiras eu só "
        "mostro para você, em mensagem direta."
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
    "age_derived_line": {
        EN: "✗ {l}: not kept, it follows from your birth date",
        PT: "✗ {l}: não guardo, ela vem da sua data de nascimento",
    },
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
        EN: "Done. I don't have any personal details saved for you any more.",
        PT: "Pronto. Não tenho mais nenhum dado pessoal seu guardado.",
    },
    "forgot_one": {
        EN: "Done. I don't have a {l} for you any more.",
        PT: "Pronto. Não tenho mais {p}.",
    },
    "forgot_every": {
        EN: "Done. I don't have {p} any more.",
        PT: "Pronto. Não tenho mais {p}.",
    },
    "forgot_that": {
        EN: "Done. If that {l} was saved, it isn't any more.",
        PT: "Pronto. Se essa {l} estava salva, não está mais.",
    },
}

NOT_KEPT_LABELS = {
    AGE: {EN: "Age", PT: "Idade"},
    WHERE_YOURE_FROM: {EN: "Where you're from", PT: "De onde você é"},
}

REJECTIONS: dict[Language, dict[FactRejection, str]] = {
    EN: {
        FactRejection.EMPTY: "it was empty",
        FactRejection.TOO_LONG: "it's too long",
        FactRejection.MALFORMED: "it isn't the right shape",
        FactRejection.DISALLOWED_CHARACTERS: (
            "it can only use letters, numbers, spaces and simple punctuation"
        ),
        FactRejection.TOO_MANY: (
            "you already have {n} saved - forget one first (`forget my wallet 0x...`)"
        ),
    },
    PT: {
        FactRejection.EMPTY: "estava vazio",
        FactRejection.TOO_LONG: "é longo demais",
        FactRejection.MALFORMED: "não está no formato certo",
        FactRejection.DISALLOWED_CHARACTERS: (
            "só pode ter letras, números, espaços e pontuação simples"
        ),
        FactRejection.TOO_MANY: (
            "você já tem {n} salvas - esqueça uma antes (`esqueça minha carteira 0x...`)"
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
        FactKind.BIRTH_DATE: (
            "it isn't a past date I can read - try `21/06/1981` or `1981-06-21`"
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
        FactKind.BIRTH_DATE: (
            "não é uma data passada que eu consiga ler - tente `21/06/1981` ou `1981-06-21`"
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


def _capital(name: str) -> str:
    # Not `str.capitalize`, which lowers the rest: "Carteira ethereum".
    return name[:1].upper() + name[1:]


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
    if rejection is FactRejection.TOO_MANY:
        return reason.format(n=MAX_WALLETS_PER_KIND)
    limits = {
        FactKind.PREFERRED_NAME: MAX_PREFERRED_NAME_CHARS,
        FactKind.FULL_NAME: MAX_FULL_NAME_CHARS,
        FactKind.HOME_ADDRESS: MAX_HOME_ADDRESS_CHARS,
    }
    if result.rejection is FactRejection.TOO_LONG and result.kind in limits:
        reason += text("at_most", language, n=limits[result.kind])
    return reason


_MONTH_NAMES = {
    EN: ("January", "February", "March", "April", "May", "June", "July", "August",
         "September", "October", "November", "December"),
    PT: ("janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto",
         "setembro", "outubro", "novembro", "dezembro"),
}


def shown_value(kind: FactKind, value: str, language: Language = EN) -> str:
    """A stored value as it reads to its owner: a birth date spelled out, since
    "1981-06-21" and "06/21" both read wrongly to somebody."""
    if kind is not FactKind.BIRTH_DATE:
        return value
    year, month, day = (int(part) for part in value.split("-"))
    month_name = _MONTH_NAMES[_lang(language)][month - 1]
    if _lang(language) is PT:
        return f"{day} de {month_name} de {year}"
    return f"{day} {month_name} {year}"


def shown_line(kind: FactKind, value: str, language: Language = EN) -> str:
    # Values were validated to carry no markdown, so they are safe to embolden;
    # an email goes in code so its underscores are not read as emphasis.
    shown = f"`{value}`" if kind is FactKind.EMAIL else f"**{shown_value(kind, value, language)}**"
    return f"• {_capital(label(kind, language))}: {shown}"


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
    if any(r.outcome is FactOutcome.NOT_STORED for r in results):
        return text("not_stored", language)
    lines = [_result_line(result, direct, language) for result in results]
    born = any(
        r.kind is FactKind.BIRTH_DATE and r.outcome is FactOutcome.STORED for r in results
    )
    lines += [_not_kept_line(kept, born, language) for kept in not_kept]
    return "\n".join([text("saved_head", language), *lines])


def _result_line(result: FactResult, direct: bool, language: Language) -> str:
    name = _capital(label(result.kind, language))
    if result.outcome is FactOutcome.REJECTED:
        return text("not_saved_line", language, l=name, r=rejection_reason(result, language))
    assert result.fact is not None
    if not direct and result.kind in DIRECT_ONLY_KINDS:
        return text("saved_hidden", language, l=name)
    return "✓ " + shown_line(result.kind, result.fact.value, language).removeprefix("• ")


def _not_kept_line(kept: str, born: bool, language: Language) -> str:
    """What was said but not kept. An age said alongside a birth date is
    explained by it: keeping both would let them disagree next birthday."""
    shown = NOT_KEPT_LABELS.get(kept, {}).get(_lang(language), kept.capitalize())
    key = "age_derived_line" if kept == AGE and born else "not_kept_line"
    return text(key, language, l=shown)


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
    held = facts.values(kind)
    if held:
        return "\n".join(shown_line(kind, value, language) for value in held)
    # "What's my name?" with no full name saved: the name they gave is the
    # answer, not "you haven't told me".
    if kind is FactKind.FULL_NAME and FactKind.PREFERRED_NAME in by_kind:
        return shown_line(FactKind.PREFERRED_NAME, by_kind[FactKind.PREFERRED_NAME], language)
    return text("one_missing", language, p=possessive(kind, language))


def fact_forgotten_reply(
    kind: FactKind | None, language: Language = EN, *, one_value: bool = False
) -> str:
    """The same words whether or not there was anything to delete.

    In a channel, "you had no email saved" would tell the room something; and
    the person's goal -- that it is gone -- holds either way. A wallet named by
    its address is not repeated back: the reply may be in a channel.
    """
    if kind is None:
        return text("forgot_all", language)
    if kind in MULTI_VALUED_KINDS and one_value:
        return text("forgot_that", language, l=label(kind, language))
    if kind in MULTI_VALUED_KINDS:
        return text("forgot_every", language, p=possessive(kind, language, plural=True))
    return text("forgot_one", language, l=label(kind, language), p=possessive(kind, language))
