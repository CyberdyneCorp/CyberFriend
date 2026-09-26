"""What `/schedule` says, in English or Portuguese.

The commands answered only in English, so a Portuguese question scheduled with
`/schedule create` was confirmed in English. The language comes from the
question when there is one, otherwise from the caller.
"""

from __future__ import annotations

from collections.abc import Sequence

import discord

from chatmemory.app.language import Language
from chatmemory.app.schedules import CreateRefusal, CreateResult
from chatmemory.ports.schedules import (
    MAX_INTERVAL_HOURS,
    MIN_INTERVAL_HOURS,
    ScheduledTask,
    TaskOutcome,
)

EN, PT = Language.ENGLISH, Language.PORTUGUESE

_TEXT: dict[str, dict[Language, str]] = {
    "created": {
        EN: (
            "Done. I'll ask that every **{hours}h**, starting {start}.\n"
            "I'll only message you when I find something, so silence means "
            "nothing new. `/schedule list` shows when each one last ran."
        ),
        PT: (
            "Pronto. Vou perguntar isso a cada **{hours}h**, começando {start}.\n"
            "Só te mando mensagem quando encontrar algo, então silêncio quer dizer "
            "nada de novo. `/schedule list` mostra quando cada uma rodou."
        ),
    },
    "interval": {
        EN: "I can ask between every **{low}h** and every **{high}h**.",
        PT: "Posso perguntar de **{low}h** em **{low}h** até a cada **{high}h**.",
    },
    "empty": {EN: "Tell me what to ask.", PT: "Me diga o que perguntar."},
    # Its own sentence: it used to fall through to the cap message, telling
    # somebody with no tasks at all that they had too many.
    "unknown": {
        EN: (
            "I need to have talked with you once before I can ask things for you. "
            "Send me any question, then try again."
        ),
        PT: (
            "Preciso ter conversado com você uma vez antes de perguntar coisas por "
            "você. Me mande qualquer pergunta e tente de novo."
        ),
    },
    "cap": {
        EN: (
            "You've reached the number of scheduled questions I can keep for one "
            "person. Delete one with `/schedule delete` first."
        ),
        PT: (
            "Você chegou ao limite de perguntas agendadas que guardo por pessoa. "
            "Apague uma com `/schedule delete` antes."
        ),
    },
    "none": {
        EN: (
            "You have no scheduled questions. `/schedule create` sets one up.\n"
            "I'll only message you when there's something to say."
        ),
        PT: (
            "Você não tem perguntas agendadas. `/schedule create` cria uma.\n"
            "Só te mando mensagem quando houver algo a dizer."
        ),
    },
    "heading": {
        EN: "**Your scheduled questions ({count}):**",
        PT: "**Suas perguntas agendadas ({count}):**",
    },
    "line": {
        EN: "**{id}** - every {hours}h - {question}",
        PT: "**{id}** - a cada {hours}h - {question}",
    },
    "stopped": {
        EN: "stopped - {reason}",
        PT: "parada - {reason}",
    },
    "not_yet": {
        EN: "not run yet - first {when}",
        PT: "ainda não rodou - primeira {when}",
    },
    "last": {
        EN: "last ran {when}, {outcome} - next {next}",
        PT: "rodou {when}, {outcome} - próxima {next}",
    },
    "deleted": {
        EN: "Stopped. I won't ask that again.",
        PT: "Parada. Não vou mais perguntar isso.",
    },
    # The same sentence whether the task is somebody else's or does not exist:
    # two sentences would reveal which task numbers are real.
    "not_yours": {
        EN: (
            "I don't have a scheduled task with that number for you. "
            "`/schedule list` shows yours."
        ),
        PT: (
            "Não tenho uma pergunta agendada com esse número para você. "
            "`/schedule list` mostra as suas."
        ),
    },
    "unavailable": {
        EN: (
            "I can't manage scheduled tasks here - the feature isn't switched on "
            "for this deployment."
        ),
        PT: (
            "Não consigo gerenciar perguntas agendadas aqui - o recurso não está "
            "ligado nesta instalação."
        ),
    },
}

_OUTCOMES: dict[Language, dict[TaskOutcome, str]] = {
    EN: {
        TaskOutcome.REPORTED: "found something and messaged you",
        TaskOutcome.NOTHING: "found nothing",
        TaskOutcome.FAILED: "failed",
        TaskOutcome.CLOSED: "couldn't reach your direct messages",
    },
    PT: {
        TaskOutcome.REPORTED: "encontrou algo e te avisou",
        TaskOutcome.NOTHING: "não encontrou nada",
        TaskOutcome.FAILED: "falhou",
        TaskOutcome.CLOSED: "não conseguiu te mandar mensagem direta",
    },
}


def _lang(language: Language) -> Language:
    return PT if language is PT else EN


def text(key: str, language: Language, **values: object) -> str:
    return _TEXT[key][_lang(language)].format(**values)


def outcome_label(outcome: TaskOutcome, language: Language) -> str:
    """What a run did, as its owner reads it."""
    return _OUTCOMES[_lang(language)][outcome]


def created_message(result: CreateResult, language: Language) -> str:
    if result.task is not None:
        start = discord.utils.format_dt(result.task.next_run_at, "R")
        return text("created", language, hours=result.task.interval_hours, start=start)
    if result.refusal is CreateRefusal.INTERVAL_OUT_OF_RANGE:
        return text("interval", language, low=MIN_INTERVAL_HOURS, high=MAX_INTERVAL_HOURS)
    if result.refusal is CreateRefusal.EMPTY_QUESTION:
        return text("empty", language)
    if result.refusal is CreateRefusal.UNKNOWN_PERSON:
        return text("unknown", language)
    return text("cap", language)


def schedule_listing(tasks: Sequence[ScheduledTask], language: Language) -> str:
    if not tasks:
        return text("none", language)
    lines = [text("heading", language, count=len(tasks))]
    for task in tasks:
        lines.append(
            text("line", language, id=task.id, hours=task.interval_hours, question=task.question)
        )
        lines.append(f"  {task_state(task, language)}")
    return "\n".join(lines)


def task_state(task: ScheduledTask, language: Language) -> str:
    """When it last ran and what happened, so silence is legible."""
    if not task.active:
        return text("stopped", language, reason=task.disabled_reason or "disabled")
    if task.last_run_at is None:
        when = discord.utils.format_dt(task.next_run_at, "R")
        return text("not_yet", language, when=when)
    outcome = outcome_label(task.last_outcome or TaskOutcome.NOTHING, language)
    return text(
        "last",
        language,
        when=discord.utils.format_dt(task.last_run_at, "R"),
        outcome=outcome,
        next=discord.utils.format_dt(task.next_run_at, "R"),
    )
