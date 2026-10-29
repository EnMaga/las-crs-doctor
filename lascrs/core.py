"""Core logic: inspect LAS/LAZ headers for CRS problems and reproject point clouds.

No GUI code here, so the same functions are used by the GUI, the CLI and the tests.
"""
from __future__ import annotations

import base64
import math
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence

import numpy as np
import laspy
import pyproj
from pyproj import CRS, Transformer

UINT32_MAX = 2**32 - 1

# Severity levels used for findings
OK, INFO, WARN, ERROR = "OK", "INFO", "WARN", "ERROR"


# --------------------------------------------------------------------------- #
# Data classes
# --------------------------------------------------------------------------- #
@dataclass
class Finding:
    level: str
    title: str
    detail: str = ""

    def __str__(self) -> str:
        s = f"[{self.level}] {self.title}"
        return s + (f"\n        {self.detail}" if self.detail else "")


@dataclass
class Inspection:
    path: str
    version: str = ""
    point_format: int = -1
    point_count: int = 0
    compressed: bool = False
    file_size: int = 0
    software: str = ""
    system_id: str = ""
    scales: tuple = (0, 0, 0)
    offsets: tuple = (0, 0, 0)
    mins: tuple = (0, 0, 0)
    maxs: tuple = (0, 0, 0)
    crs: Optional[CRS] = None
    crs_name: str = "none"
    crs_epsg: Optional[int] = None
    crs_kind: str = "missing"          # geographic / projected / missing / other
    looks_like_degrees: bool = False
    metric_span: tuple = (math.nan, math.nan)   # approx. extent in metres (x, y)
    z_span: float = math.nan
    suggested_epsg: Optional[int] = None
    findings: List[Finding] = field(default_factory=list)

    @property
    def worst(self) -> str:
        order = {OK: 0, INFO: 1, WARN: 2, ERROR: 3}
        return max((f.level for f in self.findings), key=order.get, default=OK)

    def report(self) -> str:
        lines = [
            f"File            : {self.path}",
            f"LAS version     : {self.version}   point format {self.point_format}"
            f"   {'LAZ (compressed)' if self.compressed else 'LAS'}",
            f"Points          : {self.point_count:,}",
            f"Size            : {self.file_size / 1e9:.2f} GB",
            f"Software        : {self.software or '-'}   system id: {self.system_id or '-'}",
            f"CRS             : {self.crs_name}"
            + (f" (EPSG:{self.crs_epsg})" if self.crs_epsg else "")
            + f"  [{self.crs_kind}]",
            f"Scale           : {self.scales}",
            f"Offset          : {self.offsets}",
            f"Min X Y Z       : {self.mins}",
            f"Max X Y Z       : {self.maxs}",
            f"Approx. extent  : {self.metric_span[0]:.1f} m x {self.metric_span[1]:.1f} m,"
            f" Z span {self.z_span:.2f}",
        ]
        if self.suggested_epsg:
            lines.append(f"Suggested target: EPSG:{self.suggested_epsg} ({epsg_name(self.suggested_epsg)})")
        lines.append("")
        lines += [str(f) for f in self.findings]
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def epsg_name(code: int) -> str:
    try:
        return CRS.from_epsg(code).name
    except Exception:
        return "unknown"


def utm_epsg_for(lon: float, lat: float) -> int:
    """WGS 84 / UTM zone EPSG code for a lon/lat (no special Norway/Svalbard zones)."""
    zone = int(math.floor((lon + 180.0) / 6.0)) + 1
    zone = min(max(zone, 1), 60)
    return (32600 if lat >= 0 else 32700) + zone


# Italian national frame (RDN2008 = ETRF2000 epoch 2008.0). Italian GNSS networks
# deliver coordinates in this frame, not in ITRF/WGS 84.
ITALY_RDN2008_UTM = {32: 6707, 33: 6708, 34: 6709}
RDN2008_GEOGRAPHIC = 6706


def _decode_maybe_b64(s: str) -> str:
    """DJI Terra writes the system identifier as base64 (e.g. 'DJI-L1-...')."""
    s = s.strip("\x00 ").strip()
    if not s:
        return ""
    try:
        dec = base64.b64decode(s, validate=True).decode("ascii")
        if dec.isprintable() and len(dec) >= 3:
            return f"{s} -> '{dec}'"
    except Exception:
        pass
    return s


def _approx_metric_span(mins, maxs) -> tuple:
    lat = math.radians((mins[1] + maxs[1]) / 2)
    dx = (maxs[0] - mins[0]) * 111_320.0 * math.cos(lat)
    dy = (maxs[1] - mins[1]) * 110_574.0
    return dx, dy


