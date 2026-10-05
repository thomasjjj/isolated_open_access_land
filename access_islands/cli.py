from __future__ import annotations

import argparse
import logging
import time
import tomllib
from pathlib import Path

from .analyse import analyse
from .common import atomic_json, read_json
from .export import export
from .prepare import prepare
from .sources import download_all


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Screen England's CRoW land for missing pedestrian connections"
    )
    parser.add_argument(
        "command",
        choices=[
            "download",
            "prepare",
            "reference",
            "regions",
            "research",
            "report",
            "analyse",
            "export",
            "run",
            "demo",
        ],
    )
    parser.add_argument(
        "--config", type=Path, help="TOML configuration (optional; national sources are built in)"
    )
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--output-dir", type=Path, default=Path("outputs"))
    parser.add_argument("--authority", help="ONS council or county code for regional research")
    parser.add_argument(
        "--bbox",
        nargs=4,
        type=float,
        metavar=("WEST", "SOUTH", "EAST", "NORTH"),
        help="Regional study bounds in British National Grid metres",
    )
    parser.add_argument("--refresh", action="store_true", help="Refresh sources/rebuild stages")
    parser.add_argument(
        "--county", action="append", help="County/unitary name or ONS code; repeat for combined regions"
    )
    parser.add_argument("--region", help="Named region preset or [regions.NAME] configuration")
    parser.add_argument("--name", help="Display name and Markdown report title for a combined/custom region")
    parser.add_argument("--reports-dir", type=Path, default=Path("reports"))
    parser.add_argument("--national", action="store_true", help="Explicitly opt into full-country analysis")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S"
    )
    start = time.monotonic()
    config = {}
    try:
        if args.config:
            config = tomllib.loads(args.config.read_text(encoding="utf-8"))
            # Local input paths are relative to the configuration, not the caller's working directory.
            for spec in (
                list(config.get("sources", {}).values())
                + config.get("overrides", [])
                + [config.get("coverage", {})]
            ):
                if spec.get("path"):
                    spec["path"] = str((args.config.resolve().parent / spec["path"]).resolve())
            for region_spec in config.get("regions", {}).values():
                for key in ["path", "coverage_path"]:
                    osm_spec = region_spec.get("osm", {})
                    if osm_spec.get(key):
                        osm_spec[key] = str((args.config.resolve().parent / osm_spec[key]).resolve())
        if args.bbox and (args.bbox[0] >= args.bbox[2] or args.bbox[1] >= args.bbox[3]):
            raise ValueError("Bounding box must have west < east and south < north")
        root, output = args.data_dir.resolve(), args.output_dir.resolve()
        scoped = bool(args.county or args.authority or args.region or args.bbox)
        if args.command == "report":
            from .reports import county_report

            county_report(output, args.reports_dir.resolve())
            return 0
        if args.command in {"reference", "regions", "research"} or (
            args.command in {"analyse", "run"} and scoped
        ):
            from .regions import authority_table, ensure_reference, research, resolve_region

            ensure_reference(config, root, refresh=args.refresh and args.command == "reference")
            if args.command == "reference":
                return 0
            if args.command == "regions":
                print(authority_table(root).to_string(index=False))
                return 0
            county = (args.county or []) + ([args.authority] if args.authority else [])
            spec = resolve_region(root, config, county, args.region, args.bbox, args.name)
            result = research(
                config, root, output / "regions", args.reports_dir.resolve(), spec, args.refresh
            )
            return 0 if result["complete"] else 2
        if args.command in {"run", "analyse"} and not args.national:
            raise ValueError(
                "Select --county/--region for regional work, or --national for England-wide analysis"
            )
        if args.command == "demo":
            from .demo import make_demo

            config = make_demo(root)
        if args.command in {"download", "run"}:
            download_all(config, root, args.refresh)
        if args.command in {"prepare", "run", "demo"}:
            prepare(config, root, args.refresh)
        if args.command in {"analyse", "run", "demo"}:
            analyse(config, root, output, args.authority, args.bbox, args.refresh)
        if args.command in {"export", "run", "demo"}:
            report = export(output)
            report["elapsed_seconds"] = round(time.monotonic() - start, 2)
            try:
                import resource

                report["peak_memory_platform_units"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            except ImportError:
                import ctypes
                from ctypes import wintypes

                class Memory(ctypes.Structure):
                    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                        (name, ctypes.c_size_t)
                        for name in [
                            "PeakWorkingSetSize",
                            "WorkingSetSize",
                            "QuotaPeakPagedPoolUsage",
                            "QuotaPagedPoolUsage",
                            "QuotaPeakNonPagedPoolUsage",
                            "QuotaNonPagedPoolUsage",
                            "PagefileUsage",
                            "PeakPagefileUsage",
                        ]
                    ]

                counters = Memory()
                counters.cb = ctypes.sizeof(counters)
                ctypes.windll.kernel32.GetCurrentProcess.restype = wintypes.HANDLE
                ctypes.windll.psapi.GetProcessMemoryInfo.argtypes = [
                    wintypes.HANDLE,
                    ctypes.POINTER(Memory),
                    wintypes.DWORD,
                ]
                ctypes.windll.psapi.GetProcessMemoryInfo(
                    ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
                )
                report["peak_memory_bytes"] = counters.PeakWorkingSetSize
            atomic_json(output / "run_manifest.json", report)
        state = read_json(output / "analysis.json", {})
        if args.command in {"analyse", "run", "demo", "export"} and not state.get("complete", False):
            return 2
        return 0
    except KeyboardInterrupt:
        logging.warning("Interrupted. Completed downloads and analysis tiles are retained for resume.")
        return 130
    except Exception as exc:
        logging.exception("Stage failed: %s", exc)
        output = args.output_dir.resolve()
        atomic_json(
            output / "last_failure.json", {"complete": False, "command": args.command, "error": str(exc)}
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
