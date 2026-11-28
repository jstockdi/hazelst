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
`best_bet.why`) to that entry's mountain. A mountain group names its members:
"the Vermont summits" (or mountains, resorts, peaks, bases) is every Vermont
mountain in the digest, likewise Maine and New Hampshire; "all eight" is every
mountain; "the rest" is every mountain not yet named in that sentence. A snow
amount tied to a group must fit each member of the group.

A phrase is tied to the days it names (weekday names, today, tonight,
tomorrow, weekend); a phrase that names no day inherits the days of the
previous phrase in the same section. A phrase that names no day but says
"otherwise" ("dry otherwise") is tied to every forecast day the section has
not named, and its precip words must hold on each of them. A phrase tied to
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
    "headline",
    "mountains",
    "snow_number",
    "precip_type",
    "best_bet",
    "storm_type",
    "cautions",
    "voice",
    "snow_line",
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

# "little snow" denies snow; "a little snow" claims it.
NEGATION = re.compile(
    r"(?:\b(?:no|not|without|never|nor)\b|(?<!\ba )\blittle\b)(?:\W+\w+){0,3}\W*$", re.I
)
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

# Mountain groups (state codes from the digest). Only the state word is matched, so the
# noun after it ("summits", "bases") still names the elevation.
STATE_GROUPS: tuple[tuple[str, str], ...] = (
    ("VT", "vermont"),
    ("NH", r"new\s+hampshire"),
    ("ME", "maine"),
)
GROUP_NOUN = (
    r"(?=(?:\s+four|\s+two)?\s+(?:summits?|mountains?|resorts?|peaks?|bases?|ski areas?)\b)"
)
ALL_MOUNTAINS = r"\ball eight\b"
REST = r"\bthe rest\b(?!\s+of\b(?!\s+(?:them|the\s+(?:mountains|resorts|summits|eight))\b))"
# "through Monday", whole days only ("through Thursday midday" is part of a day).
THROUGH_DAY = re.compile(
    r"\b(?:through|thru)\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b"
    r"(?!\s+(?:morning|midday|noon|afternoon|evening))",
    re.I,
)
# A phrase about the days the section has not named ("dry otherwise").
OTHERWISE = re.compile(r"\botherwise\b", re.I)

CLAUSE_SPLIT = re.compile(r"([;—]|:(?!\d)|\s–\s|\bwhile\b|\bbut\b|\bthen\b|\bwith\b)", re.I)
# "with" does not split a mix claim ("snow mixed with rain", "mixes a little snow with rain").
MIX_BEFORE_WITH = re.compile(r"\bmix(?:es|ed|ing)?\b(?:\W+\w+){0,3}\W*$", re.I)
# Words a bare list item may carry besides names and days ("Saturday night", "Pico up top").
BARE_WORDS = 3
# A day aside is not a list item ("Behind Friday's cold front", "nothing falls through Tuesday").
BARE_DAY_WORDS = 1
DAY_WORD = re.compile(
    r"\b(?:(?:mon|tues|wednes|thurs|fri|satur|sun)day(?:[’']s)?|today|tonight|tomorrow"
    r"|weekend|night|morning|afternoon|evening|the|a|at|on|in|of|to)\b",
    re.I,
)
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
    # Members of a group the phrase names: a snow amount must fit each of them.
    grouped: list[DigestMountain] = field(default_factory=list)
    # True for an "otherwise" phrase: its precip words must hold on every day.
    every_day: bool = False