def _crs_unit(crs: CRS) -> str:
    try:
        return crs.axis_info[0].unit_name or ""
    except Exception:
        return ""


def _horizontal(crs: CRS) -> CRS:
    """Return the horizontal part of a compound CRS."""
    if crs.is_compound:
        for sub in crs.sub_crs_list:
            if sub.is_geographic or sub.is_projected:
                return sub
    return crs


def free_space(path: str) -> int:
    d = os.path.dirname(os.path.abspath(path)) or "."
    return shutil.disk_usage(d).free


# --------------------------------------------------------------------------- #
# Inspection
# --------------------------------------------------------------------------- #
def inspect(path: str) -> Inspection:
    ins = Inspection(path=path)
    F = ins.findings
    ins.file_size = os.path.getsize(path)

    with laspy.open(path) as r:
        h = r.header
        ins.version = f"{h.version.major}.{h.version.minor}"
        ins.point_format = h.point_format.id
        ins.point_count = h.point_count
        ins.compressed = r.header.are_points_compressed if hasattr(h, "are_points_compressed") \
            else path.lower().endswith(".laz")
        ins.software = (h.generating_software or "").strip("\x00 ")
        ins.system_id = _decode_maybe_b64(h.system_identifier or "")
        ins.scales = tuple(float(v) for v in h.scales)
        ins.offsets = tuple(float(v) for v in h.offsets)
        ins.mins = tuple(float(v) for v in h.mins)
        ins.maxs = tuple(float(v) for v in h.maxs)
        try:
            ins.crs = h.parse_crs()
        except Exception as e:  # malformed GeoTIFF keys / WKT
            F.append(Finding(WARN, "CRS VLR present but could not be parsed", str(e)))
            ins.crs = None
        rec_len = h.point_format.size
        offset_to_points = h.offset_to_point_data

        # Check the first points actually fall inside the header bounds
        sample_bad = 0
        try:
            n = min(200_000, h.point_count)
            if n:
                pts = r.read_points(n)
                eps = np.array(ins.scales)
                for arr, lo, hi, e in ((pts.x, ins.mins[0], ins.maxs[0], eps[0]),
                                       (pts.y, ins.mins[1], ins.maxs[1], eps[1]),
                                       (pts.z, ins.mins[2], ins.maxs[2], eps[2])):
                    a = np.asarray(arr)
                    sample_bad += int(np.sum((a < lo - e) | (a > hi + e)))
        except Exception as e:
            F.append(Finding(ERROR, "Could not read points", str(e)))

    if sample_bad:
        F.append(Finding(WARN, "Header bounds do not match the points",
                         f"{sample_bad} of the first sampled points fall outside the header min/max. "
                         "The header may be stale; viewers may zoom to the wrong extent."))

    # --- File integrity (uncompressed only) --------------------------------
    if not ins.compressed:
        expected = offset_to_points + ins.point_count * rec_len
        if ins.file_size < expected:
            F.append(Finding(ERROR, "File is truncated",
                             f"Header announces {ins.point_count:,} points ({expected:,} bytes) "
                             f"but the file has {ins.file_size:,} bytes."))

    # --- CRS ----------------------------------------------------------------
    xspan = ins.maxs[0] - ins.mins[0]
    yspan = ins.maxs[1] - ins.mins[1]
    ins.z_span = ins.maxs[2] - ins.mins[2]
    in_deg_range = (-180 <= ins.mins[0] <= 180 and -180 <= ins.maxs[0] <= 180
                    and -90 <= ins.mins[1] <= 90 and -90 <= ins.maxs[1] <= 90)
    ins.looks_like_degrees = in_deg_range and xspan < 5 and yspan < 5

    crs = ins.crs
    if crs is not None:
        hcrs = _horizontal(crs)
        ins.crs_name = crs.name
        ins.crs_epsg = crs.to_epsg(min_confidence=70)
        if hcrs.is_geographic:
            ins.crs_kind = "geographic"
        elif hcrs.is_projected:
            ins.crs_kind = "projected"
        else:
            ins.crs_kind = "other"
    else:
        F.append(Finding(WARN, "No coordinate reference system in the file",
                         "Software cannot know what the numbers mean. You must set the source CRS "
                         "manually before converting."))

    geographic = ins.crs_kind == "geographic" or (crs is None and ins.looks_like_degrees)

    if geographic:
        ins.metric_span = _approx_metric_span(ins.mins, ins.maxs)
        lon_c = (ins.mins[0] + ins.maxs[0]) / 2
        lat_c = (ins.mins[1] + ins.maxs[1]) / 2
        ins.suggested_epsg = utm_epsg_for(lon_c, lat_c)
        F.append(Finding(ERROR, "Coordinates are in degrees (geographic CRS)",
                         "X/Y are longitude/latitude while Z is in metres. CloudCompare and most "
                         "point-cloud tools assume all axes share one unit, so the cloud appears as a "
                         "thin vertical line and distances, normals, densities, CSF/SOR filters and "
                         "rasterisation are meaningless. Reproject to a projected CRS in metres."))
        ratio = ins.z_span / max(xspan, yspan, 1e-12)
        if ratio > 10:
            F.append(Finding(INFO, f"Displayed Z will look ~{ratio:,.0f}x exaggerated",
                             f"XY span {max(xspan, yspan):.5f} units vs Z span {ins.z_span:.2f}."))
        if ins.scales[0] > 1.1e-7 or ins.scales[1] > 1.1e-7:
            res_m = ins.scales[1] * 110_574
            F.append(Finding(WARN, "Coarse XY scale for degrees",
                             f"Scale {ins.scales[0]:g} deg corresponds to ~{res_m:.3f} m resolution."))
        if crs is not None and ins.crs_epsg == 4326:
            F.append(Finding(INFO, "Vertical datum not declared",
                             "EPSG:4326 is 2D. For DJI/RTK data Z is usually an ellipsoidal height "
                             "(not above sea level). Check before comparing with DTMs or levelling data."))
        if crs is not None and ins.crs_epsg in (4326, 4979) and 6 <= lon_c <= 19 and 35 <= lat_c <= 48:
            F.append(Finding(INFO, "Italy: check the reference frame of your GNSS corrections",
                             "Italian NRTK networks deliver RDN2008/ETRF2000, not ITRF/WGS 84 "
                             "(~1 m apart in Italy in 2026, growing ~2.5 cm/yr). If your GCPs/RTK came "
                             "from such a network, the file "
                             "is likely ETRF2000 mislabelled as WGS 84. In that case use source "
                             f"override EPSG:{RDN2008_GEOGRAPHIC} and target EPSG:"
                             f"{ITALY_RDN2008_UTM.get(int((lon_c + 180) // 6) + 1, 6708)} so no datum "
                             "shift is applied."))
    elif ins.crs_kind == "projected":
        ins.metric_span = (xspan, yspan)
        unit = _crs_unit(_horizontal(crs))
        if unit and "metre" not in unit.lower() and "meter" not in unit.lower():
            F.append(Finding(WARN, f"Projected CRS in '{unit}'",
                             "Z is probably in metres while X/Y are not. Check units are consistent."))
        if ins.looks_like_degrees:
            F.append(Finding(ERROR, "CRS says projected but values look like degrees",
                             "The CRS label is probably wrong. Set the correct source CRS manually."))
        if ins.scales[0] > 0.01 or ins.scales[1] > 0.01:
            F.append(Finding(WARN, "Coarse XY scale", f"Scale {ins.scales[:2]} m limits precision."))
        if not crs.is_compound:
            F.append(Finding(INFO, "No vertical CRS declared",
                             "Z datum (ellipsoidal vs orthometric) is not recorded in the file."))
        if not F or all(f.level in (OK, INFO) for f in F):
            F.append(Finding(OK, "Projected CRS in metres: ready for CloudCompare / metric analysis"))
    else:
        ins.metric_span = (xspan, yspan)

    # --- Z sanity -----------------------------------------------------------
    if ins.z_span > 5000 or ins.mins[2] < -1000 or ins.maxs[2] > 9000:
        F.append(Finding(WARN, "Z range looks unrealistic",
                         f"Z from {ins.mins[2]:.1f} to {ins.maxs[2]:.1f}. Check for outliers, "
                         "feet/metre mix-ups or wrong height reference."))

    # --- Version / size limits ------------------------------------------------
    if ins.point_count > UINT32_MAX and ins.version < "1.4":
        F.append(Finding(ERROR, "Too many points for LAS < 1.4",
                         "Legacy point count overflows; save as LAS 1.4."))
    return ins


