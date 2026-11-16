from __future__ import annotations

import hashlib
import json
from typing import Protocol


class VersionedVectorStore(Protocol):
    collection: str
    def write(self, version: str, rows: list[dict], vectors: list[list[float]]) -> None: ...
    def contains(self, version: str, expected: int) -> bool: ...
    def delete_version(self, version: str) -> None: ...
    def search(self, text: str, vector: list[float], active: list[str], limit: int) -> list[dict]: ...


def collection_name(prefix, model, dimension):
    identity = hashlib.sha256(f"{model}|{dimension}|bm25-standard-v1".encode()).hexdigest()[:16]
    return f"{prefix}_{identity}"


class MilvusStore:
    """Immutable version namespaces; PostgreSQL controls publication visibility."""

    def __init__(self, settings, embedder, client=None):
        from pymilvus import MilvusClient
        self.client = client or MilvusClient(uri=settings.milvus_uri, token=settings.milvus_token, timeout=30)
        self.dimension = embedder.dimension
        self.collection = collection_name(settings.collection_prefix, embedder.model_name, self.dimension)

    def ensure(self):
        from pymilvus import DataType, Function, FunctionType
        if self.client.has_collection(self.collection):
            return
        schema = self.client.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field("id", DataType.VARCHAR, is_primary=True, max_length=64)
        schema.add_field("version", DataType.VARCHAR, max_length=64)
        schema.add_field("text", DataType.VARCHAR, max_length=32768, enable_analyzer=True,
                         analyzer_params={"type": "standard"})
        schema.add_field("dense", DataType.FLOAT_VECTOR, dim=self.dimension)
        schema.add_field("sparse", DataType.SPARSE_FLOAT_VECTOR)
        schema.add_field("metadata", DataType.JSON)
        schema.add_function(Function(name="text_bm25", input_field_names=["text"],
                                     output_field_names=["sparse"], function_type=FunctionType.BM25))
        indexes = self.client.prepare_index_params()
        indexes.add_index(field_name="dense", index_type="AUTOINDEX", metric_type="COSINE")
        indexes.add_index(field_name="sparse", index_type="SPARSE_INVERTED_INDEX", metric_type="BM25")
        try:
            self.client.create_collection(self.collection, schema=schema, index_params=indexes, consistency_level="Strong")
        except Exception:
            if not self.client.has_collection(self.collection):
                raise

    def write(self, version, rows, vectors):
        if not rows or len(rows) != len(vectors):
            raise ValueError("Chunk/vector count mismatch or empty document")
        data = []
        for i, (row, vector) in enumerate(zip(rows, vectors)):
            if len(vector) != self.dimension:
                raise ValueError("Embedding dimension mismatch")
            data.append({"id": hashlib.sha256(f"{version}:{i}".encode()).hexdigest(),
                         "version": version, "text": row["content"], "dense": vector,
                         "metadata": {k: v for k, v in row.items() if k != "content"}})
        for start in range(0, len(data), 64):
            self.client.upsert(self.collection, data=data[start:start+64])
        if not self.contains(version, len(data)):
            raise RuntimeError("Index write is not fully query-visible; publication deferred")

    def contains(self, version, expected):
        result = self.client.query(self.collection, filter="version == " + json.dumps(version),
                                   output_fields=["count(*)"], consistency_level="Strong")
        return bool(result) and int(result[0]["count(*)"]) == expected

    def delete_version(self, version, collection=None):
        self.client.delete(collection or self.collection, filter="version == " + json.dumps(version))

    def search(self, text, vector, active, limit):
        from pymilvus import AnnSearchRequest, RRFRanker
        if not active:
            return []
        expr = "version in " + json.dumps(active)
        requests = [AnnSearchRequest(data=[vector], anns_field="dense", param={"metric_type": "COSINE"},
                                     limit=limit, expr=expr),
                    AnnSearchRequest(data=[text], anns_field="sparse", param={"metric_type": "BM25"},
                                     limit=limit, expr=expr)]
        result = self.client.hybrid_search(self.collection, reqs=requests, ranker=RRFRanker(60), limit=limit,
                                           output_fields=["text", "metadata", "version"], consistency_level="Strong")
        return [{"id": str(hit["id"]), "text": hit["entity"]["text"],
                 "metadata": hit["entity"]["metadata"], "version": hit["entity"]["version"],
                 "rrf_score": hit["distance"]} for hit in result[0]]


class ChromaVersionStore:
    """Compatibility adapter for publication/recovery tests and transitional dense-only use."""

    def __init__(self, path, model, dimension):
        import chromadb
        self.collection = collection_name("v2_transition", model, dimension)
        self.client = chromadb.PersistentClient(path=str(path))
        self.index = self.client.get_or_create_collection(self.collection, metadata={"hnsw:space": "cosine"})

    def ensure(self):
        pass

    def write(self, version, rows, vectors):
        if not rows or len(rows) != len(vectors):
            raise ValueError("Invalid chunks/vectors")
        self.index.upsert(ids=[hashlib.sha256(f"{version}:{i}".encode()).hexdigest() for i in range(len(rows))],
                          documents=[r["content"] for r in rows], embeddings=vectors,
                          metadatas=[{"version": version, "payload": json.dumps({k: v for k, v in r.items() if k != "content"})}
                                     for r in rows])

    def contains(self, version, expected):
        return len(self.index.get(where={"version": version})["ids"]) == expected

    def delete_version(self, version, collection=None):
        if collection and collection != self.collection:
            raise ValueError("Collection does not belong to this adapter")
        self.index.delete(where={"version": version})

    def search(self, text, vector, active, limit):
        if not active or not self.index.count():
            return []
        r = self.index.query(query_embeddings=[vector], where={"version": {"$in": active}},
                             n_results=min(limit, self.index.count()))
        return [{"id": id_, "text": doc, "metadata": json.loads(meta["payload"]), "version": meta["version"],
                 "distance": dist} for id_, doc, meta, dist in zip(r["ids"][0], r["documents"][0], r["metadatas"][0], r["distances"][0])]
