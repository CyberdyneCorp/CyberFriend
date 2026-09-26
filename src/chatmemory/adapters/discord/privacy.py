"""What `/privacy` says: everything held about the person, and what is kept.

Two views of one report. In a server channel the reply is private to the
person and shows counts and fact kinds only -- it is rendered from
`InventorySummary`, which has no field that could carry a value -- with a
button that sends the details by direct message. In a direct message it shows
the values: fact values, the latest remembered questions, task texts, alert
addresses by suffix, suggestion texts and token labels.

Both carry the same statements, in the person's language: how long questions
and answers are recorded and that admins can read them, and the list of what
survives [Delete everything...]: a minimal person record with the name and
preferences cleared, and an anonymous voice total (`app.erasure`). The
trace count is "at least": traces exported before migration 0029 carry no
asker, so they cannot be counted per person although Langfuse may still hold
them until TRACE_RETENTION_DAYS. Every period in them comes from `RetentionFacts`, so
none is written here.

Both carry [Delete everything...] when the process can erase. It deletes
nothing itself: it shows what is deleted, the kept list and what each of the
two choices means, then two buttons, each of which asks for a typed word
(DELETE, or APAGAR in Portuguese) before anything happens. Only the person who
asked can press any of it, and the choice expires after two minutes.

Discord caps a message at 2000 characters, so the report is sent as embeds,
one per section, split over as many messages ("pages") as the embed limits
need.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

import discord
import structlog

from chatmemory.adapters.discord.schedule_replies import outcome_label
from chatmemory.adapters.discord.suggestions import status_label
from chatmemory.adapters.discord.views import RequesterOnlyView
from chatmemory.app.fact_replies import label, shown_line
from chatmemory.app.language import Language
from chatmemory.app.privacy import PrivacyReport, RetentionFacts
from chatmemory.domain.chain import SUFFIX_CHARS
from chatmemory.ports.feature_requests import RequestStatus
from chatmemory.ports.privacy import (
    ArchivedChannel,
    ErasureMode,
    ErasureRequest,
    HeldAlert,
    HeldTask,
    Inventory,
    InventorySummary,
    MediaCounts,
    MemoryCounts,
    NotificationSetting,
)
from chatmemory.ports.schedules import TaskOutcome

log = structlog.get_logger()

DETAILS_WINDOW_SECONDS = 300.0
"""How long [Send me the details] stays live under the channel reply."""

EMBED_DESCRIPTION_CHARS = 4000
"""Under Discord's 4096 per embed description, leaving room for "and N more"."""

MESSAGE_EMBED_CHARS = 5500
"""Under Discord's 6000 across one message's embeds."""

MESSAGE_EMBEDS = 10
"""Discord's limit of embeds on one message."""

LISTED_TEXT_CHARS = 100
"""How much of a question, task or suggestion the DM view shows."""

EN, PT = Language.ENGLISH, Language.PORTUGUESE