# --------------------------------------------------------------------------- #
# Conversion
# --------------------------------------------------------------------------- #
class Cancelled(Exception):
    pass


@dataclass
class ConvertOptions:
    target_epsg: int
    source_epsg: Optional[int] = None    # override the CRS stored in the file
    compress: bool = True               # write LAZ
    scale_xy: float = 0.001             # metres (projected targets)
    scale_z: float = 0.001
    chunk_size: int = 5_000_000
    verify_samples: int = 20
    space_margin: float = 1.10          # require estimated size x margin free


def estimate_output_bytes(inputs: Sequence[Inspection], compress: bool) -> int:
    total = 0
    for ins in inputs:
        with laspy.open(ins.path) as r:
            rec = r.header.point_format.size
        raw = ins.point_count * rec
        total += int(raw * (0.25 if compress else 1.0)) + 10_000
    return total


def _source_crs(ins: Inspection, opts: ConvertOptions) -> CRS:
    if opts.source_epsg:
        return CRS.from_epsg(opts.source_epsg)
    if ins.crs is None:
        raise ValueError(f"{os.path.basename(ins.path)} has no CRS: set a source EPSG override.")
    return ins.crs


def _make_transformer(src: CRS, dst: CRS) -> Transformer:
    """3D-aware transformer so ellipsoidal heights follow datum changes; falls back to 2D."""
    try:
        return Transformer.from_crs(src.to_3d(), dst.to_3d(), always_xy=True)
    except Exception:
        return Transformer.from_crs(src, dst, always_xy=True)


