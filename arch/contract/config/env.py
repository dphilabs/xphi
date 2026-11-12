# xphi.arch.contract.config.env
import os
from typing import Any
from xphi.watcher.plane.emitter import get_emitter

log = get_emitter("config.env")


class ConfigResolver:
    def __init__(self):
        self._local_overrides = {}

    def get(self, name: str, default: Any = None) -> Any:
        try:
            value = getattr(self, name)
            return default if value is None else value
        except AttributeError:
            return default

    def set_override(self, key: str, value: Any):
        self._local_overrides[key] = value

    def __getattr__(self, name: str):
        if name in self._local_overrides:
            return self._local_overrides[name]

        env_key = name.upper()
        if env_key in os.environ:
            return os.environ[env_key]
        raise AttributeError(f"'{type(self).__name__}' object (xphi config) has no attribute '{name}'")

    def __setattr__(self, name: str, value: Any):
        if name == "_local_overrides":
            super().__setattr__(name, value)
            return

        self._local_overrides[name] = value


config = ConfigResolver()


def _get_string(key: str, default: str) -> str:
    if key in config._local_overrides:
        return config.get(key)
    return os.getenv(key, default)


def _get_int(key: str, default: str) -> int:
    if key in config._local_overrides:
        return int(config.get(key))
    return int(os.getenv(key, default))

## GATEWAY
DEFAULT_GATEWAY_PORT = 8356
FIBER_GATEWAY_URL = _get_string("FIBER_GATEWAY_URL", f"http://localhost:{DEFAULT_GATEWAY_PORT}")

LOG_LEVEL = _get_string("LOG_LEVEL", "INFO")
USER_AGENT = _get_string("XPHI_USER_AGENT", "xphi-agent/1.0.1")

RPC_QUEUE_TOPIC = os.getenv("RPC_QUEUE_TOPIC", "internal.rpc.queue")
PUBSUB_CHANNEL = _get_string("PUBSUB_CHANNEL", "audit_channel")

"""Cryptography & KMS"""
XPHI_ENV = _get_string("XPHI_ENV", "local")
AIRGAP_MODE = _get_string("AIRGAP_MODE", "0")

## COMPAT & VCR
FIBER_COMPAT_RULES_PATH = _get_string("FIBER_COMPAT_RULES_PATH", "")
VCR_INCLUDE_RAW = _get_string("VCR_INCLUDE_RAW", "false")

## WASM PLANE
XPHI_WASM_PLANE = _get_string("XPHI_WASM_PLANE", "local")

## TUNNEL
XPHI_TUNNEL_URL = _get_string("XPHI_TUNNEL_URL", "")