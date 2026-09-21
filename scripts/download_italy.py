#!/usr/bin/env python3
"""Download the Geofabrik Italy PBF and keep only highway=* roads."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from oisochron.config import get_settings  # noqa: E402
from oisochron.network import write_extent_from_bbox  # noqa: E402


def _progress(prefix: str):
    def hook(block_num: int, block_size: int, total_size: int) -> None:
        downloaded = block_num * block_size
        if total_size <= 0:
            print(f"\r{prefix} {downloaded / 1_048_576:.1f} MB", end="", flush=True)
            return
        pct = min(100.0, downloaded * 100.0 / total_size)
        print(
            f"\r{prefix} {downloaded / 1_048_576:.1f}/{total_size / 1_048_576:.1f} MB ({pct:.0f}%)",
            end="",
            flush=True,
        )

    return hook


def _require_osmium() -> str:
    path = shutil.which("osmium")
    if path is None:
        raise SystemExit(
            "osmium is required. Install osmium-tool (e.g. `brew install osmium-tool` "
            "or `apt install osmium-tool`)."
        )
    return path


def _bbox_from_pbf(osmium: str, pbf: Path) -> tuple[float, float, float, float]:
    result = subprocess.run(
        [osmium, "fileinfo", "--extended", "--json", str(pbf)],
        check=True,
        capture_output=True,
        text=True,
    )
    info = json.loads(result.stdout)
    header_box = (info.get("header") or {}).get("boxes") or []
    if header_box:
        box = header_box[0]
        return float(box[0]), float(box[1]), float(box[2]), float(box[3])

    data_box = ((info.get("data") or {}).get("bbox")) or None
    if data_box:
        return float(data_box[0]), float(data_box[1]), float(data_box[2]), float(data_box[3])

    settings = get_settings()
    return settings.osm_bbox


def download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"Downloading {url}")
    urllib.request.urlretrieve(url, dest, reporthook=_progress("  "))
    print()


def filter_highways(osmium: str, source: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    print(f"Filtering highway=* ways into {dest}")
    subprocess.run(
        [osmium, "tags-filter", str(source), "w/highway", "-o", str(dest)],
        check=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--drop-full",
        action="store_true",
        help="Delete the full country PBF after the highway-only extract is written.",
    )
    args = parser.parse_args()

    settings = get_settings()
    osmium = _require_osmium()
    full_pbf = settings.data_dir / "italy-latest.osm.pbf"
    roads_pbf = settings.osm_pbf_path

    if not full_pbf.is_file():
        download(settings.osm_pbf_url, full_pbf)
    else:
        print(f"Reusing {full_pbf}")

    filter_highways(osmium, full_pbf, roads_pbf)
    bbox = _bbox_from_pbf(osmium, roads_pbf)
    write_extent_from_bbox(
        bbox,
        settings.osm_extent_path,
        {"name": "Italy roads", "source": str(roads_pbf)},
    )
    print(f"Wrote extent {settings.osm_extent_path} {bbox}")

    if args.drop_full:
        full_pbf.unlink()
        print(f"Removed {full_pbf}")

    print("Done. Point OSM_SOURCE=pbf at the highway extract and start the demo.")


if __name__ == "__main__":
    main()
