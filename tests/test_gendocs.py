from aibrarian.gendocs import discover_compose, generate, iter_sources


class LLM:
    def __init__(self):
        self.n = 0

    def chat_text(self, prompt):
        self.n += 1
        return "# doc"


def test_excludes_venv_and_caches(tmp_path):
    (tmp_path / "app.py").write_text("x=1")
    for d in ("venv/lib/python3.14/site-packages/neo4j", ".git", "node_modules"):
        (tmp_path / d).mkdir(parents=True)
        (tmp_path / d / "junk.py").write_text("y")
    (tmp_path / "docker-compose.yml").write_text("services: {}")
    (tmp_path / "venv" / "compose.yml").write_text("no")
    assert [p.name for p in iter_sources([tmp_path])] == ["app.py"]
    assert [p.name for p in discover_compose([tmp_path])] == ["docker-compose.yml"]
    llm = LLM()
    out = tmp_path / "out"
    assert generate([tmp_path], out, llm) == {"ok": 1, "failed": 0, "skipped": 0}
    assert generate([tmp_path], out, llm) == {"ok": 0, "failed": 0, "skipped": 1}
    assert (out / "app.md").read_text().startswith("---\ntitle: app")