_TEXT: dict[str, dict[Language, str]] = {
    "intro_channel": {
        EN: (
            "Here's what I hold about you. Only you can see this. Values are shown "
            "only in a direct message: press the button to get the details there."
        ),
        PT: (
            "Isto é o que eu guardo sobre você. Só você vê esta mensagem. Os valores "
            "só aparecem em mensagem direta: aperte o botão para recebê-los lá."
        ),
    },
    "intro_direct": {
        EN: "Here's everything I hold about you.",
        PT: "Isto é tudo o que eu guardo sobre você.",
    },
    "unavailable": {
        EN: "I can't show what I hold about you here - this deployment has no storage for it.",
        PT: (
            "Não consigo mostrar o que guardo sobre você aqui - esta instalação não "
            "tem onde guardar."
        ),
    },
    "details": {EN: "Send me the details", PT: "Me mande os detalhes"},
    "details_sent": {
        EN: "Sent the details to your direct messages.",
        PT: "Mandei os detalhes na sua mensagem direta.",
    },
    "details_failed": {
        EN: "I couldn't message you. Open a direct message with me and use `/privacy` there.",
        PT: "Não consegui te mandar mensagem. Abra uma mensagem direta comigo e use `/privacy` lá.",
    },
    "not_yours": {
        EN: "Only the person who asked can get these details.",
        PT: "Só quem pediu pode receber estes detalhes.",
    },
    "none": {EN: "None.", PT: "Nenhum."},
    "more": {EN: "…and {count} more.", PT: "…e mais {count}."},
    # Section titles.
    "facts": {EN: "Personal details", PT: "Dados pessoais"},
    "memory": {EN: "Remembered conversation", PT: "Conversa lembrada"},
    "tasks": {EN: "Scheduled questions", PT: "Perguntas agendadas"},
    "alerts": {EN: "Alerts", PT: "Alertas"},
    "notifications": {EN: "Notifications", PT: "Notificações"},
    "voice": {EN: "Voice and audio", PT: "Voz e áudio"},
    "media": {EN: "Attachments", PT: "Anexos"},
    "suggestions": {EN: "Suggestions", PT: "Sugestões"},
    "tokens": {EN: "Access tokens", PT: "Tokens de acesso"},
    "archive": {EN: "Archived messages", PT: "Mensagens arquivadas"},
    "traces": {EN: "Questions and answers recorded", PT: "Perguntas e respostas registradas"},
    "accounts": {EN: "Linked accounts", PT: "Contas vinculadas"},
    "kept": {
        EN: "Kept even when your data is deleted",
        PT: "Mantido mesmo quando seus dados são apagados",
    },
    # Lines.
    "fact_kinds": {EN: "Saved: {kinds}.", PT: "Salvos: {kinds}."},
    "memory_counts": {
        EN: (
            "{direct} questions and answers in direct messages, {channel} in server "
            "channels, and {summaries} summaries of older ones."
        ),
        PT: (
            "{direct} perguntas e respostas em mensagens diretas, {channel} em canais "
            "do servidor, e {summaries} resumos das mais antigas."
        ),
    },
    "memory_expiry": {
        EN: "Deleted after {days} days, or now with `/forget`.",
        PT: "Apagado depois de {days} dias, ou agora com `/forget`.",
    },
    "recent": {EN: "Your latest questions:", PT: "Suas últimas perguntas:"},
    "count": {EN: "{count}.", PT: "{count}."},
    "task_line": {
        EN: "**{id}** - every {hours}h - {question} ({state})",
        PT: "**{id}** - a cada {hours}h - {question} ({state})",
    },
    "task_stopped": {EN: "stopped", PT: "parada"},
    "task_not_run": {EN: "not run yet", PT: "ainda não rodou"},
    "notify_on": {
        EN: "Direct messages about things asked of you: on.",
        PT: "Mensagens diretas sobre o que pedem a você: ligadas.",
    },
    "notify_default": {
        EN: "Direct messages about things asked of you: on (you never changed it).",
        PT: "Mensagens diretas sobre o que pedem a você: ligadas (você nunca mudou).",
    },
    "notify_off": {
        EN: "Direct messages about things asked of you: off.",
        PT: "Mensagens diretas sobre o que pedem a você: desligadas.",
    },
    "notify_queued": {EN: "Waiting to be sent: {count}.", PT: "Esperando envio: {count}."},
    "voice_minutes": {
        EN: "{minutes} minutes of your audio transcribed this month.",
        PT: "{minutes} minutos do seu áudio transcritos este mês.",
    },
    "media_count": {
        EN: "{count} on your archived messages in channels you can read.",
        PT: "{count} nas suas mensagens arquivadas em canais que você pode ler.",
    },
    "media_kinds": {
        EN: "{kinds}; {text} of them transcribed or described.",
        PT: "{kinds}; {text} deles transcritos ou descritos.",
    },
    "token_line": {EN: "{label} - created {created}", PT: "{label} - criado em {created}"},
    "token_unlabelled": {EN: "(no label)", PT: "(sem nome)"},
    "archiving_on": {
        EN: "I archive your messages in the channels I cover.",
        PT: "Eu arquivo suas mensagens nos canais que cubro.",
    },
    "archiving_off": {
        EN: "You've opted out: I don't archive, remember or trace anything you send.",
        PT: "Você optou por sair: não arquivo, não lembro e não registro nada do que você manda.",
    },
    "archived_none": {
        EN: "No messages of yours are archived in channels you can read.",
        PT: "Nenhuma mensagem sua está arquivada em canais que você pode ler.",
    },
    "archived_line": {EN: "<#{channel}>: {count} messages", PT: "<#{channel}>: {count} mensagens"},
    "archived_line_one": {EN: "<#{channel}>: 1 message", PT: "<#{channel}>: 1 mensagem"},
    "traces_on": {
        EN: (
            "Your questions to me and my answers are recorded for up to {days} days, "
            "and admins can read them."
        ),
        PT: (
            "Suas perguntas para mim e minhas respostas ficam registradas por até "
            "{days} dias, e os administradores podem lê-las."
        ),
    },
    "traces_off": {
        EN: "I don't record your questions and answers in this deployment.",
        PT: "Não registro suas perguntas e respostas nesta instalação.",
    },
    "traces_count": {
        EN: "Recorded now: at least {count} of your questions.",
        PT: "Registradas agora: pelo menos {count} perguntas suas.",
    },
    "kept_person": {
        EN: (
            "A minimal record of you: an internal id, your account ids, when you deleted "
            "your data and, if you chose it, that I must not archive you - so nothing "
            "from before is archived again. Your name and preferences are cleared."
        ),
        PT: (
            "Um registro mínimo seu: um id interno, os ids das suas contas, quando você "
            "apagou seus dados e, se você escolheu, que eu não devo arquivar você - para "
            "que nada de antes seja arquivado de novo. Seu nome e suas preferências são "
            "apagados."
        ),
    },
    "kept_audit": {
        EN: "Entries in the admin change log that refer to you. That log can't be edited.",
        PT: "Registros no histórico de alterações dos administradores que citam você. "
        "Esse histórico não pode ser editado.",
    },
    "kept_backups": {
        EN: "Database backups, until they age out after {days} days.",
        PT: "Backups do banco de dados, até expirarem depois de {days} dias.",
    },
    "no_backups": {
        EN: "No database backups are kept, so nothing stays behind in one.",
        PT: "Não são mantidos backups do banco de dados, então nada fica guardado em um.",
    },
    "kept_account": {
        EN: "A CyberdyneAuth account, if you link one: it can't be deleted from here yet.",
        PT: "Uma conta CyberdyneAuth, se você vincular uma: ela ainda não pode ser apagada "
        "por aqui.",
    },
    "kept_mentions": {
        EN: "Other people's messages that mention you. Only my index of the mention goes.",
        PT: "Mensagens de outras pessoas que citam você. Só o meu índice da menção é apagado.",
    },
    "kept_answers": {
        EN: (
            "Other people's remembered answers, which may paraphrase what you said. "
            "They expire within {days} days."
        ),
        PT: (
            "Respostas lembradas de outras pessoas, que podem parafrasear o que você "
            "disse. Elas expiram em até {days} dias."
        ),
    },
    "kept_sent": {
        EN: "Messages I already sent on Discord.",
        PT: "Mensagens que eu já mandei no Discord.",
    },
    "kept_voice": {
        EN: "An anonymous total of voice minutes for each month, with nothing tying it to "
        "you: it keeps the monthly limit true.",
        PT: "Um total anônimo de minutos de voz de cada mês, sem nada que o ligue a você: "
        "ele mantém o limite mensal correto.",
    },
    # Delete everything.
    "delete_entry": {EN: "Delete everything…", PT: "Apagar tudo…"},
    "erase_unavailable": {
        EN: "I can't delete anything here - this deployment has no storage for it.",
        PT: "Não consigo apagar nada aqui - esta instalação não tem onde guardar.",
    },
    "erase_confirm": {
        EN: (
            "**Delete everything I hold about you?**\n"
            "This deletes your archived messages and their attachments, your personal "
            "details, remembered conversation, scheduled questions, alerts, notifications, "
            "suggestions, access tokens and voice usage records, and schedules your recorded "
            "questions for deletion. **It can't be undone.**\n\n"
            "**Delete everything**: you can keep using CyberFriend. New messages are "
            "archived as usual; nothing from before now is re-imported.\n"
            "**Delete everything and stop archiving me**: CyberFriend will also stop "
            "archiving your messages, remembering facts and conversations, answering voice "
            "questions and tracing your questions."
        ),
        PT: (
            "**Apagar tudo o que eu guardo sobre você?**\n"
            "Isso apaga suas mensagens arquivadas e os anexos delas, seus dados pessoais, a "
            "conversa lembrada, perguntas agendadas, alertas, notificações, sugestões, tokens "
            "de acesso e registros de uso de voz, e agenda a exclusão das suas perguntas "
            "registradas. **Não dá para desfazer.**\n\n"
            "**Apagar tudo**: você pode continuar usando o CyberFriend. Mensagens novas são "
            "arquivadas normalmente; nada de antes de agora é importado de novo.\n"
            "**Apagar tudo e parar de me arquivar**: o CyberFriend também para de arquivar "
            "suas mensagens, de lembrar fatos e conversas, de responder perguntas por voz e "
            "de registrar suas perguntas."
        ),
    },
    "erase_button": {EN: "Delete everything", PT: "Apagar tudo"},
    "erase_leave_button": {
        EN: "Delete everything and stop archiving me",
        PT: "Apagar tudo e parar de me arquivar",
    },
    "erase_modal_title": {EN: "Delete everything", PT: "Apagar tudo"},
    "erase_modal_label": {EN: "Type {word} to confirm", PT: "Digite {word} para confirmar"},
    "erase_wrong_word": {
        EN: "That wasn't {word}, so nothing was deleted.",
        PT: "Isso não foi {word}, então nada foi apagado.",
    },
    "erase_failed": {
        EN: (
            "Something went wrong partway. If the deletion had started, I'll finish it "
            "within a few minutes; use `/privacy` to check."
        ),
        PT: (
            "Algo deu errado no meio. Se a exclusão já tinha começado, eu termino em alguns "
            "minutos; use `/privacy` para conferir."
        ),
    },
    "erase_done": {EN: "Done. I deleted:", PT: "Pronto. Eu apaguei:"},
    "erase_messages": {
        EN: "Messages in channels you can read: {messages}, and any of yours in other channels",
        PT: "Mensagens em canais que você pode ler: {messages}, e as suas em outros canais",
    },
    "erase_media": {EN: "Attachments on them: {count}", PT: "Anexos nelas: {count}"},
    "erase_facts": {EN: "Personal details: {count}", PT: "Dados pessoais: {count}"},
    "erase_memory": {
        EN: "Remembered questions, answers and summaries: {count}",
        PT: "Perguntas, respostas e resumos lembrados: {count}",
    },
    "erase_tasks": {EN: "Scheduled questions: {count}", PT: "Perguntas agendadas: {count}"},
    "erase_alerts": {EN: "Alerts: {count}", PT: "Alertas: {count}"},
    "erase_suggestions": {EN: "Suggestions: {count}", PT: "Sugestões: {count}"},
    "erase_tokens": {EN: "Access tokens: {count}", PT: "Tokens de acesso: {count}"},
    "erase_voice": {
        EN: "your voice usage records (the minutes stay only in an anonymous monthly total)",
        PT: "seus registros de uso de voz (os minutos ficam só num total mensal anônimo)",
    },
    "erase_traces": {
        EN: "Your recorded questions scheduled for deletion from the trace store: at least "
        "{count}.",
        PT: "Suas perguntas registradas agendadas para exclusão do registro: pelo menos "
        "{count}.",
    },
    "erase_stays": {
        EN: "You can keep using CyberFriend. New messages are archived as usual; nothing "
        "from before now is re-imported.",
        PT: "Você pode continuar usando o CyberFriend. Mensagens novas são arquivadas "
        "normalmente; nada de antes de agora é importado de novo.",
    },
    "erase_left": {
        EN: "I've also stopped archiving your messages, remembering facts and conversations, "
        "answering voice questions and tracing your questions.",
        PT: "Também parei de arquivar suas mensagens, de lembrar fatos e conversas, de "
        "responder perguntas por voz e de registrar suas perguntas.",
    },
}

