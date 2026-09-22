#!/usr/bin/env python3
"""Seed SQLite graph tiles from coordinate_seed.json (or a custom path)."""

from __future__ import annotations

import argparse
import logging
import sys

from oisochrone.config import get_settings
from oisochrone.seed import seed_graph_tiles


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Build missing road-graph tiles into SQLite for each seed coordinate. "
            "Default jobs: foot+bicycle at 15 and 30 min, car at 15 min only. "
            "Larger seeded tiles cover shorter travel times at the same snap point."
        )
    )
    parser.add_argument(
        "--coordinates",
        type=str,
        default=None,
        help="JSON seed file (default: GRAPH_SEED_COORDINATES_PATH / bundled file)",
    )
    parser.add_argument(
        "--modes",
        type=str,
        default=None,
        help="Comma-separated modes (default: GRAPH_SEED_MODES)",
    )
    parser.add_argument(
        "--times",
        type=str,
        default=None,
        help="Comma-separated travel times in minutes (default: GRAPH_SEED_TRAVEL_TIMES_MIN)",
    )
    parser.add_argument(
        "--travel-time-min",
        type=float,
        default=None,
        help="Deprecated: single travel time; prefer --times",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    settings = get_settings()
    modes = None
    if args.modes:
        modes = [part.strip() for part in args.modes.split(",") if part.strip()]
    times = None
    if args.times:
        times = [float(part.strip()) for part in args.times.split(",") if part.strip()]
    coordinates = None
    if args.coordinates:
        from pathlib import Path

        coordinates = Path(args.coordinates).expanduser().resolve()

    result = seed_graph_tiles(
        settings,
        coordinates_path=coordinates,
        modes=modes,
        travel_times_min=times,
        travel_time_min=args.travel_time_min,
    )
    print(
        f"seed done: total={result.total} built={result.built} "
        f"skipped={result.skipped} failed={result.failed}"
    )
    return 1 if result.failed and result.built == 0 and result.skipped == 0 else 0


if __name__ == "__main__":
    sys.exit(main())
