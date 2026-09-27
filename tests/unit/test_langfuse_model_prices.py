"""Model prices for the configured models: from the table, a flag or Langfuse; never guessed."""

from __future__ import annotations

import argparse
import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

import httpx
import pytest

from chatmemory.adapters.tracing.langfuse_models import (
    MODEL_SETTINGS,
    KnownPatterns,
    ModelPrice,
    configured_models,
    load_price_table,
    match_pattern_for,
    plan_prices,
    price_from_flag,
)
from chatmemory.config import Settings
from tests.unit.test_trace_usage import FakeModels

ROOT = Path(__file__).resolve().parents[2]
TABLE = ROOT / "scripts" / "langfuse_model_prices.json"
KEYS = {"LANGFUSE_HOST": "http://lf.invalid", "LANGFUSE_PUBLIC_KEY": "pk",
        "LANGFUSE_SECRET_KEY": "sk"}
#: Langfuse's own definition for text-embedding-3-small, as the models API lists it.
EMBEDDING_MANAGED = {
    "id": "managed-embed",
    "modelName": "text-embedding-3-small",
    "matchPattern": "(?i)^(openai/)?(text-embedding-3-small)$",
    "inputPrice": 2e-8,
    "isLangfuseManaged": True,
}


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "langfuse_models_script", ROOT / "scripts" / "langfuse_models.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_every_model_setting_exists_with_a_default() -> None:
    for field in MODEL_SETTINGS.values():
        assert isinstance(Settings.model_fields[field].get_default(), str)


def test_the_configured_models_are_the_environment_else_the_defaults() -> None:
    defaults = {var: f"default-{var}" for var in MODEL_SETTINGS}
    environ = {"CHAT_MODEL": "gpt-5.4-mini", "EXTRACTION_MODEL": "gpt-5.4-mini",
               "EMBEDDING_MODEL": " "}
    assert configured_models(environ, defaults) == [
        "gpt-5.4-mini", "default-EMBEDDING_MODEL", "default-MEDIA_AUDIO_MODEL",
    ]


@pytest.mark.parametrize(
    ("name", "matches"),
    [("gpt-5.4-mini", True), ("openai/gpt-5.4-mini", True), ("gpt-5.4-mini-2026-03-17", True),
     ("gpt-5x4-mini", False), ("gpt-5.4-mini-high", False)],
)
def test_a_flag_price_matches_the_name_and_its_dated_forms(name: str, matches: bool) -> None:
    assert bool(re.search(match_pattern_for("gpt-5.4-mini"), name)) is matches


def test_a_flag_price_is_per_million_tokens() -> None:
    price = price_from_flag("gpt-5.4-mini=0.25,2")
    assert (price.model_name, price.input_price, price.output_price) == (
        "gpt-5.4-mini", 0.25 / 1_000_000, 2 / 1_000_000,
    )


@pytest.mark.parametrize("raw", ["gpt-5.4-mini", "=1,2", "m=1", "m=a,b", "m=-1,2"])
def test_a_malformed_flag_is_refused(raw: str) -> None:
    with pytest.raises(ValueError):
        price_from_flag(raw)


def test_an_unknown_configured_model_is_missing_not_guessed() -> None:
    table = load_price_table(TABLE)
    plan = plan_prices(
        ["gpt-5.4-mini", "text-embedding-3-small", "gpt-4o-mini-transcribe"],
        table,
        [],
        KnownPatterns(managed=[str(EMBEDDING_MANAGED["matchPattern"])]),
    )
    assert plan.missing == ["gpt-5.4-mini"]
    assert plan.earlier == []
    assert {p.model_name for p in plan.prices} == {p.model_name for p in table}


def test_a_flag_prices_it_and_replaces_a_table_row_of_that_name() -> None:
    table = [ModelPrice("chat-v1", "(?i)^(chat-v1)$", 0.0, 0.0)]
    flags = [price_from_flag("gpt-5.4-mini=0.25,2"), price_from_flag("chat-v1=1,1")]

    plan = plan_prices(["gpt-5.4-mini", "chat-v1"], table, flags, KnownPatterns())

    assert plan.missing == []
    assert {p.model_name: p.input_price for p in plan.prices} == {
        "chat-v1": 1 / 1_000_000, "gpt-5.4-mini": 0.25 / 1_000_000,
    }


def test_a_managed_pattern_langfuse_cannot_parse_prices_nothing() -> None:
    plan = plan_prices(["m"], [], [], KnownPatterns(managed=["(?i)^(m"], ours=["(?i)^(m"]))
    assert plan.missing == ["m"]


def test_a_definition_an_earlier_flag_left_prices_the_model() -> None:
    known = KnownPatterns(ours=[match_pattern_for("gpt-5.4-mini")])
    plan = plan_prices(["gpt-5.4-mini"], [], [], known)
    assert (plan.missing, plan.earlier) == ([], ["gpt-5.4-mini"])


def _args(*prices: str, dry_run: bool = False) -> argparse.Namespace:
    return argparse.Namespace(table=TABLE, dry_run=dry_run, price=list(prices))


PROD = {**KEYS, "CHAT_MODEL": "gpt-5.4-mini", "EXTRACTION_MODEL": "gpt-5.4-mini",
        "EMBEDDING_MODEL": "text-embedding-3-small"}


async def test_the_script_writes_nothing_while_a_configured_model_has_no_price(
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake = FakeModels([dict(EMBEDDING_MANAGED)])

    code = await _script().run(_args(), PROD, httpx.MockTransport(fake.handle))

    assert code == 2
    assert fake.created == [] and fake.deleted == []
    assert "no price for: gpt-5.4-mini" in capsys.readouterr().err


async def test_the_script_registers_the_configured_model_given_by_flag() -> None:
    fake = FakeModels([dict(EMBEDDING_MANAGED)])

    code = await _script().run(
        _args("gpt-5.4-mini=0.25,2"), PROD, httpx.MockTransport(fake.handle)
    )

    assert code == 0
    created = {row["modelName"]: row for row in fake.created}
    assert created["gpt-5.4-mini"]["inputPrice"] == 0.25 / 1_000_000
    # Langfuse prices the embedding model itself: no definition of ours shadows it.
    assert "text-embedding-3-small" not in created
    assert "gpt-4o-mini-transcribe" in created


async def test_a_later_run_without_the_flag_keeps_the_earlier_price(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Regression: only table rows, flags and Langfuse-managed definitions
    # counted, so after `--price gpt-5.4-mini=...` every later run without
    # the flag stopped with "no price for: gpt-5.4-mini".
    fake = FakeModels([dict(EMBEDDING_MANAGED)])
    transport = httpx.MockTransport(fake.handle)
    assert await _script().run(_args("gpt-5.4-mini=0.25,2"), PROD, transport) == 0
    created = list(fake.created)

    code = await _script().run(_args(), PROD, transport)

    assert code == 0
    assert fake.created == created and fake.deleted == []
    assert "gpt-5.4-mini: priced by an earlier --price" in capsys.readouterr().out


async def test_a_dry_run_of_the_script_changes_nothing() -> None:
    fake = FakeModels([dict(EMBEDDING_MANAGED)])
    code = await _script().run(
        _args("gpt-5.4-mini=0.25,2", dry_run=True), PROD, httpx.MockTransport(fake.handle)
    )
    assert code == 0
    assert fake.created == []
