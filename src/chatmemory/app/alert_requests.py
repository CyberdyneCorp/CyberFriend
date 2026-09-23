"""Turning "alert me if my health factor drops below 1.3" into alerts, with a confirm.

`AlertRequests.propose` is what a recognised request (`alert_intent`) gets: the
address settled, the chain read once, and a reply listing exactly what would be
watched -- chain, position and range, or limit -- with the reading right now.
Nothing is stored. The surface shows that reply with Confirm and Cancel, and
only `confirm`, reached from the Confirm button the asker pressed, creates
anything. So a misread request costs a message, never a standing watch.

The address rule is the egress guard's, applied once because the sweep has no
asker to apply it to: the address the asker typed in this request or one of
their last few questions, or else the wallet they saved (`ETH_WALLET`, read
through `asker_values`). Nothing retrieved, and nothing somebody else said, is
ever a candidate. The source is stored with the alert, so forgetting the saved
wallet removes exactly the alerts that relied on it.

Every reply is fixed text in the asker's language, English or Portuguese, and
the wallet address appears only where the asker alone reads it -- a direct
message -- as a saved wallet does.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

import structlog

from chatmemory.app.alert_intent import AlertIntent
from chatmemory.app.alerts import AlertService, fee_tier, number
from chatmemory.app.clock import Clock, utc_now
from chatmemory.app.language import Language, detect
from chatmemory.domain.identity import PersonRef
from chatmemory.ports.alerts import (
    CHAIN_NAMES,
    DEFAULT_ALERTS_PER_PERSON,
    MAX_THRESHOLD,
    MIN_THRESHOLD,
    AddressSource,
    AlertKind,
    AlertLanguage,
    AlertRefusal,
    AlertState,
    AlertTargets,
    HealthCandidate,
    LpCandidate,
    LpTarget,
    NewAlert,
    PositionAlert,
    TargetsRead,
)

log = structlog.get_logger()

EN, PT = AlertLanguage.ENGLISH, AlertLanguage.PORTUGUESE

_TEXT: dict[AlertLanguage, dict[str, str]] = {
    EN: {
        "unavailable": (
            "Alerts aren't available on this deployment, so I can't watch that for you."
        ),
        "no_address": (
            "Which wallet? Save yours with `my wallet is 0x…` or put the address in "
            "the request, and ask again."
        ),
        "need_limit": (
            "Below what health factor? For example: `alert me if my health factor "
            "drops below 1.3`. The limit can be between {low} and {high}."
        ),
        "out_of_bounds": (
            "I can watch a health factor limit between {low} and {high}, and {value} "
            "is outside that. Below {low} the message would arrive about when the "
            "liquidation does."
        ),
        "at_cap": (
            "You already have {cap} alerts, the most I keep for one person. "
            "`/alert delete` stops one."
        ),
        "no_positions": "I found no open Uniswap positions{where}, so there's nothing to watch.",
        "no_position": "I don't see an open position #{token_id}{where}.",
        "no_debt": "There's no Aave debt{where}, so there's no health factor to watch.",
        "unreadable": (
            "I couldn't read the chain just now, so I can't tell what to watch. "
            "Try again in a moment."
        ),
        "already": "I'm already watching all of that. `/alert list` shows your alerts.",
        "where": " on {chain}",
        "heading": "Here's what I'll watch{wallet}:",
        "wallet": " for `{address}`",
        "health_line": "• **{chain}** — Aave health factor below **{limit}** · now **{now}**",
        "health_below": ", already below: I'll message you when it recovers",
        "lp_line": (
            "• **{chain}** — {position}, range {low} – {high} {quote} per {base} · now "
            "{state} at {price}"
        ),
        "lp_in": "in range",
        "lp_out": "out of range",
        "lp_out_note": ": I'll message you when it's back in range",
        "later": "Positions you open later aren't covered; ask again after opening one.",
        "unreachable": "I couldn't read {chains} just now, so nothing there is included.",
        "incomplete": "Some positions on {chains} couldn't be listed, so they may be missing.",
        "duplicates": "{count} of these are already being watched, so they're left out.",
        "cut": "Only {count} fit under your limit of {cap} alerts; `/alert delete` frees one.",
        "footer": (
            "I check every {minutes} minutes and message you once each time this "
            "changes; it is not liquidation protection. Press **Confirm** to start, "
            "or **Cancel**."
        ),
        "created": "Done. I'm watching:",
        "created_line": "• **{id}** — {label}",
        "created_duplicates": "{count} were already being watched.",
        "created_cap": "{count} didn't fit under your limit of {cap} alerts.",
        "created_failed": "{count} couldn't be saved.",
        "created_none": "Nothing new was set up.",
        "created_tail": "`/alert list` shows them; `/alert delete` stops one.",
        "unknown_person": (
            "I couldn't set that up: I have no record of you yet. Save your wallet "
            "with `my wallet is 0x…` and ask again."
        ),
        "health_label": "Aave health factor on {chain} below {limit}",
        "lp_label": "{position} on {chain}",
    },
    PT: {
        "unavailable": (
            "Alertas não estão disponíveis nesta instalação, então não consigo "
            "acompanhar isso para você."
        ),
        "no_address": (
            "Qual carteira? Salve a sua com `minha carteira é 0x…` ou coloque o "
            "endereço no pedido, e peça de novo."
        ),
        "need_limit": (
            "Abaixo de qual health factor? Por exemplo: `me avisa se o health factor "
            "cair abaixo de 1,3`. O limite pode ser entre {low} e {high}."
        ),
        "out_of_bounds": (
            "Consigo acompanhar um limite de health factor entre {low} e {high}, e "
            "{value} está fora disso. Abaixo de {low} a mensagem chegaria mais ou "
            "menos junto com a liquidação."
        ),
        "at_cap": (
            "Você já tem {cap} alertas, o máximo que eu mantenho por pessoa. "
            "`/alert delete` remove um."
        ),
        "no_positions": (
            "Não encontrei posições abertas na Uniswap{where}, então não há o que acompanhar."
        ),
        "no_position": "Não encontrei uma posição aberta #{token_id}{where}.",
        "no_debt": "Não há dívida no Aave{where}, então não há health factor para acompanhar.",
        "unreadable": (
            "Não consegui ler a blockchain agora, então não sei o que acompanhar. "
            "Tente de novo em instantes."
        ),
        "already": "Eu já acompanho tudo isso. `/alert list` mostra seus alertas.",
        "where": " na {chain}",
        "heading": "Isto é o que vou acompanhar{wallet}:",
        "wallet": " na carteira `{address}`",
        "health_line": (
            "• **{chain}** — health factor no Aave abaixo de **{limit}** · agora **{now}**"
        ),
        "health_below": ", já abaixo: te aviso quando ele se recuperar",
        "lp_line": (
            "• **{chain}** — {position}, faixa {low} – {high} {quote} por {base} · agora "
            "{state} em {price}"
        ),
        "lp_in": "dentro da faixa",
        "lp_out": "fora da faixa",
        "lp_out_note": ": te aviso quando voltar para a faixa",
        "later": "Posições que você abrir depois não entram; peça de novo depois de abrir uma.",
        "unreachable": "Não consegui ler {chains} agora, então nada de lá está incluído.",
        "incomplete": (
            "Algumas posições na {chains} não puderam ser listadas, então podem estar faltando."
        ),
        "duplicates": "{count} delas já estão sendo acompanhadas, então ficaram de fora.",
        "cut": "Só {count} cabem no seu limite de {cap} alertas; `/alert delete` libera espaço.",
        "footer": (
            "Eu verifico a cada {minutes} minutos e te mando uma mensagem cada vez que "
            "isso mudar; não é proteção contra liquidação. Aperte **Confirmar** para "
            "começar, ou **Cancelar**."
        ),
        "created": "Pronto. Estou acompanhando:",
        "created_line": "• **{id}** — {label}",
        "created_duplicates": "{count} já estavam sendo acompanhados.",
        "created_cap": "{count} não couberam no seu limite de {cap} alertas.",
        "created_failed": "{count} não puderam ser salvos.",
        "created_none": "Nada novo foi criado.",
        "created_tail": "`/alert list` mostra todos; `/alert delete` remove um.",
        "unknown_person": (
            "Não consegui criar isso: ainda não tenho registro de você. Salve sua "
            "carteira com `minha carteira é 0x…` e peça de novo."
        ),
        "health_label": "health factor no Aave na {chain} abaixo de {limit}",
        "lp_label": "{position} na {chain}",
    },
}


def text(key: str, language: AlertLanguage, **values: object) -> str:
    return _TEXT[language][key].format(**values)


_PORTUGUESE_REQUEST = re.compile(
    r"\b(?:avis\w*|aviz\w*|quando|abaixo|cair|sair|faixa|posi[cç]\w*|minha|meu|"
    r"carteira|alerta|notifique|notifica)\b",
    re.IGNORECASE,
)


def alert_language(message: str, preferred: str | None) -> AlertLanguage:
    """The language the reply and the alert's messages are written in.

    The message's own language first, as every fixed reply here does. A short
    request often has too few function words for `detect` -- "me avisa se o
    health factor cair abaixo de 1,3" has none it counts -- so the request's
    own Portuguese verbs decide next; then the language the person asked to be
    answered in; then English.
    """
    detected = detect(message)
    if detected is Language.PORTUGUESE:
        return PT
    if detected is Language.ENGLISH:
        return EN
    if _PORTUGUESE_REQUEST.search(message):
        return PT
    if preferred and preferred.strip().lower().startswith(("pt", "portug")):
        return PT
    return EN


def alerts_unavailable(language: AlertLanguage) -> str:
    return text("unavailable", language)


def position_name(lp: LpTarget, language: AlertLanguage) -> str:
    """"Uniswap v3 WETH/USDC 0.05% #4558452", in the reader's notation."""
    pair = f"{lp.token0_symbol}/{lp.token1_symbol}"
    return f"{lp.protocol.label} {pair} {fee_tier(lp.fee, language)} #{lp.token_id}"


