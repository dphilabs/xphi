# xphi.node.ops.reaper
import asyncio
import json
import urllib.parse
import os
import signal
import argparse
from typing import List, Optional, Set
import psutil

from xphi.arch.contract.config.tunnel import TUNNEL_PID_PATH, TUNNEL_SOCK_PATH
from xphi.node.ops.task.supervisor import TaskSupervisor
from xphi.node.space.tunnel.factory import TunnelFactory
from xphi.state.phase.reactor import PhaseReactor
from xphi.watcher.plane.emitter import get_emitter

DEFAULT_TARGET_TAGS = [
    "xphi.node",
    "dphi",
    "deno",
    "fiber",
    "fiber.plane.mcp.worker",
    "multiprocessing.spawn",
    "multiprocessing.resource_tracker",
    "valkey-server",
]

log = get_emitter("node.ops.reaper", phase="BOOT")


class SystemOps:
    """Controls system process lifecycles and audits kernel execution state."""

    def __init__(self, tunnel: Optional[object], tag: str):
        self.tunnel = tunnel
        self.tag = tag
        self.registry_key = f"system:{self.tag}:pids"
        self.log = get_emitter(f"ops.{self.tag}", phase="EXEC")

        self.supervisor = TaskSupervisor(source=f"Commander-{self.tag}")
        self.supervisor.add_error_handler(self._on_task_error)

    def _on_task_error(self, task: asyncio.Task, exc: BaseException) -> None:
        self.log.error(f" [Supervisor] Task {task.get_name()} failed: {exc}")

    def _is_tunnel_alive(self) -> bool:
        return self.tunnel is not None and TUNNEL_SOCK_PATH.exists()

    async def get_pids_from_port(self, port: int) -> List[str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                "lsof", "-t", f"-i:{port}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            if stdout:
                return [pid.strip() for pid in stdout.decode().strip().split("\n") if pid.strip()]
            return []
        except Exception as e:
            self.log.error(f"lsof failed for port {port}: {e}")
            return []

    async def _discover_active_pids(self, wait_time: float = 2.0) -> Set[str]:
        if not self._is_tunnel_alive():
            self.log.info("Tunnel socket inactive. Skipping PubSub discovery.")
            return set()

        self.log.info(f"Broadcasting system:ping to locate [{self.tag}] nodes...")
        pubsub = self.tunnel.pubsub()
        try:
            await pubsub.subscribe("system:echo")
            await self.tunnel.publish(
                "system:ping",
                json.dumps({"ts": asyncio.get_running_loop().time(), "source": f"ops_{self.tag}"}),
            )
        except Exception as e:
            self.log.warning(f"Tunnel unavailable during ping broadcast: {e}")
            return set()

        active_pids = set()
        try:
            async with asyncio.timeout(wait_time):
                while True:
                    msg = await pubsub.get_message(ignore_subscribe_messages=True)
                    if msg:
                        try:
                            data = json.loads(msg["data"])
                            if "api_base" in data:
                                port = urllib.parse.urlparse(data["api_base"]).port
                                pids = await self.get_pids_from_port(port)
                                active_pids.update(pids)
                        except Exception:
                            continue
                    await asyncio.sleep(0.05)
        except asyncio.TimeoutError:
            self.log.info(f"Echo collection complete. Found {len(active_pids)} active processes.")
        except Exception:
            pass
        finally:
            try:
                await pubsub.unsubscribe("system:echo")
                await pubsub.close()
            except Exception:
                pass

        return active_pids

    async def _execute_kill(self, pid: str, force: bool = True):
        signal_flag = "-9" if force else "-15"
        proc = await asyncio.create_subprocess_exec(
            "kill", signal_flag, pid,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        if proc.returncode == 0:
            self.log.warning(f" [Killed] PID {pid} cleanly terminated with {signal_flag}")

    async def surgical_strike(self):
        pids = await self._discover_active_pids()
        for pid in pids:
            self.supervisor.create(self._execute_kill(pid, force=True), name=f"Strike-{pid}")
        return len(pids)

    async def scorched_earth(self):
        self.log.warning(f"Initiating process sweep for [{self.tag}]...")

        if self._is_tunnel_alive():
            try:
                pids = await self.tunnel.smembers(self.registry_key)
                if pids:
                    self.log.info(f"   [Phase 1] Dispatching kill orders for {len(pids)} PIDs in registry.")
                    for pid in pids:
                        self.supervisor.create(self._execute_kill(str(pid), force=True), name=f"ForceKill-{pid}")
                await self.tunnel.delete(self.registry_key)
            except Exception:
                pass

        my_pid = os.getpid()
        killed_count = 0
        try:
            for proc in psutil.process_iter(["pid", "cmdline"]):
                try:
                    cmdline = proc.info.get("cmdline") or []
                    cmd_str = " ".join(cmdline)

                    if (
                        self.tag in cmd_str
                        and "xphi.node.ops.reaper" not in cmd_str
                        and "phase.node.reaper" not in cmd_str
                        and proc.pid != my_pid
                    ):
                        children = proc.children(recursive=True)
                        procs_to_kill = children + [proc]

                        for p in procs_to_kill:
                            try:
                                p.kill()
                                killed_count += 1
                                self.log.info(f"   [Phase 2] Terminated PID {p.pid} (Command: {' '.join(p.cmdline()[:2])}...)")
                            except (psutil.NoSuchProcess, psutil.AccessDenied):
                                pass
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    pass

            if killed_count > 0:
                self.log.info(f"   [Phase 2] Process tree sweep completed. {killed_count} processes reaped.")
            else:
                self.log.info(f"   [Phase 2] No active processes matched for [{self.tag}].")
        except Exception as e:
            self.log.error(f"   [Phase 2] psutil sweep failed: {e}")

    async def nuke_tunnel_state(self):
        if not self._is_tunnel_alive():
            return
        self.log.warning("Flushing tunnel state store (FLUSHDB)...")
        try:
            await self.tunnel.flushdb(asynchronous=True)
            self.log.info("   └─ Tunnel state wiped clean.")
        except Exception as e:
            self.log.warning(f"   └─ Tunnel FLUSHDB skipped/failed: {e}")

    async def nuke_flare(self, target_port: int = 8787):
        self.log.warning(f"  Initiating Flare cleanup on Port {target_port}...")
        pids = await self.get_pids_from_port(target_port)

        if not pids:
            self.log.info(f"   └─ Port {target_port} is already clean.")
            return 0

        self.log.warning(f"   └─ Detected {len(pids)} process(es) holding Port {target_port}. Terminating...")
        for pid in pids:
            self.supervisor.create(self._execute_kill(pid, force=True), name=f"FlareNuke-{pid}")

        return len(pids)

    async def _verify_cpu_pinning(self, pid: str) -> bool:
        try:
            proc = await asyncio.create_subprocess_exec(
                "taskset", "-cp", pid,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            output = stdout.decode().strip()

            if "affinity list:" in output:
                affinity = output.split(":")[-1].strip()
                if "," not in affinity and "-" not in affinity:
                    self.log.warning(f"   [PASS] PID {pid} bound to Core {affinity}")
                    return True
                else:
                    self.log.error(f"   [FAIL] Floating CPU detected for PID {pid}. Affinity: {affinity}")
            return False
        except Exception:
            return False

    async def _verify_syscall_multiplexing(self, pid: str, duration: int = 3) -> bool:
        try:
            proc = await asyncio.create_subprocess_exec(
                "strace", "-c", "-p", pid,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.sleep(duration)
            proc.send_signal(signal.SIGINT)
            _, stderr = await proc.communicate()

            output = stderr.decode()
            if "epoll_wait" in output or "epoll_pwait" in output:
                self.log.warning(f"   [PASS] epoll I/O verified on PID {pid}")
                return True
            else:
                self.log.error(f"   [FAIL] No active epoll captured on PID {pid}")
                return False
        except Exception:
            return False

    async def _audit_node(self, pid: str):
        self.log.info(f" [Audit] Profiling architecture compliance for PID: {pid}")
        pin_valid = await self._verify_cpu_pinning(pid)
        io_valid = await self._verify_syscall_multiplexing(pid, duration=3)

        if pin_valid and io_valid:
            self.log.info(f" [SUCCESS] Node {pid} aligns with KernelReactor specs.")
        else:
            self.log.error(f" [VIOLATION] Node {pid} failed compliance checks.")

    async def audit_system(self):
        pids = await self._discover_active_pids()
        for pid in pids:
            self.supervisor.create(self._audit_node(pid), name=f"Audit-{pid}")
        return len(pids)

    async def execute_task(self, task_type: str) -> int:
        count = 0
        try:
            if task_type == "strike":
                count = await self.surgical_strike()
            elif task_type == "force":
                await self.scorched_earth()
            elif task_type == "clean":
                count = await self.surgical_strike()
                await self.scorched_earth()
            elif task_type == "nuke":
                await self.nuke_tunnel_state()
                await self.scorched_earth()
            elif task_type == "audit":
                count = await self.audit_system()
            elif task_type == "flare":
                count = await self.nuke_flare(target_port=8787)
            return count
        finally:
            self.log.info(f"Awaiting resolution of all {task_type} tasks for [{self.tag}]...")
            await self.supervisor.shutdown()


class CommandCLI:
    def __init__(self):
        self.tunnel = None

    async def run(self):
        parser = argparse.ArgumentParser(description="System Operations Commander (Reaper & Auditor)")
        parser.add_argument("--tag", type=str, default=None, help="Target component tag")
        parser.add_argument(
            "--task",
            type=str,
            default="clean",
            choices=["strike", "force", "clean", "nuke", "audit", "flare"],
            help="Task to run",
        )
        args = parser.parse_args()

        if TUNNEL_SOCK_PATH.exists():
            self.tunnel = await TunnelFactory.get_isolated()
        tunnel = self.tunnel.state_store if self.tunnel else None

        if args.task == "flare":
            target_tags = ["flare.edge"]
        else:
            target_tags = [args.tag] if args.tag else DEFAULT_TARGET_TAGS

        log.info(f"Executing Commander [{args.task.upper()}] sequence. Targets: {target_tags}")

        if args.task == "nuke" and tunnel is not None:
            await SystemOps(tunnel, tag="global").nuke_tunnel_state()
            tunnel = None  # Avoid redundant FLUSHDB on dead socket after process sweep

        total_nodes_processed = 0
        for tag in target_tags:
            log.info(f"\n## Orchestrating Target: [{tag}]")
            commander = SystemOps(tunnel, tag=tag)
            task_to_run = "force" if args.task == "nuke" else args.task
            count = await commander.execute_task(task_type=task_to_run)
            total_nodes_processed += count

        if args.task == "nuke":
            TUNNEL_SOCK_PATH.unlink(missing_ok=True)
            TUNNEL_PID_PATH.unlink(missing_ok=True)

        log.info(f"Commander execution finished. Total targets processed: {total_nodes_processed}")

    async def teardown(self):
        if self.tunnel and hasattr(self.tunnel.state_store, "aclose"):
            try:
                await self.tunnel.state_store.aclose()
            except Exception:
                pass

if __name__ == "__main__":
    cli = CommandCLI()
    PhaseReactor.ignite(
        main_coro_func=cli.run,
        teardown_hook=cli.teardown,
    )