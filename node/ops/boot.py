# xphi.node.ops.boot
import os
import shutil
import signal
import socket
import subprocess
import asyncio
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from xphi.arch.contract.config.tunnel import (
    KV_DIR,
    RUN_DIR,
    TUNNEL_PID_PATH,
    TUNNEL_SOCK_PATH,
    TUNNEL_URL_PATH,
    resolve_default_config,
)
from xphi.arch.contract.event.next import EventObserver, LogEvent
from xphi.arch.contract.event.psi import PsiEvent, PsiCarrier, CarrierType
from xphi.arch.contract.event.bus import AsyncEventBus
from xphi.arch.contract.registry.unified import registry

from xphi.state.phase.executor.base import BaseExecutor
from xphi.state.phase.executor.cont import SwarmExecutor
from xphi.state.phase.executor.flow import FlowExecutor
from xphi.state.phase.reactor import PhaseReactor

from xphi.state.anchor.store import AnchorStore, AnchorRole, ANCHOR_DB_PATH

from xphi.node.runtime.anchor import RuntimeAnchor
from xphi.node.space.tunnel.factory import TunnelFactory
from xphi.node.runtime.wasm.broker import WasmBroker

from xphi.watcher.plane.emitter import get_emitter
from xphi.watcher.plane.regulator import default_plane
from xphi.watcher.receptor.bootstrap import receptor_bootstrap

log = get_emitter("node.ops.boot")

_node_instance: Optional[RuntimeAnchor] = None
_valkey_proc: Optional[asyncio.subprocess.Process] = None
_background_tasks = set()

def _resolve_valkey_bin() -> str:
    """Resolves bundled valkey-server binary from valkeylite or system PATH."""
    try:
        import valkeylite
        pkg_dir = Path(valkeylite.__file__).resolve().parent
        for candidate in pkg_dir.rglob("valkey-server"):
            if os.access(candidate, os.X_OK):
                return str(candidate)
    except ImportError:
        pass

    sys_bin = shutil.which("valkey-server")
    if sys_bin:
        return sys_bin
    raise RuntimeError("valkey-server binary not found. Install 'valkeylite'.")


def _ping_endpoint(sock_path: Optional[Path] = None, host: str = "127.0.0.1", port: int = 0) -> bool:
    if sock_path and not sock_path.exists():
        return False
    try:
        if port > 0:
            s = socket.create_connection((host, port), timeout=0.2)
        else:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(0.2)
            s.connect(str(sock_path))
        with s:
            s.sendall(b"*1\r\n$4\r\nPING\r\n")
            return s.recv(16).startswith(b"+PONG")
    except OSError:
        return False


def _cleanup_stale_valkey() -> None:
    if TUNNEL_PID_PATH.exists():
        try:
            pid = int(TUNNEL_PID_PATH.read_text(encoding="utf-8").strip())
            cmd_out = subprocess.check_output(["ps", "-p", str(pid), "-o", "comm="], text=True).strip()
            if "valkey" in cmd_out:
                log.warning(f"[Boot] Terminating stale embedded Valkey (PID {pid})...")
                os.kill(pid, signal.SIGTERM)
        except Exception:
            pass
        TUNNEL_PID_PATH.unlink(missing_ok=True)
    TUNNEL_SOCK_PATH.unlink(missing_ok=True)


async def ensure_embedded_valkey() -> None:
    global _valkey_proc
    TUNNEL_URL_PATH.unlink(missing_ok=True)
    tunnel_url = os.getenv("XPHI_TUNNEL_URL") or resolve_default_config().default_url
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    TUNNEL_URL_PATH.write_text(tunnel_url, encoding="utf-8")
    parsed = urlparse(tunnel_url)

    is_tcp = parsed.scheme in ("redis", "valkey")
    if is_tcp:
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 6379
        if host not in ("127.0.0.1", "localhost"):
            log.info(f"[Boot] External tunnel configured ({tunnel_url}). Skipping embedded Valkey.")
            return
        sock_path = None
        target = f"{host}:{port}"
        bind_args = ["--bind", host, "--port", str(port)]
    else:
        host, port = "127.0.0.1", 0
        sock_path = Path(parsed.path) if parsed.path else TUNNEL_SOCK_PATH
        target = str(sock_path)
        bind_args = ["--port", "0", "--unixsocket", target, "--unixsocketperm", "700"]

    if _ping_endpoint(sock_path=sock_path, host=host, port=port):
        log.info(f"[Boot] Reusing active tunnel at {target}")
        return

    RUN_DIR.mkdir(parents=True, exist_ok=True)
    KV_DIR.mkdir(parents=True, exist_ok=True)
    _cleanup_stale_valkey()

    cmd = [
        _resolve_valkey_bin(),
        *bind_args,
        "--pidfile", str(TUNNEL_PID_PATH),
        "--dir", str(KV_DIR),
        "--appendonly", "no",
        "--save", "",
    ]

    log.info(f"[Boot] Starting embedded Valkey on {target}...")
    _valkey_proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )

    for _ in range(50):
        if _ping_endpoint(sock_path=sock_path, host=host, port=port):
            log.info(f"[Boot] Embedded Valkey ready (PID {_valkey_proc.pid}).")
            return
        if _valkey_proc.returncode is not None:
            err = (await _valkey_proc.stderr.read()).decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"[Boot] Embedded Valkey exited prematurely: {err}")
        await asyncio.sleep(0.05)

    raise RuntimeError(f"[Boot] Timed out waiting for Valkey at {target}")