_ALERT_KINDS: dict[Language, dict[str, str]] = {
    EN: {"lp_range": "LP range", "aave_health": "Aave health factor", "price": "price"},
    PT: {"lp_range": "faixa de LP", "aave_health": "health factor do Aave", "price": "preço"},
}

_MEDIA_KINDS: dict[Language, dict[str, str]] = {
    EN: {"voice": "voice notes", "audio": "audio files", "image": "images"},
    PT: {"voice": "mensagens de voz", "audio": "arquivos de áudio", "image": "imagens"},
}


def _lang(language: Language) -> Language:
    return PT if language is PT else EN


def text(key: str, language: Language, **values: object) -> str:
    return _TEXT[key][_lang(language)].format(**values)


@dataclass(frozen=True, slots=True)
class Section:
    """One embed: a title and its lines."""

    title: str
    lines: tuple[str, ...]

    def description(self, language: Language) -> str:
        """The lines, cut to fit one embed, saying how many were left out."""
        kept: list[str] = []
        used = 0
        for index, line in enumerate(self.lines):
            if used + len(line) + 1 > EMBED_DESCRIPTION_CHARS:
                kept.append(text("more", language, count=len(self.lines) - index))
                break
            kept.append(line)
            used += len(line) + 1
        return "\n".join(kept)


