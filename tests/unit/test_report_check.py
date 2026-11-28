"""report-check rules, with regression fixtures from the 2026-11-25 sample run.

`draft-first.json` carries the three precip-type errors of the sample run's
first narrative (finding 8): Killington, Cranmore, and Sunday River described
as rain or mix where `precip_type` was ice. `draft-checked.json` is the
corrected narrative and must pass every rule.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from hazelst import report_check
from hazelst.agents.schemas import ReportDraft
from hazelst.digest import ReportDigest
from hazelst.report_check import check, precip_words

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "report" / "2026-11-25"


def load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / name).read_text())
    return data


def digest(**patch: Any) -> ReportDigest:
    data = load("digest.json")
    data.update(patch)
    return ReportDigest.model_validate(data)


def draft(name: str = "draft-checked.json", **mountains: str) -> ReportDraft:
    data = load(name)
    data["mountains"].update(mountains)
    return ReportDraft.model_validate(data)


def with_draft(**fields: Any) -> ReportDraft:
    data = load("draft-checked.json") | fields
    return ReportDraft.model_validate(data)


def edit_mountain(data: dict[str, Any], key: str, **fields: Any) -> dict[str, Any]:
    for m in data["mountains"]:
        if m["key"] == key:
            m.update(fields)
    return data


def rules(errors: list[report_check.CheckError]) -> list[str]:
    return [e.rule for e in errors]


# --- sample-run regression ------------------------------------------------------------


def test_checked_narrative_passes() -> None:
    assert check(draft(), digest()) == []


def test_first_draft_fails_the_three_precip_type_errors() -> None:
    errors = check(draft("draft-first.json"), digest())
    assert rules(errors) == ["precip_type"] * 3
    text = "\n".join(map(str, errors))
    for name in ("Killington", "Cranmore", "Sunday River"):
        assert name in text
    assert all("precip_type is ice" in e.message for e in errors)


@pytest.mark.parametrize(
    ("key", "text", "word"),
    [
        (
            "killington",
            "Dry through Thursday midday, then rain overnight that turns the summit"
            " to slush Friday.",
            "rain",
        ),
        ("cranmore", "A wintry mix Thursday night even on the low summit.", "wintry mix"),
        ("sunday_river", "Rain Thursday night that lingers into Friday.", "Rain"),
    ],
)
def test_each_sample_run_error_fails_alone(key: str, text: str, word: str) -> None:
    (error,) = check(draft(**{key: text}), digest())
    assert error.rule == "precip_type"
    assert repr(word) in error.message


def test_published_stratton_wording_also_fails() -> None:
    # The published sample narrative still said "wintry mix" for Stratton Thursday (ice).
    text = "A wintry mix starts late Thursday morning (NWS Albany)."
    (error,) = check(draft(stratton=text), digest())
    assert "Stratton Thu 26" in error.message


# --- precip-type words ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("freezing rain Thursday night", [("ice", "freezing rain")]),
        ("rain to the top Friday", [("rain", "rain")]),
        ("snow showers Saturday", [("snow", "snow showers")]),
        ("a wintry mix of sleet", [("mix", "wintry mix"), ("mix", "sleet")]),
        ("glaze and icy trails", [("ice", "glaze"), ("ice", "icy")]),
        ("almost no snow", []),
        ("not a drop of rain", []),
        ("dry until afternoon", []),
        ("dry through Thursday midday", []),
        ("dry Saturday", [("dry", "dry")]),
        ("rain at the base, snow up top", [("rain", "rain"), ("snow", "snow")]),
    ],
)
def test_precip_words(text: str, expected: list[tuple[str, str]]) -> None:
    assert precip_words(text) == expected


def test_mix_day_accepts_snow_and_rain_words() -> None:
    # Killington Saturday is `mix`.
    text = "Snow Saturday, with rain mixed in at times."
    assert check(draft(killington=text), digest()) == []


def test_snow_word_on_ice_day_fails() -> None:
    (error,) = check(draft(sugarloaf="Snow Thursday night."), digest())
    assert error.rule == "precip_type"


def test_precip_word_in_storm_section_checks_named_mountains() -> None:
    data = load("draft-checked.json")
    data["storm"] = "Friday brings rain to Sugarloaf."
    (error,) = check(ReportDraft.model_validate(data), digest())
    assert error.rule == "precip_type" and "Sugarloaf Fri 27" in error.message


def test_regional_precip_words_are_not_checked() -> None:
    data = load("draft-checked.json")
    data["storm"] = "Rain everywhere Friday."
    assert check(ReportDraft.model_validate(data), digest()) == []


def test_clause_without_mountain_follows_its_sentence() -> None:
    # From a live report-write draft: "it" is Killington (Saturday mix), not the best bet.
    why = (
        "Saturday is Sugarloaf's one day of plain snow at the summit. Killington gets more"
        " on Saturday, 0.9″, but it falls as a mix."
    )
    bet = {"mountain": "sugarloaf", "day": "2026-11-28", "why": why}
    assert check(with_draft(best_bet=bet), digest()) == []


def test_sunday_river_name_is_not_a_day() -> None:
    # "Sunday" inside "Sunday River" must not tie the clause to Sunday (dry).
    text = "Sunday River holds ice Thursday night."
    assert check(draft(sunday_river=text), digest()) == []


# --- mountains -------------------------------------------------------------------------


def test_every_mountain_exactly_once() -> None:
    data = load("draft-checked.json")
    del data["mountains"]["pico"]
    data["mountains"]["stowe"] = "Not on the roster."
    errors = check(ReportDraft.model_validate(data), digest())
    assert [str(e) for e in errors] == [
        "mountains: missing entry for pico",
        "mountains: unknown mountain 'stowe'",
    ]


# --- snow numbers ------------------------------------------------------------------------


def test_snow_number_must_match_json() -> None:
    errors = check(draft(killington="About 6″ Saturday."), digest())
    assert rules(errors) == ["snow_number"]
    assert "Killington" in errors[0].message


def test_snow_number_within_tolerance_or_range_passes() -> None:
    # Killington Saturday 0.9″; 5d range 0–2″.
    assert check(draft(killington="About 1.3″ Saturday; models 0–2″."), digest()) == []


def test_regional_snow_number_checked_against_any_mountain() -> None:
    data = load("draft-checked.json")
    data["storm"] = "Up to 12 inches by the weekend."
    errors = check(ReportDraft.model_validate(data), digest())
    assert rules(errors) == ["snow_number"]


def test_ice_and_liquid_amounts_are_not_snow() -> None:
    text = "Up to 0.5″ of ice Thursday night and 3″ of liquid."
    errors = check(draft(sugarloaf=text), digest())
    assert "snow_number" not in rules(errors)


# --- best bet --------------------------------------------------------------------------


def test_best_bet_unknown_mountain_and_day() -> None:
    errors = check(
        with_draft(best_bet={"mountain": "stowe", "day": "2026-12-02", "why": "Powder."}),
        digest(),
    )
    assert [str(e) for e in errors] == [
        "best_bet: unknown mountain 'stowe'",
        "best_bet: 2026-12-02 is not a forecast day in the digest",
    ]


def test_best_bet_must_be_open() -> None:
    data = edit_mountain(load("digest.json"), "sunday_river", season_open="2026-11-28")
    errors = check(draft(), ReportDigest.model_validate(data))
    assert [str(e) for e in errors] == [
        "best_bet: Sunday River opens 2026-11-28, after the best-bet day 2026-11-26"
    ]


# --- storm type ------------------------------------------------------------------------


def test_storm_type_needs_support() -> None:
    errors = check(
        with_draft(headline="An Alberta clipper brings ice to Killington Thursday"), digest()
    )
    assert [e.rule for e in errors] == ["storm_type"]
    assert "clipper" in errors[0].message


def test_storm_type_from_discussion_only() -> None:
    # The fixture's `coastal` comes from GYX; without that word and storm_type it fails.
    data = load("digest.json")
    for d in data["discussions"]:
        d["storm_words"] = [w for w in d["storm_words"] if w != "coastal"]
    for m in data["mountains"]:
        m["storm_type"] = None
    errors = check(draft(), ReportDigest.model_validate(data))
    assert rules(errors) == ["storm_type"]


def test_upslope_needs_tag_or_discussion() -> None:
    text = "NW-flow upslope keeps it snowing on the summit Saturday."
    assert rules(check(draft(loon=text), digest())) == ["storm_type"]
    data = edit_mountain(load("digest.json"), "pico")
    data["mountains"][1]["days"][3]["tags"].append("nw_upslope")
    assert check(draft(loon=text), ReportDigest.model_validate(data)) == []


# --- cautions --------------------------------------------------------------------------


def test_active_alert_must_be_in_cautions() -> None:
    data = edit_mountain(
        load("digest.json"), "stratton", alerts=[{"event": "Winter Weather Advisory"}]
    )
    errors = check(draft(), ReportDigest.model_validate(data))
    assert [str(e) for e in errors] == [
        "cautions: active alert 'Winter Weather Advisory' is not in cautions"
    ]
    fixed = with_draft(
        cautions=[*load("draft-checked.json")["cautions"], "Winter Weather Advisory for Stratton."]
    )
    assert check(fixed, ReportDigest.model_validate(data)) == []


def test_wind_hold_likely_day_must_be_in_cautions() -> None:
    data = load("digest.json")
    loon = next(m for m in data["mountains"] if m["key"] == "loon")
    loon["days"][1]["tags"].append("wind_hold_likely")  # Thursday
    dig = ReportDigest.model_validate(data)
    ice_only = load("draft-checked.json")["cautions"][1:]
    errors = check(with_draft(cautions=ice_only), dig)
    assert [str(e) for e in errors] == [
        "cautions: wind hold likely at Loon Thursday is not in cautions"
    ]
    fixed = with_draft(
        cautions=["Wind holds likely at Loon Thursday afternoon (gusts near 48 mph)."]
    )
    assert check(fixed, dig) == []
    wrong_day = with_draft(cautions=["Wind holds likely at Loon Friday."])
    assert rules(check(wrong_day, dig)) == ["cautions"]


def test_errors_are_strings_for_revise() -> None:
    errors = check(draft("draft-first.json"), digest())
    assert all(str(e).startswith("precip_type: ") for e in errors)
    assert date(2026, 11, 26) in digest().days


# --- phrases and elevations (J33) --------------------------------------------------------
# Fixture days used below (digest.json, re-derived with per-elevation types):
#   Fri 27: Killington ice (base rain, freezing rain at the summit); Stratton rain
#           (freezing rain at the summit); Loon rain (no freezing rain).
#   Sat 28: Loon snow at the summit, rain at the base.


def with_storm(text: str) -> ReportDraft:
    return with_draft(storm=text)


def test_with_binds_each_type_to_its_own_mountains() -> None:
    # Run 181's sentence shape: the "with" phrase names its own mountains.
    text = (
        "Friday brings freezing rain to Killington, Pico, Sunday River, and Sugarloaf,"
        " with rain at Loon."
    )
    assert check(with_storm(text), digest()) == []


def test_with_split_stays_strict() -> None:
    errors = check(
        with_storm("Friday brings rain to Killington, with freezing rain at Loon."), digest()
    )
    assert [str(e) for e in errors] == [
        "precip_type: Killington Fri 27: storm says 'rain' (rain); precip_type is ice",
        "precip_type: Loon Fri 27: storm says 'freezing rain' (ice); precip_type is rain;"
        " freezing_rain_at is []",
    ]


def test_snow_number_after_with_may_follow_the_list_before_it() -> None:
    # "2.7″ at Killington with a 0.7–6.3″ range and 0.1–4.5″ at Pico" (run 8): the range
    # follows Killington. Killington's 5d range in the fixture is 0–2″.
    text = "Up to 1.5″ at Killington with a 0–2″ range, and 0.5″ at Pico."
    assert check(with_storm(text), digest()) == []


def test_list_items_bind_their_own_day() -> None:
    # Each word is checked on its own day, not on any day of the sentence.
    errors = check(draft(killington="Dry Thursday, freezing rain Saturday."), digest())
    assert [e.message.split(":")[0] for e in errors] == ["Killington Thu 26", "Killington Sat 28"]
    ok = "Freezing rain Thursday, a mix Saturday, and dry Wednesday."
    assert check(draft(killington=ok), digest()) == []


def test_and_splits_days_with_their_own_claims() -> None:
    errors = check(draft(loon="Light snow Saturday and freezing rain Friday."), digest())
    assert [str(e) for e in errors] == [
        "precip_type: Loon Fri 27: mountains.loon says 'freezing rain' (ice); precip_type is"
        " rain; freezing_rain_at is []"
    ]


def test_claim_matches_its_stated_elevation() -> None:
    assert check(draft(loon="Snow up top Saturday and rain at the base."), digest()) == []
    (error,) = check(draft(loon="Rain Saturday."), digest())
    assert error.message.endswith("precip_type is snow")
    (error,) = check(draft(loon="Snow at the base Saturday."), digest())
    assert "at the base; base_precip_type is rain" in error.message


def test_freezing_rain_holds_where_the_digest_has_it() -> None:
    # Run 147: summit rain with freezing-rain hours ("Freezing rain Monday at Sugarloaf").
    assert check(with_draft(cautions=["Freezing rain Friday at Stratton."]), digest()) == []
    assert check(draft(stratton="Freezing rain at the summit Friday."), digest()) == []
    (error,) = check(draft(stratton="Freezing rain at the base Friday."), digest())
    assert "at the base; base_precip_type is rain; freezing_rain_at is [summit]" in error.message
    (error,) = check(with_draft(cautions=["Freezing rain Friday at Loon."]), digest())
    assert error.message.endswith("freezing_rain_at is []")


# --- headline ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "headline",
    [
        "Freezing rain Thursday at all eight; no new snow through Sunday",
        "No new snow through Sunday at any of the eight",
        "Up to 1″ for Killington Saturday; freezing rain Thursday",
    ],
)
def test_headline_in_alert_shape_passes(headline: str) -> None:
    assert check(with_draft(headline=headline), digest()) == []


def test_headline_rules() -> None:
    long = "Ski the cold, quiet Thanksgiving morning; a coastal storm brings ice Thursday night"
    assert [str(e) for e in check(with_draft(headline=long), digest())] == [
        "headline: 13 words; at most 12",
        'headline: names no resort (name them, or say "all eight")',
    ]
    (error,) = check(with_draft(headline="Freezing rain at Sugarloaf Tuesday"), digest())
    assert str(error) == "headline: 'Tuesday' is not a forecast day in the digest"
    # Amounts and precip words in the headline go through the same checks as the prose.
    errors = check(with_draft(headline="6″ for Killington Saturday; rain Thursday"), digest())
    assert [e.rule for e in errors] == ["snow_number", "precip_type"]


# --- mountain groups, "otherwise", optional best bet (J37) ------------------------------
# Fixture Sat 28: the four Vermont mountains mix (rain at the base), Loon and Sugarloaf
# snow, Cranmore and Sunday River dry. Thu 26: ice at every summit.


def test_group_names_its_members_for_precip_types() -> None:
    text = "Saturday: wet snow mixed with rain on the Vermont summits, rain at the bases."
    assert check(with_storm(text), digest()) == []
    (error,) = check(with_storm("Saturday brings snow to the Maine mountains."), digest())
    assert str(error) == (
        "precip_type: Sunday River Sat 28: storm says 'snow' (snow); precip_type is dry"
    )


def test_all_eight_is_every_mountain() -> None:
    assert check(with_storm("Freezing rain Thursday at all eight."), digest()) == []
    errors = check(with_storm("Rain Saturday at all eight."), digest())
    assert [e.message.split(":")[0] for e in errors] == [
        "Loon Sat 28",
        "Cranmore Sat 28",
        "Sunday River Sat 28",
        "Sugarloaf Sat 28",
    ]


def test_the_rest_is_every_mountain_not_yet_named_in_the_sentence() -> None:
    ok = "Saturday: snow at Loon and Sugarloaf, a mix at the Vermont mountains; the rest stay dry."
    assert check(with_storm(ok), digest()) == []
    errors = check(with_storm("Saturday: snow at Loon and Sugarloaf; the rest stay dry."), digest())
    assert [e.message.split(":")[0] for e in errors] == [
        "Killington Sat 28",
        "Pico Sat 28",
        "Sugarbush Sat 28",
        "Stratton Sat 28",
    ]
    # Named in an earlier sentence does not count: "the rest" is then all eight.
    errors = check(with_storm("Saturday: snow at Loon. The rest stay dry Saturday."), digest())
    assert "Loon Sat 28" in {e.message.split(":")[0] for e in errors}


def test_the_rest_of_the_week_is_not_a_group() -> None:
    names = report_check._Names(digest())
    assert names.mountains_in("quiet the rest of the week")[0] == []
    assert [m.key for m in names.mountains_in("the rest stay dry", names.digest.mountains[:6])[0]]


def test_group_snow_amount_must_fit_every_member() -> None:
    # A list passes when any mountain has the number; a group must fit each member.
    assert check(with_storm("Saturday: 0.8″ at Stratton and Loon."), digest()) == []
    (error,) = check(with_storm("Saturday: up to 1″ at the New Hampshire mountains."), digest())
    assert str(error) == (
        "snow_number: storm: '1″' is said of a group, but matches no snow value for Cranmore"
        " (name the mountains it fits)"
    )


def test_group_counts_for_a_wind_hold_caution() -> None:
    data = load("digest.json")
    loon = next(m for m in data["mountains"] if m["key"] == "loon")
    loon["days"][1]["tags"].append("wind_hold_likely")  # Thursday
    dig = ReportDigest.model_validate(data)
    caution = "Thursday: freezing rain, and wind holds likely on the New Hampshire mountains."
    assert check(with_draft(cautions=[caution]), dig) == []


def test_headline_group_is_not_a_resort_name() -> None:
    (error,) = check(
        with_draft(headline="Freezing rain Thursday on the Vermont mountains"), digest()
    )
    assert str(error) == 'headline: names no resort (name them, or say "all eight")'


def test_otherwise_covers_every_day_the_entry_has_not_named() -> None:
    ok = (
        "Saturday: 0.9″ of wet snow mixed with rain up top;"
        " freezing rain Thursday and Friday, dry otherwise."
    )
    assert check(draft(killington=ok), digest()) == []
    (error,) = check(draft(loon="Saturday: 0.7″ of snow up top; dry otherwise."), digest())
    assert str(error) == (
        "precip_type: Loon Thu 26/Fri 27: mountains.loon says 'dry' (dry); precip_type is ice/rain"
    )


def test_through_a_day_covers_the_days_up_to_it() -> None:
    assert check(draft(cranmore="Little snow through Sunday."), digest()) == []
    (error,) = check(draft(cranmore="A little snow Saturday."), digest())
    assert error.rule == "precip_type"
    (error,) = check(draft(cranmore="Snow through Thursday."), digest())
    assert error.message.startswith("Cranmore Wed 25/Thu 26:")


def test_best_bet_may_be_null() -> None:
    assert check(with_draft(best_bet=None), digest()) == []
    data = load("draft-checked.json")
    del data["best_bet"]
    with pytest.raises(ValueError, match="best_bet"):
        ReportDraft.model_validate(data)


# --- phrasing found in the J37 comparison runs --------------------------------------------
# Each was a false error on a correct draft; each test also keeps the strict direction.


def test_elevation_is_read_within_its_list_part() -> None:
    # "snow up top, rain at the base": the elevation word nearest by offset was "up top".
    assert check(draft(loon="Saturday: 0.7″ of snow up top, rain at the base."), digest()) == []
    errors = check(draft(loon="Saturday: rain up top, snow at the base."), digest())
    assert [e.message.split(" says ")[1].split(";")[0] for e in errors] == [
        "'rain' (rain) at the summit",
        "'snow' (snow) at the base",
    ]


def test_with_does_not_split_a_mix_claim() -> None:
    ok = "Saturday: snow at Loon, and snow mixes with rain at Killington."
    assert check(with_storm(ok), digest()) == []
    bad = "Saturday: snow at Loon, and wet snow mixed with rain at Cranmore."
    words = [e.message.split(" says ")[1].split(" ")[0] for e in check(with_storm(bad), digest())]
    assert words == ["'snow'", "'mixed'", "'rain'"]  # each about Cranmore (dry), none about Loon


def test_a_claimless_part_with_its_own_day_does_not_take_the_claim() -> None:
    # "..., and Tuesday's gusts could hold the lifts" moved the freezing rain to Tuesday.
    ok = (
        "Friday is rain, with freezing rain at the summit and gusts to 40 mph,"
        " and Sunday's gusts could slow the lifts."
    )
    assert check(draft(stratton=ok), digest()) == []
    bad = "Friday is rain, and Sunday brings freezing rain at the summit."
    (error,) = check(draft(stratton=bad), digest())
    assert error.message.startswith("Stratton Sun 29:")


def test_a_leading_claim_keeps_the_clause_context() -> None:
    after_today = (
        "Saturday: 0.9″ of wet snow mixed with rain up top, rain at the base,"
        " after freezing rain on the summit Friday."
    )
    assert check(draft(killington=after_today), digest()) == []
    assert check(draft(killington="Snow and rain mixed Saturday."), digest()) == []
    # After a comma the claim stays with the mountains before it, not the next ones.
    ok = "Friday glazes Killington up top with freezing rain, Loon gets rain."
    assert check(with_storm(ok), digest()) == []
    bad = "Friday glazes Loon up top with freezing rain, Killington gets rain."
    assert [e.message.split(":")[0] for e in check(with_storm(bad), digest())] == [
        "Loon Fri 27",
        "Loon Fri 27",
        "Killington Fri 27",
    ]
