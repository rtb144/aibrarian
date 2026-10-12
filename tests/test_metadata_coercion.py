from pathlib import Path

from aibrarian import books
from aibrarian.olindex import as_str, ntitle, surname


class FakeIndirect:
    def __init__(self, target):
        self.target = target

    def get_object(self):
        return self.target


class Broken:
    def get_object(self):
        raise ValueError("unresolvable")


def test_as_str_variants():
    assert as_str(FakeIndirect("  Dune ")) == "Dune"
    assert as_str(FakeIndirect(FakeIndirect(b"Herbert"))) == "Herbert"
    assert as_str(b"\xfe\xff\x00H\x00i") == "Hi"
    assert as_str(["A", None, FakeIndirect("B")]) == "A; B"
    assert as_str("   ") is None
    assert as_str(None) is None
    assert as_str(Broken()) is None
    assert as_str(42) == "42"


def test_ntitle_surname_accept_non_str():
    assert ntitle(FakeIndirect("The Hobbit: There and Back")) == ntitle("The Hobbit")
    assert surname(FakeIndirect("J. R. R. Tolkien")) == "tolkien"
    assert ntitle(Broken()) == ""
    assert surname(None) == "" and surname(Broken()) == ""


def test_pdf_metadata_resolves_indirect(monkeypatch, tmp_path):
    import pypdf

    class FakeReader:
        def __init__(self, _p):
            self.metadata = {"/Title": FakeIndirect("  Dune  "), "/Author": FakeIndirect(b"Frank Herbert")}

    monkeypatch.setattr(pypdf, "PdfReader", FakeReader)
    f = tmp_path / "x.pdf"
    f.write_bytes(b"%PDF-1.4")
    meta = books.extract_metadata(Path(f))
    assert meta["title"] == "Dune" and meta["author"] == "Frank Herbert"
    assert all(isinstance(meta[k], str) for k in ("title", "author"))


def test_pdf_metadata_drops_empty(monkeypatch, tmp_path):
    import pypdf

    class FakeReader:
        def __init__(self, _p):
            self.metadata = {"/Title": FakeIndirect(""), "/Author": Broken()}

    monkeypatch.setattr(pypdf, "PdfReader", FakeReader)
    f = tmp_path / "y.pdf"
    f.write_bytes(b"%PDF-1.4")
    meta = books.extract_metadata(Path(f))
    assert "title" not in meta and "author" not in meta
