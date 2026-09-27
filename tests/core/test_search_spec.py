"""Tests for SearchSpec and its JSON round trip."""

import json
from dataclasses import replace
from datetime import UTC, date, datetime
from typing import Any

import pytest

from tagalot.core.search import (
    SPEC_VERSION,
    ChoiceFilter,
    RangeFilter,
    SearchSpec,
    SearchSpecError,
    SortKey,
    TextFilter,
    TextMatch,
)

FULL = SearchSpec(
    types=("music.album", "music.song"),
    include=(3, 12),
    exclude=(40,),
    fields=(
        TextFilter("country", "Ice"),
        TextFilter("artist", "The", TextMatch.STARTS_WITH),
        RangeFilter("year", 1990, None),
        RangeFilter("released", date(1997, 9, 22), date(1999, 12, 31)),
        RangeFilter("added", datetime(2026, 1, 1, 12, 30, tzinfo=UTC), None),
        RangeFilter("rating", 3.5, 5.0),
        ChoiceFilter("label", ("One Little Indian", "Elektra")),
        ChoiceFilter("explicit", (True,)),
    ),
    within=77,
    text="björk",
    inherit_tags=True,
    show_contained=True,
    aggregate_up=True,
    sort=(SortKey("year", descending=True), SortKey("title")),
)


def test_defaults() -> None:
    spec = SearchSpec()
    assert spec.types == ()
    assert spec.include == spec.exclude == ()
    assert spec.fields == ()
    assert spec.within is None
    assert spec.text is None
    assert (spec.inherit_tags, spec.show_contained, spec.aggregate_up) == (False, False, False)
    assert spec.sort == (SortKey("title"),)


@pytest.mark.parametrize("spec", [SearchSpec(), FULL], ids=["default", "full"])
def test_json_round_trip(spec: SearchSpec) -> None:
    assert SearchSpec.from_json(spec.to_json()) == spec
    assert SearchSpec.loads(spec.dumps()) == spec
    assert SearchSpec.from_json(json.loads(json.dumps(spec.to_json()))) == spec


def test_dates_come_back_as_dates() -> None:
    back = SearchSpec.loads(FULL.dumps())
    released = back.fields[3]
    added = back.fields[4]
    assert isinstance(released, RangeFilter)
    assert isinstance(added, RangeFilter)
    assert type(released.low) is date
    assert added.low == datetime(2026, 1, 1, 12, 30, tzinfo=UTC)


def test_json_is_versioned_and_readable() -> None:
    data = json.loads(FULL.dumps())
    assert data["version"] == SPEC_VERSION
    assert data["text"] == "björk"  # not escaped
    assert data["fields"][0] == {
        "kind": "text",
        "field": "country",
        "text": "Ice",
        "match": "contains",
    }
    assert data["fields"][1]["match"] == "starts_with"


def test_specs_are_hashable_and_comparable() -> None:
    copy = SearchSpec.loads(FULL.dumps())
    assert hash(copy) == hash(FULL)
    assert {FULL: "cached"}[copy] == "cached"
    assert replace(FULL, text="other") != FULL


@pytest.mark.parametrize(("text", "expected"), [("  jazz ", "jazz"), ("   ", None), (None, None)])
def test_text_is_trimmed_and_blank_means_none(text: str | None, expected: str | None) -> None:
    assert SearchSpec(text=text).text == expected


def test_missing_keys_take_defaults_and_unknown_keys_are_ignored() -> None:
    spec = SearchSpec.from_json({"include": [5], "future_option": {"x": 1}})
    assert spec == SearchSpec(include=(5,))
    assert SearchSpec.from_json({"sort": []}).sort == ()


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"version": 2}, "newer version of Tagalot"),
        ({"include": [0]}, "positive tag ids"),
        ({"include": ["3"]}, "positive tag ids"),
        ({"exclude": [True]}, "positive tag ids"),
        ({"include": 3}, "include must be a list"),
        ({"types": ["music.song", 3]}, "types must be a list of text"),
        ({"within": 0}, "positive entity id"),
        ({"within": "5"}, "positive entity id"),
        ({"text": 5}, "text must be text"),
        ({"inherit_tags": "yes"}, "inherit_tags must be true or false"),
        ({"fields": [{"kind": "range", "field": "year", "low": [1]}]}, "unexpected value"),
        ({"fields": [{"kind": "range", "field": "d", "low": {"$date": "31/12"}}]}, "invalid date"),
        ({"fields": [{"kind": "fuzzy", "field": "x"}]}, "unknown kind 'fuzzy'"),
        ({"fields": [{"kind": "text"}]}, "Invalid field filter"),
        ({"fields": [{"kind": "text", "field": "x"}]}, "'text' must be text"),
        (
            {"fields": [{"kind": "text", "field": "x", "text": "a", "match": "ends"}]},
            "unknown match 'ends'",
        ),
        ({"fields": [{"kind": "choice", "field": "x", "values": [None]}]}, "can't be empty"),
        ({"sort": [{"descending": True}]}, "Invalid sort key"),
    ],
)
def test_invalid_json_is_rejected(data: dict[str, Any], message: str) -> None:
    with pytest.raises(SearchSpecError, match=message):
        SearchSpec.from_json(data)


@pytest.mark.parametrize("text", ["not json", "[1, 2]", "null"])
def test_loads_rejects_non_objects(text: str) -> None:
    with pytest.raises(SearchSpecError):
        SearchSpec.loads(text)


def test_direct_construction_is_validated_too() -> None:
    with pytest.raises(SearchSpecError):
        SearchSpec(include=(-1,))
    with pytest.raises(SearchSpecError):
        SearchSpec(within=0)


def test_text_filters_default_to_contains() -> None:
    assert TextFilter("title", "road").match is TextMatch.CONTAINS
    back = SearchSpec.from_json({"fields": [{"kind": "text", "field": "title", "text": "road"}]})
    assert back.fields == (TextFilter("title", "road", TextMatch.CONTAINS),)


def test_starts_with_round_trips() -> None:
    spec = SearchSpec(fields=(TextFilter("artist", "The", TextMatch.STARTS_WITH),))
    assert SearchSpec.loads(spec.dumps()) == spec
