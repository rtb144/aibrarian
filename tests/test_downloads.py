import json
import sqlite3
import zipfile
from pathlib import Path

from aibrarian import downloads
from aibrarian.calibre import Calibre
from aibrarian.progress import ProgressReporter


def w(p: Path, data: bytes | str = b"x"):
    p.parent.mkdir(parents=True, exist_ok=True)
    (p.write_bytes if isinstance(data, bytes) else p.write_text)(data)
    return p


def calibre_lib(tmp: Path) -> Calibre:
    lib = tmp / "books"
    w(lib / "Robert Greene/The 48 Laws of Power (1)/The 48 Laws of Power - Robert Greene.epub",
      b"E" * 500)
    c = sqlite3.connect(lib / "metadata.db")
    c.executescript("""
      CREATE TABLE books(id INTEGER PRIMARY KEY, title TEXT, path TEXT);
      CREATE TABLE authors(id INTEGER PRIMARY KEY, name TEXT);
      CREATE TABLE books_authors_link(id INTEGER PRIMARY KEY, book INT, author INT);
      CREATE TABLE identifiers(id INTEGER PRIMARY KEY, book INT, type TEXT, val TEXT);
      CREATE TABLE data(id INTEGER PRIMARY KEY, book INT, format TEXT, name TEXT,
                        uncompressed_size INT);
      INSERT INTO books VALUES (1,'The 48 Laws of Power','Robert Greene/The 48 Laws of Power (1)'),
                               (2,'Atomic Habits','James Clear/Atomic Habits (2)');
      INSERT INTO authors VALUES (1,'Robert Greene'),(2,'James Clear');
      INSERT INTO books_authors_link VALUES (1,1,1),(2,2,2);
      INSERT INTO data VALUES (1,1,'EPUB','The 48 Laws of Power - Robert Greene',500),
                              (2,2,'EPUB','Atomic Habits - James Clear',999);
    """)
    c.commit()
    c.close()
    return Calibre(lib, tmp / ".lock")


def tree(tmp: Path):
    dl = tmp / "downloads"
    w(dl / "$RECYCLE.BIN/desktop.ini", "x")
    w(dl / "[TGx]Downloaded from torrentgalaxy.to .txt", "ad")
    w(dl / "Downloaded from wWw.786zx.png", b"png")
    w(dl / "NEW upcoming releases by Xclusive.txt", "ad")
    (dl / "empty dir").mkdir(parents=True)
    # same bytes as the library copy -> hash match
    w(dl / "65 (2023) [720p] [WEBRip] [YTS.MX]/65.2023.720p.WEBRip.mp4", b"V" * 3000)
    w(dl / "65 (2023) [720p] [WEBRip] [YTS.MX]/www.YTS.MX.jpg", b"j")
    w(dl / "65 (2023) [720p] [WEBRip] [YTS.MX]/Sample/sample.mp4", b"s")
    # title+year match, library is 1080p vs 720p download
    w(dl / "A Bad Moms Christmas (2017) [720p] [YTS.AG]/A.Bad.Moms.Christmas.2017.720p.mp4",
      b"B" * 1000)
    # library is 720p, download 1080p -> smaller -> review
    w(dl / "Heat (1995) [1080p]/Heat.1995.1080p.mkv", b"H" * 1000)
    w(dl / "Zorro.2024.S01.COMPLETE.720p.AMZN.WEBRip.x264-GalaxyTV[TGx]/Zorro.2024.S01E01.mkv",
      b"Z" * 100)
    w(dl / "48 Laws of Power - Robert Greene.epub", b"E" * 500)       # hash match
    w(dl / "Atomic Habits - James Clear.mobi", b"m" * 10)               # other format
    w(dl / "Unknown Book - Nobody.pdf", b"p" * 10)                      # pending, suggestion
    w(dl / "Moved Book.epub", b"q")                                     # moved, still here
    w(dl / "2-books/Some Novel - Some Author.epub", b"n" * 20)
    w(dl / "2-books/Applied Book.epub", b"a" * 7)
    w(dl / "24 Dark Psychology Collection Pack-1/Book One.pdf", b"1" * 300)
    w(dl / "24 Dark Psychology Collection Pack-1/Covers/one.png", b"c")
    w(dl / "24 Dark Psychology Collection Pack-1/free audiobook version.txt", "ad")
    with zipfile.ZipFile(w(dl / "stuff.zip", b""), "w") as z:
        z.writestr("a/readme.md", "hi")
        z.writestr("a/b.epub", "x")
    w(dl / "setup.exe", b"M" * 50)
    w(dl / "Some Artist - Song.mp3", b"3" * 10)
    w(dl / "Other Artist - Tune.mp3", b"3" * 11)
    w(dl / "weird.xyz", b"?")
    w(tmp / "03-music/Some Artist/Some Artist - Song.mp3", b"3")
    lib = tmp / "01-movies"
    w(lib / "65 (2023)/65 (2023).mp4", b"V" * 3000)
    w(lib / "A Bad Moms Christmas (2017)/A Bad Moms Christmas (2017).mkv", b"b" * 5000)
    w(lib / "Heat (1995)/Heat (1995).mkv", b"h" * 900)
    w(tmp / "elsewhere/Moved Book.epub", b"q")
    w(tmp / "books/Applied/Applied Book.epub", b"a" * 7)
    return dl