def _clip(value: str) -> str:
    flat = " ".join(value.split())
    return flat if len(flat) <= LISTED_TEXT_CHARS else flat[: LISTED_TEXT_CHARS - 1] + "…"


def _or_none(lines: Sequence[str], language: Language) -> tuple[str, ...]:
    return tuple(lines) or (text("none", language),)


# --- sections shown in both views -------------------------------------------


def _notifications(setting: NotificationSetting, language: Language) -> Section:
    state = {True: "notify_on", False: "notify_off", None: "notify_default"}[setting.enabled]
    lines = [text(state, language)]
    if setting.queued:
        lines.append(text("notify_queued", language, count=setting.queued))
    return Section(text("notifications", language), tuple(lines))


def _voice(seconds: int, language: Language) -> Section:
    minutes = f"{seconds / 60:.1f}".rstrip("0").rstrip(".")
    return Section(text("voice", language), (text("voice_minutes", language, minutes=minutes),))


def _archive(archiving: bool, archived: Sequence[ArchivedChannel], language: Language) -> Section:
    lines = [text("archiving_on" if archiving else "archiving_off", language)]
    lines += [
        text(
            "archived_line_one" if a.messages == 1 else "archived_line",
            language,
            channel=a.channel.platform_channel_id,
            count=a.messages,
        )
        for a in archived
    ]
    if not archived:
        lines.append(text("archived_none", language))
    return Section(text("archive", language), tuple(lines))


