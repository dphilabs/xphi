# xphi.arch.contract.config.tunnel
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from xphi.node.space.bind.resolver import resolve_path
from xphi.arch.contract.config.env import XPHI_TUNNEL_URL

TUNNEL_ROOT = resolve_path("tunnel")
RUN_DIR = TUNNEL_ROOT / "run"
KV_DIR = TUNNEL_ROOT / "kv"

TUNNEL_SOCK_PATH = RUN_DIR / "tunnel.sock"
TUNNEL_PID_PATH = RUN_DIR / "tunnel.pid"
TUNNEL_URL_PATH = RUN_DIR / "tunnel.url"

def get_default_tunnel_url() -> str:
    env_url = XPHI_TUNNEL_URL.strip()
    if env_url:
        return env_url
    if TUNNEL_URL_PATH.exists():
        return TUNNEL_URL_PATH.read_text(encoding="utf-8").strip()
    return f"unix://{TUNNEL_SOCK_PATH}"

class BackendProtocol(str, Enum):
    REDIS = "redis"
    KAFKA = "kafka"

@dataclass(frozen=True)
class TunnelConfig:
    default_url: str = ""
    def __post_init__(self):
        if not self.default_url:
            object.__setattr__(self, "default_url", get_default_tunnel_url())

def resolve_default_config() -> TunnelConfig:
    return TunnelConfig(default_url=get_default_tunnel_url())

def parse_connection_urls(url: str) -> tuple[BackendProtocol, str, str]:
    target = (url or get_default_tunnel_url()).strip()
    if target.startswith("kafka://"):
        return BackendProtocol.KAFKA, get_default_tunnel_url(), target
    # Maps unix://, redis://, rediss:// to BackendProtocol.REDIS
    return BackendProtocol.REDIS, target, target