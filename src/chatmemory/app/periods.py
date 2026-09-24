"""Spans of days named in Portuguese, for the routes that read a period.

English is read by `routing.named_period`. Portuguese lives here rather than in
`routing._PERIODS` on purpose: that table is also what obligation questions
are read against, and teaching it a new language would change what "o que me
pediram ontem" answers -- a worthwhile change, and a different one from this.

A module of its own so the catch-up and wallet-activity routes share one table
without either importing the other.
"""

from __future__ import annotations

from chatmemory.app.routing import Period

#: Longest phrase first, so "semana passada" is not read as "semana".
PT_PERIODS: tuple[tuple[str, Period], ...] = (
    ("hoje de manha", Period(0)),
    ("hoje de manhã", Period(0)),
    ("esta manha", Period(0)),
    ("esta manhã", Period(0)),
    ("anteontem", Period(2, 1)),
    ("ontem", Period(1, 1)),
    ("hoje", Period(0)),
    ("semana passada", Period(14, 7)),
    ("ultima semana", Period(7)),
    ("última semana", Period(7)),
    ("esta semana", Period(7)),
    ("ultimos 7 dias", Period(7)),
    ("últimos 7 dias", Period(7)),
    ("mes passado", Period(60, 30)),
    ("mês passado", Period(60, 30)),
    ("ultimo mes", Period(30)),
    ("último mês", Period(30)),
    ("este mes", Period(30)),
    ("este mês", Period(30)),
)


def padded(text: str) -> str:
    """Lowercased, whitespace-collapsed and space-padded, so " x " matches a word."""
    return f" {' '.join(text.lower().replace('?', ' ').split())} "


def portuguese_period(text: str) -> Period | None:
    """The span a Portuguese question named, or None."""
    words = padded(text)
    return next((p for phrase, p in PT_PERIODS if phrase in words), None)