@dataclass
class _Names:
    """Mountain names, mountain groups, and day words for one digest."""

    digest: ReportDigest
    mountain_re: re.Pattern[str] = field(init=False)
    name_re: re.Pattern[str] = field(init=False)
    by_name: dict[str, DigestMountain] = field(init=False)

    def __post_init__(self) -> None:
        self.by_name = {}
        for m in self.digest.mountains:
            self.by_name[m.name.lower()] = m
            self.by_name[m.key.replace("_", " ")] = m
        names = sorted(self.by_name, key=len, reverse=True)
        name_alt = "|".join(map(re.escape, names))
        self.name_re = re.compile(rf"\b(?:{name_alt})\b", re.I)
        groups = [f"(?P<{code}>\\b{word}(?:[’']s)?{GROUP_NOUN})" for code, word in STATE_GROUPS]
        groups += [f"(?P<all>{ALL_MOUNTAINS})", f"(?P<rest>{REST})"]
        self.mountain_re = re.compile(rf"(?P<name>\b(?:{name_alt})\b)|" + "|".join(groups), re.I)

    def resolve(
        self, match: re.Match[str], named: list[DigestMountain]
    ) -> tuple[list[DigestMountain], bool]:
        """The mountains one match names, and whether it is a group."""
        if match.group("name"):
            return [self.by_name[match.group(0).lower()]], False
        mountains = self.digest.mountains
        if match.group("all"):
            return list(mountains), True
        if match.group("rest"):
            return [m for m in mountains if m not in named], True
        code = next(c for c, _ in STATE_GROUPS if match.group(c))
        return [m for m in mountains if m.state == code], True

    def mountains_in(
        self, text: str, before: Iterable[DigestMountain] = ()
    ) -> tuple[list[DigestMountain], str]:
        """Mountains named in `text` (groups expanded), and the text with the names blanked.

        `before` holds the mountains already named in the sentence, which "the rest" excludes.
        """
        found, _ = self.mountains_grouped(text, before)
        return found, self.mountain_re.sub(" ", text)

    def mountains_grouped(
        self, text: str, before: Iterable[DigestMountain] = ()
    ) -> tuple[list[DigestMountain], list[DigestMountain]]:
        """Mountains named in `text`, and the members of the groups among them."""
        named = list(before)
        found: list[DigestMountain] = []
        grouped: list[DigestMountain] = []
        for match in self.mountain_re.finditer(text):
            members, is_group = self.resolve(match, named)
            for m in members:
                if m not in found:
                    found.append(m)
                if m not in named:
                    named.append(m)
                if is_group and m not in grouped:
                    grouped.append(m)
        return found, grouped

    def names_in(self, text: str) -> tuple[list[DigestMountain], str]:
        """Mountains named by name only (no groups), and the text with them blanked."""
        found: list[DigestMountain] = []
        for match in self.name_re.finditer(text):
            m = self.by_name[match.group(0).lower()]
            if m not in found:
                found.append(m)
        return found, self.name_re.sub(" ", text)

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
        # "through Monday": every day from the earliest one named (else the first) to Monday.
        for match in THROUGH_DAY.finditer(lowered):
            end = next(
                (d for d in days if d.strftime("%A").lower() == match.group(1).lower()), None
            )
            if end is None:
                continue
            start = min([d for d in found if d < end], default=days[0])
            for d in days:
                if start <= d <= end:
                    add(d)
        return sorted(found)


def _claims(text: str) -> bool:
    return bool(precip_words(text)) or any(
        not NOT_SNOW_AFTER.match(text[m.end() :]) for m in SNOW_AMOUNT.finditer(text)
    )


LEAD_COMMA = re.compile(r",\s*(?:and\s*)?$", re.I)


# The height where snow turns to rain is a setup, not a precip claim.
SNOW_LINE = re.compile(
    r"\b(?:rain[-/ ](?:and[- ])?)?snow(?:[-/ ]rain)?[- ](?:line|level)s?\b", re.I
)


def _has_elevation(text: str) -> bool:
    return any(pattern.search(text) for _, pattern in ELEVATION_WORDS)


def _shares_claim(names: _Names, before: str, rest: str) -> bool:
    """True when `before` is a mountain list with no claim and no day of its own, and
    the part after it (`rest`, names blanked) names no day either."""
    named, before_rest = names.mountains_in(before)
    return (
        bool(named)
        and not _claims(before_rest)
        and not names.days_in(before_rest)
        and not names.days_in(rest)
    )


def _bare(text: str, names_mountain: bool) -> bool:
    """True for a list item that is only a name or day ("Pico up top", "Saturday night")."""
    limit = BARE_WORDS if names_mountain else BARE_DAY_WORDS
    return len(re.findall(r"\w+", DAY_WORD.sub(" ", text))) <= limit


