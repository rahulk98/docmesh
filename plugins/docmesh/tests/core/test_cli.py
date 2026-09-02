from docmesh import cli


def test_index_cli_forwards_force(monkeypatch) -> None:
    received = {}

    def _index(**kwargs):
        received.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(cli.api, "index", _index)
    args = cli._parser().parse_args(
        ["index", "--project-root", "/tmp/project", "--force", "--deterministic"]
    )

    assert cli.execute(args) == {"ok": True}
    assert received["project_root"] == "/tmp/project"
    assert received["force"] is True
    assert received["deterministic"] is True
