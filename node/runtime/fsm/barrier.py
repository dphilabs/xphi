# xphi.node.runtime.fsm.barrier
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from xphi.arch.bound.adapter.pta import (
    NodeSigner,
    PhaseAnchorOutput,
    PtaInput,
    PtaPointer,
)
from xphi.arch.bound.adapter.state import StateAdapter
from xphi.arch.contract.fsm import (
    END_TOKEN,
    MAX_BLOB_BYTES,
    CompactKey,
    NodeType,
)


@dataclass
class DagTopology:
    entry_point: str
    back_edges: Set[Tuple[str, str]] = field(default_factory=set)
    join_nodes: Set[str] = field(default_factory=set)


class DagJoinBarrier:
    """Synchronizes parallel branches at shared DAG join nodes (forward in-degree >= 2)."""

    def __init__(self, root_blob: Dict[str, Any]):
        self.topology = self._analyze_blob(root_blob)
        self._waiting: Dict[str, List["BranchCursor"]] = {jn: [] for jn in self.topology.join_nodes}

    @staticmethod
    def _analyze_blob(blob: Dict[str, Any]) -> DagTopology:
        entry = blob.get(CompactKey.META, {}).get(CompactKey.ENTRY, "")
        graph = blob.get(CompactKey.GRAPH, {})
        normal_in_edges: Dict[str, Set[str]] = {nid: set() for nid in graph}
        out_edges: Dict[str, Set[str]] = {nid: set() for nid in graph}

        for nid, spec in graph.items():
            if spec.get(CompactKey.TYPE) == NodeType.ROUTER.value:
                normal_targets = [
                    r[CompactKey.NEXT] for r in spec.get(CompactKey.RULES, []) if CompactKey.NEXT in r
                ]
                if spec.get(CompactKey.DEFAULT_NEXT):
                    normal_targets.append(spec[CompactKey.DEFAULT_NEXT])
            else:
                nx = spec.get(CompactKey.NEXT, [])
                normal_targets = list(nx) if isinstance(nx, list) else [nx]

            for t in normal_targets:
                if t in graph:
                    out_edges[nid].add(t)
                    normal_in_edges[t].add(nid)

            fb_target = spec.get(CompactKey.FALLBACK)
            if fb_target and fb_target in graph:
                out_edges[nid].add(fb_target)

        color: Dict[str, int] = {nid: 0 for nid in graph}
        back_edges: Set[Tuple[str, str]] = set()

        def _dfs(u: str) -> None:
            color[u] = 1
            for v in out_edges.get(u, set()):
                if color.get(v, 0) == 1:
                    back_edges.add((u, v))
                elif color.get(v, 0) == 0:
                    _dfs(v)
            color[u] = 2

        if entry in color:
            _dfs(entry)

        join_nodes = {
            nid for nid, preds in normal_in_edges.items()
            if len({p for p in preds if (p, nid) not in back_edges}) >= 2
        }
        return DagTopology(entry_point=entry, back_edges=back_edges, join_nodes=join_nodes)

    def is_join_point(self, node_id: str) -> bool:
        return node_id in self.topology.join_nodes

    def arrive(self, cursor: "BranchCursor", expected_count: int) -> Tuple[bool, List["BranchCursor"]]:
        bucket = self._waiting.setdefault(cursor.curr_node, [])
        if all(c.stream_id != cursor.stream_id for c in bucket):
            bucket.append(cursor)
        if len(bucket) >= expected_count:
            ready = list(bucket)
            bucket.clear()
            return True, ready
        return False, []

    def drain_waiting(self, node_id: Optional[str] = None) -> List["BranchCursor"]:
        """Collects and clears cursors waiting at a specific join node or across all join barriers."""
        if node_id is not None:
            bucket = self._waiting.get(node_id, [])
            stranded = list(bucket)
            bucket.clear()
            return stranded

        stranded: List["BranchCursor"] = []
        for bucket in self._waiting.values():
            stranded.extend(bucket)
            bucket.clear()
        return stranded


@dataclass
class BranchCursor:
    """Active branch state handle with built-in PTA input signing and output projection."""
    stream_id: str
    pointer: PtaPointer
    active_blob_hash: str
    curr_node: str
    fuel: int
    owner_pubkey: str
    meta_ctx: Dict[str, Any] = field(default_factory=dict)
    call_stack: List[str] = field(default_factory=list)
    tick: int = 0

    @property
    def is_terminated(self) -> bool:
        return self.curr_node == END_TOKEN

    def to_signed_input(self, signer: NodeSigner) -> PtaInput:
        canonical = StateAdapter.to_canonical_bytes({"payload": self.pointer.to_key()})
        return PtaInput(
            pointer=self.pointer,
            signature=signer.sign_payload(canonical),
            owner_address=self.owner_pubkey,
        )

    def to_anchor_output(self, status: str) -> PhaseAnchorOutput:
        payload: Dict[str, Any] = {
            "blob_ref": self.active_blob_hash,
            "node": self.curr_node,
            "meta": self.meta_ctx,
            "tick": self.tick,
        }
        if self.call_stack:
            payload["stack"] = self.call_stack

        raw_bytes = StateAdapter.to_canonical_bytes(payload)
        if len(raw_bytes) > MAX_BLOB_BYTES:
            raise OverflowError(f"Anchor payload ({len(raw_bytes)}B) exceeds {MAX_BLOB_BYTES}B limit.")
        return PhaseAnchorOutput(
            amount=self.fuel,
            owner=self.owner_pubkey,
            handle_id=self.stream_id,
            phase_status=status,
            executable_payload=payload,
        )

    @classmethod
    def from_anchor_output(
        cls,
        pointer: PtaPointer,
        output: PhaseAnchorOutput,
        fallback_stream_id: str = "",
        tick: int = 1,
    ) -> "BranchCursor":
        payload = output.executable_payload
        return cls(
            stream_id=output.handle_id or fallback_stream_id,
            pointer=pointer,
            active_blob_hash=payload["blob_ref"],
            curr_node=payload["node"],
            fuel=output.amount,
            owner_pubkey=output.owner,
            meta_ctx=dict(payload.get("meta", {})),
            call_stack=list(payload.get("stack", [])),
            tick=int(payload.get("tick", tick)),
        )