def _phrases(names: _Names, clause: str) -> list[str]:
    """A clause's list items. A comma or "and" starts a new phrase before a part about
    its own mountain or day (or "otherwise") that makes its own claim or says more than a
    bare list item ("and Tuesday's gusts could hold the lifts"); other parts join the
    phrase before them. At the start, parts join the first phrase after them, except
    that a part with a claim stays a phrase of its own when a comma follows it, or when
    it names its elevation and the next phrase names a day."""
    parts = LIST_SPLIT.split(clause)
    phrases: list[str] = []
    pending = ""
    for i in range(0, len(parts), 2):
        piece = parts[i]
        joiner = parts[i - 1] if i else ""
        named, rest = names.mountains_in(piece)
        subject = bool(named) or bool(names.days_in(rest)) or bool(OTHERWISE.search(rest))
        own = subject and (_claims(rest) or not _bare(rest, bool(named)))
        if own and phrases and _shares_claim(names, phrases[-1], rest):
            # "only the upper mountain at Sugarbush, Killington and Pico gets wet snow":
            # the claim at the end of a mountain list is about the whole list.
            phrases[-1] += joiner + piece
        elif own:
            # A leading claim with no subject joins a later part in an "and" list ("snow
            # and rain mixed Saturday", "snow and rain mixed at Stratton"). After a comma,
            # or before a day when it names its elevation, it keeps the context of the
            # clause instead ("with freezing rain, Stratton gets rain"; "1″ of wet snow
            # up top, after rain today"; "with rain at the base, and Saturday adds").
            lead = names.mountains_in(pending)[1]
            if (
                pending
                and _claims(lead)
                and (
                    LEAD_COMMA.search(pending)
                    or (bool(names.days_in(rest)) and _has_elevation(lead))
                )
            ):
                phrases += [pending, piece]
            else:
                phrases.append(pending + piece)
            pending = ""
        elif phrases:
            phrases[-1] += joiner + piece
        else:
            pending += piece + (parts[i + 1] if i + 1 < len(parts) else "")
    if pending:
        phrases.append(pending)
    return phrases


def _clause_parts(sentence: str) -> list[str]:
    """`CLAUSE_SPLIT.split`, with a "with" after a mix word joined back into its clause."""
    parts = CLAUSE_SPLIT.split(sentence)
    out = parts[:1]
    for i in range(1, len(parts), 2):
        delimiter, clause = parts[i], parts[i + 1]
        if delimiter.lower() == "with" and MIX_BEFORE_WITH.search(out[-1]):
            out[-1] += delimiter + clause
        else:
            out += [delimiter, clause]
    return out


def _clauses(
    names: _Names, section: str, text: str, default: DigestMountain | None, day: date | None
) -> Iterator[Clause]:
    current_days: list[date] = [day] if day else []
    section_days: list[date] = list(current_days)
    for sentence in SENTENCE_SPLIT.split(text):
        sentence_mountains: list[DigestMountain] = []
        sentence_named: list[DigestMountain] = []
        parts = _clause_parts(sentence)
        # re.split with a group: [clause, delimiter, clause, delimiter, ...]
        for i in range(0, len(parts), 2):
            clause = parts[i]
            if not clause.strip():
                continue
            after_with = i > 0 and parts[i - 1].lower() == "with"
            for j, phrase in enumerate(_phrases(names, clause)):
                named, grouped = names.mountains_grouped(phrase, sentence_named)
                _, rest = names.mountains_in(phrase)
                days = names.days_in(rest)
                every_day = False
                if days:
                    current_days = days
                    section_days += [d for d in days if d not in section_days]
                elif OTHERWISE.search(rest):
                    current_days = [d for d in names.digest.days if d not in section_days]
                    every_day = True
                follows: list[DigestMountain] = []
                if named:
                    if after_with and j == 0:
                        follows = sentence_mountains
                    sentence_mountains = named
                    sentence_named += [m for m in named if m not in sentence_named]
                # A phrase that names no mountain is about the mountains named last in
                # its sentence ("Killington gets 0.9″, but it falls as a mix"), else the
                # section's own mountain.
                mountains = list(sentence_mountains) or ([default] if default else [])
                yield Clause(
                    rest,
                    mountains,
                    list(current_days),
                    section,
                    follows,
                    grouped if named else [],
                    every_day,
                )


