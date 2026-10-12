import os
from pathlib import Path

from fastapi.testclient import TestClient

from aibrarian import duplicates as d
from aibrarian.api import create_app


def mk(tmp, rel, data, mtime=None):
    p = tmp / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    if mtime:
        os.utime(p, (mtime, mtime))
    return p


def row(i, p, **kw):
    st = p.stat()
    return {"id": i, "path": str(p), "size_bytes": st.st_size, "mtime": st.st_mtime,
            "title": None, "year": None, "duration_s": None, "media_info": None,
            "audio_layout": None, "stage": "cataloged"} | kw


def info(w, h, n_audio=1, n_subs=0):
    return {"video": [{"codec_name": "h264", "width": w, "height": h}],
            "audio": [{}] * n_audio, "subtitles": [{}] * n_subs, "container": {"bit_rate": w * 1000}}


def library(tmp):
    body = os.urandom(9 * 1024 * 1024)  # > 2 samples so head/tail sampling is exercised
    a = mk(tmp, "01-movies/5lbs.Of.Pressure (2024)/5lbs.Of.Pressure.2024.mkv", body, 1_600_000_000)
    b = mk(tmp, "01-movies/5lbs Of Pressure (2024)/5lbs Of Pressure (2024).mkv", body, 1_700_000_000)
    mk(tmp, "01-movies/5lbs.Of.Pressure (2024)/5lbs.Of.Pressure.2024.eng.srt", b"1")
    # same size, same head/tail, different middle -> must NOT be identical
    mid = bytearray(body)
    mid[4 * 1024 * 1024 + 512 * 1024] ^= 0xFF
    c = mk(tmp, "01-movies/Other (2020)/Other (2020).mkv", bytes(mid))
    e = mk(tmp, "01-movies/5lbs Of Pressure (2024)/5lbs Of Pressure (2024).mp4", b"x" * 1000)
    f = mk(tmp, "01-movies/Xmas/1982 - A Disney Christmas Gift.avi", b"y" * 500)
    g = mk(tmp, "01-movies/Xmas/A Disney Christmas Gift (1982).avi", b"z" * 400)
    u = mk(tmp, "01-movies/Unrelated (2001)/Unrelated (2001).mkv", b"q" * 300)
    rows = [row(1, a, title="5lbs Of Pressure", year=2024, duration_s=5400.0, media_info=info(1920, 1080)),
            row(2, b, title="5lbs Of Pressure", year=2024, duration_s=5400.0, media_info=info(1920, 1080)),
            row(3, c, title="Other", year=2020),
            row(4, e, title="5lbs Of Pressure", year=2024, duration_s=5401.5, media_info=info(1280, 720)),
            row(5, f, duration_s=3000.0), row(6, g, duration_s=3001.0, media_info=info(640, 480)),
            row(7, u, title="Unrelated", year=2001, duration_s=5400.5)]
    return rows


def test_sample_and_full_hash_ignore_mtime(tmp_path):
    a = mk(tmp_path, "a", b"abc", 1)
    b = mk(tmp_path, "b", b"abc", 2)
    assert d.full_hash(a) == d.full_hash(b) and d.sample_hash(a, 3) == d.sample_hash(b, 3)


def test_report_groups_and_keep(tmp_path):
    rep = d.report(library(tmp_path), d.HashCache())
    assert rep["read_only"] and rep["summary"]["identical_groups"] == 1
    ident = rep["identical"][0]
    assert {Path(c["path"]).name for c in ident["copies"]} == {
        "5lbs.Of.Pressure.2024.mkv", "5lbs Of Pressure (2024).mkv"}
    assert ident["suggested_keep"].endswith("5lbs Of Pressure (2024)/5lbs Of Pressure (2024).mkv")
    assert "canonical" in ident["keep_reason"]
    orphan = ident["sidecars_only_on_others"][0]
    assert orphan["sidecar"].endswith("5lbs.Of.Pressure.2024.eng.srt")
    assert orphan["would_be"].endswith("5lbs Of Pressure (2024).eng.srt")
    assert ident["reclaimable_bytes"] == 9 * 1024 * 1024
    review = rep["review"]
    paths = [{Path(c["path"]).name for c in g["copies"]} for g in review]
    assert any("5lbs Of Pressure (2024).mp4" in p for p in paths)          # title+year / duration
    assert {"1982 - A Disney Christmas Gift.avi", "A Disney Christmas Gift (1982).avi"} in paths
    assert not any("Unrelated (2001).mkv" in p for p in paths)            # duration alone is not enough
    assert not any("Other (2020).mkv" in p for p in paths)                # same size, different bytes
    xmas = next(g for g in review if len(g["copies"]) == 2 and "Disney" in g["suggested_keep"])
    assert xmas["kind"] == "review" and xmas["suggested_keep"].endswith("A Disney Christmas Gift (1982).avi")
    assert d.report(library(tmp_path / "x"), d.HashCache(), exact_only=True)["review"] == []
    tsv = d.to_tsv(rep)
    assert tsv.splitlines()[0].startswith("section\tgroup\tkeep") and "\tKEEP\t" in tsv


