"""Regression corpus: every report draft Claude has written in production.

`tests/fixtures/report/corpus/call-<id>.json` holds one `agent_call` row
(`report-write` or `report-revise`) exported from frank: the digest the call
saw, its output, the `report-check` errors stored with it, and, for a revise,
the draft it revised. Each digest was re-derived on frank from the same ingest
run with the J33 code (`base_precip_type`, `freezing_rain_at`; every stored
`precip_type` and tag reproduced exactly) and carries only the digest tags.

Call 9 (2026-11-28 7 AM, J37) was written by the J33 code, so its stored digest
is used as is; the report-write example is built on it.

Every output must pass `report-check`. The drafts handed to `report-revise`
are the morning draft re-checked on a later edition's numbers; their errors
below are genuine contradictions with those numbers, and the list is exact.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from hazelst.agents.prompts import role_prompt
from hazelst.agents.schemas import ReportDraft
from hazelst.digest import ReportDigest
from hazelst.report_check import check

CORPUS = Path(__file__).resolve().parents[1] / "fixtures" / "report" / "corpus"
CALLS = sorted(int(p.stem.removeprefix("call-")) for p in CORPUS.glob("call-*.json"))


def load(call_id: int) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((CORPUS / f"call-{call_id}.json").read_text())
    return data


def errors(call_id: int, field: str) -> list[str]:
    record = load(call_id)
    digest = ReportDigest.model_validate(record["digest"])
    return [str(e) for e in check(ReportDraft.model_validate(record[field]), digest)]


def test_corpus_has_every_stored_draft() -> None:
    assert CALLS == [1, 2, 3, 4, 5, 6, 9]


@pytest.mark.parametrize("call_id", CALLS)
def test_every_stored_draft_passes(call_id: int) -> None:
    assert errors(call_id, "output") == []


def test_todays_rejected_drafts_now_pass() -> None:
    # 2026-11-27: the 2:40 PM refresh (run 147) and the 5:43 PM rebuild (run 181)
    # were rejected by checker and data bugs, not by the prose.
    assert load(5)["run_id"] == 147 and load(5)["stored_check_errors"]
    assert load(6)["run_id"] == 181 and len(load(6)["stored_check_errors"]) == 9
    assert errors(5, "output") == errors(6, "output") == []


# The morning draft re-checked on a later edition's numbers: each error is a claim
# the newer numbers contradict (the forecast changed between editions). Reviewed by
# hand for J33; the list is exact, so a checker change that adds or drops one fails.
GENUINE: dict[str, list[str]] = json.loads((CORPUS / "genuine.json").read_text())


@pytest.mark.parametrize("call_id", sorted(GENUINE))
def test_revised_drafts_fail_only_on_genuine_contradictions(call_id: str) -> None:
    assert errors(int(call_id), "revised_draft") == GENUINE[call_id]


def test_owner_approved_headline_passes_on_run_181() -> None:
    # Owner-approved headline for 2026-11-27 (J33), on that evening's digest.
    record = load(6)
    headline = "4\N{EN DASH}5″ for Killington, Pico and Sugarbush Saturday; rain Monday"
    draft = ReportDraft.model_validate(record["output"]).model_copy(update={"headline": headline})
    assert check(draft, ReportDigest.model_validate(record["digest"])) == []


def test_report_write_example_passes_on_its_digest() -> None:
    # The complete example in report-write.md is built on 2026-11-28's 7 AM digest (call 9),
    # around the owner-approved storm paragraph (J37). It must pass as written.
    match = re.search(r"```json\n(.*?)\n```", role_prompt("report-write"), re.S)
    assert match is not None
    example = ReportDraft.model_validate_json(match.group(1))
    assert example.storm.startswith("Today: wet snow mixed with rain on the Vermont summits")
    assert check(example, ReportDigest.model_validate(load(9)["digest"])) == []
