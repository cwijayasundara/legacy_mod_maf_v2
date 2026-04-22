"""Tests for the translator fenced-block output parser.

Pins the five path-header conventions we accept. Silent parser
regressions here have previously hidden full multi-file translations
in ``translator_response.md`` (INTCALC once lost three attempts this
way).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from maf_generic_migrator_v1.platform_core.maf_workflows.unit_worker import (
    _apply_translator_output,
)


@dataclass
class _FakeUnitResult:
    """Stand-in for UnitResult — we only need a mutable object."""
    dummy: str = ""


def _write(tmp_path: Path, response: str) -> list[str]:
    """Run the parser against ``response`` and return the list of files
    that ended up on disk (relative to ``tmp_path``)."""
    _apply_translator_output(response, tmp_path, _FakeUnitResult())
    files: list[str] = []
    for p in sorted(tmp_path.rglob("*")):
        if p.is_file() and p.name != "translator_response.md":
            files.append(str(p.relative_to(tmp_path)))
    return files


def test_inline_path_header_with_lang(tmp_path: Path):
    """``` ```xml path=pom.xml ``` → writes pom.xml."""
    response = """
```xml path=pom.xml
<project/>
```
"""
    files = _write(tmp_path, response)
    assert files == ["pom.xml"]
    assert (tmp_path / "pom.xml").read_text().strip() == "<project/>"


def test_inline_path_header_without_lang(tmp_path: Path):
    """``` ```path=pom.xml ``` (no lang) → writes pom.xml."""
    response = """
```path=pom.xml
<project/>
```
"""
    files = _write(tmp_path, response)
    assert files == ["pom.xml"]


def test_html_comment_path_header(tmp_path: Path):
    """``` ```xml\\n<!-- path=pom.xml -->\\n<body> ```.

    This was the INTCALC failure case — all 8 files were stuck in
    translator_response.md because we didn't accept this form.
    """
    response = """
```xml
<!-- path=pom.xml -->
<project>
  <modelVersion>4.0.0</modelVersion>
</project>
```
"""
    files = _write(tmp_path, response)
    assert files == ["pom.xml"]
    content = (tmp_path / "pom.xml").read_text()
    # Path comment must not leak into the emitted file.
    assert "<!-- path=pom.xml -->" not in content
    assert "<modelVersion>4.0.0</modelVersion>" in content


def test_double_slash_comment_path_header(tmp_path: Path):
    """``// path=src/main/java/Foo.java`` inside a Java fence."""
    response = """
```java
// path=src/main/java/com/example/Foo.java
package com.example;
public class Foo {}
```
"""
    files = _write(tmp_path, response)
    assert files == ["src/main/java/com/example/Foo.java"]
    content = (tmp_path / "src/main/java/com/example/Foo.java").read_text()
    assert "// path=" not in content
    assert "public class Foo" in content


def test_hash_comment_path_header(tmp_path: Path):
    """``# path=application.yml`` inside a YAML or shell fence."""
    response = """
```yaml
# path=src/main/resources/application.yml
spring:
  application:
    name: demo
```
"""
    files = _write(tmp_path, response)
    assert files == ["src/main/resources/application.yml"]


def test_c_block_comment_path_header(tmp_path: Path):
    """``/* path=Module.java */`` — seen less often but still valid."""
    response = """
```java
/* path=src/main/java/Module.java */
public class Module {}
```
"""
    files = _write(tmp_path, response)
    assert files == ["src/main/java/Module.java"]


def test_mixed_conventions_in_one_response(tmp_path: Path):
    """A real response often mixes conventions across its files.
    Every block that carries ANY recognized header MUST land on disk.
    """
    response = """
Explanation prose the LLM added.

```xml path=pom.xml
<project/>
```

```java
// path=src/main/java/App.java
public class App {}
```

```yaml
# path=src/main/resources/application.yml
debug: true
```

```java
<!-- path=src/test/java/AppTest.java -->
public class AppTest {}
```
"""
    files = sorted(_write(tmp_path, response))
    assert files == sorted([
        "pom.xml",
        "src/main/java/App.java",
        "src/main/resources/application.yml",
        "src/test/java/AppTest.java",
    ])


def test_block_without_any_path_is_skipped(tmp_path: Path):
    """A fenced block with no path header is prose-style content, not
    a file directive. Skip it; don't pollute the workdir with a
    mis-named file.
    """
    response = """
```java
public class NoPath {}
```
"""
    files = _write(tmp_path, response)
    assert files == []


def test_empty_response_writes_translator_response_md(tmp_path: Path):
    """When nothing parses, the raw response is archived for debug."""
    _apply_translator_output("just prose, no blocks", tmp_path, _FakeUnitResult())
    assert (tmp_path / "translator_response.md").is_file()


def test_path_traversal_refused(tmp_path: Path):
    """Malicious / confused ``path=../../etc/passwd`` is dropped."""
    response = """
```xml
<!-- path=../pwned.xml -->
<malicious/>
```

```xml path=pom.xml
<safe/>
```
"""
    files = _write(tmp_path, response)
    assert files == ["pom.xml"]
    # Confirm the escape attempt didn't write anywhere above workdir.
    parent = tmp_path.parent
    assert not (parent / "pwned.xml").exists()


def test_path_comment_stripped_from_emitted_body(tmp_path: Path):
    """The path-header comment line itself must not appear in the
    generated file — javac would complain about a stray HTML comment
    in a Java source.
    """
    response = """
```java
// path=src/main/java/Foo.java
package com.example;
public class Foo {}
```
"""
    _write(tmp_path, response)
    content = (tmp_path / "src/main/java/Foo.java").read_text()
    assert not content.lstrip().startswith("// path=")
    assert content.lstrip().startswith("package com.example;")
