"""Generate config/stations.json: GHCN-Daily stations near each mountain.

Usage (run once; the output is committed):

    curl -O https://www.ncei.noaa.gov/pub/data/ghcn/daily/ghcnd-stations.txt
    curl -O https://www.ncei.noaa.gov/pub/data/ghcn/daily/ghcnd-inventory.txt
    python scripts/make_stations.py ghcnd-stations.txt ghcnd-inventory.txt

A station is listed for a mountain when it is within 20 mi of the mountain's
configured point and its inventory shows `SNOW` in 2024 or later (the
inventory gives years only, so a station whose last 2024 report came before
2024-01-20 is listed and simply has no rows). Mount Washington (USW00014755)
is listed for Loon, Cranmore, Sunday River, and Sugarloaf whatever its
distance. A station near two mountains has one entry per mountain. See
docs/design/data-sources.md, "GHCN-Daily stations".
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RADIUS_MI = 20.0
SINCE_YEAR = 2024
MOUNT_WASHINGTON = "USW00014755"
MOUNT_WASHINGTON_FOR = ("loon", "cranmore", "sunday_river", "sugarloaf")
STATES = {"VT", "NH", "ME", "NY", "MA"}
EARTH_RADIUS_MI = 3958.8
FT_PER_M = 3.28084


def distance_mi(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_MI * math.asin(math.sqrt(a))


def read_stations(path: Path) -> dict[str, dict[str, object]]:
    """US stations in the region, keyed by id (fixed-width `ghcnd-stations.txt`)."""
    out: dict[str, dict[str, object]] = {}
    for line in path.read_text(encoding="ascii", errors="replace").splitlines():
        sid, state = line[0:11], line[38:40]
        if not sid.startswith("US") or state not in STATES:
            continue
        elev_m = float(line[31:37])
        out[sid] = {
            "id": sid,
            "name": f"{line[41:71].strip()}, {state}",
            "lat": float(line[12:20]),
            "lon": float(line[21:30]),
            "elev_ft": None if elev_m <= -999 else round(elev_m * FT_PER_M),
        }
    return out


def snow_since(path: Path, ids: set[str]) -> set[str]:
    """Ids whose inventory has `SNOW` with a last year of `SINCE_YEAR` or later."""
    out = set()
    for line in path.read_text(encoding="ascii", errors="replace").splitlines():
        parts = line.split()
        if (
            len(parts) == 6
            and parts[0] in ids
            and parts[3] == "SNOW"
            and int(parts[5]) >= SINCE_YEAR
        ):
            out.add(parts[0])
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("stations", type=Path, help="ghcnd-stations.txt")
    parser.add_argument("inventory", type=Path, help="ghcnd-inventory.txt")
    parser.add_argument("--mountains", type=Path, default=ROOT / "config" / "mountains.json")
    parser.add_argument("--out", type=Path, default=ROOT / "config" / "stations.json")
    args = parser.parse_args(argv)

    mountains = json.loads(args.mountains.read_text(encoding="utf-8"))
    stations = read_stations(args.stations)
    with_snow = snow_since(args.inventory, set(stations))
    entries = []
    for m in mountains:
        for sid in sorted(with_snow):
            s = stations[sid]
            miles = distance_mi(m["lat"], m["lon"], float(s["lat"]), float(s["lon"]))  # type: ignore[arg-type]
            listed = miles <= RADIUS_MI or (
                sid == MOUNT_WASHINGTON and m["key"] in MOUNT_WASHINGTON_FOR
            )
            if listed:
                entries.append({**s, "mountain": m["key"], "distance_mi": round(miles, 1)})
    entries.sort(key=lambda e: (e["mountain"], e["distance_mi"], e["id"]))
    doc = {
        "generated": datetime.now(UTC).date().isoformat(),
        "source": "NCEI ghcnd-stations.txt and ghcnd-inventory.txt (scripts/make_stations.py)",
        "rule": f"SNOW in {SINCE_YEAR} or later, within {RADIUS_MI:g} mi;"
        f" {MOUNT_WASHINGTON} for {', '.join(MOUNT_WASHINGTON_FOR)}",
        "stations": entries,
    }
    args.out.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    counts: dict[str, int] = {}
    for e in entries:
        counts[e["mountain"]] = counts.get(e["mountain"], 0) + 1
    print(json.dumps({"entries": len(entries), "per_mountain": counts}), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