def _sections(draft: ReportDraft, digest: ReportDigest) -> Iterator[Clause]:
    names = _Names(digest)
    yield from _clauses(names, "headline", draft.headline, None, None)
    if draft.best_bet is not None:
        bet = draft.best_bet
        yield from _clauses(names, "best_bet", bet.why, digest.mountain(bet.mountain), bet.day)
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
    named, rest = _Names(digest).names_in(headline)
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
    partial-day uses and the snow line ("the snow line sits near 3,000 ft")."""
    taken: list[tuple[int, int]] = [m.span() for m in SNOW_LINE.finditer(text)]
    found: list[tuple[int, PrecipType, str]] = []
    for kind, pattern in PRECIP_WORDS:
        for match in pattern.finditer(text):
            start, end = match.span()
            if any(start < b and a < end for a, b in taken):
                continue
            taken.append((start, end))
            if NEGATION.search(text[:start]):
                continue
            partial = DRY_PARTIAL.match(text[end:])
            if kind == "dry" and partial and not THROUGH_DAY.search(text, end):
                continue
            found.append((start, kind, match.group(0)))
    return sorted(found)


def precip_words(text: str) -> list[tuple[PrecipType, str]]:
    """Precip-type claims in a phrase: (type, word)."""
    return [(kind, word) for _, kind, word in precip_claims(text)]


def elevation_at(text: str, offset: int) -> Elevation | None:
    """The elevation named nearest to `offset` within its list part (between commas and
    "and"s) of a phrase, or None when that part names none: in "snow up top, rain at the
    base" the snow is at the summit and the rain at the base."""
    start, end = 0, len(text)
    for match in LIST_SPLIT.finditer(text):
        if match.end() <= offset:
            start = match.end()
        elif match.start() >= offset:
            end = match.start()
            break
    best: tuple[int, Elevation] | None = None
    for elevation, pattern in ELEVATION_WORDS:
        for match in pattern.finditer(text, start, end):
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
        if not clause.mountains:
            continue
        # A claim that names no day must hold on at least one forecast day.
        clause_days = clause.days or digest.days
        for offset, kind, word in precip_claims(clause.text):
            elevation = elevation_at(clause.text, offset)
            # "dry otherwise" and "dry through Monday" are claims about every one of the days.
            every_day = clause.every_day or (
                kind == "dry" and THROUGH_DAY.search(clause.text) is not None
            )
            for mountain in clause.mountains:
                days = [d for day in clause_days if (d := mountain.day(day))]
                if every_day:
                    days = [d for d in days if not accepts(d, kind, elevation)]
                    if not days:
                        continue
                elif not days or any(accepts(d, kind, elevation) for d in days):
                    continue
                label = (
                    _day_label(d.date for d in days)
                    if every_day
                    else _day_label(clause.days)
                    if clause.days
                    else "any day"
                )
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


def _fits(lo: float, hi: float, mountain: DigestMountain) -> bool:
    """True when an amount from `lo` to `hi` (equal for one number) fits one mountain's numbers."""
    values, ranges = snow_values(mountain)
    tol = SNOW_TOLERANCE_IN + 1e-9
    if any(lo - tol <= v <= hi + tol for v in values):
        return True
    return any(r_lo - 1e-9 <= hi and lo <= r_hi + 1e-9 for r_lo, r_hi in ranges)


def check_snow_numbers(draft: ReportDraft, digest: ReportDigest) -> list[CheckError]:
    errors: list[CheckError] = []
    for clause in _sections(draft, digest):
        scope = clause.mountains or digest.mountains
        for match in SNOW_AMOUNT.finditer(clause.text):
            if NOT_SNOW_AFTER.match(clause.text[match.end() :]):
                continue
            amount = match.group(0).strip()
            bounds: list[float] = []
            for group in ("a", "b"):
                raw = match.group(group)
                if raw is None:
                    continue
                value = _number(raw)
                bounds.append(value)
                if not _snow_ok(value, scope) and not _snow_ok(value, clause.follows):
                    who = ", ".join(m.name for m in clause.mountains) or "any mountain"
                    errors.append(
                        CheckError(
                            "snow_number",
                            f"{clause.section}: {amount!r} matches no snow value"
                            f" for {who} (± {SNOW_TOLERANCE_IN}″ or a window range)",
                        )
                    )
                    break
            else:
                lo, hi = min(bounds), max(bounds)
                misfits = [m.name for m in clause.grouped if not _fits(lo, hi, m)]
                if misfits:
                    errors.append(
                        CheckError(
                            "snow_number",
                            f"{clause.section}: {amount!r} is said of a group, but matches no"
                            f" snow value for {', '.join(misfits)} (name the mountains it fits)",
                        )
                    )
    return errors


