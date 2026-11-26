"""Resort report parsing: declarative presets over three shared parsers.

A preset (`presets.json`) names a `kind` (`json`, `embedded_json`, `html`), a
URL template, and a field map. `parse()` turns one response body into a
`Report`; it is pure and never fetches, so it reruns over stored `fetch` rows.
See docs/design/data-sources.md, "Resort adapters".

A body that does not parse, or parses to no report data, raises
`AdapterError`: a blocked or changed page is a failure, never zeros.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cache
from importlib import resources
from typing import Any, Literal
from zoneinfo import ZoneInfo

from selectolax.lexbor import LexborHTMLParser, LexborNode

Kind = Literal["json", "embedded_json", "html"]
KINDS: frozenset[str] = frozenset({"json", "embedded_json", "html"})

SNOW_FIELDS = ("new_24h_in", "new_48h_in", "new_7d_in", "base_in", "season_in")
COUNT_FIELDS = (
    "trails_open",
    "trails_total",
    "lifts_open",
    "lifts_total",
    "lifts_hold",
)
# `trails` / `lifts` with the `ratio` parser fill the `_open` and `_total` pair.
RATIO_FIELDS = ("trails", "lifts")
VALUE_FIELDS = SNOW_FIELDS + COUNT_FIELDS
FIELDS = (*VALUE_FIELDS, "reported_at", *RATIO_FIELDS)
PARSERS = frozenset({"inches", "range_max", "ratio", "int", "datetime"})

_NO_VALUE = frozenset({"", "--", "-", "n/a", "na", "none", "null"})
_NUMBER = re.compile(r"^-?\d+(?:\.\d+)?$")
_INCHES_SUFFIX = re.compile(r'\s*(?:"|\u201d|\u2033|in\.?|inches)$', re.IGNORECASE)
_RANGE = re.compile(r"^(-?\d+(?:\.\d+)?)\s*[-\u2013\u2014]\s*(-?\d+(?:\.\d+)?)$")
_RATIO = re.compile(r"^(\d+)\s*/\s*(\d+)$")
_SPACE = re.compile(r"\s+")


class AdapterError(ValueError):
    """A response that does not parse as its preset expects."""


@dataclass(frozen=True)
class Prefetch:
    """A config document read before the data URL (`mtnpowder` widget config)."""

    url: str
    vars: dict[str, str]
    cache_hours: float


@dataclass(frozen=True)
class Preset:
    name: str
    kind: Kind
    url: str
    fields: dict[str, Any]
    prefetch: Prefetch | None = None
    scope: str | None = None
    labels: dict[str, str] | None = None
    script: str | None = None


@dataclass(frozen=True)
class Report:
    """Parsed values (`None` = not reported) plus the raw text each came from."""

    values: dict[str, float | int | None]
    reported_at: datetime | None
    raw: dict[str, Any] = field(default_factory=dict)


# --- presets -------------------------------------------------------------------


def _preset(name: str, spec: Mapping[str, Any]) -> Preset:
    kind = spec.get("kind")
    if kind not in KINDS:
        raise ValueError(f"preset {name}: unknown kind {kind!r}")
    fields = dict(spec.get("fields") or {})
    unknown = sorted(set(fields) - set(FIELDS))
    if unknown:
        raise ValueError(f"preset {name}: unknown fields {unknown}")
    for key, entry in fields.items():
        parse = entry.get("parse") if isinstance(entry, Mapping) else None
        if parse is not None and parse not in PARSERS:
            raise ValueError(f"preset {name}.{key}: unknown parser {parse!r}")
        if key in RATIO_FIELDS and parse != "ratio":
            raise ValueError(f"preset {name}.{key}: needs the ratio parser")
    if kind == "embedded_json" and not spec.get("script"):
        raise ValueError(f"preset {name}: embedded_json needs a script selector")
    prefetch = spec.get("prefetch")
    return Preset(
        name=name,
        kind=kind,
        url=str(spec["url"]),
        fields=fields,
        prefetch=Prefetch(
            url=str(prefetch["url"]),
            vars=dict(prefetch["vars"]),
            cache_hours=float(prefetch.get("cache_hours", 24)),
        )
        if prefetch
        else None,
        scope=spec.get("scope"),
        labels=dict(spec["labels"]) if spec.get("labels") else None,
        script=spec.get("script"),
    )


@cache
def presets() -> dict[str, Preset]:
    """The packaged presets, validated once."""
    text = resources.files("hazelst.sources.resorts").joinpath("presets.json").read_text("utf-8")
    return {name: _preset(name, spec) for name, spec in json.loads(text).items()}


def preset(name: str) -> Preset:
    try:
        return presets()[name]
    except KeyError:
        raise ValueError(f"unknown resort preset {name!r}; known: {sorted(presets())}") from None


def format_url(template: str, values: Mapping[str, Any]) -> str:
    try:
        return template.format(**values)
    except KeyError as exc:
        raise ValueError(f"URL template {template!r} needs parameter {exc.args[0]!r}") from None


# --- value parsers ---------------------------------------------------------------


def _clean(value: Any) -> str | None:
    """Collapse whitespace; `None` for an empty or placeholder value."""
    if value is None:
        return None
    text = _SPACE.sub(" ", str(value)).strip()
    return None if text.lower() in _NO_VALUE else text


def parse_inches(value: Any) -> float | None:
    if isinstance(value, bool):
        raise AdapterError(f"not an inch value: {value!r}")
    if isinstance(value, int | float):
        return float(value)
    text = _clean(value)
    if text is None:
        return None
    text = _INCHES_SUFFIX.sub("", text).strip()
    if not _NUMBER.match(text):
        raise AdapterError(f"not an inch value: {value!r}")
    return float(text)


def parse_range_max(value: Any) -> float | None:
    """`"26-40\\""` -> 40.0 (the upper bound); a single value parses as inches."""
    text = _clean(value)
    if text is None or isinstance(value, int | float):
        return parse_inches(value)
    match = _RANGE.match(_INCHES_SUFFIX.sub("", text).strip())
    if match:
        return max(float(match.group(1)), float(match.group(2)))
    return parse_inches(value)


def parse_int(value: Any) -> int | None:
    if isinstance(value, bool):
        raise AdapterError(f"not an integer: {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    text = _clean(value)
    if text is None:
        return None
    if not re.fullmatch(r"-?\d+", text):
        raise AdapterError(f"not an integer: {value!r}")
    return int(text)


def parse_ratio(value: Any) -> tuple[int, int] | None:
    """`"55/62"` -> (55, 62)."""
    text = _clean(value)
    if text is None:
        return None
    match = _RATIO.match(text)
    if not match:
        raise AdapterError(f"not an open/total ratio: {value!r}")
    return int(match.group(1)), int(match.group(2))


def parse_datetime(
    value: Any,
    *,
    regex: str | None = None,
    format: str | None = None,
    tz: str | None = None,
) -> datetime | None:
    """ISO-8601 (`Z`, `-05:00`, `-0500`), or `format` in local zone `tz`; returns UTC."""
    text = _clean(value)
    if text is None:
        return None
    if regex is not None:
        match = re.search(regex, text)
        if not match:
            raise AdapterError(f"date pattern {regex!r} not found in {text[:80]!r}")
        text = match.group(1).strip()
    try:
        parsed = (
            datetime.strptime(text, format)
            if format is not None
            else datetime.fromisoformat(text.replace("Z", "+00:00"))
        )
    except ValueError:
        raise AdapterError(f"not a date: {text[:80]!r}") from None
    if parsed.tzinfo is None:
        if tz is None:
            raise AdapterError(f"date without a zone and no tz: {text!r}")
        parsed = parsed.replace(tzinfo=ZoneInfo(tz))
    return parsed.astimezone(UTC)


def _default_parser(name: str) -> str:
    if name == "reported_at":
        return "datetime"
    if name in COUNT_FIELDS:
        return "int"
    if name in RATIO_FIELDS:
        return "ratio"
    return "inches"


def _apply(parser: str, raw: Any, entry: Mapping[str, Any]) -> Any:
    if parser == "inches":
        return parse_inches(raw)
    if parser == "range_max":
        return parse_range_max(raw)
    if parser == "int":
        return parse_int(raw)
    if parser == "ratio":
        return parse_ratio(raw)
    return parse_datetime(
        raw, regex=entry.get("regex"), format=entry.get("format"), tz=entry.get("tz")
    )


# --- json paths ------------------------------------------------------------------

_PATH_PART = re.compile(r"([^.\[\]]+)|\[(\d+)\]")


def resolve_path(doc: Any, path: str) -> Any:
    """Follow `a.b[0].c`; `None` when any step is missing."""
    node = doc
    for match in _PATH_PART.finditer(path):
        key, index = match.group(1), match.group(2)
        if key is not None:
            if not isinstance(node, Mapping) or key not in node:
                return None
            node = node[key]
        else:
            if not isinstance(node, list) or int(index) >= len(node):
                return None
            node = node[int(index)]
        if node is None:
            return None
    return node


# --- html ----------------------------------------------------------------------------


def _text(node: LexborNode) -> str:
    return _SPACE.sub(" ", node.text(separator=" ")).strip()


def _matches(node: LexborNode, css: str) -> bool:
    """Whether `node` itself matches `css` (`css_matches` also looks at descendants)."""
    parent = node.parent
    return parent is not None and any(n.mem_id == node.mem_id for n in parent.css(css))


def _select(scope: LexborNode, css: str) -> LexborNode | None:
    """The first element matching `css` within `scope`, the scope itself included."""
    if _matches(scope, css):
        return scope
    return scope.css_first(css)


def _label_value(scope: LexborNode, labels: Mapping[str, str], label: str) -> str | None:
    """Text of the next sibling matching `labels.value` after the label whose text is `label`."""
    wanted = label.casefold()
    for node in scope.css(labels["label"]):
        if _text(node).casefold() != wanted:
            continue
        sibling = node.next
        while sibling is not None:
            if sibling.is_element_node and _matches(sibling, labels["value"]):
                return _text(sibling)
            sibling = sibling.next
        return None
    return None


# --- parse ---------------------------------------------------------------------------


def _json_body(body: bytes) -> Any:
    try:
        return json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise AdapterError("response is not JSON") from None


def _html_tree(body: bytes) -> LexborHTMLParser:
    try:
        return LexborHTMLParser(body.decode("utf-8", errors="replace"))
    except Exception as exc:  # pragma: no cover - lexbor accepts any text
        raise AdapterError(f"response is not HTML: {exc}") from None


def _json_raw(doc: Any, entry: Any, parser: str, spec: Mapping[str, Any]) -> tuple[Any, Any]:
    """(raw, parsed) for one json field: the first path whose value parses non-null."""
    paths = entry.get("path") if isinstance(entry, Mapping) else entry
    candidates: Sequence[str] = [paths] if isinstance(paths, str) else list(paths or [])
    first_raw: Any = None
    for path in candidates:
        raw = resolve_path(doc, path)
        parsed = _apply(parser, raw, spec)
        if parsed is not None:
            return raw, parsed
        if first_raw is None:
            first_raw = raw
    return first_raw, None


def _html_raw(
    scope: LexborNode, entry: Mapping[str, Any], labels: Mapping[str, str] | None
) -> str | None:
    if "select" in entry:
        node = _select(scope, entry["select"])
        return _text(node) if node is not None else None
    if "label" in entry:
        if labels is None:
            raise ValueError("a label field needs the preset's labels block")
        return _label_value(scope, labels, entry["label"])
    raise ValueError(f"html field needs select or label: {entry!r}")


def parse(spec: Preset, body: bytes) -> Report:
    """Parse one response body with `spec`; raises `AdapterError` on a failed parse."""
    doc: Any = None
    scope: LexborNode | None = None
    if spec.kind == "json":
        doc = _json_body(body)
    elif spec.kind == "embedded_json":
        assert spec.script is not None
        node = _html_tree(body).css_first(spec.script)
        if node is None:
            raise AdapterError(f"script {spec.script!r} not found")
        doc = _json_body(node.text().encode("utf-8"))
    else:
        tree = _html_tree(body)
        root = tree.root
        if root is None:
            raise AdapterError("empty HTML document")
        scope = _select(root, spec.scope) if spec.scope else root
        if scope is None:
            raise AdapterError(f"scope {spec.scope!r} not found")

    values: dict[str, float | int | None] = dict.fromkeys(VALUE_FIELDS)
    raw: dict[str, Any] = {}
    reported_at: datetime | None = None
    for name, entry in spec.fields.items():
        options: Mapping[str, Any] = entry if isinstance(entry, Mapping) else {}
        parser = options.get("parse") or _default_parser(name)
        try:
            if scope is None:
                raw_value, parsed = _json_raw(doc, entry, parser, options)
            else:
                raw_value = _html_raw(scope, options, spec.labels)
                parsed = _apply(parser, raw_value, options)
        except AdapterError as exc:
            raise AdapterError(f"{name}: {exc}") from None
        if name == "reported_at" and options.get("regex") and isinstance(raw_value, str):
            match = re.search(options["regex"], _SPACE.sub(" ", raw_value))
            raw_value = match.group(1) if match else raw_value
        raw[name] = raw_value
        if name == "reported_at":
            reported_at = parsed
        elif name in RATIO_FIELDS:
            opened, total = parsed if parsed is not None else (None, None)
            values[f"{name}_open"], values[f"{name}_total"] = opened, total
        else:
            values[name] = parsed

    if reported_at is None:
        raise AdapterError("no report time")
    if all(value is None for value in values.values()):
        raise AdapterError("no report values")
    return Report(values=values, reported_at=reported_at, raw=raw)


def page_text(body: bytes, limit: int = 20_000) -> str:
    """Visible text of a response (HTML stripped of scripts and styles), for extraction."""
    text = body.decode("utf-8", errors="replace")
    if text.lstrip().startswith(("{", "[")):
        return text[:limit]
    tree = LexborHTMLParser(text)
    for node in tree.css("script, style, noscript, svg, template"):
        node.decompose()
    root = tree.body or tree.root
    return (_text(root) if root is not None else "")[:limit]
