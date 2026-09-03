from pathlib import Path

from docmesh import api
from docmesh.config import (
    ApprovalRequired,
    discover_corpus,
    infer_role,
    initialize_project,
)


def test_discovery_assigns_roles_and_requires_approval(tmp_path: Path) -> None:
    (tmp_path / "guide.md").write_text("# Guide\nA source.", encoding="utf-8")
    (tmp_path / "generated" / "guide.md").parent.mkdir()
    (tmp_path / "generated" / "guide.md").write_text(
        "# Generated\nA mirror.", encoding="utf-8"
    )
    (tmp_path / "paper.pdf").write_bytes(b"not a pdf")

    report = discover_corpus(tmp_path)

    roles = {
        Path(item.path).relative_to(tmp_path).as_posix(): item.role
        for item in report.included
    }
    assert roles == {
        "guide.md": "editable",
        "generated/guide.md": "mirror",
        "paper.pdf": "reference",
    }
    try:
        initialize_project(tmp_path)
    except ApprovalRequired as exc:
        assert "approval" in str(exc).lower()
    else:
        raise AssertionError("initialization must require explicit approval")
    assert not (tmp_path / ".docmesh" / "manifest.toml").exists()


def test_role_inference_uses_exact_directory_components_relative_to_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "build-42" / "site-builder"
    root.mkdir(parents=True)

    for name in ("building-blocks.md", "rebuild.md", "mirrors.md"):
        role, _, generated_from = infer_role(root / name, "markdown", root=root)
        assert role == "editable"
        assert generated_from is None

    for directory in ("generated", "mirror", "derived", "build", "_gen"):
        role, _, generated_from = infer_role(
            root / directory / "output.md", "markdown", root=root
        )
        assert role == "mirror"
        assert generated_from == directory


def test_discovery_summary_bounds_lists_and_counts_by_role_format_and_reason(
    tmp_path: Path,
) -> None:
    (tmp_path / "guide.md").write_text("# Guide\nA source.", encoding="utf-8")
    (tmp_path / "paper.pdf").write_bytes(b"not a pdf")
    (tmp_path / "assets").mkdir()
    for index in range(12):
        (tmp_path / "assets" / f"image-{index}.png").write_bytes(b"\x89PNG")
    report = discover_corpus(tmp_path)

    summary = report.summary_dict(included_samples=1, excluded_samples=4)
    assert summary["summary"]["included"]["total"] == 2
    assert summary["summary"]["included"]["by_role"] == {
        "editable": 1,
        "reference": 1,
    }
    assert summary["summary"]["included"]["by_format"] == {
        "markdown": 1,
        "pdf": 1,
    }
    assert summary["summary"]["included"]["includes_all"] is False
    assert len(summary["included"]) == 1
    assert summary["summary"]["excluded"]["total"] == 12
    assert summary["summary"]["excluded"]["by_reason"] == {
        "unsupported format (V1 supports Markdown/MDX/LaTeX/BibTeX/text/PDF)": 12
    }
    assert len(summary["excluded"]) == 4
    assert summary["summary"]["excluded"]["includes_all"] is False
    assert summary["estimated_documents"] == 2
    assert summary["root"] == str(tmp_path.resolve())

    full = report.to_dict()
    assert len(full["excluded"]) == 12
    assert len(full["included"]) == 2


def test_setup_and_init_default_to_bounded_summary_reports(tmp_path: Path) -> None:
    (tmp_path / "guide.md").write_text("# Guide\nA source.", encoding="utf-8")
    for index in range(220):
        (tmp_path / f"image-{index}.png").write_bytes(b"\x89PNG")

    dry_report = api.setup(tmp_path, dry_run=True)
    assert "summary" in dry_report
    assert dry_report["summary"]["included"]["total"] == 1
    assert dry_report["summary"]["excluded"]["total"] == 220
    assert len(dry_report["excluded"]) < 220
    assert len(dry_report["excluded"]) == dry_report["summary"]["excluded"]["sampled"]
    assert dry_report["summary"]["excluded"]["includes_all"] is False

    detailed = api.setup(tmp_path, dry_run=True, summary=False)
    assert "summary" not in detailed
    assert len(detailed["excluded"]) == 220
    assert len(detailed["included"]) == 1

    bounded_both = api.setup(
        tmp_path, dry_run=True, included_samples=0, excluded_samples=1
    )
    assert bounded_both["included"] == []
    assert len(bounded_both["excluded"]) == 1
