# xphi.arch.contract.config.tunnel
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from xphi.node.space.bind.resolver import resolve_path

TUNNEL_ROOT = resolve_path("tunnel")
RUN_DIR = TUNNEL_ROOT / "run"
KV_DIR = TUNNEL_ROOT / "kv"

TUNNEL_SOCK_PATH = RUN_DIR / "tunnel.sock"
TUNNEL_PID_PATH = RUN_DIR / "tunnel.pid"
DEFAULT_TUNNEL_URL = f"unix://{TUNNEL_SOCK_PATH}"

class BackendProtocol(str, Enum):
    REDIS = "redis"
    KAFKA = "kafka"


@dataclass(frozen=True)
class TunnelConfig:
    default_url: str = DEFAULT_TUNNEL_URL


def resolve_default_config() -> TunnelConfig:
    return TunnelConfig(default_url=DEFAULT_TUNNEL_URL)


def parse_connection_urls(url: str) -> tuple[BackendProtocol, str, str]:
    target = (url or DEFAULT_TUNNEL_URL).strip()
    if target.startswith("kafka://"):
        return BackendProtocol.KAFKA, DEFAULT_TUNNEL_URL, target
    # Maps unix://, redis://, rediss:// to BackendProtocol.REDIS
    return BackendProtocol.REDIS, target, target