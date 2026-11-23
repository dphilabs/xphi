# xphi.arch.model.content.artifact
## @lineage: fiber.node.runbook.inspect.artifact
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    sha256: str
    source_uri: str
    filename: str | None = None
    row_count: int | None = None


class ArtifactStore:
    """Content-addressed `io/{sha256}.json` artifact storage and previewer."""

    def __init__(self, io_dir: Path | None = None) -> None:
        self.io_dir = io_dir
        self._blobs: dict[str, Any] = {}
        self._refs: dict[str, ArtifactRef] = {}

    def put(
        self,
        payload: Any,
        *,
        source_uri: str = "mem://step",
        filename: str | None = None,
    ) -> ArtifactRef:
        serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]
        ref = ArtifactRef(
            sha256=digest,
            source_uri=source_uri,
            filename=filename,
            row_count=len(payload) if isinstance(payload, list) else None,
        )
        self._blobs[digest] = payload
        self._refs[digest] = ref

        if self.io_dir is not None:
            self.io_dir.mkdir(parents=True, exist_ok=True)
            (self.io_dir / f"{digest}.json").write_text(
                json.dumps({"ref": asdict(ref), "payload": payload}, indent=2, ensure_ascii=False, default=str) + "\n",
                encoding="utf-8",
            )
        return ref

    def get(self, io_sha: str) -> Any:
        if io_sha in self._blobs:
            return self._blobs[io_sha]
        if self.io_dir is not None and (blob_file := self.io_dir / f"{io_sha}.json").is_file():
            raw = json.loads(blob_file.read_text(encoding="utf-8"))
            self._blobs[io_sha] = raw.get("payload")
            if isinstance(raw_ref := raw.get("ref"), dict) and "sha256" in raw_ref:
                self._refs[io_sha] = ArtifactRef(**raw_ref)
            return self._blobs[io_sha]
        raise KeyError(f"Artifact '{io_sha}' not found in io/ store.")

    def get_ref(self, io_sha: str) -> ArtifactRef | None:
        if io_sha not in self._refs:
            self.get(io_sha)
        return self._refs.get(io_sha)

    def preview(self, io_sha: str, max_rows: int = 3) -> tuple[str, list[dict[str, Any]], int]:
        payload = self.get(io_sha)
        ref = self._refs.get(io_sha)
        uri_part = f"{ref.source_uri} (sha {io_sha[:8]})" if ref else f"sha {io_sha[:8]}"
        name_part = f" · {ref.filename}" if (ref and ref.filename) else ""

        if isinstance(payload, list):
            sample = [r if isinstance(r, dict) else {"value": r} for r in payload[:max_rows]]
            return f"{len(payload)}행 · 입력 {uri_part}{name_part}", sample, max(0, len(payload) - len(sample))
        if isinstance(payload, dict):
            return f"객체({len(payload)}키) · {uri_part}{name_part}", [payload], 0
        return f"값({str(payload)[:40]}) · {uri_part}{name_part}", [], 0