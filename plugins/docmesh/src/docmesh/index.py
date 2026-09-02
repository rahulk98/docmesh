"""SQLite FTS5/vector storage and incremental corpus indexing."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, TypedDict

from .chunking import Chunker, compute_embedding_strategy_id
from .config import (
    canonical_path,
    discover_corpus,
    infer_role,
    is_model_ready,
    load_manifest,
    model_cache_dir,
    source_format,
)
from .embeddings import (
    EmbeddingBackend,
    FastEmbedBackend,
    blob_to_vector,
    cosine_similarity,
    embed_passages,
    vector_to_blob,
)
from .models import Chunk, DiscoveryItem, IndexStatus, Manifest, ModelNotInstalledError
from .parsing import ParsedDocument, parse_file

SCHEMA_VERSION = "1"
FTS_SYNC_VERSION = "1"

# Embedding is batched so onnxruntime never materializes one giant tensor:
# a batch of N passages needs N x seq_len x hidden_dim x 2 bytes of
# activations (5351 chunks -> ~4GB as a single call). 64 keeps the peak
# around ~50MB regardless of corpus or document size.
EMBED_BATCH_SIZE = 64


class ChangedFiles(TypedDict):
    changed: list[str]
    added: list[str]
    deleted: list[str]
    current: dict[str, str]


class SQLiteIndex:
    """A small transactional store; sqlite-vec is used when available.

    The vector table has a portable BLOB representation even when the optional
    sqlite-vec extension is absent.  This keeps offline retrieval functional;
    deployments with the extension can inspect ``sqlite_vec_available`` and
    replace the fallback query without changing public result semantics.
    """

    def __init__(
        self, path: str = ":memory:", *, connection: sqlite3.Connection | None = None
    ) -> None:
        self.path = path
        self.conn = connection or sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.sqlite_vec_available = False
        self._sqlite_vec_module: object | None = None
        self._vec_table_name: str | None = None
        self._vec_dimensions: int | None = None
        self._load_sqlite_vec()
        self._ensure_schema()

    def _load_sqlite_vec(self) -> None:
        try:
            import sqlite_vec  # type: ignore

            load = getattr(sqlite_vec, "load", None)
            if callable(load):
                enable_extensions = getattr(self.conn, "enable_load_extension", None)
                if callable(enable_extensions):
                    enable_extensions(True)
                try:
                    load(self.conn)
                finally:
                    if callable(enable_extensions):
                        enable_extensions(False)
                self._sqlite_vec_module = sqlite_vec
                self.sqlite_vec_available = True
        except (
            ImportError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
            sqlite3.Error,
        ):
            # sqlite-vec is an optional acceleration, never a reason to make
            # an already-indexed corpus unavailable offline.
            self.sqlite_vec_available = False

    def ensure_vector_table(self, dimensions: int) -> bool:
        """Create the sqlite-vec mirror for one embedding dimension.

        The ordinary ``vectors`` table is always retained as a portable
        fallback.  A strategy/dimension change drops and recreates the vec0
        mirror so stale rows cannot participate in a later query.
        """

        dimensions = int(dimensions)
        if not self.sqlite_vec_available or dimensions <= 0:
            return False
        if self._vec_table_name == "docmesh_vec" and self._vec_dimensions == dimensions:
            return True
        table_name = "docmesh_vec"
        try:
            existing = self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,),
            ).fetchone()
            stored_dimensions = self.get_metadata("sqlite_vec_dimensions")
            if existing is not None and stored_dimensions == str(dimensions):
                self._vec_table_name = table_name
                self._vec_dimensions = dimensions
            else:
                if existing is not None:
                    self.conn.execute("DROP TABLE IF EXISTS docmesh_vec")
                try:
                    self.conn.execute(
                        f"CREATE VIRTUAL TABLE docmesh_vec USING vec0(embedding float[{dimensions}] distance_metric=cosine)"
                    )
                except sqlite3.DatabaseError:
                    # Older sqlite-vec releases use the default distance metric
                    # and reject the optional declaration.
                    self.conn.execute(
                        f"CREATE VIRTUAL TABLE docmesh_vec USING vec0(embedding float[{dimensions}])"
                    )
                self.conn.execute(
                    "INSERT INTO metadata(key,value) VALUES('sqlite_vec_dimensions',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(dimensions),),
                )
                self.conn.commit()
                self._vec_table_name = table_name
                self._vec_dimensions = dimensions
            # A vec0 table may be created after a previous offline/BLOB-only
            # index. Rehydrate it from the canonical portable rows so a newly
            # available extension never returns an empty vector channel.  A
            # reopened connection can reuse a synchronized table without
            # rewriting every vector.  Compare id sets only; materializing
            # every vector BLOB at startup would spike memory on large
            # corpora, and vec0 stays incrementally synchronized in practice.
            portable_dim = self.conn.execute(
                "SELECT dimensions FROM vectors LIMIT 1"
            ).fetchone()
            if portable_dim is not None and int(portable_dim[0]) != dimensions:
                # The owning Indexer will clear/rebuild portable vectors when
                # its strategy changed. Keep the correctly dimensioned vec0
                # table ready for those fresh rows instead of disabling it.
                self.conn.execute("DELETE FROM docmesh_vec")
                self.conn.commit()
                return True
            portable_ids = {
                int(row[0])
                for row in self.conn.execute("SELECT chunk_id FROM vectors")
            }
            vec_ids = {
                int(row[0])
                for row in self.conn.execute("SELECT rowid FROM docmesh_vec")
            }
            if vec_ids == portable_ids:
                return True
            with self.conn:
                self.conn.execute("DELETE FROM docmesh_vec")
                for row in self.conn.execute(
                    "SELECT chunk_id, dimensions, vector FROM vectors"
                ):
                    self._insert_vec(
                        int(row["chunk_id"]),
                        blob_to_vector(row["vector"], row["dimensions"]),
                    )
            return self._vec_table_name is not None
        except sqlite3.DatabaseError:
            # Keep the extension load fact for diagnostics, but use the
            # portable vector table if vec0 cannot be created by this build.
            self._vec_table_name = None
            self._vec_dimensions = None
            return False

    def _serialize_sqlite_vec(self, vector: Sequence[float]) -> bytes:
        serializer = getattr(self._sqlite_vec_module, "serialize_float32", None)
        if callable(serializer):
            return bytes(serializer([float(value) for value in vector]))
        return vector_to_blob(vector)

    def _delete_vec_ids(self, chunk_ids: Sequence[int]) -> None:
        if not self.sqlite_vec_available or not self._vec_table_name or not chunk_ids:
            return
        placeholders = ",".join("?" for _ in chunk_ids)
        try:
            self.conn.execute(
                "DELETE FROM docmesh_vec WHERE rowid IN (" + placeholders + ")",
                tuple(int(item) for item in chunk_ids),
            )
        except sqlite3.DatabaseError:
            self._vec_table_name = None

    def _insert_vec(self, chunk_id: int, vector: Sequence[float]) -> None:
        if not self.sqlite_vec_available or not self._vec_table_name:
            return
        try:
            self.conn.execute(
                "INSERT OR REPLACE INTO docmesh_vec(rowid,embedding) VALUES(?,?)",
                (int(chunk_id), self._serialize_sqlite_vec(vector)),
            )
        except sqlite3.DatabaseError:
            self._vec_table_name = None

    def _ensure_schema(self) -> None:
        self.conn.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS documents (
                path TEXT PRIMARY KEY,
                role TEXT NOT NULL,
                format TEXT NOT NULL,
                content TEXT NOT NULL,
                file_hash TEXT NOT NULL,
                indexed_at REAL NOT NULL,
                active INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_path TEXT NOT NULL REFERENCES documents(path) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL,
                breadcrumb TEXT NOT NULL,
                text TEXT NOT NULL,
                start_line INTEGER NOT NULL,
                end_line INTEGER NOT NULL,
                page INTEGER,
                token_count INTEGER NOT NULL,
                embedding_input TEXT NOT NULL,
                text_hash TEXT NOT NULL,
                embedding_strategy_id TEXT NOT NULL,
                UNIQUE(document_path, ordinal)
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                breadcrumb, text, content='chunks', content_rowid='id'
            );
            CREATE TABLE IF NOT EXISTS vectors (
                chunk_id INTEGER PRIMARY KEY REFERENCES chunks(id) ON DELETE CASCADE,
                dimensions INTEGER NOT NULL,
                vector BLOB NOT NULL
            );
            CREATE TABLE IF NOT EXISTS impact_runs (
                run_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS baselines (
                run_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL
            );
            """
        )
        self.set_metadata_default("schema_version", SCHEMA_VERSION)
        self.set_metadata_default("edit_generation", "0")
        self.set_metadata_default("corpus_revision", "")
        # Contentless FTS tables do not automatically maintain themselves.
        # Rebuild once for databases created by older V1 versions, then rely
        # on the transactional updates in replace/remove_document.  This
        # avoids an O(number-of-chunks) rebuild on every new connection.
        if self.get_metadata("fts_sync_version") != FTS_SYNC_VERSION:
            try:
                self.conn.execute(
                    "INSERT INTO chunks_fts(chunks_fts) VALUES ('rebuild')"
                )
            except sqlite3.DatabaseError:
                pass
            else:
                self.conn.execute(
                    "INSERT INTO metadata(key,value) VALUES('fts_sync_version',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (FTS_SYNC_VERSION,),
                )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def set_metadata(self, key: str, value: Any) -> None:
        if not isinstance(value, str):
            value = json.dumps(value, sort_keys=True)
        self.conn.execute(
            "INSERT INTO metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.conn.commit()

    def set_metadata_default(self, key: str, value: Any) -> None:
        if self.get_metadata(key) is None:
            if not isinstance(value, str):
                value = json.dumps(value, sort_keys=True)
            self.conn.execute(
                "INSERT INTO metadata(key,value) VALUES(?,?)", (key, value)
            )

    def get_metadata(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute(
            "SELECT value FROM metadata WHERE key=?", (key,)
        ).fetchone()
        return default if row is None else row[0]

    def _mark_fts_synchronized(self) -> None:
        self.conn.execute(
            "INSERT INTO metadata(key,value) VALUES('fts_sync_version',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (FTS_SYNC_VERSION,),
        )

    @property
    def edit_generation(self) -> int:
        return int(self.get_metadata("edit_generation", "0"))

    @property
    def corpus_revision(self) -> str:
        return str(self.get_metadata("corpus_revision", ""))

    def set_edit_generation(self, value: int) -> None:
        self.set_metadata("edit_generation", str(int(value)))

    def set_corpus_revision(self, value: str) -> None:
        self.set_metadata("corpus_revision", value)

    def document(self, path: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM documents WHERE path=? AND active=1", (canonical_path(path),)
        ).fetchone()

    def documents(self) -> list[sqlite3.Row]:
        return list(
            self.conn.execute("SELECT * FROM documents WHERE active=1 ORDER BY path")
        )

    def document_paths(self) -> set[str]:
        """Paths only; ``documents()`` pulls every content column into memory."""
        return {
            str(row[0])
            for row in self.conn.execute(
                "SELECT path FROM documents WHERE active=1"
            )
        }

    def document_meta(self) -> list[sqlite3.Row]:
        """Lightweight rows for hash comparisons without full document text."""
        return list(
            self.conn.execute(
                "SELECT path, role, format, file_hash FROM documents WHERE active=1 ORDER BY path"
            )
        )

    def count_documents(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM documents WHERE active=1"
        ).fetchone()
        return 0 if row is None else int(row[0])

    def count_chunks(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) FROM chunks").fetchone()
        return 0 if row is None else int(row[0])

    def chunks(self, path: str | None = None) -> list[sqlite3.Row]:
        if path is None:
            return list(
                self.conn.execute(
                    "SELECT * FROM chunks ORDER BY document_path, ordinal"
                )
            )
        return list(
            self.conn.execute(
                "SELECT * FROM chunks WHERE document_path=? ORDER BY ordinal",
                (canonical_path(path),),
            )
        )

    def chunk(self, chunk_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT c.*, d.role, d.format, d.file_hash FROM chunks c JOIN documents d ON d.path=c.document_path WHERE c.id=?",
            (int(chunk_id),),
        ).fetchone()

    def replace_document(
        self,
        parsed: ParsedDocument,
        chunks: Sequence[Chunk],
        vectors: Sequence[Sequence[float]],
    ) -> None:
        if len(chunks) != len(vectors):
            raise ValueError("one vector is required for every chunk")
        path = canonical_path(parsed.path)
        with self.conn:
            old_ids = [
                int(row["id"])
                for row in self.conn.execute(
                    "SELECT id FROM chunks WHERE document_path=?", (path,)
                )
            ]
            self._delete_vec_ids(old_ids)
            self.conn.execute(
                "DELETE FROM chunks_fts WHERE rowid IN (SELECT id FROM chunks WHERE document_path=?)",
                (path,),
            )
            self.conn.execute("DELETE FROM chunks WHERE document_path=?", (path,))
            self.conn.execute(
                "INSERT INTO documents(path,role,format,content,file_hash,indexed_at,active) VALUES(?,?,?,?,?,?,1) "
                "ON CONFLICT(path) DO UPDATE SET role=excluded.role, format=excluded.format, content=excluded.content, file_hash=excluded.file_hash, indexed_at=excluded.indexed_at, active=1",
                (
                    path,
                    getattr(parsed, "role", "editable"),
                    parsed.format,
                    parsed.text,
                    parsed.file_hash,
                    time.time(),
                ),
            )
            for chunk, vector in zip(chunks, vectors):
                chunk.document_path = path
                cursor = self.conn.execute(
                    "INSERT INTO chunks(document_path,ordinal,breadcrumb,text,start_line,end_line,page,token_count,embedding_input,text_hash,embedding_strategy_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        path,
                        chunk.ordinal,
                        chunk.breadcrumb,
                        chunk.text,
                        chunk.start_line,
                        chunk.end_line,
                        chunk.page,
                        chunk.token_count,
                        chunk.embedding_input,
                        chunk.text_hash,
                        chunk.embedding_strategy_id,
                    ),
                )
                if cursor.lastrowid is None:
                    raise sqlite3.DatabaseError(
                        "SQLite did not return an inserted chunk id"
                    )
                chunk_id = int(cursor.lastrowid)
                chunk.chunk_id = chunk_id
                self.conn.execute(
                    "INSERT INTO chunks_fts(rowid,breadcrumb,text) VALUES(?,?,?)",
                    (chunk_id, chunk.breadcrumb, chunk.text),
                )
                if isinstance(vector, bytes):
                    blob = vector
                    values = blob_to_vector(blob)
                else:
                    values = list(vector)
                    blob = vector_to_blob(values)
                self.conn.execute(
                    "INSERT INTO vectors(chunk_id,dimensions,vector) VALUES(?,?,?)",
                    (chunk_id, len(values), blob),
                )
                self._insert_vec(chunk_id, values)
            self._mark_fts_synchronized()

    def remove_document(self, path: str) -> None:
        path = canonical_path(path)
        with self.conn:
            old_ids = [
                int(row["id"])
                for row in self.conn.execute(
                    "SELECT id FROM chunks WHERE document_path=?", (path,)
                )
            ]
            self._delete_vec_ids(old_ids)
            self.conn.execute(
                "DELETE FROM chunks_fts WHERE rowid IN (SELECT id FROM chunks WHERE document_path=?)",
                (path,),
            )
            self.conn.execute("DELETE FROM documents WHERE path=?", (path,))
            self._mark_fts_synchronized()

    def clear_vectors(self) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM vectors")
            if self.sqlite_vec_available and self._vec_table_name:
                try:
                    self.conn.execute("DELETE FROM docmesh_vec")
                except sqlite3.DatabaseError:
                    self._vec_table_name = None

    def lexical_search(self, query: str, limit: int = 200) -> list[sqlite3.Row]:
        query = str(query).strip()
        if not query:
            return []
        # Quoted tokens avoid FTS syntax injection while retaining AND/OR free
        # natural-language matching.  FTS5 bm25 is the authoritative lexical
        # score; callers convert its lower-is-better value to a rank score.
        tokens = [token for token in query.replace('"', " ").split() if token]
        fts_query = " OR ".join(
            '"{}"'.format(token.replace('"', " ")) for token in tokens
        )
        try:
            return list(
                self.conn.execute(
                    "SELECT c.*, d.role, d.format, d.file_hash, bm25(chunks_fts) AS bm25_score "
                    "FROM chunks_fts JOIN chunks c ON c.id=chunks_fts.rowid JOIN documents d ON d.path=c.document_path "
                    "WHERE chunks_fts MATCH ? ORDER BY bm25_score LIMIT ?",
                    (fts_query, int(limit)),
                )
            )
        except sqlite3.DatabaseError:
            # A LIKE fallback is useful on Python builds lacking FTS5 and is
            # still deterministic, though normal V1 builds use FTS5.
            needle = "%" + query.lower() + "%"
            return list(
                self.conn.execute(
                    "SELECT c.*, d.role, d.format, d.file_hash, 0.0 AS bm25_score FROM chunks c JOIN documents d ON d.path=c.document_path WHERE lower(c.text) LIKE ? OR lower(c.breadcrumb) LIKE ? ORDER BY c.id LIMIT ?",
                    (needle, needle, int(limit)),
                )
            )

    def _vector_search_portable(
        self, vector: Sequence[float], limit: int = 200
    ) -> list[tuple[sqlite3.Row, float]]:
        values: list[tuple[sqlite3.Row, float]] = []
        rows = self.conn.execute(
            "SELECT c.*, d.role, d.format, d.file_hash, v.dimensions, v.vector FROM vectors v JOIN chunks c ON c.id=v.chunk_id JOIN documents d ON d.path=c.document_path"
        )
        for row in rows:
            score = cosine_similarity(
                vector, blob_to_vector(row["vector"], row["dimensions"])
            )
            values.append((row, score))
        values.sort(
            key=lambda item: (-item[1], item[0]["document_path"], item[0]["ordinal"])
        )
        return values[: int(limit)]

    def _vector_search_sqlite_vec(
        self, vector: Sequence[float], limit: int = 200
    ) -> list[tuple[sqlite3.Row, float]]:
        """Query the loaded vec0 table using sqlite-vec's KNN interface."""

        if not self.sqlite_vec_available or not self._vec_table_name:
            raise sqlite3.DatabaseError("sqlite-vec table is unavailable")
        rows = self.conn.execute(
            "SELECT rowid, distance FROM docmesh_vec WHERE embedding MATCH ? AND k = ?",
            (self._serialize_sqlite_vec(vector), int(limit)),
        ).fetchall()
        values: list[tuple[sqlite3.Row, float]] = []
        for row in rows:
            chunk = self.chunk(int(row["rowid"]))
            if chunk is None:
                continue
            distance = float(row["distance"])
            values.append((chunk, 1.0 - distance))
        values.sort(
            key=lambda item: (-item[1], item[0]["document_path"], item[0]["ordinal"])
        )
        return values[: int(limit)]

    def vector_search(
        self, vector: Sequence[float], limit: int = 200
    ) -> list[tuple[sqlite3.Row, float]]:
        if self.sqlite_vec_available and self._vec_table_name:
            try:
                return self._vector_search_sqlite_vec(vector, limit)
            except (sqlite3.DatabaseError, ValueError, TypeError):
                # A broken optional extension must not make an indexed corpus
                # unusable.  Fall back to the synchronized portable mirror.
                pass
        return self._vector_search_portable(vector, limit)

    def save_run(self, run_id: str, payload: Mapping[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO impact_runs(run_id,payload) VALUES(?,?) ON CONFLICT(run_id) DO UPDATE SET payload=excluded.payload",
            (run_id, json.dumps(payload, sort_keys=True)),
        )
        self.conn.commit()

    def load_run(self, run_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT payload FROM impact_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        return None if row is None else json.loads(row[0])

    def save_baseline(self, run_id: str, payload: Mapping[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO baselines(run_id,payload) VALUES(?,?) ON CONFLICT(run_id) DO UPDATE SET payload=excluded.payload",
            (run_id, json.dumps(payload, sort_keys=True)),
        )
        self.conn.commit()

    def load_baseline(self, run_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT payload FROM baselines WHERE run_id=?", (run_id,)
        ).fetchone()
        return None if row is None else json.loads(row[0])


class Indexer:
    """Incrementally parse, chunk, embed, and persist a configured corpus."""

    def __init__(
        self,
        root: str | Path = ".",
        *,
        manifest: Manifest | None = None,
        db_path: str | None = None,
        index: SQLiteIndex | None = None,
        embedder: EmbeddingBackend | None = None,
        load_model: bool = True,
        local_files_only: bool = True,
        cache_dir: str | Path | None = None,
    ) -> None:
        self.manifest = manifest or load_manifest(root)
        self.root = Path(self.manifest.root)
        self.model_cache_dir = (
            Path(cache_dir).expanduser().resolve(strict=False)
            if cache_dir
            else model_cache_dir(self.root)
        )
        default_db = self.root / ".docmesh" / "index.sqlite3"
        if index is not None:
            self.store = index
        else:
            if db_path is None:
                default_db.parent.mkdir(parents=True, exist_ok=True)
                db_path = str(default_db)
            self.store = SQLiteIndex(db_path)
        supplied_embedder = embedder is not None
        if embedder is not None:
            self.embedder: EmbeddingBackend | None = embedder
        elif load_model:
            # Production indexing always uses the configured FastEmbed model.
            # ``local_files_only`` remains true for all normal query/index
            # paths; explicit approved setup is the only downloader.
            self.embedder = FastEmbedBackend(
                self.manifest.model,
                cache_dir=str(self.model_cache_dir),
                local_files_only=local_files_only,
            )
        else:
            # status/doctor intentionally inspect only persisted metadata and
            # model markers; constructing this backend could load/download.
            self.embedder = None
        backend_model = getattr(self.embedder, "model", self.manifest.model)
        backend_dimensions = getattr(
            self.embedder, "dimensions", self.manifest.dimensions
        )
        backend_tokenizer = getattr(
            self.embedder, "tokenizer_name", self.manifest.tokenizer
        )
        previous_strategy = self.store.get_metadata("embedding_strategy_id")
        read_only = self.embedder is None and not load_model
        if read_only and previous_strategy not in (None, ""):
            # A metadata-only status/doctor instance cannot know the model
            # dimensions without loading it. Preserve the persisted strategy
            # and, importantly, do not force a rebuild merely by inspecting it.
            self.strategy_id = str(previous_strategy)
            self.strategy_changed = False
        else:
            self.strategy_id = compute_embedding_strategy_id(
                model=backend_model,
                dimensions=backend_dimensions,
                tokenizer=(
                    backend_tokenizer if supplied_embedder else self.manifest.tokenizer
                ),
                passage_method=getattr(
                    self.embedder, "passage_method", "passage_embed"
                ),
                query_method=getattr(self.embedder, "query_method", "query_embed"),
                max_tokens=self.manifest.max_embedding_tokens,
                hard_max_tokens=self.manifest.hard_embedding_tokens,
                chunking_version=self.manifest.chunking_version,
                breadcrumb_format=self.manifest.breadcrumb_format,
                retrieval_prefix=self.manifest.retrieval_prefix,
            )
            self.strategy_changed = previous_strategy not in (
                None,
                "",
                self.strategy_id,
            )
            if not read_only:
                self.store.set_metadata("embedding_strategy_id", self.strategy_id)
        if not read_only:
            self.store.set_metadata("model", backend_model)
        if (
            self.embedder is not None
            and isinstance(backend_dimensions, int)
            and backend_dimensions > 0
        ):
            self.store.ensure_vector_table(backend_dimensions)

    @property
    def edit_generation(self) -> int:
        return self.store.edit_generation

    @property
    def corpus_revision(self) -> str:
        return self.store.corpus_revision

    @property
    def sqlite_vec_available(self) -> bool:
        return self.store.sqlite_vec_available

    def _chunker(self) -> Chunker:
        if self.embedder is None:
            raise ModelNotInstalledError(
                "FastEmbed model is not loaded; status/doctor are read-only"
            )
        return Chunker(
            self.manifest.max_embedding_tokens,
            self.manifest.hard_embedding_tokens,
            self.embedder,
            self.manifest.breadcrumb_format,
            self.strategy_id,
            self.manifest.retrieval_prefix,
        )

    def _configured_item(
        self, path: str, report: Sequence[DiscoveryItem] | None = None
    ) -> DiscoveryItem:
        path = canonical_path(path, base=self.root)
        if report:
            for item in report:
                if canonical_path(item.path) == path:
                    return item
        for source_config in self.manifest.sources:
            if canonical_path(source_config.path, base=self.root) == path:
                fmt = source_format(path) or "text"
                return DiscoveryItem(
                    path,
                    source_config.role,
                    fmt,
                    "role assigned by manifest",
                    source_config.generated_from,
                )
        fmt = source_format(path) or "text"
        role, reason, generated_from = infer_role(path, fmt, root=self.root)
        return DiscoveryItem(path, role, fmt, reason, generated_from)

    def _parse_for_index(
        self, item: DiscoveryItem, *, data: bytes | None = None
    ) -> ParsedDocument:
        parsed = parse_file(item.path, data=data)
        # ParsedDocument remains format focused; role is attached dynamically to
        # keep it compatible with parsers used independently by callers.
        parsed.role = item.role  # type: ignore[attr-defined]
        return parsed

    def _embed_chunk_batches(self, chunks: Sequence[Chunk]) -> list[bytes]:
        """Embed chunks in bounded batches, returning serialized blobs.

        A single ``embed_passages`` call over every chunk of a document makes
        onnxruntime build one padded tensor of N x token_budget activations
        (hours-of-thesis corpora held ~6-7GB that way).  Batching keeps the
        transient tensor ~50MB and vectors are stored as 1.5KB BLOBs instead
        of 12KB Python float lists.
        """
        blobs: list[bytes] = []
        for start in range(0, len(chunks), EMBED_BATCH_SIZE):
            batch = chunks[start : start + EMBED_BATCH_SIZE]
            vectors = embed_passages(
                self.embedder, [chunk.embedding_input for chunk in batch]
            )
            if len(vectors) != len(batch):
                raise ValueError(
                    "embedding backend must return one vector per chunk"
                )
            blobs.extend(vector_to_blob(list(embedding)) for embedding in vectors)
        return blobs

    def _current_discovery(self) -> list[DiscoveryItem]:
        report = discover_corpus(
            self.root,
            include=self.manifest.include,
            exclude=self.manifest.exclude,
            configured_sources=self.manifest.sources,
        )
        return report.included

    def _refresh_vectors_if_strategy_changed(self) -> bool:
        if not self.strategy_changed:
            return False
        if self.embedder is None:
            raise ModelNotInstalledError(
                "FastEmbed model is not loaded; cannot rebuild vectors"
            )
        # One transaction so a crash mid-refresh cannot leave the vector
        # tables partially rewritten while the strategy metadata already
        # names the new one.  Chunks stream in bounded batches instead of
        # loading every embedding input (and rebuilding every vector) at once.
        with self.store.conn:
            self.store.conn.execute("DELETE FROM vectors")
            if self.store.sqlite_vec_available and self.store._vec_table_name:
                try:
                    self.store.conn.execute("DELETE FROM docmesh_vec")
                except sqlite3.DatabaseError:
                    self.store._vec_table_name = None
            cursor = self.store.conn.execute(
                "SELECT id, embedding_input FROM chunks ORDER BY id"
            )
            inserted = 0
            while True:
                batch = cursor.fetchmany(EMBED_BATCH_SIZE)
                if not batch:
                    break
                vectors = embed_passages(
                    self.embedder,
                    [row["embedding_input"] for row in batch],
                )
                if len(vectors) != len(batch):
                    raise ValueError(
                        "embedding backend must return one vector per chunk"
                    )
                for row, vector in zip(batch, vectors):
                    values = list(vector)
                    self.store.conn.execute(
                        "INSERT INTO vectors(chunk_id,dimensions,vector) VALUES(?,?,?)",
                        (row["id"], len(values), vector_to_blob(values)),
                    )
                    self.store._insert_vec(int(row["id"]), values)
                inserted += len(batch)
        self.strategy_changed = False
        return bool(inserted)

    def index(
        self, paths: Sequence[str | Path] | None = None, *, force: bool = False
    ) -> IndexStatus:
        if self.embedder is None:
            raise ModelNotInstalledError(
                "FastEmbed model is not loaded; status/doctor are read-only"
            )
        changed = self._refresh_vectors_if_strategy_changed()
        full_scan = paths is None
        discovery = self._current_discovery() if full_scan else None
        items: list[DiscoveryItem]
        if discovery is not None:
            items = list(discovery)
        else:
            items = [self._configured_item(str(path)) for path in paths or ()]
        seen = set()
        chunker = self._chunker()
        for item in items:
            path = canonical_path(item.path)
            seen.add(path)
            if not Path(path).exists():
                if self.store.document(path):
                    self.store.remove_document(path)
                    changed = True
                continue
            raw = Path(path).read_bytes()
            raw_hash = hashlib.sha256(raw).hexdigest()
            existing = self.store.document(path)
            if (
                existing is not None
                and existing["file_hash"] == raw_hash
                and existing["role"] == item.role
                and existing["format"] == item.format
                and not force
            ):
                continue
            parsed = self._parse_for_index(item, data=raw)
            chunks = chunker.chunk_document(
                parsed.path, parsed.sections, text_hash=raw_hash
            )
            vectors = self._embed_chunk_batches(chunks)
            self.store.replace_document(parsed, chunks, vectors)
            changed = True
        if full_scan:
            existing_paths = self.store.document_paths()
            for path in sorted(existing_paths - seen):
                self.store.remove_document(path)
                changed = True
        if changed:
            generation = self.store.edit_generation + 1
            self.store.set_edit_generation(generation)
            self.store.set_corpus_revision(self._compute_indexed_revision())
        elif not self.store.corpus_revision:
            self.store.set_corpus_revision(self._compute_indexed_revision())
        return self.status()

    def reindex_path(self, path: str | Path) -> IndexStatus:
        return self.index(paths=[path], force=True)

    def _compute_indexed_revision(self) -> str:
        digest = hashlib.sha256()
        for row in self.store.document_meta():
            digest.update(row["path"].encode("utf-8"))
            digest.update(b"\0")
            digest.update(row["file_hash"].encode("ascii"))
            digest.update(b"\0")
            digest.update(row["role"].encode("utf-8"))
        return digest.hexdigest()

    def current_file_hashes(self) -> dict[str, str]:
        paths = self.store.document_paths()
        try:
            paths.update(item.path for item in self._current_discovery())
        except (FileNotFoundError, NotADirectoryError):
            pass
        values: dict[str, str] = {}
        for path in sorted(paths):
            path_obj = Path(path)
            if path_obj.exists() and path_obj.is_file():
                values[canonical_path(path)] = hashlib.sha256(
                    path_obj.read_bytes()
                ).hexdigest()
        return values

    def current_corpus_revision(self) -> str:
        digest = hashlib.sha256()
        for path, file_hash in sorted(self.current_file_hashes().items()):
            digest.update(path.encode("utf-8"))
            digest.update(b"\0")
            digest.update(file_hash.encode("ascii"))
        return digest.hexdigest()

    def changed_files_since(self, baseline_hashes: Mapping[str, str]) -> ChangedFiles:
        current = self.current_file_hashes()
        old_paths = set(baseline_hashes)
        new_paths = set(current)
        changed = sorted(
            path
            for path in old_paths & new_paths
            if baseline_hashes[path] != current[path]
        )
        added = sorted(new_paths - old_paths)
        deleted = sorted(old_paths - new_paths)
        return {
            "changed": changed,
            "added": added,
            "deleted": deleted,
            "current": current,
        }

    def status(self) -> IndexStatus:
        stale: list[str] = []
        hashes = self.current_file_hashes()
        for row in self.store.document_meta():
            if hashes.get(row["path"]) != row["file_hash"]:
                stale.append(row["path"])
        backend_ready = self.embedder is not None and hasattr(
            self.embedder, "local_files_only"
        )
        return IndexStatus(
            documents=self.store.count_documents(),
            chunks=self.store.count_chunks(),
            corpus_revision=self.store.corpus_revision,
            edit_generation=self.store.edit_generation,
            embedding_strategy_id=self.strategy_id,
            sqlite_vec_available=self.sqlite_vec_available,
            stale_documents=stale,
            model=str(self.store.get_metadata("model", self.manifest.model)),
            model_ready=bool(
                backend_ready or is_model_ready(self.root, self.manifest.model)
            ),
            model_cache_dir=str(self.model_cache_dir),
        )