def catalog(tmp: Path, cal: Calibre) -> dict:
    lib = tmp / "01-movies"
    dl = tmp / "downloads"
    media = [
        {"id": 1, "path": str(lib / "65 (2023)/65 (2023).mp4"), "root": "01-movies",
         "size_bytes": 3000, "title": "65", "year": 2023, "season": None, "episode": None,
         "height": 720},
        {"id": 2, "path": str(lib / "A Bad Moms Christmas (2017)/A Bad Moms Christmas (2017).mkv"),
         "root": "01-movies", "size_bytes": 5000, "title": "A Bad Moms Christmas", "year": 2017,
         "season": None, "episode": None, "height": 1080},
        {"id": 3, "path": str(lib / "Heat (1995)/Heat (1995).mkv"), "root": "01-movies",
         "size_bytes": 900, "title": "Heat", "year": 1995, "season": None, "episode": None,
         "height": 720},
    ]
    sugs = [
        {"id": 10, "domain": "book", "source_path": str(dl / "Unknown Book - Nobody.pdf"),
         "target_path": None, "status": "open", "confidence": 0.42, "decision": "review",
         "metadata": {}},
        {"id": 11, "domain": "book", "source_path": str(dl / "2-books/Applied Book.epub"),
         "target_path": str(tmp / "books/Applied/Applied Book.epub"), "status": "applied",
         "confidence": 0.95, "decision": "auto", "metadata": {}},
    ]
    moves = [{"old": str(dl / "Moved Book.epub"), "new": str(tmp / "elsewhere/Moved Book.epub"),
              "via": "file_op"}]
    return {"media": media, "samples": {}, "suggestions": sugs, "moves": moves,
            "calibre": cal.books()}


def run(tmp, **kw):
    dl = tree(tmp)
    cal = calibre_lib(tmp)
    before = {str(p): p.stat().st_mtime_ns for p in tmp.rglob("*")}
    rep = downloads.triage(dl, catalog(tmp, cal), calibre_dir=tmp / "books",
                           music_dir=tmp / "03-music",
                           progress=ProgressReporter("t", interval=0, logger=_Log()), **kw)
    after = {str(p): p.stat().st_mtime_ns for p in tmp.rglob("*")}
    assert before == after  # strictly read-only
    return dl, {Path(r["path"]).name: r for r in rep["items"]}, rep


class _Log:
    def __init__(self):
        self.lines = []

    def info(self, m):
        self.lines.append(m)


