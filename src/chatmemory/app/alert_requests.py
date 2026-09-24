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

A price alert has no wallet and reads no chain: its proposal is the price now,
from the same source the sweep will read (`PriceFeed`), and the direction when
the request gave only a level ("when BTC hits 100k") is the side the price is
not on yet. A range alert with an edge distance shows how far the position is
from its nearer edge now, in the orientation the range is quoted in.

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
from chatmemory.app.alerts import (
    EDGE_NAMES,
    AlertService,
    dollars,
    fee_tier,
    level,
    number,
    percent,
    quoted_at,
)
from chatmemory.app.clock import Clock, utc_now
from chatmemory.app.language import Language, detect
from chatmemory.domain.chain import suffix_list
from chatmemory.domain.identity import PersonRef
from chatmemory.ports.alerts import (
    CHAIN_NAMES,
    DEFAULT_ALERTS_PER_PERSON,
    MAX_EDGE_PERCENT,
    MAX_THRESHOLD,
    MIN_EDGE_PERCENT,
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
    PriceDirection,
    PriceFeed,
    PriceObservation,
    PriceTarget,
    TargetsRead,
    edge_distance,
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
        "which_wallet": (
            "You have several wallets saved ({wallets}). Which one? Ask again with "
            "its last four characters, or with the address."
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
        "edge_label": "{position} on {chain}, warning within {within} of an edge",
        "price_label": "{asset} {direction} {level}",
        "above": "above",
        "below": "below",
        "edge_out_of_bounds": (
            "I can warn you between {low} and {high} from a range edge, and {value} is "
            "outside that."
        ),
        "lp_edge": " · **{distance} from the {edge} edge**; I'll warn you within {within}",
        "lp_edge_now": " (already that near: I'll warn you after it moves away and back)",
        "price_unavailable": (
            "Price alerts aren't available on this deployment, so I can't watch that for you."
        ),
        "need_level": (
            "At what price? For example: `alert me when BTC goes above 100k` or "
            "`me avisa se o ETH cair abaixo de 2500`."
        ),
        "price_unreadable": (
            "I couldn't read the {asset} price just now, so I can't show where it is. "
            "Try again in a moment."
        ),
        "price_heading": "Here's what I'll watch:",
        "price_line": (
            "• **{asset}** {direction} **{level}** · now **{price}** ({source}, {time})"
        ),
        "price_there": (
            ", already there: I'll message you the next time it crosses, after it has "
            "come back"
        ),
        "price_footer": (
            "I check every {minutes} minutes and message you once when the price "
            "crosses the level, and again only after it has crossed back. Press "
            "**Confirm** to start, or **Cancel**."
        ),
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
        "which_wallet": (
            "Você tem várias carteiras salvas ({wallets}). Qual delas? Peça de novo "
            "com os quatro últimos caracteres dela, ou com o endereço."
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
        "edge_label": "{position} na {chain}, aviso a {within} de uma borda",
        "price_label": "{asset} {direction} {level}",
        "above": "acima de",
        "below": "abaixo de",
        "edge_out_of_bounds": (
            "Consigo avisar entre {low} e {high} de uma borda da faixa, e {value} "
            "está fora disso."
        ),
        "lp_edge": " · **a {distance} da borda {edge}**; te aviso a {within}",
        "lp_edge_now": " (já está perto assim: te aviso depois que se afastar e voltar)",
        "price_unavailable": (
            "Alertas de preço não estão disponíveis nesta instalação, então não consigo "
            "acompanhar isso para você."
        ),
        "need_level": (
            "Em qual preço? Por exemplo: `me avisa quando o BTC passar de 100k` ou "
            "`me avisa se o ETH cair abaixo de 2500`."
        ),
        "price_unreadable": (
            "Não consegui ler o preço do {asset} agora, então não sei onde ele está. "
            "Tente de novo em instantes."
        ),
        "price_heading": "Isto é o que vou acompanhar:",
        "price_line": (
            "• **{asset}** {direction} **{level}** · agora **{price}** ({source}, {time})"
        ),
        "price_there": (
            ", já está lá: te aviso na próxima vez que cruzar, depois de voltar"
        ),
        "price_footer": (
            "Eu verifico a cada {minutes} minutos e te mando uma mensagem quando o "
            "preço cruzar o nível, e de novo só depois que ele voltar. Aperte "
            "**Confirmar** para começar, ou **Cancelar**."
        ),
    },
}


def text(key: str, language: AlertLanguage, **values: object) -> str:
    return _TEXT[language][key].format(**values)


_PORTUGUESE_REQUEST = re.compile(
    r"\b(?:avis\w*|aviz\w*|quando|abaixo|acima|cair|sair|passar|faixa|borda|"
    r"posi[cç]\w*|minha|meu|carteira|alerta|notifique|notifica)\b",
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
    chain: str | None,
    lp: LpTarget | None,
    threshold: Decimal | None,
    language: AlertLanguage,
    *,
    price: PriceTarget | None = None,
    edge_percent: Decimal | None = None,
) -> str:
    """What an alert watches, in one line, for a confirmation or a listing."""
    if kind is AlertKind.PRICE and price is not None:
        return price_label(price, language)
    name = CHAIN_NAMES.get(chain or "", chain or "")
    if kind is AlertKind.LP_RANGE and lp is not None:
        position = position_name(lp, language)
        if edge_percent is None:
            return text("lp_label", language, position=position, chain=name)
        within = percent(edge_percent, language)
        return text("edge_label", language, position=position, chain=name, within=within)
    limit = number(threshold or Decimal(0), language)
    return text("health_label", language, chain=name, limit=limit)


def alert_label(alert: NewAlert | PositionAlert, language: AlertLanguage) -> str:
    """`target_label` for a whole alert, whatever its kind."""
    return target_label(
        alert.kind,
        alert.chain,
        alert.lp,
        alert.threshold,
        language,
        price=alert.price,
        edge_percent=alert.edge_percent,
    )


def price_label(price: PriceTarget, language: AlertLanguage) -> str:
    """"BTC above $100,000", "ETH abaixo de US$ 2.500"."""
    return text(
        "price_label",
        language,
        asset=price.asset,
        direction=text(str(price.direction), language),
        level=level(price.level, language),
    )


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
        prices: PriceFeed | None = None,
        cap: int = DEFAULT_ALERTS_PER_PERSON,
        sweep_seconds: float = 300.0,
        clock: Clock = utc_now,
    ) -> None:
        self._service = service
        self._targets = targets
        self._prices = prices
        self._cap = cap
        self._minutes = max(1, round(sweep_seconds / 60))
        self._clock = clock

    # --- proposing -----------------------------------------------------------

    async def propose(
        self,
        person: PersonRef,
        intent: AlertIntent,
        *,
        saved_wallets: Sequence[str] = (),
        language: AlertLanguage,
        direct: bool,
    ) -> AlertReply:
        """What would be watched, or why nothing can be. Stores nothing.

        `saved_wallets` are the asker's saved Ethereum addresses the request
        may use. With several and none typed, the asker is asked which: an
        alert on the wrong wallet is a standing watch nobody wanted.
        """
        if intent.kind is AlertKind.PRICE:
            return await self._propose_price(person, intent, language)
        address = intent.address or (saved_wallets[0] if len(saved_wallets) == 1 else None)
        if address is None and saved_wallets:
            wallets = suffix_list(tuple(saved_wallets))
            return AlertReply(text("which_wallet", language, wallets=wallets))
        if address is None:
            return AlertReply(text("no_address", language))
        refused = _limit_refusal(intent, language)
        if refused is not None:
            return AlertReply(refused)
        active = [a for a in await self._service.list_for(person) if a.active]
        room = self._cap - len(active)
        # An edge distance on a position already watched needs no new slot.
        if room <= 0 and intent.edge_percent is None:
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
        fresh = [p for p in found if not _covered(p.alert, active)]
        if not fresh:
            return AlertReply(text("already", request.language))
        # Adding an edge distance to a watched position takes no slot.
        watched = {_watch(a) for a in active}
        new = [p for p in fresh if _watch(p.alert) not in watched]
        admitted = new[: max(room, 0)]
        kept = [p for p in fresh if _watch(p.alert) in watched or p in admitted]
        if not kept:
            return AlertReply(text("at_cap", request.language, cap=self._cap))
        notes = _notes(
            request,
            read,
            duplicates=len(found) - len(fresh),
            cut=len(new) > len(admitted),
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

    async def _propose_price(
        self, person: PersonRef, intent: AlertIntent, language: AlertLanguage
    ) -> AlertReply:
        """A price level: no wallet, no chain, and the price now as the baseline."""
        if self._prices is None:
            return AlertReply(text("price_unavailable", language))
        if intent.level is None or intent.level <= 0 or intent.asset is None:
            return AlertReply(text("need_level", language))
        active = [a for a in await self._service.list_for(person) if a.active]
        if len(active) >= self._cap:
            return AlertReply(text("at_cap", language, cap=self._cap))
        quote = await self._price_now(intent.asset)
        if quote is None:
            return AlertReply(text("price_unreadable", language, asset=intent.asset))
        proposed = _price(person, intent.level, intent.direction, quote, language)
        if _covered(proposed.alert, active):
            return AlertReply(text("already", language))
        lines = [text("price_heading", language), proposed.line, ""]
        lines.append(text("price_footer", language, minutes=self._minutes))
        body = "\n".join(lines)
        return AlertReply(body, AlertProposal(person, language, (proposed.alert,), body))

    async def _price_now(self, asset: str) -> PriceObservation | None:
        if self._prices is None:
            return None
        try:
            return (await self._prices.latest()).get(asset)
        except Exception as exc:  # noqa: BLE001 - an unreadable price is said, not raised
            log.warning("alerts.price_unreadable", error=str(exc)[:200])
            return None

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
    if intent.kind is AlertKind.LP_RANGE:
        return _edge_refusal(intent.edge_percent, language)
    if intent.kind is not AlertKind.AAVE_HEALTH:
        return None
    low, high = number(MIN_THRESHOLD, language), number(MAX_THRESHOLD, language)
    if intent.threshold is None:
        return text("need_limit", language, low=low, high=high)
    if not MIN_THRESHOLD <= intent.threshold <= MAX_THRESHOLD:
        value = number(intent.threshold, language)
        return text("out_of_bounds", language, low=low, high=high, value=value)
    return None


def _edge_refusal(edge: Decimal | None, language: AlertLanguage) -> str | None:
    if edge is None or MIN_EDGE_PERCENT <= edge <= MAX_EDGE_PERCENT:
        return None
    low, high = percent(MIN_EDGE_PERCENT, language), percent(MAX_EDGE_PERCENT, language)
    value = percent(edge, language)
    return text("edge_out_of_bounds", language, low=low, high=high, value=value)


def _watch(alert: NewAlert | PositionAlert) -> tuple[object, ...]:
    """What makes two alerts the same watch, as the store's unique index says."""
    token = alert.lp.token_id if alert.lp is not None else -1
    address = (alert.address or "").lower()
    return (alert.kind, alert.chain, address, token, alert.threshold or 0, alert.price)


def _covered(alert: NewAlert, active: Sequence[PositionAlert]) -> bool:
    """An active alert already does what this one would.

    The edge distance counts only when this one has one: asking for a warning
    near the edge of a position already watched is offered, and the store then
    adds the distance to the existing alert; a plain range request for a
    position with an edge warning is already covered by it.
    """
    same = [a for a in active if _watch(a) == _watch(alert)]
    if alert.edge_percent is None:
        return bool(same)
    return any(a.edge_percent == alert.edge_percent for a in same)


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
    edge_percent: Decimal | None = None,
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
        edge_percent=edge_percent,
        state=state,
        last_value=last_value,
    )


