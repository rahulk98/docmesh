from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from docmesh import api
from docmesh.config import initialize_project
from docmesh.embeddings import FastEmbedBackend
from docmesh.index import Indexer, SQLiteIndex

INDEX_MODULE = importlib.import_module("docmesh.index")


class _FakeTextEmbedding:
    def __init__(self, dimensions: int = 3) -> None:
        self.dimensions = dimensions
        self.token_calls: list[str] = []

    def passage_embed(self, values: list[str]):
        return [[1.0, 0.0, 0.0] for _ in values]

    def query_embed(self, values: list[str]):
        return [[1.0, 0.0, 0.0] for _ in values]

    def token_count(self, value: str) -> int:
        self.token_calls.append(value)
        return 37


def test_fastembed_uses_model_token_count() -> None:
    model = _FakeTextEmbedding()
    backend = FastEmbedBackend(text_embedding=model)

    assert backend.count_tokens("actual tokenizer input") == 37
    assert model.token_calls == ["actual tokenizer input"]


def test_fastembed_legacy_without_token_count_uses_conservative_byte_bound() -> None:
    backend = FastEmbedBackend(text_embedding=object())
    value = "tokenization fallback: café"

    assert backend.count_tokens(value) == len(value.encode("utf-8"))


def test_indexer_defaults_to_local_only_production_fastembed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "guide.md").write_text(
        "# Guide\nA production source.", encoding="utf-8"
    )
    calls: list[dict[str, object]] = []

    class _RecordingBackend(_FakeTextEmbedding):
        def __init__(
            self,
            model: str,
            *,
            cache_dir: str | None = None,
            local_files_only: bool = True,
        ) -> None:
            super().__init__()
            calls.append(
                {
                    "model": model,
                    "cache_dir": cache_dir,
                    "local_files_only": local_files_only,
                }
            )
            self.model = model
            self.tokenizer_name = "fake-fastembed"
            self.dimensions = 3

    monkeypatch.setattr(INDEX_MODULE, "FastEmbedBackend", _RecordingBackend)
    indexer = Indexer(tmp_path, index=SQLiteIndex(":memory:"))
    assert calls == [
        {
            "model": "BAAI/bge-small-en-v1.5",
            "cache_dir": str(tmp_path / ".docmesh" / "models"),
            "local_files_only": True,
        }
    ]
    indexer.store.close()


def test_approved_setup_fetches_to_project_model_cache_and_reports_readiness(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "guide.md").write_text("# Guide\nA setup source.", encoding="utf-8")
    calls: list[dict[str, object]] = []

    class _SetupBackend(_FakeTextEmbedding):
        def __init__(
            self,
            model: str,
            *,
            cache_dir: str | None = None,
            local_files_only: bool = True,
        ) -> None:
            super().__init__()
            calls.append(
                {
                    "model": model,
                    "cache_dir": cache_dir,
                    "local_files_only": local_files_only,
                }
            )

    monkeypatch.setattr("docmesh.embeddings.FastEmbedBackend", _SetupBackend)
    report = initialize_project(tmp_path, approve=True)

    assert report.model_ready is True
    assert calls == [
        {
            "model": "BAAI/bge-small-en-v1.5",
            "cache_dir": str(tmp_path / ".docmesh" / "models"),
            "local_files_only": False,
        }
    ]
    assert report.to_dict()["model_ready"] is True


def test_status_and_doctor_do_not_load_the_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "guide.md").write_text("# Guide\nOffline status.", encoding="utf-8")

    def _must_not_load(*args: object, **kwargs: object):
        raise AssertionError("status/doctor must not construct FastEmbed")

    monkeypatch.setattr(INDEX_MODULE, "FastEmbedBackend", _must_not_load)
    assert api.status(tmp_path)["model"] == "BAAI/bge-small-en-v1.5"
    assert api.doctor(tmp_path)["model"] == "BAAI/bge-small-en-v1.5"
