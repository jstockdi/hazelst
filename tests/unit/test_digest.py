from datetime import date

from hazelst import digest, report_check
from hazelst.models import Accumulation, AccumulationDay, ContextReport
from tests.pages import sample_payload


def test_digest_takes_accumulation_fields_it_knows() -> None:
    """The page's accumulation carries `sources` (forecast-verify); the digest does not."""
    payload = sample_payload()
    first = payload.mountains[0].model_copy(
        update={
            "accumulation": Accumulation(
                source="resort",
                last7_in=[0.0, 1.5, None, 0.0, 2.0, 0.0, 0.5],
                season_in=12.0,
                base_in=8.0,
                sources=["resort", "snocountry"],
            )
        }
    )
    payload = payload.model_copy(update={"mountains": [first, *payload.mountains[1:]]})
    built = digest.from_payload(payload)
    acc = built.mountains[0].accumulation
    assert acc is not None
    assert (acc.source, acc.season_in, acc.base_in) == ("resort", 12.0, 8.0)
    assert acc.last7_in[1] == 1.5
    assert acc.not_resort_reported == []


def _point(place: str, snow_in: float, elev_ft: int | None) -> ContextReport:
    return ContextReport(
        source="ghcn", place=place, snow_in=snow_in, elev_ft=elev_ft, distance_mi=4.0, hours=24
    )


def test_digest_keeps_context_apart_from_the_resort_number() -> None:
    day = date(2026, 11, 24)
    payload = sample_payload()
    context = [
        ContextReport(source="nohrsc", place="NOHRSC 24 h grid", snow_in=2.2, elev_ft=2100),
        _point("VALLEY", 0.5, 700),
        _point("HUT", 4.0, 2746),
        _point("MID", 1.5, 1500),
        _point("UNKNOWN", 9.0, None),
        _point("TOWN", 1.0, 1000),
    ]
    first = payload.mountains[0].model_copy(
        update={
            "accumulation": Accumulation(
                source="none",
                last7_in=[None] * 7,
                days=[AccumulationDay(date=day, context=context)],
            )
        }
    )
    payload = payload.model_copy(update={"mountains": [first, *payload.mountains[1:]]})
    built = digest.from_payload(payload)
    acc = built.mountains[0].accumulation
    assert acc is not None
    assert acc.last7_in == [None] * 7  # no resort report: nothing is filled in
    # NOHRSC, then the three highest point reports.
    assert [(c.source, c.place) for c in acc.not_resort_reported] == [
        ("nohrsc", "NOHRSC 24 h grid"),
        ("ghcn", "HUT"),
        ("ghcn", "MID"),
        ("ghcn", "TOWN"),
    ]
    assert {c.date for c in acc.not_resort_reported} == {day}
    # The writer may cite a context value (it is in the digest).
    values, _ = report_check.snow_values(built.mountains[0])
    assert 4.0 in values and 2.2 in values