def _side(direction: PriceDirection, level_usd: Decimal, price: Decimal) -> AlertState:
    """The side of the level the price is on, as `price_state` counts it."""
    if direction is PriceDirection.ABOVE:
        return AlertState.ABOVE if price >= level_usd else AlertState.BELOW
    return AlertState.BELOW if price <= level_usd else AlertState.ABOVE


def _price(
    person: PersonRef,
    level_usd: Decimal,
    direction: PriceDirection | None,
    quote: PriceObservation,
    lang: AlertLanguage,
) -> _Proposed:
    # "When BTC hits 100k" names no side: it is the one the price is not on.
    chosen = direction or (
        PriceDirection.ABOVE if level_usd > quote.price else PriceDirection.BELOW
    )
    target = PriceTarget(quote.asset, chosen, level_usd)
    state = _side(chosen, level_usd, quote.price)
    line = text(
        "price_line",
        lang,
        asset=quote.asset,
        direction=text(str(chosen), lang),
        level=level(level_usd, lang),
        price=dollars(quote.price, lang),
        source=quote.source,
        time=quoted_at(quote.as_of),
    )
    if state is chosen.state:
        line += text("price_there", lang)
    alert = NewAlert(
        person=person,
        kind=AlertKind.PRICE,
        chain=None,
        address=None,
        address_source=None,
        language=lang,
        price=target,
        state=state,
        last_value=quote.price,
    )
    return _Proposed(alert, line)


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
    edge = request.intent.edge_percent
    state = found.state
    if edge is not None and not out:
        state, note = _edge_note(found, edge, lang)
        line += note
    alert = _new(
        request,
        found.chain,
        lp=found.target,
        state=state,
        last_value=Decimal(found.tick),
        edge_percent=edge,
    )
    return _Proposed(alert, line)


def _edge_note(found: LpCandidate, edge: Decimal, lang: AlertLanguage) -> tuple[AlertState, str]:
    """The baseline against the edge distance, and how far the nearer edge is now."""
    near = edge_distance(found.price, found.price_lower, found.price_upper)
    note = text(
        "lp_edge",
        lang,
        distance=percent(near.percent, lang),
        edge=EDGE_NAMES[lang][near.edge],
        within=percent(edge, lang),
    )
    if near.percent <= edge:
        return AlertState.NEAR_EDGE, note + text("lp_edge_now", lang)
    return AlertState.IN_RANGE, note


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
        label = alert_label(alert, lang)
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