def target_label(
    kind: AlertKind,
    chain: str,
    lp: LpTarget | None,
    threshold: Decimal | None,
    language: AlertLanguage,
) -> str:
    """What an alert watches, in one line, for a confirmation or a listing."""
    name = CHAIN_NAMES.get(chain, chain)
    if kind is AlertKind.LP_RANGE and lp is not None:
        return text("lp_label", language, position=position_name(lp, language), chain=name)
    limit = number(threshold or Decimal(0), language)
    return text("health_label", language, chain=name, limit=limit)


# --- what a proposal holds ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AlertProposal:
    """Alerts to create if, and only if, `person` presses Confirm.

    `text` is the confirmation as they read it.
    """

    person: PersonRef
    language: AlertLanguage
    alerts: tuple[NewAlert, ...]
    text: str


@dataclass(frozen=True, slots=True)
class AlertReply:
    """What a recognised request is answered with. A proposal only when there
    is something to confirm."""

    text: str
    proposal: AlertProposal | None = None


@dataclass(frozen=True, slots=True)
class _Proposed:
    alert: NewAlert
    line: str


@dataclass(frozen=True, slots=True)
class _Request:
    """One request, settled: whose, where to read, and how to say it."""

    person: PersonRef
    intent: AlertIntent
    address: str
    source: AddressSource
    language: AlertLanguage
    direct: bool


