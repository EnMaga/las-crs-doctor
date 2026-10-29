"""Command line interface: `lascrs inspect ...` and `lascrs convert ...`."""
from __future__ import annotations

import argparse
import os
import sys

from . import core


def _bar(done, total, msg):
    pct = 100 * done / max(total, 1)
    sys.stderr.write(f"\r{pct:5.1f}%  {msg[:100]:<100}")
    sys.stderr.flush()


def main(argv=None):
    p = argparse.ArgumentParser(prog="lascrs", description="Check and fix the CRS of LAS/LAZ point clouds.")
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("inspect", help="Report CRS problems")
    pi.add_argument("files", nargs="+")

    pc = sub.add_parser("convert", help="Reproject (and optionally merge) files")
    pc.add_argument("files", nargs="+")
    pc.add_argument("-t", "--target", type=int, help="Target EPSG (default: auto UTM for geographic input)")
    pc.add_argument("-s", "--source", type=int, help="Override source EPSG (e.g. 6706 for RDN2008)")
    pc.add_argument("-o", "--out", help="Output file (merge) or folder (one output per input)")
    pc.add_argument("--merge", action="store_true", help="Merge all inputs into one output file")
    pc.add_argument("--las", action="store_true", help="Write uncompressed LAS instead of LAZ")
    pc.add_argument("--precision", type=float, default=0.001, help="Output scale in metres (default 0.001)")
    pc.add_argument("-y", "--yes", action="store_true", help="Do not ask for confirmation on warnings")

    a = p.parse_args(argv)
    ins = [core.inspect(f) for f in a.files]

    if a.cmd == "inspect":
        for i in ins:
            print(i.report()); print("-" * 80)
        return 1 if any(i.worst == core.ERROR for i in ins) else 0

    target = a.target
    if target is None:
        sug = {i.suggested_epsg for i in ins if i.suggested_epsg}
        if len(sug) != 1:
            p.error("cannot choose a target automatically, pass --target EPSG")
        target = sug.pop()
    ext = ".las" if a.las else ".laz"
    opts = core.ConvertOptions(target_epsg=target, source_epsg=a.source, compress=not a.las,
                               scale_xy=a.precision, scale_z=a.precision)
    if a.merge:
        out = a.out or os.path.join(os.path.dirname(a.files[0]), f"merged_epsg{target}{ext}")
        jobs = [(ins, out)]
    else:
        d = a.out or None
        jobs = [([i], os.path.join(d or os.path.dirname(i.path),
                                  os.path.splitext(os.path.basename(i.path))[0] + f"_epsg{target}{ext}"))
                for i in ins]

    for inputs, out in jobs:
        pre = core.preflight(inputs, out, opts)
        for f in pre:
            print(f)
        if any(f.level == core.ERROR for f in pre):
            print(f"Skipping {out}: blocking problems."); return 2
        if any(f.level == core.WARN for f in pre) and not a.yes:
            if input("Warnings above. Continue? [y/N] ").strip().lower() != "y":
                return 3
        print(f"-> {out}  (EPSG:{target} {core.epsg_name(target)})")
        res = core.convert(inputs, out, opts, progress=_bar)
        sys.stderr.write("\n")
        for f in res:
            print(f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
