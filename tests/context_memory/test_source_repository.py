from src.context.sources.repository import RepositorySource
from src.context.types import Layer, Task


def write(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_collects_readme_and_docs(tmp_path):
    write(tmp_path, "README.md", "# Project\n\nStores memories in pgvector.\n")
    write(tmp_path, "docs/architecture.md", "# Architecture\n\nThe API is FastAPI.\n")
    items = RepositorySource(root=tmp_path).collect(Task(description="how is data stored"))
    assert {i.layer for i in items} == {Layer.REPOSITORY}
    assert {i.source for i in items} == {"README.md", "docs/architecture.md"}


def test_files_named_by_the_task_are_always_collected(tmp_path):
    write(tmp_path, "README.md", "# P\n\nx\n")
    write(tmp_path, "server/main.py", "DEFAULT_LLM_PROVIDER = 'gemini'\n")
    items = RepositorySource(root=tmp_path).collect(
        Task(description="change the provider", files=("server/main.py",))
    )
    assert any(i.source == "server/main.py" for i in items)


def test_manifests_are_collected(tmp_path):
    write(tmp_path, "pyproject.toml", "[project]\nname = 'x'\n")
    items = RepositorySource(root=tmp_path).collect(Task(description="what are the deps"))
    assert any(i.source == "pyproject.toml" for i in items)


def test_oversized_files_are_skipped_and_the_skip_is_recorded(tmp_path):
    write(tmp_path, "README.md", "# P\n\nx\n")
    write(tmp_path, "big.md", "y" * 500)
    source = RepositorySource(root=tmp_path, max_file_bytes=100)
    items = source.collect(Task(description="x", files=("big.md",)))
    assert all(i.source != "big.md" for i in items)
    assert "big.md" in source.last_report.skipped


def test_binary_files_are_skipped_and_the_skip_is_recorded(tmp_path):
    write(tmp_path, "README.md", "# P\n\nx\n")
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01\x02" * 100)
    source = RepositorySource(root=tmp_path)
    items = source.collect(Task(description="x", files=("blob.bin",)))
    assert all(i.source != "blob.bin" for i in items)
    assert "blob.bin" in source.last_report.skipped


def test_a_missing_task_file_is_recorded_rather_than_silently_ignored(tmp_path):
    write(tmp_path, "README.md", "# P\n\nx\n")
    source = RepositorySource(root=tmp_path)
    source.collect(Task(description="x", files=("does/not/exist.py",)))
    assert "does/not/exist.py" in source.last_report.skipped


def test_the_report_states_how_many_files_it_considered_and_kept(tmp_path):
    write(tmp_path, "README.md", "# P\n\nx\n")
    write(tmp_path, "docs/a.md", "# A\n\ny\n")
    source = RepositorySource(root=tmp_path)
    source.collect(Task(description="x"))
    assert source.last_report.considered == 2
    assert source.last_report.collected == 2


def test_repository_items_are_never_stale_and_are_fully_confident(tmp_path):
    # Repository truth is current by definition; confidence is a memory concept.
    write(tmp_path, "README.md", "# P\n\nx\n")
    items = RepositorySource(root=tmp_path).collect(Task(description="x"))
    assert all(i.signals.staleness == 0.0 for i in items)
    assert all(i.signals.confidence == 1.0 for i in items)


def test_nested_readmes_are_collected(tmp_path):
    # Measured on the real repo: 30 README.md files, only the root one was reachable.
    # A question about the server was scored against server/README.md, which no
    # arm could ever retrieve - that reads as poor ranking, not as missing input.
    write(tmp_path, "README.md", "# Root\n\nroot text\n")
    write(tmp_path, "server/README.md", "# Server\n\nhow to start the stack\n")
    items = RepositorySource(root=tmp_path).collect(Task(description="start the stack"))
    assert any(i.source == "server/README.md" for i in items)


def test_nested_manifests_are_collected(tmp_path):
    # The repo has no root docker-compose.yaml; the only one is server/docker-compose.yaml,
    # so a root-only manifest list matched nothing at all.
    write(tmp_path, "server/docker-compose.yaml", "services:\n  postgres:\n    image: pgvector\n")
    items = RepositorySource(root=tmp_path).collect(Task(description="which services run"))
    assert any(i.source == "server/docker-compose.yaml" for i in items)


def test_vendor_directories_are_never_walked(tmp_path):
    write(tmp_path, "README.md", "# P\n\nx\n")
    write(tmp_path, "node_modules/pkg/README.md", "# Vendor\n\nnot ours\n")
    write(tmp_path, ".venv/lib/README.md", "# Vendor\n\nnot ours\n")
    items = RepositorySource(root=tmp_path).collect(Task(description="x"))
    sources = {i.source for i in items}
    assert not any(s.startswith("node_modules/") for s in sources)
    assert not any(s.startswith(".venv/") for s in sources)


def test_the_number_of_walked_files_is_capped_and_the_cap_is_reported(tmp_path):
    for n in range(30):
        write(tmp_path, f"pkg{n}/README.md", f"# P{n}\n\nbody {n}\n")
    source = RepositorySource(root=tmp_path, max_files=10)
    source.collect(Task(description="x"))
    assert source.last_report.considered <= 10
    assert source.last_report.capped is True


def test_docs_are_walked_recursively(tmp_path):
    write(tmp_path, "docs/deep/nested/guide.md", "# G\n\nz\n")
    items = RepositorySource(root=tmp_path).collect(Task(description="x"))
    assert any(i.source == "docs/deep/nested/guide.md" for i in items)


def test_non_markdown_task_files_are_chunked_as_a_single_item(tmp_path):
    write(tmp_path, "server/main.py", "# a comment that looks like a heading\nX = 1\n")
    items = RepositorySource(root=tmp_path).collect(Task(description="x", files=("server/main.py",)))
    assert len(items) == 1
    assert "X = 1" in items[0].content


def test_env_example_is_collected_as_a_manifest(tmp_path):
    # It is the canonical list of a service's configuration variables and the
    # file server/AGENTS.md points at for configuration.
    write(tmp_path, "server/.env.example", "MEM0_DEFAULT_LLM_PROVIDER=openai\n")
    items = RepositorySource(root=tmp_path).collect(Task(description="which env vars exist"))
    assert any(i.source == "server/.env.example" for i in items)


def test_source_files_are_not_collected_unless_the_task_names_them(tmp_path):
    # A stated limit, not an accident: indexing all source would blow the budget
    # and is out of scope for this layer. The eval harness reports a ground-truth
    # item living only in code as unreachable rather than as a ranking failure.
    write(tmp_path, "README.md", "# P\n\nx\n")
    write(tmp_path, "server/main.py", "DEFAULT = 1\n")
    without = RepositorySource(root=tmp_path).collect(Task(description="what is the default"))
    assert all(i.source != "server/main.py" for i in without)
    with_file = RepositorySource(root=tmp_path).collect(
        Task(description="what is the default", files=("server/main.py",))
    )
    assert any(i.source == "server/main.py" for i in with_file)
