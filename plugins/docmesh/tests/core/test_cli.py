import io
import json
import sys

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


def test_impact_finish_cli_stdin_forwards_decisions(monkeypatch, capsys) -> None:
    received = {}

    def _finish(**kwargs):
        received.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(cli.api, "impact_finish", _finish)
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "project_root": "/tmp/project",
                    "run_id": "run-1",
                    "decisions": {"candidate-1": "consistent"},
                }
            )
            + "\n"
        ),
    )

    assert cli.main(["impact-finish"]) == 0
    assert received["project_root"] == "/tmp/project"
    assert received["run_id"] == "run-1"
    assert received["decisions"] == {"candidate-1": "consistent"}
    assert json.loads(capsys.readouterr().out)["ok"] is True