def _traces(count: int, retention: RetentionFacts, language: Language) -> Section:
    if retention.tracing:
        lines = [text("traces_on", language, days=retention.trace_retention_days)]
    else:
        lines = [text("traces_off", language)]
    if retention.tracing or count:
        lines.append(text("traces_count", language, count=count))
    return Section(text("traces", language), tuple(lines))


def _accounts(platforms: Sequence[str], language: Language) -> Section:
    named = ", ".join(p.capitalize() for p in platforms)
    return Section(text("accounts", language), _or_none([named] if named else [], language))


def kept_section(retention: RetentionFacts, language: Language) -> Section:
    """What survives a deletion, with the backup period as configured."""
    backups = (
        text("kept_backups", language, days=retention.backup_retention_days)
        if retention.backup_retention_days is not None
        else text("no_backups", language)
    )
    lines = (
        text("kept_person", language),
        text("kept_audit", language),
        backups,
        text("kept_account", language),
        text("kept_mentions", language),
        text("kept_answers", language, days=retention.memory_retention_days),
        text("kept_sent", language),
        text("kept_voice", language),
    )
    return Section(text("kept", language), tuple(f"• {line}" for line in lines))


def _memory_counts(
    memory: MemoryCounts, retention: RetentionFacts, language: Language
) -> list[str]:
    return [
        text(
            "memory_counts",
            language,
            direct=memory.direct_turns,
            channel=memory.channel_turns,
            summaries=memory.direct_summaries + memory.channel_summaries,
        ),
        text("memory_expiry", language, days=retention.memory_retention_days),
    ]


# --- the channel view: counts and kinds ---------------------------------------


