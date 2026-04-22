"""Tests for the unit-worker workdir snapshot + rollback helpers."""
from __future__ import annotations

from pathlib import Path

from maf_generic_migrator_v1.platform_core.maf_workflows.unit_worker import (
    _restore_workdir,
    _snapshot_workdir,
)


def _seed(root: Path, tree: dict[str, str]) -> None:
    for rel, content in tree.items():
        dest = root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")


def test_snapshot_captures_nested_files(tmp_path: Path):
    _seed(tmp_path, {
        "pom.xml": "<project/>",
        "src/main/java/com/example/A.java": "class A {}",
        "src/test/java/com/example/ATest.java": "class ATest {}",
        "README.md": "# README",
    })
    snap = _snapshot_workdir(tmp_path)
    assert set(snap) == {
        "pom.xml",
        "src/main/java/com/example/A.java",
        "src/test/java/com/example/ATest.java",
        "README.md",
    }


def test_snapshot_skips_build_dirs(tmp_path: Path):
    _seed(tmp_path, {
        "pom.xml": "<project/>",
        "target/classes/com/example/A.class": "compiled binary",
        "target/maven-status/foo.lst": "internal",
        "node_modules/left-pad/index.js": "module.exports = x => x;",
        ".git/HEAD": "ref: refs/heads/main",
        "src/main/java/A.java": "class A {}",
    })
    snap = _snapshot_workdir(tmp_path)
    assert "target/classes/com/example/A.class" not in snap
    assert "node_modules/left-pad/index.js" not in snap
    assert ".git/HEAD" not in snap
    assert "pom.xml" in snap
    assert "src/main/java/A.java" in snap


def test_restore_deletes_non_snapshot_files(tmp_path: Path):
    """After restore, the workdir must look exactly like the snapshot —
    files added since the snapshot must be gone.
    """
    _seed(tmp_path, {
        "pom.xml": "<project/>",
        "src/main/java/A.java": "class A {}",
    })
    snap = _snapshot_workdir(tmp_path)

    # Simulate a bad retranslation that adds new (broken) files.
    _seed(tmp_path, {
        "pom.xml": "<project>BROKEN</project>",                # edited
        "src/main/java/A.java": "class A { extra stuff }",     # edited
        "src/main/java/BogusHallucination.java": "garbage",    # new
    })

    _restore_workdir(tmp_path, snap)

    assert (tmp_path / "pom.xml").read_text() == "<project/>"
    assert (tmp_path / "src/main/java/A.java").read_text() == "class A {}"
    assert not (tmp_path / "src/main/java/BogusHallucination.java").exists()


def test_restore_preserves_build_dirs(tmp_path: Path):
    """``mvn compile`` produces ``target/`` output between the snapshot
    and the restore — we mustn't delete those, the next compile would
    have to rebuild from scratch.
    """
    _seed(tmp_path, {"pom.xml": "<project/>"})
    snap = _snapshot_workdir(tmp_path)

    # Simulate Maven writing target/ after the snapshot was taken.
    _seed(tmp_path, {"target/classes/X.class": "compiled"})

    _restore_workdir(tmp_path, snap)

    assert (tmp_path / "target/classes/X.class").is_file()
    assert (tmp_path / "pom.xml").read_text() == "<project/>"


def test_restore_is_idempotent(tmp_path: Path):
    _seed(tmp_path, {
        "pom.xml": "<project/>",
        "src/main/java/A.java": "class A {}",
    })
    snap = _snapshot_workdir(tmp_path)

    _restore_workdir(tmp_path, snap)
    _restore_workdir(tmp_path, snap)          # twice
    _restore_workdir(tmp_path, snap)          # three times

    assert (tmp_path / "pom.xml").read_text() == "<project/>"
    assert (tmp_path / "src/main/java/A.java").read_text() == "class A {}"


def test_snapshot_of_empty_dir_returns_empty(tmp_path: Path):
    snap = _snapshot_workdir(tmp_path)
    assert snap == {}


def test_byte_level_fidelity_with_non_utf8_bytes(tmp_path: Path):
    """Snapshot uses raw bytes, so a file with non-UTF-8 bytes still
    round-trips cleanly (LLM output is always UTF-8 but belt-and-suspenders).
    """
    weird = b"\xff\xfe\x00\x01\x02binary"
    (tmp_path / "data.bin").write_bytes(weird)

    snap = _snapshot_workdir(tmp_path)
    (tmp_path / "data.bin").unlink()

    _restore_workdir(tmp_path, snap)
    assert (tmp_path / "data.bin").read_bytes() == weird
