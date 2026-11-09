# xphi.watcher.plane.tracer.base
import asyncio
import importlib
import inspect
from abc import ABC, abstractmethod
from typing import List, Tuple, Type, Union, Callable, Awaitable, Optional, Any
from xphi.watcher.plane.emitter import get_emitter

class BaseBoundary(ABC):
    @abstractmethod
    def collapse(self) -> None:
        pass

class SystemBound(BaseBoundary):
    def __init__(self):
        self.process_pool: List[asyncio.subprocess.Process] = []

    async def run_command(self, cmd: list, cwd: str = ".", capture: bool = False) -> Tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE if capture else None,
            stderr=asyncio.subprocess.PIPE if capture else None,
        )
        self.process_pool.append(proc)
        try:
            if capture:
                stdout, stderr = await proc.communicate()
                return (
                    proc.returncode,
                    stdout.decode("utf-8", errors="replace").strip(),
                    stderr.decode("utf-8", errors="replace").strip(),
                )

            await proc.wait()
            return proc.returncode, "", ""
        finally:
            if proc in self.process_pool:
                self.process_pool.remove(proc)

    def collapse(self) -> None:
        """Sends a termination signal to all live OS processes."""
        for proc in list(self.process_pool):
            try:
                if proc.returncode is None:
                    proc.terminate()
            except ProcessLookupError:
                pass
        self.process_pool.clear()


class BaseAuditor(ABC):
    def __init__(self, target: str, namespace: str, boundary: Union[BaseBoundary, Any], interval: int = 2):
        self.target = target
        self.namespace = namespace
        self.boundary = boundary
        self.interval = interval
        self._task: Optional[asyncio.Task] = None

    def attach(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._observe())

    def detach(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()

    async def _observe(self) -> None:
        sensors = []
        for name in dir(self):
            method = getattr(self, name)
            if hasattr(method, "__sensor_cmd__"):
                sensors.append(method)

        try:
            while True:
                for sensor in sensors:
                    cmd = ExecutorOp.render_cmd(sensor.__sensor_cmd__, self)
                    cwd = ExecutorOp.render_cwd(getattr(sensor, "__sensor_cwd__", "."), self)
                    if hasattr(self.boundary, "run_command"):
                        code, out, _ = await self.boundary.run_command(cmd, cwd=cwd, capture=True)
                        if code == 0 and out:
                            if inspect.iscoroutinefunction(sensor):
                                await sensor(out)
                            else:
                                sensor(out)
                await asyncio.sleep(self.interval)
        except asyncio.CancelledError:
            pass

class BaseStreamAuditor(ABC):
    def __init__(self, target: str, boundary: Union[BaseBoundary, Any], delay: int = 1):
        self.target = target
        self.boundary = boundary
        self.delay = delay
        self._task: Optional[asyncio.Task] = None

    def attach(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._start_stream())

    def detach(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()

    async def _start_stream(self) -> None:
        if self.delay > 0:
            await asyncio.sleep(self.delay)
        await self.run_stream()

    @abstractmethod
    async def run_stream(self, *args, **kwargs) -> None:
        pass

class BaseTracer(ABC):
    def __init__(self, tracer_name: str, timeout: int = 60, boundary: Optional[BaseBoundary] = None):
        self.log = get_emitter(f"tracer.{tracer_name}", phase="agent")
        self.timeout = timeout
        self.boundary = boundary or SystemBound()
        self.tasks: List[asyncio.Task] = []
        self.auditors: List[Union[BaseAuditor, BaseStreamAuditor]] = []
        self.rupture_confirmed: bool = False
        self.equilibrium_achieved: bool = False

    @classmethod
    async def select(cls, module_path: str, class_name: Optional[str] = None, **kwargs) -> "BaseTracer":
        try:
            module = importlib.import_module(module_path)
        except ImportError as e:
            raise ImportError(f"[Framework] Failed to load tracer module '{module_path}': {e}")

        candidates: List[Type["BaseTracer"]] = []
        for name, obj in inspect.getmembers(module, inspect.isclass):
            if issubclass(obj, cls) and obj not in (cls, ReproBaseTracer) and not inspect.isabstract(obj):
                if class_name and name == class_name:
                    candidates = [obj]
                    break
                candidates.append(obj)

        if not candidates:
            raise ValueError(f"[Framework] No valid BaseTracer found in '{module_path}'")

        tracer_cls = candidates[-1]
        instance = tracer_cls(**kwargs)
        await instance.trace()
        return instance

    def register_auditors(self, *auditors: Union[BaseAuditor, BaseStreamAuditor]) -> None:
        """@desc: Auditor들을 등록하고 즉시 라이프사이클(attach)에 편입시킵니다."""
        for auditor in auditors:
            self.auditors.append(auditor)
            auditor.attach()

    async def await_rupture(self, tick: int = 1, hook_fn: Optional[Callable[[int], Awaitable[None]]] = None) -> None:
        """@desc: 타임아웃까지 파괴(Rupture) 상태를 폴링합니다. hook_fn을 통해 매 틱마다 로직을 주입할 수 있습니다."""
        remaining = self.timeout
        while remaining > 0 and not self.rupture_confirmed:
            if hook_fn:
                await hook_fn(remaining)

            if remaining % 5 == 0 and remaining != self.timeout:
                self.log.info(f"  -> Resonance Window: {remaining}s remaining...")

            await asyncio.sleep(tick)
            remaining -= tick

    @abstractmethod
    async def execute(self) -> None:
        pass

    async def trace(self) -> None:
        self.log.crit("## @trace.init: Initiating Structural Trace Lifecycle")
        try:
            await asyncio.wait_for(self.execute(), timeout=self.timeout)
        except asyncio.TimeoutError:
            self.log.warning(f"## @trace.timeout: Trace execution reached time limit ({self.timeout}s).")
        except asyncio.CancelledError:
            self.log.warning("## @trace.cancel: Trace execution was cancelled externally.")
            raise
        except Exception as e:
            self.log.crit(f"## @trace.error: Framework Fault Detected: {str(e)}")
            self.rupture_confirmed = True
        finally:
            self.log.info("## @trace.teardown: Collapsing space and reclaiming resources...")
            for auditor in self.auditors:
                auditor.detach()

            for task in self.tasks:
                if not task.done():
                    task.cancel()
            self.boundary.collapse()
            self.log.crit("## @trace.exit: Field Restored.")

class ReproBaseTracer(BaseTracer):
    """@desc: Specialized BaseTracer utilizing PhaseOp/LifecycleOp for declarative infrastructure control"""

    def __init__(self, target_name: str, timeout: int = 60, boundary: Optional[BaseBoundary] = None):
        super().__init__(tracer_name=f"repro.{target_name}", timeout=timeout, boundary=boundary)
        # self.config = TargetRegistry.get(target_name)
        # self.workspace = self.config["workspace_path"]