def channel_sections(
    summary: InventorySummary, retention: RetentionFacts, language: Language
) -> list[Section]:
    """The server-channel view. Takes the summary, so no value can reach it."""
    kinds = ", ".join(label(kind, language) for kind in summary.fact_kinds)
    facts = [text("fact_kinds", language, kinds=kinds)] if kinds else []

    def counted(key: str, count: int) -> Section:
        return Section(text(key, language), (text("count", language, count=count),))

    return [
        Section(text("facts", language), _or_none(facts, language)),
        Section(
            text("memory", language), tuple(_memory_counts(summary.memory, retention, language))
        ),
        counted("tasks", summary.tasks),
        counted("alerts", summary.alerts),
        _notifications(summary.notifications, language),
        _voice(summary.voice_seconds_this_month, language),
        Section(text("media", language), (text("media_count", language, count=summary.media),)),
        counted("suggestions", summary.suggestions),
        counted("tokens", summary.tokens),
        _archive(summary.archiving, summary.archived, language),
        _traces(summary.traces, retention, language),
        _accounts(summary.platforms, language),
        kept_section(retention, language),
    ]


# --- the direct-message view: values ------------------------------------------


def _task_line(task: HeldTask, language: Language) -> str:
    if task.disabled:
        state = text("task_stopped", language)
    elif task.last_outcome is None:
        state = text("task_not_run", language)
    else:
        state = outcome_label(TaskOutcome(task.last_outcome), language)
    return text(
        "task_line",
        language,
        id=task.id,
        hours=task.interval_hours,
        question=_clip(task.question),
        state=state,
    )


def _alert_line(alert: HeldAlert, language: Language) -> str:
    kind = _ALERT_KINDS[_lang(language)].get(alert.kind, alert.kind)
    where = [part for part in (alert.chain, alert.asset) if part]
    if alert.address:
        where.append(f"`…{alert.address[-SUFFIX_CHARS:]}`")
    return f"**{alert.id}** - {kind}" + (f" - {' '.join(where)}" if where else "")


def _media_lines(media: MediaCounts, language: Language) -> list[str]:
    lines = [text("media_count", language, count=media.total)]
    if media.by_kind:
        names = _MEDIA_KINDS[_lang(language)]
        kinds = ", ".join(f"{count} {names.get(kind, kind)}" for kind, count in media.by_kind)
        lines.append(text("media_kinds", language, kinds=kinds, text=media.with_text))
    return lines


def _memory_section(
    inventory: Inventory, retention: RetentionFacts, language: Language
) -> Section:
    lines = _memory_counts(inventory.memory, retention, language)
    if inventory.recent_questions:
        lines.append(text("recent", language))
        lines += [f"• {_clip(q)}" for q in inventory.recent_questions]
    return Section(text("memory", language), tuple(lines))


def _suggestion_line(item_id: int, body: str, status: str, language: Language) -> str:
    try:
        shown = status_label(RequestStatus(status), language)
    except ValueError:
        shown = status
    return f"**#{item_id}** - {shown} - {_clip(body)}"


def direct_sections(
    inventory: Inventory, retention: RetentionFacts, language: Language
) -> list[Section]:
    """The direct-message view, with values."""
    facts = [shown_line(f.kind, f.value, language) for f in inventory.facts]
    tokens = [
        text(
            "token_line",
            language,
            label=t.label or text("token_unlabelled", language),
            created=t.issued_at.date().isoformat(),
        )
        for t in inventory.tokens
    ]
    suggestions = [
        _suggestion_line(s.id, s.text, s.status, language) for s in inventory.suggestions
    ]
    return [
        Section(text("facts", language), _or_none(facts, language)),
        _memory_section(inventory, retention, language),
        Section(
            text("tasks", language),
            _or_none([_task_line(t, language) for t in inventory.tasks], language),
        ),
        Section(
            text("alerts", language),
            _or_none([_alert_line(a, language) for a in inventory.alerts], language),
        ),
        _notifications(inventory.notifications, language),
        _voice(inventory.voice_seconds_this_month, language),
        Section(text("media", language), tuple(_media_lines(inventory.media, language))),
        Section(text("suggestions", language), _or_none(suggestions, language)),
        Section(text("tokens", language), _or_none(tokens, language)),
        _archive(inventory.archiving, inventory.archived, language),
        _traces(inventory.traces, retention, language),
        _accounts(inventory.platforms, language),
        kept_section(retention, language),
    ]


# --- sending ---------------------------------------------------------------------