def preflight(inputs: Sequence[Inspection], out_path: str, opts: ConvertOptions) -> List[Finding]:
    """Checks before converting. ERROR findings should block the run."""
    F: List[Finding] = []
    dst = CRS.from_epsg(opts.target_epsg)
    if dst.is_geographic:
        F.append(Finding(WARN, "Target CRS is geographic (degrees)",
                         "This reproduces the original problem. Choose a projected CRS in metres."))
    for ins in inputs:
        if os.path.abspath(ins.path) == os.path.abspath(out_path):
            F.append(Finding(ERROR, "Output would overwrite an input file", ins.path))
        try:
            _source_crs(ins, opts)
        except ValueError as e:
            F.append(Finding(ERROR, "Source CRS unknown", str(e)))
    fmts = {(i.point_format,) for i in inputs}
    if len(inputs) > 1:
        dims = set()
        for ins in inputs:
            with laspy.open(ins.path) as r:
                dims.add(tuple(r.header.point_format.dimension_names))
        if len(fmts) > 1 or len(dims) > 1:
            F.append(Finding(ERROR, "Inputs have different point formats / extra dimensions",
                             "They cannot be merged into one file. Convert them separately."))
    need = int(estimate_output_bytes(inputs, opts.compress) * opts.space_margin)
    have = free_space(out_path)
    if have < need:
        F.append(Finding(ERROR, "Not enough free disk space",
                         f"Estimated {need / 1e9:.1f} GB needed (incl. margin), {have / 1e9:.1f} GB free "
                         "on the output drive. Choose another folder/drive or use LAZ."))
    elif have < need * 1.5:
        F.append(Finding(WARN, "Disk space is tight",
                         f"Estimated {need / 1e9:.1f} GB, {have / 1e9:.1f} GB free."))
    if os.path.exists(out_path):
        F.append(Finding(WARN, "Output file exists and will be overwritten", out_path))
    total = sum(i.point_count for i in inputs)
    if total > UINT32_MAX:
        F.append(Finding(INFO, "More than 4.29 billion points: output will be LAS 1.4"))
    return F


ProgressCB = Callable[[int, int, str], None]