def check_best_bet(draft: ReportDraft, digest: ReportDigest) -> list[CheckError]:
    """A best bet, when there is one, names an open mountain and a forecast day."""
    bet = draft.best_bet
    if bet is None:
        return []
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
        *((("best_bet", draft.best_bet.why),) if draft.best_bet else ()),
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


# A snow-line height must sit near the digest's freezing level for that phrase.
SNOW_LINE_TOLERANCE_FT = 700
LINE_WORDS = re.compile(r"snow[- ]?(?:line|level)|freezing level", re.I)
HEIGHT = re.compile(r"\b(\d{1,2},\d{3}|\d{3,5})(?:\s*|-)(?:ft|feet|foot)\b", re.I)
# Heights in discussion text, ranges included ("snow levels fall into the 1500 to 2000 foot range").
DISCUSSION_HEIGHT = re.compile(
    r"\b(\d{1,2},?\d{3})(?:\s*(?:-|–|to)\s*(\d{1,2},?\d{3}))?\s*(?:-|\s)?(?:ft|feet|foot)\b",
    re.I,
)


def discussion_heights(digest: ReportDigest) -> list[float]:
    """Every height an NWS discussion states, in feet."""
    heights: list[float] = []
    for d in digest.discussions:
        for match in DISCUSSION_HEIGHT.finditer(" ".join(d.text.split())):
            heights += [float(g.replace(",", "")) for g in match.groups() if g]
    return heights


def check_snow_line(draft: ReportDraft, digest: ReportDigest) -> list[CheckError]:
    """A snow-line or freezing-level height is within `SNOW_LINE_TOLERANCE_FT` of a
    `freezing_level_ft` for the phrase's mountains (any, when it names none) and days
    (any, when it names none), or is a height an NWS discussion states."""
    errors: list[CheckError] = []
    stated = discussion_heights(digest)
    for clause in _sections(draft, digest):
        if not LINE_WORDS.search(clause.text):
            continue
        mountains = clause.mountains or digest.mountains
        days = clause.days or digest.days
        levels = [
            d.freezing_level_ft
            for m in mountains
            for day in days
            if (d := m.day(day)) is not None and d.freezing_level_ft is not None
        ]
        for match in HEIGHT.finditer(clause.text):
            feet = float(match.group(1).replace(",", ""))
            if feet in stated:
                continue
            if not any(abs(feet - level) <= SNOW_LINE_TOLERANCE_FT for level in levels):
                near = f"{min(levels):,.0f}–{max(levels):,.0f} ft" if levels else "none"
                errors.append(
                    CheckError(
                        "snow_line",
                        f"{clause.section}: snow line {match.group(0)!r} is not within"
                        f" {SNOW_LINE_TOLERANCE_FT} ft of a freezing level for"
                        f" {_day_label(days)} (freezing_level_ft {near}) or a height a"
                        " discussion states",
                    )
                )
    return errors


# Banned in every section (owner, J37): "Today is the day here", "Saturday's the day".
IS_THE_DAY = re.compile(r"\bis\s+the\s+day\b|\b\w+[’']s\s+the\s+day\b", re.I)


def _texts(draft: ReportDraft) -> list[tuple[str, str]]:
    """Every prose field of a draft as (section, text)."""
    return [
        ("headline", draft.headline),
        *((("best_bet", draft.best_bet.why),) if draft.best_bet else ()),
        ("storm", draft.storm),
        *((f"mountains.{k}", v) for k, v in draft.mountains.items()),
        *((f"cautions[{i}]", v) for i, v in enumerate(draft.cautions)),
    ]


def check_voice(draft: ReportDraft, digest: ReportDigest) -> list[CheckError]:
    """No "X is the day" in any section; say what the day brings instead."""
    return [
        CheckError(
            "voice",
            f'{section} says {match.group(0)!r}; never write "X is the day",'
            " say what the day brings",
        )
        for section, text in _texts(draft)
        if (match := IS_THE_DAY.search(text))
    ]


RULES = (
    check_headline,
    check_mountains,
    check_snow_numbers,
    check_precip_types,
    check_best_bet,
    check_storm_types,
    check_cautions,
    check_voice,
    check_snow_line,
)


def check(draft: ReportDraft, digest: ReportDigest) -> list[CheckError]:
    """Every rule's errors, in rule order; empty means publishable."""
    return [error for rule in RULES for error in rule(draft, digest)]
