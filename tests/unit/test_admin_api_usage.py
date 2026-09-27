"""The usage routes: who may read what, what the answer holds, and what is recorded."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from starlette.testclient import TestClient

from chatmemory.admin.audit import ChangeKind
from chatmemory.app.usage import UsageService
from chatmemory.ports.usage import (
    ModelCall,
    TraceCount,
    TracedQuestion,
    UsageExclusions,
    UsageRows,
    ViewedPerson,
)
from tests.unit.oidc_support import PUBLIC_URL, browser_sign_in, rig
from tests.unit.test_admin_api import Console, build_console
from tests.unit.usage_fakes import FakeUsageDirectory, FakeUsageSource

ANA_ID, BEA_ID = "101", "202"
NOTICE = datetime(2026, 9, 10, tzinfo=UTC)
WINDOW = {"from": "2026-09-01", "to": "2026-09-20"}
QUESTIONS = f"/api/usage/people/{ANA_ID}/questions"
SUMMARY = "/api/usage/summary"


def _held() -> dict[str, list[TracedQuestion]]:
    return {
        ANA_ID: [
            TracedQuestion("old", NOTICE - timedelta(days=2), "corpus.fixed", (),
                           "asked before she was told", 5, 1, 0.001),
            TracedQuestion("new", NOTICE + timedelta(days=2), "market.price",
                           ("market:price",), "what is BTC at?", 50, 10, 0.002),
            TracedQuestion("gone", NOTICE + timedelta(days=3), "corpus.fixed", (),
                           "withdrawn question", 5, 1, 0.001),
        ]
    }


def _rows() -> UsageRows:
    day = date(2026, 9, 12)
    return UsageRows(
        traces=(TraceCount(ANA_ID, day, "market.price", 2), TraceCount(BEA_ID, day, "time", 1)),
        models=(ModelCall(ANA_ID, day, "market.price", "gpt-5.4-mini", 100, 20, 0.004),),
    )


class Rigged:
    def __init__(self) -> None:
        self.source = FakeUsageSource(rows=_rows(), questions_held=_held())
        self.directory = FakeUsageDirectory(
            exclusion=UsageExclusions(trace_ids=frozenset({"gone"})),
            display={ANA_ID: "Ana"},
            people={ANA_ID: ViewedPerson(7, "Ana", NOTICE)},
        )
        self.signing = rig()

    async def console(self) -> Console:
        return await build_console(
            sign_in=self.signing.sign_in, usage=UsageService(self.source, self.directory)
        )

    def browser(self, console: Console, sub: str) -> TestClient:
        client = TestClient(console.client.app, base_url=PUBLIC_URL)
        assert browser_sign_in(client, self.signing.fake, sub) == 302
        return client


@pytest.fixture
def rigged() -> Rigged:
    return Rigged()


async def test_an_operator_signed_in_is_refused_question_text(rigged: Rigged) -> None:
    console = await rigged.console()
    ben = rigged.browser(console, "ben-sub")

    response = ben.get(QUESTIONS, params=WINDOW)

    assert response.status_code == 403
    assert rigged.source.question_calls == []
    assert console.changes._entries == []  # noqa: SLF001


@pytest.mark.parametrize("oidc_configured", [True, False])
async def test_a_token_is_refused_question_text_whatever_its_role(
    oidc_configured: bool,
) -> None:
    source = FakeUsageSource(questions_held=_held())
    directory = FakeUsageDirectory(people={ANA_ID: ViewedPerson(7, "Ana", NOTICE)})
    # Without an issuer the token is admin; still not a person signed in.
    console = await build_console(
        oidc_configured=oidc_configured, usage=UsageService(source, directory)
    )

    response = console.client.get(QUESTIONS, params=WINDOW, headers=console.auth())

    assert response.status_code == 403
    assert source.question_calls == []


async def test_an_admin_reads_only_the_askers_words_after_the_notice(rigged: Rigged) -> None:
    console = await rigged.console()
    ana = rigged.browser(console, "ana-sub")

    response = ana.get(QUESTIONS, params=WINDOW)

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert [q["question"] for q in body["questions"]] == ["what is BTC at?"]
    assert body["hidden_before_notice"] == 1
    assert body["person"] == {"platform_user_id": ANA_ID, "person_id": 7, "name": "Ana"}
    assert set(body["questions"][0]) == {
        "timestamp", "feature", "tools", "question", "input_tokens", "output_tokens", "cost",
    }
    for forbidden in ("answer", "evidence", "output", "metadata", "citations", "decisions"):
        assert f'"{forbidden}"' not in response.text


async def test_viewing_is_recorded_against_the_viewer_and_the_viewed_person(
    rigged: Rigged,
) -> None:
    console = await rigged.console()
    ana = rigged.browser(console, "ana-sub")

    ana.get(QUESTIONS, params={**WINDOW, "page": "2"})

    [entry] = console.changes._entries  # noqa: SLF001
    assert entry.kind is ChangeKind.APPLIED
    assert entry.setting == "usage.questions_viewed"
    assert (entry.operator, entry.operator_display) == ("oidc:ana-sub", "ana@cyberdyne.test")
    assert entry.after == (
        f"person 7 (Ana), platform id {ANA_ID}, 2026-09-01..2026-09-20, page 2"
    )
    # The record names who was viewed, never what they asked.
    assert "BTC" not in repr(entry)


async def test_a_person_with_no_record_is_audited_and_shows_nothing(rigged: Rigged) -> None:
    console = await rigged.console()
    ana = rigged.browser(console, "ana-sub")

    body = ana.get(f"/api/usage/people/{BEA_ID}/questions", params=WINDOW).json()

    assert body["questions"] == []
    [entry] = console.changes._entries  # noqa: SLF001
    assert entry.after is not None and entry.after.startswith("no person on record")


@pytest.mark.parametrize("exclusion", ["opted out or erasing", "erased"])
async def test_excluded_peoples_text_is_absent_while_the_store_still_holds_it(
    rigged: Rigged, exclusion: str
) -> None:
    rigged.directory.exclusion = (
        UsageExclusions(people=frozenset({ANA_ID}))
        if exclusion != "erased"
        else UsageExclusions(erased_before={ANA_ID: NOTICE + timedelta(days=5)})
    )
    console = await rigged.console()
    ana = rigged.browser(console, "ana-sub")

    questions = ana.get(QUESTIONS, params=WINDOW).json()
    summary = ana.get(SUMMARY, params=WINDOW).json()

    assert questions["questions"] == []
    assert "BTC" not in str(questions)
    assert ANA_ID not in {row["key"] for row in summary["rows"]}
    assert rigged.source.questions_held[ANA_ID], "the trace store still holds them"


async def test_the_summary_is_counts_for_an_operator(rigged: Rigged) -> None:
    console = await rigged.console()
    ben = rigged.browser(console, "ben-sub")

    response = ben.get(SUMMARY, params={**WINDOW, "group": "person"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["label"] == "traced question runs"
    assert (body["from"], body["to"]) == ("2026-09-01", "2026-09-20")
    ana = next(row for row in body["rows"] if row["key"] == ANA_ID)
    assert (ana["name"], ana["questions"], ana["input_tokens"], ana["cost"]) == (
        "Ana", 2, 100, 0.004,
    )
    bea = next(row for row in body["rows"] if row["key"] == BEA_ID)
    assert bea["name"] is None
    assert body["totals"]["questions"] == 3
    assert "question\"" not in response.text  # no text on the operator's screen
    [(_, pending)] = rigged.source.aggregate_calls
    assert pending == frozenset({"gone"})


async def test_a_token_reads_the_summary(rigged: Rigged) -> None:
    console = await rigged.console()
    response = console.client.get(SUMMARY, headers=console.auth())
    assert response.status_code == 200


@pytest.mark.parametrize(
    "params",
    [
        {"from": "2026-01-01", "to": "2026-09-20"},
        {"from": "2026-09-20", "to": "2026-09-01"},
        {"from": "soon"},
        {"group": "channel"},
    ],
    ids=["over-90-days", "reversed", "malformed", "unknown-group"],
)
async def test_a_bad_window_or_grouping_is_refused(
    rigged: Rigged, params: dict[str, str]
) -> None:
    console = await rigged.console()
    response = console.client.get(SUMMARY, params=params, headers=console.auth())
    assert response.status_code == 400
    assert rigged.source.aggregate_calls == []


async def test_a_store_that_is_down_is_a_503(rigged: Rigged) -> None:
    rigged.source.down = True
    console = await rigged.console()
    ana = rigged.browser(console, "ana-sub")

    summary = ana.get(SUMMARY, params=WINDOW)
    questions = ana.get(QUESTIONS, params=WINDOW)

    assert (summary.status_code, summary.json()) == (503, {"error": "usage unavailable"})
    assert (questions.status_code, questions.json()) == (503, {"error": "usage unavailable"})
    assert questions.headers["cache-control"] == "no-store"


async def test_a_non_numeric_person_is_refused(rigged: Rigged) -> None:
    console = await rigged.console()
    ana = rigged.browser(console, "ana-sub")
    assert ana.get("/api/usage/people/ana/questions").status_code == 400


@pytest.mark.parametrize("user_id", ["%C2%B2", "%D9%A3", "1" * 25])
async def test_a_non_ascii_or_oversized_id_is_a_400_not_a_500(
    rigged: Rigged, user_id: str
) -> None:
    # Regression: '²' passed `str.isdigit` and failed in `int()` as a 500.
    console = await rigged.console()
    ana = rigged.browser(console, "ana-sub")
    assert ana.get(f"/api/usage/people/{user_id}/questions").status_code == 400
    assert rigged.source.question_calls == []


@pytest.mark.parametrize("page", ["9" * 5000, "1001", "0", "x"])
async def test_a_bad_page_is_a_400_not_a_500(rigged: Rigged, page: str) -> None:
    console = await rigged.console()
    ana = rigged.browser(console, "ana-sub")
    response = ana.get(QUESTIONS, params={**WINDOW, "page": page})
    assert response.status_code == 400
    assert rigged.source.question_calls == []
