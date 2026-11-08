# xphi.state.anchor.key
from dataclasses import dataclass, field
from enum import Enum
import hashlib
import time
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

from xphi.arch.bound.adapter.state import StateAdapter


class AnchorRole(Enum):
    SEALER = "SEALER"        # Acquired physical lock; can invoke WASM and seal states
    PROPOSER = "PROPOSER"    # Read-Only mode


def deterministic_hash(data: Dict[str, Any]) -> str:
    canonical_bytes = StateAdapter.to_canonical_bytes(data)
    return hashlib.sha256(canonical_bytes).hexdigest()


class LogicStream(BaseModel):
    """Ψ_open - The external request flowing into the system."""
    id: str
    action: str = "default_action"
    payload: Any
    metadata: Dict[str, Any] = Field(default_factory=dict)


@dataclass
class ToposBlob:
    """OOB Log / Residue representation from Phase transitions or System Anomalies."""
    action: str
    from_state: str
    to_state: str
    tension: float
    details: str
    timestamp: float = field(default_factory=time.time)


@dataclass
class PhaseCommit:
    """Sealed kernel snapshot verified by Phase or Multi Signature"""
    stream_id: str
    executable_payload: Any
    tension_at_seal: float
    blob_hashes: List[str]
    parent_hash: Optional[str] = None
    sealed_at: float = field(default_factory=time.time)


class SealedKernel(BaseModel):
    """Ω_knot - The mathematically invariant, executable closed boundary."""
    kernel_id: str
    stream_id: str
    executable_payload: Any
    tension_at_seal: float
    signature: str


class KeyGen:
    @staticmethod
    def stream_fuel(tenant_id: str) -> str:
        """자산(Fuel) 트랜잭션을 묶는 테넌트별 독립 체인 ID"""
        return f"fuel_ledger:{tenant_id}"

    @staticmethod
    def stream_mcp(handle_id: str) -> str:
        """특정 인텐트(MCP FSM)의 상태 전이를 묶는 체인 ID"""
        return f"mcp_bridge:{handle_id}"

    @staticmethod
    def stream_pta_fallback(tx_hash: str) -> str:
        """명시적 stream_id가 없을 때 PtaAdapter가 사용하는 폴백 체인 ID"""
        return f"pta_tx:{tx_hash[:8]}"

    # Physical RocksDB Object Keys - (주의: RocksDB에 넣을 때는 .encode('utf-8') 필수)
    @staticmethod
    def commit(commit_hash: str) -> str:
        return f"commit:{commit_hash}"

    @staticmethod
    def blob(blob_hash: str) -> str:
        return f"blob:{blob_hash}"

    @staticmethod
    def ref(stream_id: str) -> str:
        """특정 스트림(체인)의 최신 HEAD(Commit Hash)를 가리키는 포인터 키"""
        return f"ref:{stream_id}"

    # Mempool / Tunnel Channels
    @staticmethod
    def mempool_logic() -> str:
        return "ledger:mempool:logic_streams"

    @staticmethod
    def mempool_objects() -> str:
        return "ledger:mempool:objects"

    @staticmethod
    def mempool_heads() -> str:
        return "ledger:mempool:heads"

    @staticmethod
    def ack_channel(req_id: str) -> str:
        """SEALER가 PROPOSER에게 동기적 확정 영수증을 내려보내는 ACK 채널"""
        return f"ledger:ack:{req_id}"

    @staticmethod
    def pta_pointer(tx_hash: str, output_index: int) -> str:
        """PTA 미소비 객체의 전역 고유 식별자 (로컬 _unfold_pool 등에서 사용)"""
        return f"{tx_hash}:{output_index}"