async def stop_embedded_valkey() -> None:
    global _valkey_proc
    if _valkey_proc is None:
        return

    if _valkey_proc.returncode is None:
        log.info(f"[Boot] Stopping embedded Valkey (PID {_valkey_proc.pid})...")
        _valkey_proc.terminate()
        try:
            await asyncio.wait_for(_valkey_proc.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            _valkey_proc.kill()
            await _valkey_proc.wait()

    TUNNEL_SOCK_PATH.unlink(missing_ok=True)
    TUNNEL_PID_PATH.unlink(missing_ok=True)
    _valkey_proc = None


async def _acquire_sealer_store() -> AnchorStore:
    store = AnchorStore()
    if store.role == AnchorRole.SEALER:
        return store

    lock_file = Path(ANCHOR_DB_PATH) / "LOCK"
    log.warning(f"[Boot] Master node failed to acquire SEALER lock on {lock_file}. Inspecting holder PIDs...")
    store.close()

    my_pid = os.getpid()
    try:
        out = subprocess.check_output(["lsof", "-t", str(lock_file)], text=True).strip()
        holder_pids = [int(p) for p in out.splitlines() if p.strip().isdigit() and int(p) != my_pid]
    except Exception:
        holder_pids = []

    for pid in holder_pids:
        log.warning(f"[Boot] Stale DB lock at {lock_file} held by PID {pid}. Sending SIGKILL...")
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    if holder_pids:
        await asyncio.sleep(0.3)

    store = AnchorStore()
    if store.role != AnchorRole.SEALER:
        raise RuntimeError(f"[Boot] Critical: Failed to acquire SEALER role on {lock_file} after evicting {holder_pids}.")
    return store


class PhaseSignal(EventObserver):
    def __init__(self, event_bus: AsyncEventBus):
        self.bus = event_bus
        self.log = get_emitter("anchor.bridge", phase="BOOT")

    def update(self, event: LogEvent) -> None:
        if event.level != "SIGNAL":
            return

        event_message = str(event.message).strip()
        payload = event.context.copy() if event.context else {}
        payload.update({
            "bridged_from": "Surface.LogEvent",
            "original_source": getattr(event, "source_id", "unknown"),
            "boundary": "surface_to_kernel",
        })

        carrier = PsiCarrier(
            kind="SIGNAL",
            tag=event_message,
            payload=payload,
            carrier_type=CarrierType.FIXED,
        )

        psi_event = PsiEvent(
            event_id=event.event_id,
            parent_id=getattr(event, "parent_id", None),
            source_id="phase.bridge",
            scope=getattr(event, "scope", "GLOBAL"),
            tick=getattr(event, "tick", 0) or 0,
            phase_id=getattr(event, "phase_id", 0),
            carrier=carrier,
            context={"domain": "boundary.crossing"},
        )

        try:
            loop = asyncio.get_running_loop()
            if loop.is_closed():
                raise RuntimeError("Event loop is closed")
            loop.create_task(self._async_dispatch(psi_event, event_message))
        except RuntimeError as e:
            self.log.error(f"[PhaseBridge] Cannot dispatch SIGNAL. Event loop unavailable: {e}")

    async def _async_dispatch(self, psi_event: PsiEvent, original_msg: str):
        try:
            symbol = getattr(psi_event, "symbol", original_msg)
            self.log.trace(f"[PhaseBridge] Elevating SIGNAL -> Psi: {symbol}")
            await self.bus.publish(psi_event)
        except Exception as e:
            self.log.error(f"[PhaseBridge] Dispatch failed for {original_msg}: {e}", exc_info=True)


class RoutingExecutor(BaseExecutor):
    def __init__(self, completion_signal: asyncio.Event):
        super().__init__()
        self.completion_signal = completion_signal
        self.swarm_executor = SwarmExecutor(completion_signal)
        self.flow_executor = FlowExecutor(completion_signal)
        self._node = None

    @property
    def node(self):
        return self._node

    @node.setter
    def node(self, value):
        self._node = value
        self.swarm_executor.node = value
        self.flow_executor.node = value

    async def execute(self, psi) -> list:
        if not hasattr(psi, "carrier") or psi.carrier.kind != "COMMAND":
            return []

        context = psi.carrier.payload.get("_context", {})
        command = context.get("command") or psi.carrier.tag
        task_info_list = registry.registered_cli_tasks.get(command)
        if not task_info_list:
            return await self.swarm_executor.execute(psi)

        task_type = task_info_list[0].get("type", "cli")
        if task_type == "flow":
            try:
                return await self.flow_executor.execute(psi)
            except Exception as e:
                log.error(f"[Router] FlowExecutor failed with {e}. Falling back to SwarmExecutor.")
                return await self.swarm_executor.execute(psi)
        else:
            return await self.swarm_executor.execute(psi)


async def main_async():
    global _node_instance

    log.info("[Boot] Initiating Control Plane (Master Watcher)...")
    await ensure_embedded_valkey()

    system_bus = AsyncEventBus()

    bridge_watcher = PhaseSignal(event_bus=system_bus)
    default_plane.attach(bridge_watcher)

    tunnel = await TunnelFactory.get_default()
    receptor_task = asyncio.create_task(receptor_bootstrap(tunnel))
    _background_tasks.add(receptor_task)
    receptor_task.add_done_callback(_background_tasks.discard)

    log.info("[Boot] Waiting for module discovery and registry sync...")
    active_daemons_str = os.getenv("NODE_DAEMONS", "")
    active_daemons = [d.strip() for d in active_daemons_str.split(",") if d.strip()]

    wait_timeout = 30.0
    elapsed = 0.0
    poll_interval = 0.2
    if active_daemons:
        while elapsed < wait_timeout:
            discovered = getattr(registry, "_daemons", {})
            if all(d in discovered for d in active_daemons):
                log.info(f"[Boot] Registry sync verified in {elapsed:.1f}s. Proceeding to ignite node...")
                break

            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
        else:
            missing_daemons = [d for d in active_daemons if d not in getattr(registry, "_daemons", {})]
            log.warning(f"[Boot] Registry sync timed out after {wait_timeout}s! Missing daemons: {missing_daemons}")
    else:
        log.info("[Boot] No specific NODE_DAEMONS requested. Proceeding directly to ignite node...")

    wasm_timeout = float(os.getenv("XPHI_WASM_TIMEOUT", "30.0"))
    log.info(f"[Boot] Provisioning Shared WASM Broker (Timeout: {wasm_timeout}s)...")
    shared_broker = WasmBroker.get_shared(timeout=wasm_timeout, tunnel_factory=TunnelFactory)

    log.info("[Boot] Initializing AnchorStore and enforcing SEALER lock...")
    store = await _acquire_sealer_store()

    log.info("[Boot] Igniting Embedded Phase Runtime Node...")
    completion_signal = asyncio.Event()
    executor = RoutingExecutor(completion_signal)

    _node_instance = RuntimeAnchor(executor=executor)
    _node_instance.tunnel = tunnel
    _node_instance.broker = shared_broker

    await _node_instance.start()

    log.info("[Boot] Control Plane and Embedded Node fully operational.")
    log.info("[Boot] Entering observation mode (External layers delegated to Daemons).")
    await _node_instance.wait_until_stopped()


async def teardown():
    global _node_instance
    log.info("[Boot] Releasing system resources...")
    if _node_instance and getattr(_node_instance, "running", False):
        await _node_instance.shutdown()

    for task in list(_background_tasks):
        if not task.done():
            task.cancel()
    if _background_tasks:
        await asyncio.gather(*_background_tasks, return_exceptions=True)  # <- 취소 완료 대기

    try:
        if AnchorStore._instance is not None:
            AnchorStore._instance.close()
            log.info("[Boot] AnchorStore lock safely released.")
    except Exception as e:
        log.warning(f"[Boot] Error while releasing AnchorStore lock: {e}")

    if WasmBroker._shared_instance is not None:
        try:
            await WasmBroker._shared_instance.close()
            log.info("[Boot] Shared WASM Broker closed cleanly.")
        except Exception as e:
            log.warning(f"[Boot] Error while closing Shared WASM Broker: {e}")

    await TunnelFactory.close_all()
    await stop_embedded_valkey()
    TUNNEL_URL_PATH.unlink(missing_ok=True)
    log.info("[Boot] Resource cleanup complete.")

if __name__ == "__main__":
    PhaseReactor.ignite(main_coro_func=main_async, teardown_hook=teardown)