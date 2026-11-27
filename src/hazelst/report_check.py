"""report-check: deterministic claim checks on a `ReportDraft` against its `ReportDigest`.

The publish gate (agent-runs.md, "report-check rules"). Pure: no I/O. Each
rule returns `CheckError`s; an empty list means the draft may be published.

Prose is read phrase by phrase. A sentence splits into clauses at `;`, `:`,
dashes, "while", "but", "then", and "with"; a clause splits into phrases at
list commas and "and". A comma starts a new phrase only when the part after it makes its
own claim (a precip word or a snow amount) about its own mountain or day
("rain today, a light mix Saturday, and dry Sunday"); a bare name joins the
list it belongs to ("freezing rain at Killington, Pico, and Sugarloaf"). "and"
splits the same way ("a little freezing rain Sunday and rain Monday").

A phrase is tied to the mountains it names; a phrase that names none is tied
to the mountains named last in its sentence, else (inside `mountains[key]` and
`best_bet.why`) to that entry's mountain. It is tied to the days it names
(weekday names, today, tonight, tomorrow, weekend); a phrase that names no day
inherits the days of the previous phrase in the same section. A phrase tied to
no mountain is regional: its precip-type words are not checked, and its snow
numbers are checked against every mountain.

Each precip word is read at the elevation named nearest to it in its phrase
(base or summit words); with none it describes the summit, except freezing
rain, which holds wherever the digest has it on that mountain-day
(`freezing_rain_at`).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Literal

from hazelst.agents.schemas import ReportDraft
from hazelst.digest import DigestDay, DigestMountain, ReportDigest
from hazelst.models import Elevation, PrecipType

Rule = Literal[
    "headline", "mountains", "snow_number", "precip_type", "best_bet", "storm_type", "cautions"
]

SNOW_TOLERANCE_IN = 0.5
TRACKS = ("clipper", "coastal", "inland_runner")


@dataclass(frozen=True)
class CheckError:
    rule: Rule
    message: str

    def __str__(self) -> str:
        return f"{self.rule}: {self.message}"


# --- vocabulary -------------------------------------------------------------------

# Precip-type words, matched in this order; an earlier match claims its span
# ("freezing rain" is ice, not rain; "snow showers" is snow, not rain).
PRECIP_WORDS: tuple[tuple[PrecipType, re.Pattern[str]], ...] = (
    (
        "ice",
        re.compile(r"freezing (?:rain|drizzle)|\bice\b|\bicy\b|\bicing\b|\bglaze[ds]?\b", re.I),
    ),
    ("snow", re.compile(r"\bsnow (?:showers?|squalls?)\b", re.I)),
    ("mix", re.compile(r"wintry mix|\bmix(?:ed|es)?\b|\bsleet\b", re.I)),
    ("rain", re.compile(r"\brain(?:s|y|ing|fall)?\b|\bdrizzle\b|\bshowers?\b", re.I)),
    ("snow", re.compile(r"\bsnow(?:s|fall|ing|y)?\b|\bdusting\b|\bflurries\b|\bpowder\b", re.I)),
    ("dry", re.compile(r"\bdry\b|\bdries\b|\bdrying\b", re.I)),
)

# Word types each computed `precip_type` accepts. `mix` means part snow, part
# liquid, so snow and rain words are consistent with it.
COMPATIBLE: dict[PrecipType, frozenset[PrecipType]] = {
    "snow": frozenset({"snow"}),
    "ice": frozenset({"ice"}),
    "rain": frozenset({"rain"}),
    "mix": frozenset({"mix", "snow", "rain"}),
    "dry": frozenset({"dry"}),
}

NEGATION = re.compile(r"\b(?:no|not|without|never|nor)\b(?:\W+\w+){0,3}\W*$", re.I)
# "dry until afternoon", "dry through Thursday midday": a part of the day, not the day.
DRY_PARTIAL = re.compile(r"^(?:\W+\w+){0,3}?\W+(?:until|till|before|through|ahead)\b", re.I)
# Elevation words: a precip word is read at the nearest one in its phrase.
ELEVATION_WORDS: tuple[tuple[Elevation, re.Pattern[str]], ...] = (
    (
        "base",
        re.compile(
            r"\b(?:base|bases|base-elevation|valley|valleys|lower (?:elevations?|slopes|mountain)"
            r"|low elevations?)\b",
            re.I,
        ),
    ),
    (
        "summit",
        re.compile(
            r"\b(?:summits?|peaks?|up top|upper (?:elevations?|slopes|mountain)"
            r"|higher (?:elevations?|terrain)|high elevations?|to the top)\b",
            re.I,
        ),
    ),
)

STORM_TYPE_WORDS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("clipper", re.compile(r"\bclipper\b", re.I)),
    ("coastal", re.compile(r"nor[’']?easter|coastal (?:low|storm|system)", re.I)),
    ("inland_runner", re.compile(r"inland runner|lakes cutter|\bcutter\b", re.I)),
    ("upslope", re.compile(r"\bupslope\b", re.I)),
)

WIND_WORDS = re.compile(r"\bwind|\bgust|\bhold", re.I)

# The headline is a text alert: amount, resorts by name, day; then the one catch.
HEADLINE_MAX_WORDS = 12
EVERY_MOUNTAIN = re.compile(r"\ball eight\b|\b(?:any|none) of the eight\b", re.I)
WEEKDAY = re.compile(r"\b(?:mon|tues|wednes|thurs|fri|satur|sun)day\b", re.I)

_FRACTIONS = {"½": 0.5, "¼": 0.25, "¾": 0.75}
_NUM = r"(?:\d+(?:\.\d+)?[½¼¾]?|[½¼¾])"
SNOW_AMOUNT = re.compile(
    rf"(?P<a>{_NUM})(?:\s*(?:–|—|-|to)\s*(?P<b>{_NUM}))?\s*(?:″|\"|”|-?\binch(?:es)?\b)",
    re.I,
)
NOT_SNOW_AFTER = re.compile(
    r"^\s*(?:of\s+)?(?:ice|glaze|liquid|rain|water|precip(?:itation)?|freezing)", re.I
)

CLAUSE_SPLIT = re.compile(r"([;—]|:(?!\d)|\s–\s|\bwhile\b|\bbut\b|\bthen\b|\bwith\b)", re.I)
SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
LIST_SPLIT = re.compile(r"(,(?=\s|$)|\band\b)", re.I)


def _number(text: str) -> float:
    whole = text.rstrip("½¼¾")
    frac = _FRACTIONS.get(text[-1], 0.0) if text else 0.0
    return (float(whole) if whole else 0.0) + frac


# --- clause parsing ---------------------------------------------------------------


@dataclass
class Clause:
    """One phrase: its text (mountain names blanked), mountains, days, and section.

    `follows` is the mountain list named just before a "with" phrase that names
    its own ("2.7″ at Killington with a 0.7-6.3″ range and 0.1-4.5″ at Pico": the
    range follows Killington); a snow number may match either list.
    """

    text: str
    mountains: list[DigestMountain]
    days: list[date]
    section: str
    follows: list[DigestMountain] = field(default_factory=list)


@dataclass
class _Names:
    """Mountain names and day words for one digest."""

    digest: ReportDigest
    mountain_re: re.Pattern[str] = field(init=False)
    by_name: dict[str, DigestMountain] = field(init=False)

    def __post_init__(self) -> None:
        self.by_name = {}
        for m in self.digest.mountains:
            self.by_name[m.name.lower()] = m
            self.by_name[m.key.replace("_", " ")] = m
        names = sorted(self.by_name, key=len, reverse=True)
        self.mountain_re = re.compile(r"\b(?:" + "|".join(map(re.escape, names)) + r")\b", re.I)

    def mountains_in(self, text: str) -> tuple[list[DigestMountain], str]:
        """Mountains named in `text`, and the text with their names blanked out."""
        found: list[DigestMountain] = []
        for match in self.mountain_re.finditer(text):
            m = self.by_name[match.group(0).lower()]
            if m not in found:
                found.append(m)
        return found, self.mountain_re.sub(" ", text)

    def days_in(self, text: str) -> list[date]:
        days = self.digest.days
        found: list[date] = []

        def add(d: date) -> None:
            if d in days and d not in found:
                found.append(d)

        lowered = text.lower()
        for d in days:
            if re.search(rf"\b{d.strftime('%A').lower()}\b", lowered):
                add(d)
        first = self.digest.date
        if re.search(r"\b(?:today|tonight|this (?:morning|afternoon|evening))\b", lowered):
            add(first)
        if re.search(r"\btomorrow\b", lowered):
            add(first + timedelta(days=1))
        if re.search(r"\bweekend\b", lowered):
            for d in days:
                if d.weekday() in (5, 6):
                    add(d)
        return found


def _claims(text: str) -> bool:
    return bool(precip_words(text)) or any(
        not NOT_SNOW_AFTER.match(text[m.end() :]) for m in SNOW_AMOUNT.finditer(text)
    )


def _phrases(names: _Names, clause: str) -> list[str]:
    """A clause's list items. A comma or "and" starts a new phrase only before a part
    that makes its own claim about its own mountain or day; other parts join the
    phrase before them (or, at the start, the first phrase after them)."""
    parts = LIST_SPLIT.split(clause)
    phrases: list[str] = []
    pending = ""
    for i in range(0, len(parts), 2):
        piece = parts[i]
        joiner = parts[i - 1] if i else ""
        named, rest = names.mountains_in(piece)
        own = (bool(named) or bool(names.days_in(rest))) and _claims(rest)
        if own:
            phrases.append(pending + piece)
            pending = ""
        elif phrases:
            phrases[-1] += joiner + piece
        else:
            pending += piece + (parts[i + 1] if i + 1 < len(parts) else "")
    if pending:
        phrases.append(pending)
    return phrases


def _clauses(
    names: _Names, section: str, text: str, default: DigestMountain | None, day: date | None
) -> Iterator[Clause]:
    current_days: list[date] = [day] if day else []
    for sentence in SENTENCE_SPLIT.split(text):
        sentence_mountains: list[DigestMountain] = []
        parts = CLAUSE_SPLIT.split(sentence)
        # re.split with a group: [clause, delimiter, clause, delimiter, ...]
        for i in range(0, len(parts), 2):
            clause = parts[i]
            if not clause.strip():
                continue
            after_with = i > 0 and parts[i - 1].lower() == "with"
            for j, phrase in enumerate(_phrases(names, clause)):
                named, rest = names.mountains_in(phrase)
                days = names.days_in(rest)
                if days:
                    current_days = days
                follows: list[DigestMountain] = []
                if named:
                    if after_with and j == 0:
                        follows = sentence_mountains
                    sentence_mountains = named
                # A phrase that names no mountain is about the mountains named last in
                # its sentence ("Killington gets 0.9″, but it falls as a mix"), else the
                # section's own mountain.
                mountains = list(sentence_mountains) or ([default] if default else [])
                yield Clause(rest, mountains, list(current_days), section, follows)


def _sections(draft: ReportDraft, digest: ReportDigest) -> Iterator[Clause]:
    names = _Names(digest)
    best = digest.mountain(draft.best_bet.mountain)
    yield from _clauses(names, "headline", draft.headline, None, None)
    yield from _clauses(names, "best_bet", draft.best_bet.why, best, draft.best_bet.day)
    yield from _clauses(names, "storm", draft.storm, None, None)
    for key, text in draft.mountains.items():
        yield from _clauses(names, f"mountains.{key}", text, digest.mountain(key), None)
    for i, text in enumerate(draft.cautions):
        yield from _clauses(names, f"cautions[{i}]", text, None, None)


# --- rules ------------------------------------------------------------------------


def check_headline(draft: ReportDraft, digest: ReportDigest) -> list[CheckError]:
    """At most 12 words, names a resort (or all / any / none of the eight), and names only
    forecast days. Its amounts and precip words are checked with the rest of the prose."""
    headline = draft.headline
    errors: list[CheckError] = []
    words = len(headline.split())
    if words > HEADLINE_MAX_WORDS:
        errors.append(CheckError("headline", f"{words} words; at most {HEADLINE_MAX_WORDS}"))
    named, rest = _Names(digest).mountains_in(headline)
    if not named and not EVERY_MOUNTAIN.search(rest):
        errors.append(CheckError("headline", 'names no resort (name them, or say "all eight")'))
    forecast_days = {d.strftime("%A").lower() for d in digest.days}
    for match in WEEKDAY.finditer(rest):
        if match.group(0).lower() not in forecast_days:
            errors.append(
                CheckError("headline", f"{match.group(0)!r} is not a forecast day in the digest")
            )
    return errors


def check_mountains(draft: ReportDraft, digest: ReportDigest) -> list[CheckError]:
    expected = [m.key for m in digest.mountains]
    errors = [
        CheckError("mountains", f"missing entry for {key}")
        for key in expected
        if key not in draft.mountains
    ]
    errors += [
        CheckError("mountains", f"unknown mountain {key!r}")
        for key in draft.mountains
        if key not in expected
    ]
    errors += [
        CheckError("mountains", f"empty entry for {key}")
        for key, text in draft.mountains.items()
        if not text.strip()
    ]
    return errors


def precip_claims(text: str) -> list[tuple[int, PrecipType, str]]:
    """Precip-type claims in a phrase: (offset, type, word), skipping negated and
    partial-day uses."""
    taken: list[tuple[int, int]] = []
    found: list[tuple[int, PrecipType, str]] = []
    for kind, pattern in PRECIP_WORDS:
        for match in pattern.finditer(text):
            start, end = match.span()
            if any(start < b and a < end for a, b in taken):
                continue
            taken.append((start, end))
            if NEGATION.search(text[:start]):
                continue
            if kind == "dry" and DRY_PARTIAL.match(text[end:]):
                continue
            found.append((start, kind, match.group(0)))
    return sorted(found)


def precip_words(text: str) -> list[tuple[PrecipType, str]]:
    """Precip-type claims in a phrase: (type, word)."""
    return [(kind, word) for _, kind, word in precip_claims(text)]


def elevation_at(text: str, offset: int) -> Elevation | None:
    """The elevation named nearest to `offset` in a phrase, or None when it names none."""
    best: tuple[int, Elevation] | None = None
    for elevation, pattern in ELEVATION_WORDS:
        for match in pattern.finditer(text):
            distance = abs(match.start() - offset)
            if best is None or distance < best[0]:
                best = (distance, elevation)
    return best[1] if best else None


def accepts(day: DigestDay, kind: PrecipType, elevation: Elevation | None) -> bool:
    """True when a `kind` word at `elevation` (None: unnamed) agrees with the day.

    Unnamed, a word describes the summit (`precip_type`), except freezing rain,
    which holds at any elevation in `freezing_rain_at`.
    """
    if kind == "ice" and day.freezing_rain_at and elevation in (None, *day.freezing_rain_at):
        return True
    return kind in COMPATIBLE[day.type_at(elevation or "summit")]


def _facts(days: list[DigestDay], kind: PrecipType, elevation: Elevation | None) -> str:
    field = "base_precip_type" if elevation == "base" else "precip_type"
    types = "/".join(sorted({d.type_at(elevation or "summit") for d in days}))
    facts = f"{field} is {types}"
    if kind == "ice":
        where = sorted({e for d in days for e in d.freezing_rain_at})
        facts += f"; freezing_rain_at is [{', '.join(where)}]"
    return facts


def _day_label(days: Iterable[date]) -> str:
    return "/".join(d.strftime("%a %-d") for d in days)


def check_precip_types(draft: ReportDraft, digest: ReportDigest) -> list[CheckError]:
    errors: list[CheckError] = []
    seen: set[tuple[str, str, str, str, Elevation | None]] = set()
    for clause in _sections(draft, digest):
        if not clause.days or not clause.mountains:
            continue
        for offset, kind, word in precip_claims(clause.text):
            elevation = elevation_at(clause.text, offset)
            for mountain in clause.mountains:
                days = [d for day in clause.days if (d := mountain.day(day))]
                if not days or any(accepts(d, kind, elevation) for d in days):
                    continue
                label = _day_label(clause.days)
                key = (mountain.key, label, clause.section, word.lower(), elevation)
                if key in seen:
                    continue
                seen.add(key)
                at = f" at the {elevation}" if elevation else ""
                errors.append(
                    CheckError(
                        "precip_type",
                        f"{mountain.name} {label}: {clause.section} says {word!r} ({kind}){at};"
                        f" {_facts(days, kind, elevation)}",
                    )
                )
    return errors


def snow_values(mountain: DigestMountain) -> tuple[list[float], list[tuple[float, float]]]:
    """Snow numbers the JSON states for a mountain, and its window ranges."""
    values: list[float] = []
    ranges: list[tuple[float, float]] = []
    daily = [d.snow_in for d in mountain.days]
    values += daily
    for i in range(len(daily)):
        for j in range(i + 2, len(daily) + 1):
            values.append(sum(daily[i:j]))
    for w in mountain.windows.values():
        values += [w.snow_in, w.lo_in, w.hi_in]
        values += [v for v in w.by_model.values() if v is not None]
        values += [v for v in (w.base_snow_in, w.nws_in) if v is not None]
        ranges.append((w.lo_in, w.hi_in))
    acc = mountain.accumulation
    if acc is not None:
        values += [v for v in (*acc.last7_in, acc.season_in, acc.base_in) if v is not None]
        values += [c.snow_in for c in acc.not_resort_reported]
    return values, ranges


def _snow_ok(value: float, mountains: Iterable[DigestMountain]) -> bool:
    for mountain in mountains:
        values, ranges = snow_values(mountain)
        if any(abs(value - v) <= SNOW_TOLERANCE_IN + 1e-9 for v in values):
            return True
        if any(lo - 1e-9 <= value <= hi + 1e-9 for lo, hi in ranges):
            return True
    return False


def check_snow_numbers(draft: ReportDraft, digest: ReportDigest) -> list[CheckError]:
    errors: list[CheckError] = []
    for clause in _sections(draft, digest):
        scope = clause.mountains or digest.mountains
        for match in SNOW_AMOUNT.finditer(clause.text):
            if NOT_SNOW_AFTER.match(clause.text[match.end() :]):
                continue
            for group in ("a", "b"):
                raw = match.group(group)
                if raw is None:
                    continue
                value = _number(raw)
                if not _snow_ok(value, scope) and not _snow_ok(value, clause.follows):
                    who = ", ".join(m.name for m in clause.mountains) or "any mountain"
                    errors.append(
                        CheckError(
                            "snow_number",
                            f"{clause.section}: {match.group(0).strip()!r} matches no snow value"
                            f" for {who} (± {SNOW_TOLERANCE_IN}″ or a window range)",
                        )
                    )
                    break
    return errors


def check_best_bet(draft: ReportDraft, digest: ReportDigest) -> list[CheckError]:
    bet = draft.best_bet
    mountain = digest.mountain(bet.mountain)
    errors: list[CheckError] = []
    if mountain is None:
        errors.append(CheckError("best_bet", f"unknown mountain {bet.mountain!r}"))
    if bet.day not in digest.days:
        errors.append(CheckError("best_bet", f"{bet.day} is not a forecast day in the digest"))
    if mountain is not None and mountain.season_open is not None and mountain.season_open > bet.day:
        errors.append(
            CheckError(
                "best_bet",
                f"{mountain.name} opens {mountain.season_open}, after the best-bet day {bet.day}",
            )
        )
    return errors


def supported_storm_words(digest: ReportDigest) -> set[str]:
    supported: set[str] = {
        m.storm_type for m in digest.mountains if m.storm_type not in (None, "none")
    }
    for d in digest.discussions:
        supported.update(d.storm_words)
    if any("nw_upslope" in day.tags for m in digest.mountains for day in m.days):
        supported.add("upslope")
    return {s for s in supported if s is not None}


def check_storm_types(draft: ReportDraft, digest: ReportDigest) -> list[CheckError]:
    supported = supported_storm_words(digest)
    texts = [
        ("headline", draft.headline),
        ("best_bet", draft.best_bet.why),
        ("storm", draft.storm),
        *((f"mountains.{k}", v) for k, v in draft.mountains.items()),
        *((f"cautions[{i}]", v) for i, v in enumerate(draft.cautions)),
    ]
    errors: list[CheckError] = []
    reported: set[str] = set()
    for section, text in texts:
        for name, pattern in STORM_TYPE_WORDS:
            match = pattern.search(text)
            if match and name not in supported and name not in reported:
                reported.add(name)
                source = (
                    "a tag or discussion" if name == "upslope" else "storm_type or a discussion"
                )
                errors.append(
                    CheckError(
                        "storm_type",
                        f"{section} names {match.group(0)!r}, but no {source} supports {name}",
                    )
                )
    return errors


def _day_words(d: date, digest: ReportDigest) -> list[str]:
    words = [d.strftime("%A").lower()]
    if d == digest.date:
        words += ["today", "tonight", "this morning", "this afternoon"]
    if d == digest.date + timedelta(days=1):
        words.append("tomorrow")
    return words


def check_cautions(draft: ReportDraft, digest: ReportDigest) -> list[CheckError]:
    names = _Names(digest)
    cautions = [c.lower() for c in draft.cautions]
    errors: list[CheckError] = []
    events = sorted({a.event for m in digest.mountains for a in m.alerts})
    for event in events:
        if not any(event.lower() in c for c in cautions):
            errors.append(CheckError("cautions", f"active alert {event!r} is not in cautions"))
    for mountain in digest.mountains:
        for day in mountain.days:
            if "wind_hold_likely" not in day.tags:
                continue
            ok = False
            for caution in draft.cautions:
                named, rest = names.mountains_in(caution)
                if mountain not in named or not WIND_WORDS.search(rest):
                    continue
                if any(re.search(rf"\b{w}\b", rest.lower()) for w in _day_words(day.date, digest)):
                    ok = True
                    break
            if not ok:
                errors.append(
                    CheckError(
                        "cautions",
                        f"wind hold likely at {mountain.name} {day.date.strftime('%A')}"
                        " is not in cautions",
                    )
                )
    return errors


RULES = (
    check_headline,
    check_mountains,
    check_snow_numbers,
    check_precip_types,
    check_best_bet,
    check_storm_types,
    check_cautions,
)


def check(draft: ReportDraft, digest: ReportDigest) -> list[CheckError]:
    """Every rule's errors, in rule order; empty means publishable."""
    return [error for rule in RULES for error in rule(draft, digest)]