def test_classification(tmp_path):
    dl, by, rep = run(tmp_path)
    assert by["$RECYCLE.BIN"]["class"] == "junk"
    for n in ("[TGx]Downloaded from torrentgalaxy.to .txt", "Downloaded from wWw.786zx.png",
              "NEW upcoming releases by Xclusive.txt", "empty dir"):
        assert by[n]["class"] == "junk" and by[n]["action"] == "quarantine_safe", n
    m = by["65 (2023) [720p] [WEBRip] [YTS.MX]"]
    assert m["class"] == "in_library_movie" and m["details"]["match"] == "hash"
    assert m["action"] == "quarantine_safe" and m["library_paths"][0].endswith("65 (2023).mp4")
    bm = by["A Bad Moms Christmas (2017) [720p] [YTS.AG]"]
    assert bm["class"] == "in_library_movie"
    assert bm["details"]["library_vs_download"] == "bigger"
    heat = by["Heat (1995) [1080p]"]
    assert heat["class"] == "in_library_movie" and heat["action"] == "review"
    assert heat["details"]["library_vs_download"] == "smaller"
    z = by["Zorro.2024.S01.COMPLETE.720p.AMZN.WEBRip.x264-GalaxyTV[TGx]"]
    assert z["class"] == "pending_video" and z["action"] == "import"
    b = by["48 Laws of Power - Robert Greene.epub"]
    assert b["class"] == "in_library_book" and b["action"] == "quarantine_safe"
    a = by["Atomic Habits - James Clear.mobi"]
    assert a["class"] == "in_library_book" and a["action"] == "review"
    u = by["Unknown Book - Nobody.pdf"]
    assert u["class"] == "pending_book"
    assert u["details"]["suggestions"] == [{"id": 10, "status": "open", "score": 0.42}]
    assert by["Moved Book.epub"]["class"] == "moved_already"
    # 2-books is expanded
    assert "2-books" not in by
    assert by["Some Novel - Some Author.epub"]["class"] == "pending_book"
    assert by["Applied Book.epub"]["class"] == "in_library_book"
    pack = by["24 Dark Psychology Collection Pack-1"]
    assert pack["class"] == "pending_book" and pack["file_count"] == 3
    zp = by["stuff.zip"]
    assert zp["class"] == "archive" and zp["details"]["peek"]["entries"] == 2
    assert by["setup.exe"]["class"] == "software" and by["setup.exe"]["action"] == "keep"
    assert by["Some Artist - Song.mp3"]["details"]["in_music_library"] is True
    assert by["Other Artist - Tune.mp3"]["details"]["in_music_library"] is False
    assert by["weird.xyz"]["class"] == "unknown"
    s = rep["summary"]
    assert s["per_class"]["junk"]["count"] == 5
    safe = sum(r["size_bytes"] for r in rep["items"] if r["action"] == "quarantine_safe")
    assert s["reclaimable_bytes"] == safe
    assert rep["hashed"] >= 2


def test_hash_budget(tmp_path):
    _, by, rep = run(tmp_path, max_hash=0)
    m = by["65 (2023) [720p] [WEBRip] [YTS.MX]"]
    assert m["details"]["match"] == "size" and m["action"] == "review"
    assert rep["hashed"] == 0 and rep["hash_budget_exhausted"]


def test_reports(tmp_path):
    _, _, rep = run(tmp_path)
    jp, cp = downloads.write_reports(rep, tmp_path / "r/downloads-triage.json")
    assert json.loads(jp.read_text())["entries"] == rep["entries"]
    assert cp.read_text().splitlines()[0].startswith("path,class,action")
    txt = downloads.format_summary(rep)
    assert "reclaimable (quarantine_safe)" in txt and "in_library_movie" in txt


def test_junk_patterns():
    j = downloads.is_junk_file
    assert j(Path("/d/x/Thumbs.db"), 1)
    assert j(Path("/d/free audiobook version.txt"), 1)
    assert j(Path("/d/m/movie-sample.mkv"), 10)
    assert not j(Path("/d/m/movie-sample.mkv"), 300 * 1024 ** 2)
    assert not j(Path("/d/Notes on Python.txt"), 1)
    assert not j(Path("/d/Book/cover.png"), 1)