class Store:
    def __init__(self):
        self.rows = {}

    def get_file_hash(self, p):
        return self.rows.get(p)

    def put_file_hash(self, p, size, mtime, sample, sha):
        self.rows[p] = {"path": p, "size_bytes": size, "mtime": mtime, "sample": sample, "sha256": sha}


def test_hash_cache_reused_and_invalidated(tmp_path):
    rows = library(tmp_path)
    store = Store()
    first = d.HashCache(store)
    d.find_exact(rows, first)
    assert first.computed["sha256"] == 3   # 2 identical + the same-sample decoy
    again = d.HashCache(store)
    d.find_exact(rows, again)
    assert again.computed == {"sample": 0, "sha256": 0}
    rows[0]["mtime"] += 10                 # file changed -> rehash just that one
    third = d.HashCache(store)
    d.find_exact(rows, third)
    assert third.computed == {"sample": 1, "sha256": 1}


def test_files_never_touched(tmp_path):
    rows = library(tmp_path)
    before = {r["path"]: Path(r["path"]).stat().st_mtime for r in rows}
    d.report(rows, d.HashCache())
    assert all(Path(p).exists() and Path(p).stat().st_mtime == m for p, m in before.items())


def test_api_and_ui(ctx, settings, tmp_path):
    rows = library(tmp_path / "lib")

    class DB:
        def duplicate_candidates(self):
            return rows

        def get_file_hash(self, p):
            return None

        def put_file_hash(self, *a):
            pass

    ctx.db = DB()
    c = TestClient(create_app(ctx, migrate=False))
    r = c.get("/duplicates").json()
    assert r["summary"]["identical_groups"] == 1 and (settings.report_dir / "duplicates.json").exists()
    assert c.get("/duplicates", params={"cached": True}).json()["summary"] == r["summary"]
    assert c.get("/duplicates", params={"exact_only": True}).json()["review"] == []
    assert "Duplicates (read-only)" in c.get("/duplicates/ui").text
    qp = c.get("/duplicates/quarantine-plan", params={"limit": 1}).json()
    assert qp["dry_run"] and qp["files"] == 1
    assert all(Path(r["path"]).exists() for r in rows)               # preview moves nothing


# -- quarantine -------------------------------------------------------------

class QDB:
    def __init__(self):
        self.events = []

    def quarantine_media(self, mid, old, new, started, details):
        self.events.append(("quarantine", mid, old, new))

    def restore_media(self, mid, qpath, path, stage, started):
        self.events.append(("restore", mid, qpath, path, stage))


def test_quarantine_dry_run_execute_restore(tmp_path):
    from datetime import date

    from aibrarian import quarantine as q
    from aibrarian.config import Settings

    media = tmp_path / "media"
    rows = library(media)
    s = Settings(_env_file=None, media_root=media)
    rep = d.report(rows, d.HashCache(), exact_only=True)
    p = q.plan(rep, s, limit=3, day=date(2026, 10, 12))
    assert p["files"] == 1 and p["dry_run"]
    it = p["items"][0]
    extra = Path(it["path"])
    assert extra.name == "5lbs.Of.Pressure.2024.mkv"
    assert it["quarantine_path"] == str(media / "_quarantine/2026-10-12" / extra.relative_to(media))
    assert "DRY RUN" in q.format_plan(p) and extra.exists()          # dry run moves nothing
    assert q.plan(rep, s, groups=[2])["files"] == 0

    db = QDB()
    res = q.execute(p, db)
    keeper = Path(it["keeper"])
    assert res["moved"] == 1 and not extra.exists() and keeper.exists()
    assert Path(it["quarantine_path"]).exists()
    assert (keeper.parent / "5lbs Of Pressure (2024).eng.srt").exists()  # sidecar copied to keeper
    manifest = Path(res["manifest"])
    assert manifest.exists() and (manifest.parent / "manifest.tsv").exists()
    assert db.events[0][0] == "quarantine"
    # quarantine dir is invisible to discover/library scans
    from aibrarian.media import iter_files
    assert not any(x.is_relative_to(s.quarantine_root()) for x in iter_files(media))
    custom = media / "01-movies" / "q"
    (custom / "a").mkdir(parents=True)
    (custom / "a" / "x.mkv").write_bytes(b"1")
    assert not any(x.is_relative_to(custom) for x in iter_files(media, exclude=[custom]))

    assert q.restore(manifest)["items"][0]["result"] == "would restore"
    r = q.restore(manifest, execute=True, db=db)
    assert r["restored"] == 1 and extra.exists() and not Path(it["quarantine_path"]).exists()
    assert db.events[-1][0] == "restore"
    assert q.restore(manifest, execute=True)["restored"] == 0      # idempotent


def test_quarantine_skips_when_bytes_differ(tmp_path):
    from aibrarian import quarantine as q
    from aibrarian.config import Settings

    media = tmp_path / "media"
    rows = library(media)
    s = Settings(_env_file=None, media_root=media)
    p = q.plan(d.report(rows, d.HashCache(), exact_only=True), s)
    extra = Path(p["items"][0]["path"])
    data = bytearray(extra.read_bytes())
    data[100] ^= 0xFF                                               # changed after the report
    extra.write_bytes(bytes(data))
    res = q.execute(p)
    assert res["moved"] == 0 and res["results"][0]["reason"] == "sha256 differs" and extra.exists()