class AlertRequests:
    """Proposing alerts from a request, and creating them on confirmation."""

    def __init__(
        self,
        service: AlertService,
        targets: AlertTargets,
        *,
        cap: int = DEFAULT_ALERTS_PER_PERSON,
        sweep_seconds: float = 300.0,
        clock: Clock = utc_now,
    ) -> None:
        self._service = service
        self._targets = targets
        self._cap = cap
        self._minutes = max(1, round(sweep_seconds / 60))
        self._clock = clock

    # --- proposing -----------------------------------------------------------

    async def propose(
        self,
        person: PersonRef,
        intent: AlertIntent,
        *,
        saved_wallet: str | None,
        language: AlertLanguage,
        direct: bool,
    ) -> AlertReply:
        """What would be watched, or why nothing can be. Stores nothing."""
        address = intent.address or saved_wallet
        if address is None:
            return AlertReply(text("no_address", language))
        refused = _limit_refusal(intent, language)
        if refused is not None:
            return AlertReply(refused)
        active = [a for a in await self._service.list_for(person) if a.active]
        room = self._cap - len(active)
        if room <= 0:
            return AlertReply(text("at_cap", language, cap=self._cap))
        source = AddressSource.TYPED if intent.address else AddressSource.SAVED
        request = _Request(person, intent, address, source, language, direct)
        read = await self._targets.read(address, intent.kind, intent.chain)
        log.info(
            "alerts.proposal_read",
            kind=str(intent.kind),
            source=str(source),
            found=len(read.lp) + len(read.health),
            unreachable=len(read.unreachable),
        )
        return self._reply(request, read, active, room)

    def _reply(
        self,
        request: _Request,
        read: TargetsRead,
        active: Sequence[PositionAlert],
        room: int,
    ) -> AlertReply:
        found = _proposed(request, read)
        if not found:
            return AlertReply(_nothing_found(request, read))
        taken = {_key(a) for a in active}
        fresh = [p for p in found if _key(p.alert) not in taken]
        if not fresh:
            return AlertReply(text("already", request.language))
        kept = fresh[:room]
        notes = _notes(
            request,
            read,
            duplicates=len(found) - len(fresh),
            cut=len(fresh) > len(kept),
            kept=len(kept),
            cap=self._cap,
        )
        body = self._confirmation(request, kept, notes)
        proposal = AlertProposal(
            person=request.person,
            language=request.language,
            alerts=tuple(p.alert for p in kept),
            text=body,
        )
        return AlertReply(body, proposal)

    def _confirmation(self, request: _Request, kept: Sequence[_Proposed], notes: list[str]) -> str:
        lang = request.language
        # The wallet only where the asker alone reads it, as a saved wallet is.
        wallet = text("wallet", lang, address=request.address) if request.direct else ""
        lines = [text("heading", lang, wallet=wallet), *(p.line for p in kept)]
        if notes:
            lines += ["", *notes]
        lines += ["", text("footer", lang, minutes=self._minutes)]
        return "\n".join(lines)

    # --- confirming ----------------------------------------------------------

    async def confirm(self, proposal: AlertProposal) -> str:
        """Create what the proposal listed, and say what was created.

        Duplicates and the cap are enforced again by the store: another
        request may have been confirmed between this one's proposal and its
        press.
        """
        lang = proposal.language
        created: list[PositionAlert] = []
        refusals: list[AlertRefusal] = []
        for alert in proposal.alerts:
            result = await self._service.create(alert, self._clock())
            if result.alert is not None:
                created.append(result.alert)
            elif result.refusal is not None:
                refusals.append(result.refusal)
        log.info("alerts.confirmed", created=len(created), refused=len(refusals))
        if not created and AlertRefusal.UNKNOWN_PERSON in refusals:
            return text("unknown_person", lang)
        return _created_text(created, refusals, lang, self._cap)

    # --- listing and deleting ------------------------------------------------

    async def list_for(self, person: PersonRef) -> Sequence[PositionAlert]:
        return await self._service.list_for(person)

    async def delete(self, person: PersonRef, alert_id: int) -> bool:
        return await self._service.delete(person, alert_id)


