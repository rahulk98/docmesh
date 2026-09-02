from pathlib import Path

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
