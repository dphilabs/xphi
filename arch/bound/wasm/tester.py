# xphi.arch.bound.wasm.tester
import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Type

from xphi.arch.contract.event.next import next_id
from xphi.node.ops.task.supervisor import TaskSupervisor
from xphi.node.ops.task.wasm import (
    DEFAULT_WASM_PATH,
    REDIS_CONTROL_CHANNEL,
    REDIS_STREAM_TOPIC,
    TaskWasm,
)
from xphi.node.runtime.wasm.broker import WasmBroker
from xphi.node.space.tunnel.factory import TunnelFactory
from xphi.watcher.plane.emitter import flow_scope, get_emitter

log = get_emitter("wasm.tester")


class CanonicalProofAuditor:
    def __init__(self) -> None:
        self.flow_id = next_id()
        self.canonical_records: List[Dict[str, Any]] = []
        self.is_collapsed: bool = False
        self.is_exhausted: bool = False

    def project_state(self, action: str, metrics: dict) -> None:
        if not metrics:
            return

        fuel = metrics.get("fuel_consumed")
        if fuel is not None:
            record = {
                "action": action,
                "tier": metrics.get("tier", "UNKNOWN"),
                "fuel": fuel,
                "mem_usage": metrics.get("mem_usage_bytes"),
                "mem_peak": metrics.get("mem_peak_bytes"),
            }
            clean_record = {k: v for k, v in record.items() if v is not None}
            self.canonical_records.append(clean_record)

    def generate_payload(self) -> str:
        return json.dumps(self.canonical_records, sort_keys=True, separators=(",", ":"))