def _limit_refusal(intent: AlertIntent, language: AlertLanguage) -> str | None:
    if intent.kind is not AlertKind.AAVE_HEALTH:
        return None
    low, high = number(MIN_THRESHOLD, language), number(MAX_THRESHOLD, language)
    if intent.threshold is None:
        return text("need_limit", language, low=low, high=high)
    if not MIN_THRESHOLD <= intent.threshold <= MAX_THRESHOLD:
        value = number(intent.threshold, language)
        return text("out_of_bounds", language, low=low, high=high, value=value)
    return None


def _key(alert: NewAlert | PositionAlert) -> tuple[object, ...]:
    """What makes two alerts the same watch, as the store's unique index says."""
    token = alert.lp.token_id if alert.lp is not None else -1
    return (alert.kind, alert.chain, alert.address.lower(), token, alert.threshold or 0)


def _proposed(request: _Request, read: TargetsRead) -> list[_Proposed]:
    if request.intent.kind is AlertKind.AAVE_HEALTH:
        return [_health(request, c) for c in read.health if c.health_factor is not None]
    wanted = request.intent.token_id
    return [_lp(request, c) for c in read.lp if wanted is None or c.target.token_id == wanted]


def _new(
    request: _Request,
    chain: str,
    *,
    state: AlertState,
    last_value: Decimal,
    threshold: Decimal | None = None,
    lp: LpTarget | None = None,
) -> NewAlert:
    return NewAlert(
        person=request.person,
        kind=request.intent.kind,
        chain=chain,
        address=request.address,
        address_source=request.source,
        language=request.language,
        lp=lp,
        threshold=threshold,
        state=state,
        last_value=last_value,
    )


