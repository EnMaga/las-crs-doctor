import numpy as np
import laspy
import pyproj
import pytest

from lascrs import core


def make_las(path, epsg=4326, n=50_000, lon0=12.705, lat0=45.54, seed=0, laz=False, pf=3):
    """Synthetic DJI-Terra-like cloud: lon/lat degrees, Z ellipsoidal metres."""
    rng = np.random.default_rng(seed)
    h = laspy.LasHeader(point_format=pf, version="1.2")
    if epsg in (4326, 6706, None):
        h.scales = np.array([1e-8, 1e-8, 1e-4]); h.offsets = np.array([12.7, 45.5, 0.0])
        x = lon0 + rng.uniform(0, 0.01, n); y = lat0 + rng.uniform(0, 0.01, n)
    else:
        h.scales = np.array([1e-3, 1e-3, 1e-3]); h.offsets = np.array([320000, 5045000, 0])
        x = 320500 + rng.uniform(0, 700, n); y = 5045500 + rng.uniform(0, 1000, n)
    if epsg:
        h.add_crs(pyproj.CRS.from_epsg(epsg))
    las = laspy.LasData(h)
    las.x = x; las.y = y; las.z = 42 + rng.uniform(0, 20, n)
    las.intensity = rng.integers(0, 65535, n).astype(np.uint16)
    las.gps_time = np.sort(rng.uniform(0, 1e5, n))
    las.classification = rng.integers(0, 10, n).astype(np.uint8)
    las.write(str(path), laz_backend=laspy.LazBackend.Lazrs if laz else None)
    return path


def levels(findings):
    return [f.level for f in findings]


def test_inspect_geographic_flags_degrees(tmp_path):
    ins = core.inspect(make_las(tmp_path / "a.las"))
    assert ins.crs_kind == "geographic"
    assert ins.crs_epsg == 4326
    assert ins.suggested_epsg == 32633
    assert any("degrees" in f.title for f in ins.findings if f.level == core.ERROR)
    assert any("Italy" in f.title for f in ins.findings)
    assert 700 < ins.metric_span[0] < 850 and 1050 < ins.metric_span[1] < 1150


def test_inspect_projected_ok(tmp_path):
    ins = core.inspect(make_las(tmp_path / "p.las", epsg=32633))
    assert ins.crs_kind == "projected"
    assert ins.worst in (core.OK, core.INFO)


def test_inspect_missing_crs_looks_like_degrees(tmp_path):
    ins = core.inspect(make_las(tmp_path / "n.las", epsg=None))
    assert ins.crs is None and ins.looks_like_degrees
    assert ins.worst == core.ERROR


def test_truncated_file_detected(tmp_path):
    p = make_las(tmp_path / "t.las")
    data = p.read_bytes(); p.write_bytes(data[: len(data) - 3400])
    ins = core.inspect(p)
    assert any(f.title == "File is truncated" for f in ins.findings)


@pytest.mark.parametrize("laz_out", [True, False])
def test_convert_single(tmp_path, laz_out):
    src = make_las(tmp_path / "a.las")
    ins = core.inspect(src)
    out = tmp_path / ("o.laz" if laz_out else "o.las")
    opts = core.ConvertOptions(target_epsg=32633, compress=laz_out, chunk_size=7_000)
    res = core.convert([ins], str(out), opts)
    assert all(f.level == core.OK for f in res), res
    a = laspy.read(src); b = laspy.read(out)
    tr = pyproj.Transformer.from_crs(4326, 32633, always_xy=True)
    ex, ey = tr.transform(np.asarray(a.x), np.asarray(a.y))
    assert np.max(np.abs(b.x - ex)) <= 0.0006
    assert np.max(np.abs(b.y - ey)) <= 0.0006
    assert np.max(np.abs(b.z - a.z)) <= 0.0006        # same ellipsoid: Z unchanged
    assert np.array_equal(a.intensity, b.intensity)
    assert np.array_equal(a.gps_time, b.gps_time)
    assert b.header.parse_crs().to_epsg() == 32633
    assert core.inspect(out).crs_kind == "projected"


def test_merge_preserves_order_and_count(tmp_path):
    p1 = make_las(tmp_path / "a.las", seed=1, n=20_000)
    p2 = make_las(tmp_path / "b.laz", seed=2, n=30_000, lat0=45.55, laz=True)
    ins = [core.inspect(p1), core.inspect(p2)]
    out = tmp_path / "m.laz"
    res = core.convert(ins, str(out), core.ConvertOptions(target_epsg=32633, chunk_size=6_000))
    assert all(f.level == core.OK for f in res), res
    b = laspy.read(out)
    assert len(b) == 50_000
    a2 = laspy.read(p2)
    assert np.array_equal(b.gps_time[20_000:], a2.gps_time)


def test_source_override_rdn2008_no_datum_shift(tmp_path):
    """ETRF2000 coords mislabelled as WGS84: override 6706 -> 6708 must be a pure projection."""
    src = make_las(tmp_path / "a.las")
    ins = core.inspect(src)
    out = tmp_path / "o.las"
    core.convert([ins], str(out), core.ConvertOptions(target_epsg=6708, source_epsg=6706, compress=False))
    a = laspy.read(src); b = laspy.read(out)
    tr = pyproj.Transformer.from_crs(6706, 6708, always_xy=True)
    ex, ey = tr.transform(np.asarray(a.x), np.asarray(a.y))
    assert np.max(np.abs(b.x - ex)) <= 0.0006
    # same as WGS84 UTM 33 numerically (same projection, ellipsoids GRS80 vs WGS84 differ < 0.1 mm)
    tr2 = pyproj.Transformer.from_crs(4326, 32633, always_xy=True)
    wx, wy = tr2.transform(np.asarray(a.x), np.asarray(a.y))
    assert np.max(np.abs(b.y - wy)) < 0.01


def test_preflight_blocks(tmp_path):
    src = make_las(tmp_path / "a.las")
    ins = core.inspect(src)
    f = core.preflight([ins], str(src), core.ConvertOptions(target_epsg=32633))
    assert core.ERROR in levels(f)                         # overwrite input
    f = core.preflight([ins], str(tmp_path / "x.laz"), core.ConvertOptions(target_epsg=4326))
    assert any("geographic" in x.title for x in f)
    nocrs = core.inspect(make_las(tmp_path / "n.las", epsg=None))
    f = core.preflight([nocrs], str(tmp_path / "y.laz"), core.ConvertOptions(target_epsg=32633))
    assert core.ERROR in levels(f)
    p8 = make_las(tmp_path / "f1.las", pf=1)
    f = core.preflight([ins, core.inspect(p8)], str(tmp_path / "z.laz"), core.ConvertOptions(target_epsg=32633))
    assert any("point formats" in x.title for x in f)


def test_cancel(tmp_path):
    import threading
    ins = core.inspect(make_las(tmp_path / "a.las"))
    ev = threading.Event(); ev.set()
    with pytest.raises(core.Cancelled):
        core.convert([ins], str(tmp_path / "c.laz"), core.ConvertOptions(target_epsg=32633), cancel=ev)


def test_cli_inspect_exit_code(tmp_path):
    from lascrs import cli
    assert cli.main(["inspect", str(make_las(tmp_path / "a.las"))]) == 1
    assert cli.main(["inspect", str(make_las(tmp_path / "p.las", epsg=32633))]) == 0