def pages(sections: Sequence[Section], language: Language) -> list[list[discord.Embed]]:
    """The sections as embeds, split over messages within Discord's limits."""
    out: list[list[discord.Embed]] = [[]]
    used = 0
    for section in sections:
        embed = discord.Embed(title=section.title, description=section.description(language))
        size = len(embed)
        if out[-1] and (len(out[-1]) >= MESSAGE_EMBEDS or used + size > MESSAGE_EMBED_CHARS):
            out.append([])
            used = 0
        out[-1].append(embed)
        used += size
    return out


def channel_pages(report: PrivacyReport, language: Language) -> list[list[discord.Embed]]:
    return pages(channel_sections(report.inventory.summary(), report.retention, language), language)


def direct_pages(report: PrivacyReport, language: Language) -> list[list[discord.Embed]]:
    return pages(direct_sections(report.inventory, report.retention, language), language)


Details = Callable[[], Awaitable[PrivacyReport]]
"""Reads the report again when the button is pressed, so the DM is current."""


class SendDetailsView(RequesterOnlyView):
    """[Send me the details] under the channel reply, for the person who asked."""

    def __init__(
        self,
        requester_id: int,
        language: Language,
        details: Details,
        timeout: float = DETAILS_WINDOW_SECONDS,
        erase: tuple[Erase, RetentionFacts] | None = None,
    ) -> None:
        super().__init__(requester_id, text("not_yours", language), timeout=timeout)
        self._language = language
        self._details = details
        self.send_button.label = text("details", language)
        if erase is not None:
            self.add_item(DeleteEverythingButton(requester_id, language, *erase))

    @discord.ui.button(label="Send me the details", style=discord.ButtonStyle.primary)
    async def send_button(
        self, interaction: discord.Interaction, button: discord.ui.Button[Any]
    ) -> None:
        # A deferred update: the message the button is on stays the original
        # response, so the button can be taken away once the DM is out.
        await interaction.response.defer()
        sent = await self._send_direct(interaction.user)
        if sent:
            # Only this button goes: [Delete everything...] beside it stays live.
            button.disabled = True
            if all(getattr(item, "disabled", True) for item in self.children):
                self.stop()
            try:
                await interaction.edit_original_response(view=self)
            except discord.HTTPException:
                log.info("privacy.details.button_not_disabled")
        await interaction.followup.send(
            text("details_sent" if sent else "details_failed", self._language), ephemeral=True
        )

    async def _send_direct(self, user: discord.User | discord.Member) -> bool:
        report = await self._details()
        try:
            channel = await user.create_dm()
            for embeds in direct_pages(report, self._language):
                await channel.send(embeds=embeds)
        except discord.HTTPException:
            log.info("privacy.details.dm_refused", user_id=user.id)
            return False
        return True


# --- delete everything -------------------------------------------------------------

ERASE_CHOICE_SECONDS = 120.0
"""How long the two delete buttons, and the typed confirmation, stay live."""

CONFIRM_WORDS: dict[Language, str] = {EN: "DELETE", PT: "APAGAR"}
"""The word the person types, in their language."""

Erase = Callable[[ErasureMode], Awaitable[ErasureRequest]]
"""Runs the erasure for the person who pressed, and returns the finished request."""


def confirm_word(language: Language) -> str:
    return CONFIRM_WORDS[_lang(language)]


def erasure_reply(request: ErasureRequest, language: Language) -> str:
    """What was deleted, what is scheduled, and what happens from now on."""
    counts = request.counts
    lines = [
        text("erase_messages", language, messages=counts.messages),
        text("erase_media", language, count=counts.media),
        text("erase_facts", language, count=counts.facts),
        text("erase_memory", language, count=counts.memory),
        text("erase_tasks", language, count=counts.tasks),
        text("erase_alerts", language, count=counts.alerts),
        text("erase_suggestions", language, count=counts.suggestions),
        text("erase_tokens", language, count=counts.tokens),
        text("erase_voice", language),
    ]
    after = "erase_left" if request.mode is ErasureMode.ERASE_AND_OPT_OUT else "erase_stays"
    return "\n".join(
        [
            text("erase_done", language),
            *(f"• {line}" for line in lines),
            text("erase_traces", language, count=counts.traces),
            text(after, language),
        ]
    )