class WasmTester:
    def __init__(
        self,
        wasm_module_path: str,
        sandbox_root: str,
        suites: Optional[Dict[str, Type]] = None,
        kernel_wasm_path: str = DEFAULT_WASM_PATH,
    ):
        self.wasm_module_path = wasm_module_path
        self.sandbox_root = sandbox_root
        self.kernel_wasm_path = str(Path(sandbox_root) / kernel_wasm_path)
        self.is_halted: bool = False
        self.last_error_context: str = ""
        self.auditors: List[Any] = []
        self.suites = suites or {}
        self.test_execution_hash: Optional[str] = None
        self.suite_runners: Dict[str, Any] = {}

    async def _await_rupture(self) -> None:
        """Polls auditor flags in the background while test suites run."""
        try:
            while not self.is_halted:
                for auditor in self.auditors:
                    if getattr(auditor, "is_collapsed", False) or getattr(auditor, "is_exhausted", False):
                        self.is_halted = True
                        self.last_error_context = (
                            f"Auditor '{auditor.__class__.__name__}' detected fatal system state."
                        )
                        log.crit(f"[FATAL] {self.last_error_context}")
                        return
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            pass

    async def _run_all_suites(self, broker: WasmBroker) -> int:
        total_fails = 0
        for suite_name, suite_cls in self.suites.items():
            log.info(f"\n>>> [PHASE] Starting Test Suite: {suite_name.upper()} <<<")
            try:
                suite_instance = suite_cls(broker)
                self.suite_runners[suite_name] = suite_instance

                await suite_instance.run_all()
                total_fails += getattr(suite_instance, "fail_count", 0)
            except Exception as e:
                log.error(f"[ERROR] Suite '{suite_name}' crashed unexpectedly: {e}", exc_info=True)
                total_fails += 1

        return total_fails

    async def _run_with_broker(
        self,
        broker: WasmBroker,
        proof_auditor: CanonicalProofAuditor,
    ) -> Tuple[bool, str]:
        with flow_scope(phase="TEST_EXECUTION", flow_id=proof_auditor.flow_id):
            observer_task = asyncio.create_task(self._await_rupture())
            scenario_task = asyncio.create_task(self._run_all_suites(broker))

            done, pending = await asyncio.wait(
                [observer_task, scenario_task],
                return_when=asyncio.FIRST_COMPLETED,
            )

            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

            if self.is_halted:
                return False, self.last_error_context

            if scenario_task in done:
                total_fails = scenario_task.result()
                if total_fails > 0:
                    err_msg = f"Test suites failed with {total_fails} total errors."
                    return False, err_msg

                log.info("\n[SYSTEM] Generating Canonical Proof via Phase Kernel (phase.wasm)...")
                canonical_payload = proof_auditor.generate_payload()

                if not canonical_payload or canonical_payload == "[]":
                    err_msg = "[AnchorStore] Canonical payload is empty. Structural projection failed."
                    log.error(err_msg)
                    return False, err_msg

                try:
                    proof_res = await broker.invoke(
                        "compute_fingerprint",
                        canonical_payload,
                        wasm_path=self.kernel_wasm_path,
                    )
                    if getattr(proof_res, "success", False):
                        self.test_execution_hash = json.loads(proof_res.output).get("fingerprint")
                        log.info(f"[AnchorStore] Test Execution Proof sealed. Hash: {self.test_execution_hash}")
                    else:
                        error_msg = (
                            getattr(proof_res.error, "message", "Unknown Error")
                            if hasattr(proof_res, "error")
                            else "Unknown Error"
                        )
                        log.error(f"[AnchorStore] Failed to seal test execution: {error_msg}")
                except Exception as proof_err:
                    log.error(f"[AnchorStore] Exception during proof generation: {proof_err}")

                return True, ""

        return False, "Unexpected execution termination."

    async def execute(self) -> Tuple[bool, str]:
        target_name = Path(self.wasm_module_path).name
        log.info(f"\n--- [START] Orchestrating Distributed WASM Environment (Target: {target_name}) ---")
        supervisor = None
        tunnel = None
        broker = None
        proof_auditor = CanonicalProofAuditor()
        self.auditors = [proof_auditor]

        try:
            tunnel = await TunnelFactory.get_default()
            log.info("[SYSTEM] Initializing TaskSupervisor and WasmTaskerDaemon...")
            supervisor = TaskSupervisor(source="WasmTester")
            tasker_daemon = TaskWasm(
                tunnel=tunnel,
                supervisor=supervisor,
                default_wasm_path=self.kernel_wasm_path,
            )

            test_stream = "wasm:execute:stream:tester_isolated"
            test_control = "wasm:control:req:tester_isolated"

            tasker_daemon.topic = test_stream
            tasker_daemon.control_channel = test_control
            tasker_daemon.group_name = "wasm_tester_group"

            supervisor.mount_daemon(tasker_daemon)
            await asyncio.sleep(1)

            log.info("[SYSTEM] Initializing WasmBroker & Scenarios...")
            broker = WasmBroker(
                request_stream=test_stream,
                timeout=5.0,
                target_auditor=proof_auditor,
            )
            broker.control_channel = test_control

            return await self._run_with_broker(broker, proof_auditor)
        except Exception as e:
            log.error(f"[FATAL] Test orchestration crashed: {e}", exc_info=True)
            return False, str(e)
        finally:
            if broker:
                await broker.close()

            if supervisor:
                log.info("[SYSTEM] Tearing down Sandbox (Shutting down Supervisor)...")
                await supervisor.shutdown()

            if tunnel:
                if hasattr(tunnel.state_store, "aclose"):
                    await tunnel.state_store.aclose()
                elif hasattr(tunnel.state_store, "close"):
                    await tunnel.state_store.close()
            await TunnelFactory.close_all()


class WasmNodeTester(WasmTester):
    """
    격리 데몬(TaskSupervisor / TaskWasm)을 중복 생성하지 않고,
    기동 중인 실제 Node의 TaskWasm 스트림에 직접 바인딩하여 E2E 스위트를 실행하는 테스터.
    """
    async def execute(self) -> Tuple[bool, str]:
        target_name = Path(self.wasm_module_path).name
        log.info(f"\n--- [START] Orchestrating Live-Node WASM Environment (Target: {target_name}) ---")
        broker = None
        proof_auditor = CanonicalProofAuditor()
        self.auditors = [proof_auditor]

        try:
            log.info("[SYSTEM] Binding WasmBroker to Live Node TaskWasm...")
            broker = WasmBroker(
                request_stream=REDIS_STREAM_TOPIC,
                timeout=5.0,
                target_auditor=proof_auditor,
            )
            broker.control_channel = REDIS_CONTROL_CHANNEL

            return await self._run_with_broker(broker, proof_auditor)
        except Exception as e:
            log.error(f"[FATAL] Live-Node test orchestration crashed: {e}", exc_info=True)
            return False, str(e)
        finally:
            if broker:
                try:
                    await broker.update_policy("SYSTEM")
                except Exception:
                    pass
                await broker.close()