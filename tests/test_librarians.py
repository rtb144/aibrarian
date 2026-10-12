import zipfile
from pathlib import Path

from aibrarian import books
from aibrarian.video_librarian import (
    best_match,
    canonical_target,
    decide,
    make_suggestion,
    normalize_result,
)
from conftest import FakeOllama

ROOTS = {"movie": "01-movies", "tv": "02-tvshows", "music": "03-music"}


def test_normalize_result_coerces():
    r = normalize_result({"kind": "film", "year": "1999.0", "confidence": 7,
                          "suggested_canonical_name": "A/B  C"})
    assert r["kind"] == "unknown" and r["year"] == 1999 and r["confidence"] == 1.0
    assert r["suggested_canonical_name"] == "A-B C"
    assert normalize_result("nope")["confidence"] == 0.0


def test_best_match_year_boost():
    cands = [{"Name": "Alien", "ProductionYear": 1979, "Id": "a"},
             {"Name": "Aliens", "ProductionYear": 1986, "Id": "b"}]
    item, score = best_match({"title": "Alien", "year": 1979}, cands)
    assert item["Id"] == "a" and score == 1.0


def test_canonical_targets(tmp_path):
    src = tmp_path / "downloads" / "x.mkv"
    m = canonical_target({"kind": "movie", "title": "Alien: Director's Cut", "year": 1979},
                         src, tmp_path, ROOTS)
    assert m == tmp_path / "01-movies/Alien - Director's Cut (1979)/Alien - Director's Cut (1979).mkv"
    t = canonical_target({"kind": "tv", "series": "Show", "season": 1, "episode": 2,
                          "episode_title": "Pilot"}, src, tmp_path, ROOTS)
    assert t == tmp_path / "02-tvshows/Show/Season 01/Show - S01E02 - Pilot.mkv"
    mu = canonical_target({"kind": "music", "artist": "A", "album": None, "track": 3,
                           "track_title": "Song"}, src.with_suffix(".flac"), tmp_path, ROOTS)
    assert mu == tmp_path / "03-music/A/Unknown Album/03 - Song.flac"
    assert canonical_target({"kind": "unknown"}, src, tmp_path, ROOTS) is None


def test_make_suggestion_decisions(tmp_path):
    src = tmp_path / "01-movies" / "a.mkv"
    parsed = normalize_result({"kind": "movie", "title": "A", "year": 2000, "confidence": 0.7})
    s = make_suggestion(src, parsed, {"Id": "j", "Name": "A"}, 1.0, tmp_path, ROOTS, 0.85, 0.6)
    assert s.decision == "suggest" and s.confidence == 0.85 and s.op == "move"
    s2 = make_suggestion(src, parsed, None, 0, tmp_path, ROOTS, 0.85, 0.6)
    assert s2.decision == "review"
    assert decide(0.1, 0.85, 0.6) == "reject"


def make_epub(path: Path, title="Dune", author="Frank Herbert", isbn="9780441013593"):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("META-INF/container.xml",
                   '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                   '<rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>')
        z.writestr("OEBPS/content.opf",
                   '<package xmlns="http://www.idpf.org/2007/opf" '
                   'xmlns:dc="http://purl.org/dc/elements/1.1/"><metadata>'
                   f"<dc:title>{title}</dc:title><dc:creator>{author}</dc:creator>"
                   f"<dc:identifier>urn:isbn:{isbn}</dc:identifier><dc:date>1965-08-01</dc:date>"
                   "</metadata></package>")


def test_epub_metadata(tmp_path):
    p = tmp_path / "d.epub"
    make_epub(p)
    m = books.extract_metadata(p)
    assert (m["title"], m["author"], m["isbn"], m["year"]) == ("Dune", "Frank Herbert",
                                                              "9780441013593", 1965)


def test_book_catalog_high_confidence_ref(tmp_path, settings):
    p = tmp_path / "drop" / "dune.epub"
    p.parent.mkdir()
    make_epub(p)
    ref = {"title": "Dune", "author": "Frank Herbert", "source": "openlibrary"}
    s = books.catalog_file(p, settings, FakeOllama(), ol_lookup=lambda *a: ref)
    assert s.decision == "suggest" and s.media_kind == "ebook"
    assert s.confidence == 0.95          # embedded title+author agree 100 % with the reference
    assert s.target_path == str(settings.calibre_dir / "Frank Herbert" / "Dune" / "dune.epub")


def test_book_catalog_auto_mode_and_llm(tmp_path, settings):
    settings.book_mode = "auto"
    p = tmp_path / "book.m4b"
    p.write_bytes(b"\0")
    llm = FakeOllama({"kind": "audiobook", "title": "Book", "author": "Auth", "series": "S",
                      "year": None, "confidence": 0.9, "notes": "m4b"})
    s = books.catalog_file(p, settings, llm, ol_lookup=lambda *a: None,
                           gb_lookup=lambda *a: None)
    # LLM-only metadata is weak evidence now: 0.35, whatever the LLM claims (0.9)
    assert s.confidence == 0.35 and s.decision == "reject"
    assert s.metadata["classification"]["llm_confidence"] == 0.9
    assert s.target_path.endswith("audiobooks/Auth/S/Book/book.m4b")


def test_book_low_confidence_without_destination(tmp_path, settings):
    p = tmp_path / "x.pdf"
    p.write_bytes(b"%PDF")
    llm = FakeOllama({"kind": "ebook", "title": None, "confidence": 0.95, "notes": ""})
    s = books.catalog_file(p, settings, llm, gb_lookup=lambda *a: None)
    assert s.target_path is None and s.decision == "reject"


def test_book_decide_thresholds(settings):
    assert books.decide(0.85, settings) == "suggest"
    assert books.decide(0.6, settings) == "review"
    assert books.decide(0.59, settings) == "reject"