def convert(inputs: Sequence[Inspection], out_path: str, opts: ConvertOptions,
            progress: Optional[ProgressCB] = None,
            cancel: Optional[threading.Event] = None) -> List[Finding]:
    """Reproject one or more files into ONE output file (merge when several inputs).

    Returns verification findings. Raises Cancelled if the cancel event is set.
    """
    if not inputs:
        raise ValueError("No input files")
    progress = progress or (lambda done, total, msg: None)
    dst = CRS.from_epsg(opts.target_epsg)
    total = sum(i.point_count for i in inputs)

    # Output bounds -> offsets
    xs, ys = [], []
    for ins in inputs:
        t2d = Transformer.from_crs(_source_crs(ins, opts), dst, always_xy=True)
        cx = [ins.mins[0], ins.maxs[0], ins.mins[0], ins.maxs[0]]
        cy = [ins.mins[1], ins.mins[1], ins.maxs[1], ins.maxs[1]]
        x, y = t2d.transform(cx, cy)
        xs += list(x); ys += list(y)

    with laspy.open(inputs[0].path) as r0:
        pf = r0.header.point_format
    version = "1.4" if total > UINT32_MAX else "1.2"
    if version == "1.2" and pf.id > 5:
        version = "1.4"
    h = laspy.LasHeader(point_format=pf, version=version)
    if dst.is_geographic:
        h.scales = np.array([1e-8, 1e-8, opts.scale_z])
        h.offsets = np.array([math.floor(min(xs)), math.floor(min(ys)), 0.0])
    else:
        h.scales = np.array([opts.scale_xy, opts.scale_xy, opts.scale_z])
        h.offsets = np.array([math.floor(min(xs) / 1000) * 1000,
                              math.floor(min(ys) / 1000) * 1000, 0.0])
    h.add_crs(dst)
    h.system_identifier = "LAS CRS Doctor"
    h.generating_software = f"lascrs (laspy {laspy.__version__}, PROJ {pyproj.proj_version_str})"[:31]

    dims = [d for d in pf.dimension_names if d not in ("X", "Y", "Z")]
    done = 0
    t0 = time.time()
    kw = {}
    if opts.compress:
        kw = dict(do_compress=True, laz_backend=laspy.LazBackend.detect_available()[0])

    with laspy.open(out_path, mode="w", header=h, **kw) as w:
        for ins in inputs:
            tr = _make_transformer(_source_crs(ins, opts), dst)
            with laspy.open(ins.path) as r:
                for ch in r.chunk_iterator(opts.chunk_size):
                    if cancel is not None and cancel.is_set():
                        raise Cancelled()
                    x, y, z = tr.transform(np.asarray(ch.x), np.asarray(ch.y), np.asarray(ch.z))
                    if not (np.all(np.isfinite(x)) and np.all(np.isfinite(y))):
                        raise ValueError("Transformation produced non-finite coordinates: "
                                         "the source CRS is probably wrong.")
                    out = laspy.ScaleAwarePointRecord.zeros(len(ch), header=h)
                    for d in dims:
                        out[d] = ch[d]
                    out.x, out.y, out.z = x, y, z
                    w.write_points(out)
                    done += len(ch)
                    rate = done / max(time.time() - t0, 1e-6)
                    eta = (total - done) / max(rate, 1)
                    progress(done, total, f"{os.path.basename(ins.path)}: {done:,}/{total:,} pts, "
                                          f"ETA {eta / 60:.1f} min")

    progress(total, total, "Verifying output...")
    return verify(inputs, out_path, opts)


def verify(inputs: Sequence[Inspection], out_path: str, opts: ConvertOptions) -> List[Finding]:
    """Re-read random points from inputs and output and compare to an independent transform."""
    F: List[Finding] = []
    dst = CRS.from_epsg(opts.target_epsg)
    with laspy.open(out_path) as o:
        oh = o.header
        expected = sum(i.point_count for i in inputs)
        if oh.point_count != expected:
            F.append(Finding(ERROR, "Point count mismatch",
                             f"output {oh.point_count:,} vs inputs {expected:,}"))
        parsed = oh.parse_crs()
        if parsed is None or parsed.to_epsg(min_confidence=70) != opts.target_epsg:
            F.append(Finding(WARN, "Output CRS VLR does not round-trip to the target EPSG",
                             f"read back: {parsed.name if parsed else 'none'}"))
        rng = np.random.default_rng(0)
        base = 0
        max_err = 0.0
        attr_ok = True
        for ins in inputs:
            tr = _make_transformer(_source_crs(ins, opts), dst)
            n = ins.point_count
            k = min(max(1, opts.verify_samples // len(inputs)), n)
            idx = sorted(set([0, n - 1] + list(rng.integers(0, n, k))))
            with laspy.open(ins.path) as r:
                for i in idx:
                    r.seek(int(i)); a = r.read_points(1)
                    o.seek(int(base + i)); b = o.read_points(1)
                    ex, ey, ez = tr.transform(a.x[0], a.y[0], a.z[0])
                    err = max(abs(b.x[0] - ex), abs(b.y[0] - ey), abs(b.z[0] - ez))
                    max_err = max(max_err, float(err))
                    for d in ("intensity", "gps_time", "classification"):
                        if d in a.point_format.dimension_names and a[d][0] != b[d][0]:
                            attr_ok = False
            base += n
    tol = max(opts.scale_xy, opts.scale_z) if not dst.is_geographic else 1e-7
    if max_err <= tol:
        F.append(Finding(OK, f"Coordinates verified (max error {max_err * 1000:.2f} mm, "
                             f"tolerance = output scale)"))
    else:
        F.append(Finding(ERROR, f"Verification failed: max error {max_err:.4f}"))
    F.append(Finding(OK if attr_ok else ERROR,
                     "Attributes preserved" if attr_ok else "Attributes differ between input and output"))
    return F
