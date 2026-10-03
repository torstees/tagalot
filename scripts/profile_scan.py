"""Profile scanning and thumbnailing a folder (#131; DESIGN.md §3 "Performance on shares").

    uv run python scripts/profile_scan.py PATH [--theme assets2d] [--threads 1 4 8]
                                               [--thumbnails 500] [--cprofile]

makes a fresh keep in scratch/profile/ with one root at PATH (a local folder or a network
share; Tagalot only reads it), scans it with the theme, times each phase from the scan's
own progress messages, scans again (nothing changed: the cost of a routine rescan), then
makes thumbnails for the first ``--thumbnails`` items, one thread at a time and with each
``--threads`` count, from a cold cache each time. ``--cprofile`` also profiles the first
scan and prints where its time went.
"""

import argparse
import cProfile
import io
import pstats
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sqlalchemy import select

from tagalot.core.keep import DEFAULT_EXCLUDES, RootConfig, ThemeRef, create_keep
from tagalot.core.models import Entity
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.themes.loader import load_themes

SCRATCH = Path(__file__).resolve().parents[1] / "scratch" / "profile"


class PhaseClock:
    """Times a scan's phases from its progress messages ("Walking…", "Fingerprinting…")."""

    def __init__(self) -> None:
        self.start = time.perf_counter()
        self.last = self.start
        self.current = "Starting"
        self.phases: list[tuple[str, float]] = []

    def __call__(self, message: str) -> None:
        now = time.perf_counter()
        self.phases.append((self.current, now - self.last))
        self.current, self.last = message, now

    def finish(self) -> list[tuple[str, float]]:
        self("done")
        return phase_totals(self.phases)


def phase_totals(phases: list[tuple[str, float]]) -> list[tuple[str, float]]:
    """Each phase's total time: steps of one phase ("Ingesting 100 of 9320…", "Ingesting
    200 of 9320…") are added up under its first word."""
    totals: dict[str, float] = {}
    for name, seconds in phases:
        phase = name.split()[0].rstrip("…") if name else name
        totals[phase] = totals.get(phase, 0.0) + seconds
    return [(name, seconds) for name, seconds in totals.items() if seconds >= 0.005]


def fresh_keep(path: Path, theme_id: str, version: int) -> Path:
    keep_dir = SCRATCH / f"{theme_id}.keep"
    shutil.rmtree(keep_dir, ignore_errors=True)
    SCRATCH.mkdir(parents=True, exist_ok=True)
    create_keep(
        keep_dir,
        f"Profile ({theme_id})",
        ThemeRef(theme_id, version),
        [RootConfig("profiled", "Profiled", str(path), list(DEFAULT_EXCLUDES))],
    )
    return keep_dir


def scan(session: KeepSession, label: str, profile: bool) -> float:
    clock = PhaseClock()
    profiler = cProfile.Profile() if profile else None
    if profiler is not None:
        profiler.enable()
    [report] = session.scan_all(progress=clock)
    if profiler is not None:
        profiler.disable()
    total = time.perf_counter() - clock.start
    print(f"\n{label}: {total:.1f} s")
    print(
        f"  {report.new} new, {report.changed} changed, {report.unchanged} unchanged, "
        f"{report.fingerprinted} fingerprinted, {report.ingested} read by the theme, "
        f"{len(report.read_errors)} unreadable"
    )
    for name, seconds in clock.finish():
        print(f"  {seconds:7.2f} s  {name.rstrip('…')}")
    if profiler is not None:
        out = io.StringIO()
        pstats.Stats(profiler, stream=out).sort_stats("tottime").print_stats(15)
        print("\n  Where the time went (own time, top 15):")
        lines = out.getvalue().splitlines()
        start = next(i for i, line in enumerate(lines) if "ncalls" in line)
        for line in lines[start : start + 16]:
            print(f"  {line}")
    return total


def thumbnails(session: KeepSession, count: int, threads: list[int]) -> None:
    with session.reader.connect() as conn:
        ids = list(conn.scalars(select(Entity.id).order_by(Entity.id).limit(count)))
    resolver = session.thumbnails
    print(f"\nThumbnails for {len(ids)} items (cold cache each run):")
    for n in threads:
        resolver.cache.clear()
        resolver.forget_failures()
        start = time.perf_counter()
        if n == 1:
            results = [resolver.resolve(i) for i in ids]
        else:
            with ThreadPoolExecutor(n) as pool:
                results = list(pool.map(resolver.resolve, ids))
        seconds = time.perf_counter() - start
        pictures = sum(r.thumbnail is not None for r in results)
        rate = len(ids) / seconds if seconds else 0
        label = f"{n:2d} thread{'s' if n > 1 else ' '}"
        print(f"  {label}: {seconds:7.2f} s  ({rate:6.1f} /s, {pictures} pictures)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("path", type=Path)
    parser.add_argument("--theme", default="assets2d")
    parser.add_argument("--thumbnails", type=int, default=500, help="items to thumbnail")
    parser.add_argument("--threads", type=int, nargs="+", default=[1, 4, 8])
    parser.add_argument("--cprofile", action="store_true", help="profile the first scan")
    args = parser.parse_args(argv)

    theme = load_themes().get(args.theme)
    if theme is None:
        print(f"No theme {args.theme!r}")
        return 1
    files = sum(1 for p in args.path.rglob("*") if p.is_file())
    print(f"{args.path}: {files:,} files; theme {args.theme}")
    keep_dir = fresh_keep(args.path, args.theme, theme.theme.version)
    with KeepSession.open(keep_dir, Settings()) as session:
        first = scan(session, "First scan", args.cprofile)
        print(f"  {files / first:.0f} files/s")
        scan(session, "Rescan (nothing changed)", False)
        if args.thumbnails:
            thumbnails(session, args.thumbnails, args.threads)
    return 0


if __name__ == "__main__":
    sys.exit(main())