def _health(request: _Request, found: HealthCandidate) -> _Proposed:
    lang, limit = request.language, request.intent.threshold or MIN_THRESHOLD
    health = found.health_factor or Decimal(0)
    state = AlertState.BELOW if health < limit else AlertState.OK
    line = text(
        "health_line",
        lang,
        chain=CHAIN_NAMES.get(found.chain, found.chain),
        limit=number(limit, lang),
        now=number(health, lang),
    )
    if state is AlertState.BELOW:
        line += text("health_below", lang)
    alert = _new(request, found.chain, threshold=limit, state=state, last_value=health)
    return _Proposed(alert, line)


def _lp(request: _Request, found: LpCandidate) -> _Proposed:
    lang = request.language
    out = found.state is AlertState.OUT_OF_RANGE
    line = text(
        "lp_line",
        lang,
        chain=CHAIN_NAMES.get(found.chain, found.chain),
        position=position_name(found.target, lang),
        low=number(found.price_lower, lang),
        high=number(found.price_upper, lang),
        quote=found.quote_symbol,
        base=found.base_symbol,
        state=text("lp_out" if out else "lp_in", lang),
        price=number(found.price, lang),
    )
    if out:
        line += text("lp_out_note", lang)
    alert = _new(
        request, found.chain, lp=found.target, state=found.state, last_value=Decimal(found.tick)
    )
    return _Proposed(alert, line)


def _chains(keys: Sequence[str]) -> str:
    return ", ".join(CHAIN_NAMES.get(k, k) for k in keys)


def _nothing_found(request: _Request, read: TargetsRead) -> str:
    lang, intent = request.language, request.intent
    everything_failed = not (read.lp or read.health) and read.unreachable
    if everything_failed:
        return text("unreadable", lang)
    where = text("where", lang, chain=_chains([intent.chain])) if intent.chain else ""
    if intent.kind is AlertKind.AAVE_HEALTH:
        return text("no_debt", lang, where=where)
    if intent.token_id is not None and read.lp:
        return text("no_position", lang, token_id=intent.token_id, where=where)
    return text("no_positions", lang, where=where)


def _notes(
    request: _Request, read: TargetsRead, *, duplicates: int, cut: bool, kept: int, cap: int
) -> list[str]:
    """Everything the list above leaves out, said rather than implied."""
    lang = request.language
    notes: list[str] = []
    if request.intent.kind is AlertKind.LP_RANGE:
        notes.append(text("later", lang))
    if read.unreachable:
        notes.append(text("unreachable", lang, chains=_chains(read.unreachable)))
    if read.incomplete:
        notes.append(text("incomplete", lang, chains=_chains(read.incomplete)))
    if duplicates:
        notes.append(text("duplicates", lang, count=duplicates))
    if cut:
        notes.append(text("cut", lang, count=kept, cap=cap))
    return notes


def _created_text(
    created: Sequence[PositionAlert],
    refusals: Sequence[AlertRefusal],
    lang: AlertLanguage,
    cap: int,
) -> str:
    lines = [text("created", lang)] if created else [text("created_none", lang)]
    for alert in created:
        label = target_label(alert.kind, alert.chain, alert.lp, alert.threshold, lang)
        lines.append(text("created_line", lang, id=alert.id, label=label))
    counts = {
        "created_duplicates": sum(r is AlertRefusal.DUPLICATE for r in refusals),
        "created_cap": sum(r is AlertRefusal.AT_CAP for r in refusals),
        "created_failed": sum(
            r not in (AlertRefusal.DUPLICATE, AlertRefusal.AT_CAP) for r in refusals
        ),
    }
    notes = [text(key, lang, count=n, cap=cap) for key, n in counts.items() if n]
    if notes:
        lines += ["", *notes]
    lines += ["", text("created_tail", lang)]
    return "\n".join(lines)
