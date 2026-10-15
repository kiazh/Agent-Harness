"""RepoMap tests (Stream D): inventory, zero-work repeat, evidence map, self-map."""

import os
import time
from pathlib import Path

import pytest

from machina.repomap import ContentHashCache, EvidenceMap, build_repomap

REPO_ROOT = str(Path(__file__).resolve().parents[1])


@pytest.fixture
def tiny_tree(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "a.py").write_text(
        "import os\nfrom pkg import b\n\nif __name__ == '__main__':\n    print('hi')\n"
    )
    (tmp_path / "pkg" / "b.py").write_text("X = 1\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("def test_x():\n    assert True\n")
    (tmp_path / "notes.txt").write_text("hello\n")
    return tmp_path


def test_inventory_files_loc_entry_tests_edges(tiny_tree):
    m = build_repomap(str(tiny_tree))
    rel_a = os.path.join("pkg", "a.py")
    assert rel_a in m.files
    assert m.loc_by_file[rel_a] == 5
    assert m.total_loc == sum(m.loc_by_file.values()) and m.total_loc > 0
    assert rel_a in m.entry_points  # __main__ guard detected
    assert os.path.join("tests", "test_a.py") in m.test_files
    assert "notes.txt" in m.files and "notes.txt" not in m.loc_by_file
    assert "pkg.a" in m.import_edges
    assert "os" in m.import_edges["pkg.a"]


def test_repeat_with_cache_is_zero_work(tiny_tree):
    cache = ContentHashCache()
    first = build_repomap(str(tiny_tree), cache=cache)
    assert first.files_scanned > 0
    second = build_repomap(str(tiny_tree), cache=cache)
    assert second.files_scanned == 0
    assert second.total_loc == first.total_loc
    assert second.files == first.files


def test_cache_survives_to_disk(tiny_tree, tmp_path):
    cache_file = str(tmp_path / "repomap.json")
    c1 = ContentHashCache(cache_file)
    assert build_repomap(str(tiny_tree), cache=c1).files_scanned > 0
    c1.save()
    c2 = ContentHashCache(cache_file)
    # entries load from disk but files_scanned counts parses; lookup hits -> 0
    m = build_repomap(str(tiny_tree), cache=c2)
    assert m.files_scanned == 0
    c2.save()


def test_lookup_rejects_stale_digest_even_when_stat_matches(tmp_path):
    """R4-088(e): the stored sha256 is verified, not write-only."""
    path = tmp_path / "m.py"
    path.write_text("import os\n", encoding="utf-8")
    cache = ContentHashCache()
    cache.store(str(path), (1, ["os"], False))
    assert cache.lookup(str(path)) == (1, ["os"], False)

    st = os.stat(path)
    path.write_text("import re\n", encoding="utf-8")
    os.utime(path, (st.st_atime, st.st_mtime))
    # Same mtime and size — only the digest can tell.
    assert cache.lookup(str(path)) is None

    cache.store(str(path), (1, ["re"], False))
    assert cache.lookup(str(path)) == (1, ["re"], False)


def test_rebuild_detects_mtime_preserving_rewrite(tiny_tree):
    """R4-088(e): a same-stat rewrite cannot keep stale inventory data."""
    target = tiny_tree / "pkg" / "a.py"
    cache = ContentHashCache()
    first = build_repomap(str(tiny_tree), cache=cache)
    assert "os" in first.import_edges["pkg.a"]

    original = target.read_text(encoding="utf-8")
    rewritten = original.replace("import os", "import re")
    assert len(rewritten) == len(original)
    st = os.stat(target)
    target.write_text(rewritten, encoding="utf-8")
    os.utime(target, (st.st_atime, st.st_mtime))

    second = build_repomap(str(tiny_tree), cache=cache)
    assert second.files_scanned == 1
    assert "re" in second.import_edges["pkg.a"]
    assert "os" not in second.import_edges["pkg.a"]


def test_evidence_map_ranges_and_zero_repeat_work(tmp_path):
    f = tmp_path / "doc.txt"
    f.write_text("\n".join(f"line{i}" for i in range(1, 11)))
    ev = EvidenceMap()
    assert ev.read(str(f), 1, 4) == ["line1", "line2", "line3"]
    assert ev.reads_from_disk == 1
    assert ev.read(str(f), 2, 3) == ["line2"]  # covered -> served from cache
    assert ev.reads_from_disk == 1
    assert ev.is_covered(str(f), 1, 4)
    assert not ev.is_covered(str(f), 1, 11)
    assert ev.covered(str(f)) == [(1, 4)]
    with pytest.raises(ValueError):
        ev.read(str(f), 0, 2)
    with pytest.raises(FileNotFoundError):
        ev.read(str(tmp_path / "missing.txt"))


def test_self_map_completes_in_seconds_with_zero_repeat_reads():
    cache = ContentHashCache()
    t0 = time.monotonic()
    first = build_repomap(REPO_ROOT, cache=cache)
    dt = time.monotonic() - t0
    assert dt < 30, f"self-map took {dt:.1f}s"
    assert len(first.files) > 100
    assert first.total_loc > 10_000
    assert len(first.test_files) > 50
    assert len(first.import_edges) > 50
    second = build_repomap(REPO_ROOT, cache=cache)
    assert second.files_scanned == 0
    assert second.total_loc == first.total_loc
