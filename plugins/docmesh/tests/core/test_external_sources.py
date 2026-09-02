from pathlib import Path

from docmesh.config import discover_corpus
from docmesh.models import SourceConfig


def test_manifest_can_include_an_external_source_without_walking_its_parent(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    external = tmp_path / "shared" / "references.bib"
    project.mkdir()
    external.parent.mkdir()
    external.write_text("@article{one, title={Shared}}", encoding="utf-8")

    report = discover_corpus(
        project, configured_sources=[SourceConfig(str(external), "reference")]
    )

    assert [item.path for item in report.included] == [str(external.resolve())]
    assert report.included[0].role == "reference"