class ConfirmErasureModal(discord.ui.Modal):
    """The typed confirmation. Anything but the exact word deletes nothing."""

    def __init__(
        self,
        mode: ErasureMode,
        language: Language,
        erase: Erase,
        choice: ErasureChoiceView,
    ) -> None:
        super().__init__(title=text("erase_modal_title", language), timeout=ERASE_CHOICE_SECONDS)
        self._mode = mode
        self._language = language
        self._erase = erase
        self._choice = choice
        self._word = confirm_word(language)
        self.typed: discord.ui.TextInput[ConfirmErasureModal] = discord.ui.TextInput(
            label=text("erase_modal_label", language, word=self._word),
            placeholder=self._word,
            max_length=20,
        )
        self.add_item(self.typed)

    async def on_submit(self, interaction: discord.Interaction, /) -> None:
        if self.typed.value.strip() != self._word:
            log.info("privacy.erase.wrong_word", mode=self._mode.value)
            await interaction.response.send_message(
                text("erase_wrong_word", self._language, word=self._word), ephemeral=True
            )
            return
        # One erasure per confirmation: the buttons that led here stop now.
        self._choice.stop()
        await interaction.response.defer(ephemeral=True, thinking=True)
        request = await self._erase(self._mode)
        await interaction.followup.send(
            erasure_reply(request, self._language),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def on_error(  # type: ignore[override] # Modal narrows BaseView.on_error
        self, interaction: discord.Interaction, error: Exception, /
    ) -> None:
        log.error("privacy.erase.failed", mode=self._mode.value, error=str(error))
        message = text("erase_failed", self._language)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            log.info("privacy.erase.failure_not_shown")


class ErasureChoiceView(RequesterOnlyView):
    """The two buttons: delete everything, or delete everything and leave."""

    def __init__(
        self,
        requester_id: int,
        language: Language,
        erase: Erase,
        timeout: float = ERASE_CHOICE_SECONDS,
    ) -> None:
        super().__init__(requester_id, text("not_yours", language), timeout=timeout)
        self._language = language
        self._erase = erase
        self.erase_button.label = text("erase_button", language)
        self.leave_button.label = text("erase_leave_button", language)

    @discord.ui.button(label="Delete everything", style=discord.ButtonStyle.danger)
    async def erase_button(
        self, interaction: discord.Interaction, button: discord.ui.Button[Any]
    ) -> None:
        await self._ask_for_word(interaction, ErasureMode.ERASE)

    @discord.ui.button(
        label="Delete everything and stop archiving me", style=discord.ButtonStyle.danger
    )
    async def leave_button(
        self, interaction: discord.Interaction, button: discord.ui.Button[Any]
    ) -> None:
        await self._ask_for_word(interaction, ErasureMode.ERASE_AND_OPT_OUT)

    async def _ask_for_word(self, interaction: discord.Interaction, mode: ErasureMode) -> None:
        await interaction.response.send_modal(
            ConfirmErasureModal(mode, self._language, self._erase, self)
        )


class DeleteEverythingButton(discord.ui.Button[RequesterOnlyView]):
    """[Delete everything...] under `/privacy`: shows the choice, deletes nothing."""

    def __init__(
        self, requester_id: int, language: Language, erase: Erase, retention: RetentionFacts
    ) -> None:
        super().__init__(label=text("delete_entry", language), style=discord.ButtonStyle.danger)
        self._requester_id = requester_id
        self._language = language
        self._erase = erase
        self._retention = retention

    async def callback(self, interaction: discord.Interaction) -> None:
        kept = kept_section(self._retention, self._language)
        # Deferred, then a followup: the choice is a message of its own, which
        # its two buttons are stored against.
        await interaction.response.defer(ephemeral=True, thinking=True)
        await interaction.followup.send(
            text("erase_confirm", self._language),
            embeds=[
                discord.Embed(title=kept.title, description=kept.description(self._language))
            ],
            view=ErasureChoiceView(self._requester_id, self._language, self._erase),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


def delete_everything_view(
    requester_id: int,
    language: Language,
    erase: Erase,
    retention: RetentionFacts,
    timeout: float = DETAILS_WINDOW_SECONDS,
) -> RequesterOnlyView:
    """The view under the direct-message reply: [Delete everything...] alone."""
    view = RequesterOnlyView(requester_id, text("not_yours", language), timeout=timeout)
    view.add_item(DeleteEverythingButton(requester_id, language, erase, retention))
    return view
