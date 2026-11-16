from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4


class ArtifactStore:
    """Immutable content-addressed JSON. Atomic rename precedes checkpoint commit."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, value) -> dict:
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        target = self.root / digest[:2] / (digest + ".json")
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix("." + uuid4().hex + ".tmp")
        try:
            with tmp.open("xb") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, target)
        finally:
            tmp.unlink(missing_ok=True)
        return {"uri": target.relative_to(self.root).as_posix(), "hash": digest}

    def get(self, ref):
        path = (self.root / ref["uri"]).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("Artifact path escapes configured root")
        payload = path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != ref["hash"]:
            raise ValueError("Artifact checksum mismatch")
        return json.loads(payload)
