"""Assemble the page JSON (`ReportPayload`) from a derived forecast. Pure."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta

from hazelst.config import Mountain
from hazelst.models import (
    Accumulation,
    Forecast,
    MountainReport,
    ReportPayload,
    ReportStatus,
    VerificationSummary,
)
from hazelst.sources import discussions


def is_open(mountain: Mountain, day: date) -> bool:
    """Open unless a known opening day is still ahead (unknown counts as open)."""
    return mountain.season_open is None or mountain.season_open <= day


def build_payload(
    forecast: Forecast,
    mountains: Sequence[Mountain],
    *,
    issued: datetime,
    status: ReportStatus,
    edition: str | None = None,
    alerts: Mapping[str, Sequence[str]] | None = None,
    discussion_texts: Mapping[str, str] | None = None,
    accumulation: Mapping[str, Accumulation] | None = None,
    sources_unavailable: Sequence[str] = (),
    notes: Sequence[str] = (),
    verification: VerificationSummary | None = None,
) -> ReportPayload:
    alerts = alerts or {}
    accumulation = accumulation or {}
    reports = [
        MountainReport(
            key=m.key,
            name=m.name,
            state=m.state,
            summit_ft=m.summit_ft,
            base_ft=m.base_ft,
            season_open=m.season_open,
            is_open=is_open(m, forecast.date),
            forecast=forecast.mountain(m.key),
            accumulation=accumulation.get(m.key),
            alerts=sorted(set(alerts.get(m.key, []))),
        )
        for m in mountains
    ]
    first = forecast.mountains[0].days if forecast.mountains else []
    anchor = forecast.anchor
    return ReportPayload(
        date=forecast.date,
        issued=issued,
        edition=edition,
        status=status,
        anchor=anchor,
        days=[d.label for d in first],
        past_days=[
            f"{(anchor - timedelta(days=n)):%a} {(anchor - timedelta(days=n)).day:02d}"
            for n in range(7, 0, -1)
        ],
        models=forecast.models,
        used_models=forecast.used_models,
        discussions={
            office: discussions.summary(text) for office, text in (discussion_texts or {}).items()
        },
        storm_words=forecast.storm_words,
        sources_unavailable=list(sources_unavailable),
        notes=list(notes),
        verification=verification,
        mountains=reports,
    )
