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

from oisochrone.config import get_settings  # noqa: E402
from oisochrone.network import write_extent_from_bbox  # noqa: E402


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


def _bbox_from_osmium(osmium: str, pbf: Path) -> tuple[float, float, float, float] | None:
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
    return None


def _bbox_from_pyosmium(pbf: Path) -> tuple[float, float, float, float] | None:
    import osmium

    reader = osmium.io.Reader(str(pbf))
    try:
        box = reader.header().box()
        if box is not None and box.valid():
            return (
                float(box.bottom_left.lon),
                float(box.bottom_left.lat),
                float(box.top_right.lon),
                float(box.top_right.lat),
            )
    finally:
        reader.close()
    return None


def _filter_highways_osmium(osmium: str, source: Path, dest: Path) -> None:
    subprocess.run(
        [osmium, "tags-filter", str(source), "w/highway", "-o", str(dest)],
        check=True,
    )


def _filter_highways_pyosmium(source: Path, dest: Path) -> None:
    import osmium

    class CollectNodes(osmium.SimpleHandler):
        def __init__(self) -> None:
            super().__init__()
            self.node_ids: set[int] = set()

        def way(self, way: osmium.osm.Way) -> None:
            if "highway" in way.tags:
                self.node_ids.update(node.ref for node in way.nodes)

    class WriteHighways(osmium.SimpleHandler):
        def __init__(self, writer: osmium.SimpleWriter, node_ids: set[int]) -> None:
            super().__init__()
            self.writer = writer
            self.node_ids = node_ids

        def node(self, node: osmium.osm.Node) -> None:
            if node.id in self.node_ids:
                self.writer.add_node(node)

        def way(self, way: osmium.osm.Way) -> None:
            if "highway" in way.tags:
                self.writer.add_way(way)

    print("  pass 1/2: collecting highway node ids (pyosmium)")
    collector = CollectNodes()
    collector.apply_file(str(source))
    print(f"  kept {len(collector.node_ids):,} nodes referenced by highway ways")

    writer = osmium.SimpleWriter(str(dest))
    try:
        print("  pass 2/2: writing highway extract")
        WriteHighways(writer, collector.node_ids).apply_file(str(source))
    finally:
        writer.close()


def download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"Downloading {url}")
    urllib.request.urlretrieve(url, dest, reporthook=_progress("  "))
    print()


def filter_highways(source: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()

    osmium = shutil.which("osmium")
    if osmium:
        print(f"Filtering highway=* ways with osmium into {dest}")
        _filter_highways_osmium(osmium, source, dest)
        return

    print(f"Filtering highway=* ways with pyosmium into {dest}")
    print("  (install osmium-tool for a faster filter)")
    _filter_highways_pyosmium(source, dest)


def bbox_from_pbf(pbf: Path) -> tuple[float, float, float, float]:
    osmium = shutil.which("osmium")
    bbox = _bbox_from_osmium(osmium, pbf) if osmium else None
    if bbox is None:
        bbox = _bbox_from_pyosmium(pbf)
    return bbox or get_settings().osm_bbox


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--drop-full",
        action="store_true",
        help="Delete the full country PBF after the highway-only extract is written.",
    )
    args = parser.parse_args()

    settings = get_settings()
    full_pbf = settings.data_dir / "italy-latest.osm.pbf"
    roads_pbf = settings.osm_pbf_path

    if not full_pbf.is_file():
        download(settings.osm_pbf_url, full_pbf)
    else:
        print(f"Reusing {full_pbf}")

    filter_highways(full_pbf, roads_pbf)
    bbox = bbox_from_pbf(roads_pbf)
    write_extent_from_bbox(
        bbox,
        settings.osm_extent_path,
        {"name": "Italy roads", "source": roads_pbf.name},
    )
    print(f"Wrote extent {settings.osm_extent_path} {bbox}")

    if args.drop_full:
        full_pbf.unlink()
        print(f"Removed {full_pbf}")

    print("Done. Point OSM_SOURCE=pbf at the highway extract and start the demo.")


if __name__ == "__main__":
    main()
