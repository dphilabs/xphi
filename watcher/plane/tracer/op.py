# xphi.watcher.plane.tracer.op
import asyncio
from functools import wraps
from typing import List, Tuple, Optional, Any


class ExecutorOp:
    """@desc: PhaseOp와 LifecycleOp의 시퀀스/단일 실행 및 템플릿 렌더링을 통합 관리하는 코어 엔진"""

    @staticmethod
    def render_cmd(cmd_template: list, instance: Any) -> list:
        return [
            str(arg.format(**instance.__dict__)) if isinstance(arg, str) else str(arg)
            for arg in cmd_template
        ]

    @staticmethod
    def render_cwd(cwd_template: Optional[str], instance: Any) -> str:
        if not cwd_template:
            return "."
        rendered = cwd_template.format(**instance.__dict__) if isinstance(cwd_template, str) else cwd_template
        return str(rendered.__fspath__()) if hasattr(rendered, "__fspath__") else str(rendered)

    @staticmethod
    async def run_single(
        instance: Any,
        cmd_template: list,
        phase_name: str,
        cwd: str = ".",
        capture: bool = False,
        strict: bool = True,
    ) -> Tuple[bool, int, str, str]:
        if getattr(instance, "rupture_confirmed", False):
            instance.log.warning(f"## @{phase_name}.aborted: Rupture already confirmed.")
            return False, -1, "", "Rupture already confirmed"

        actual_cmd = ExecutorOp.render_cmd(cmd_template, instance)
        actual_cwd = ExecutorOp.render_cwd(cwd, instance)
        instance.log.info(f"## @{phase_name}: {' '.join(actual_cmd)}")

        code, out, err = await instance.boundary.run_command(actual_cmd, cwd=actual_cwd, capture=capture)
        if strict and code != 0:
            instance.log.crit(f"## @{phase_name}.fault: (Code {code}) {err}")
            instance.rupture_confirmed = True
            return False, code, out, err

        return True, code, out, err

    @staticmethod
    async def run_sequence(
        instance: Any,
        cmds: List[list],
        phase_name: str,
        cwd: str = ".",
        strict: bool = True,
    ) -> bool:
        if getattr(instance, "rupture_confirmed", False):
            instance.log.warning(f"## @{phase_name}.aborted: Rupture already confirmed.")
            return False

        actual_cwd = ExecutorOp.render_cwd(cwd, instance)
        for i, cmd_template in enumerate(cmds, 1):
            if getattr(instance, "rupture_confirmed", False):
                return False

            actual_cmd = ExecutorOp.render_cmd(cmd_template, instance)
            instance.log.info(f"## @{phase_name} [{i}/{len(cmds)}]: {' '.join(actual_cmd)}")

            code, _, err = await instance.boundary.run_command(actual_cmd, cwd=actual_cwd, capture=False)
            if strict and code != 0:
                instance.log.crit(f"## @{phase_name}.fault at step {i}: (Code {code}) {err}")
                instance.rupture_confirmed = True
                return False
        return True


class PhaseOp:
    """@desc: 부수효과(Side-Effect) 경계에 따라 분리된 선언적 실행 오퍼레이터
    - probe:   Read-Only 상태 추출 (capture=True 강제)
    - verify:  Dry-Run 검증 및 아티팩트 생성 시퀀스
    - actuate: 검증된 상태 변이(Mutation) 집행
    """

    @staticmethod
    def _single_op(cmd: list, cwd: str, capture: bool, strict: bool, phase_name: str):
        def decorator(func):
            @wraps(func)
            async def wrapper(self, *args, **kwargs):
                ok, code, out, _ = await ExecutorOp.run_single(
                    self, cmd, phase_name=phase_name, cwd=cwd, capture=capture, strict=strict
                )
                if not ok:
                    return
                return await func(self, exit_code=code, stdout=out, *args, **kwargs)
            return wrapper
        return decorator

    @staticmethod
    def _sequence_op(cmds: List[list], cwd: str, strict: bool, phase_name: str):
        def decorator(func):
            @wraps(func)
            async def wrapper(self, *args, **kwargs):
                success = await ExecutorOp.run_sequence(
                    self, cmds, phase_name=phase_name, cwd=cwd, strict=strict
                )
                if not success:
                    return
                return await func(self, *args, **kwargs)
            return wrapper
        return decorator

    @staticmethod
    def probe(cmd: list, cwd: str = ".", strict: bool = True):
        """[Read-Only Boundary] 외부 상태를 변경하지 않고 stdout을 캡처하여 반환합니다."""
        return PhaseOp._single_op(cmd, cwd=cwd, capture=True, strict=strict, phase_name="probe")

    @staticmethod
    def verify(cmds: List[list], cwd: str = ".", strict: bool = True):
        """[Dry-Run Boundary] 원격 변이 없이 정합성 검증 및 Plan 시퀀스를 수행합니다."""
        return PhaseOp._sequence_op(cmds, cwd=cwd, strict=strict, phase_name="verify")

    @staticmethod
    def actuate(cmd: list, cwd: str = ".", capture: bool = False, strict: bool = True):
        """[Mutation Boundary] 실제 물리적 상태 변이(Apply/Stimulus)를 집행합니다."""
        return PhaseOp._single_op(cmd, cwd=cwd, capture=capture, strict=strict, phase_name="actuate")

    # Legacy Aliases (기존 코드 하위 호환 유지)
    @staticmethod
    def stimulus(cmd: list, cwd: str = ".", capture: bool = False, strict: bool = True):
        return PhaseOp._single_op(cmd, cwd=cwd, capture=capture, strict=strict, phase_name="stimulus")

    @staticmethod
    def sequence(cmds: List[list], cwd: str = ".", strict: bool = True):
        return PhaseOp._sequence_op(cmds, cwd=cwd, strict=strict, phase_name="sequence")


class LifecycleOp:
    @staticmethod
    def dynamic_sequence(cmd_provider_attr: str, strict: bool = True):
        def decorator(func):
            @wraps(func)
            async def wrapper(self, *args, **kwargs):
                cmds = getattr(self, cmd_provider_attr)()
                cwd = getattr(self, "workspace", ".")
                success = await ExecutorOp.run_sequence(self, cmds, "lifecycle", cwd, strict)
                if not success:
                    return
                return await func(self, *args, **kwargs)
            return wrapper
        return decorator


class SensorOp:
    @staticmethod
    def poll(cmd: list, cwd: str = "."):
        def decorator(func):
            func.__sensor_cmd__ = cmd
            func.__sensor_cwd__ = cwd
            return func
        return decorator


def log_streamer(cmd_list: list, cwd: Optional[str] = None):
    def decorator(func):
        @wraps(func)
        async def wrapper(self, *args, **kwargs):
            cmd = ExecutorOp.render_cmd(cmd_list, self)
            actual_cwd = ExecutorOp.render_cwd(cwd, self) if cwd else None

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=actual_cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )

            if hasattr(self, "boundary") and hasattr(self.boundary, "process_pool"):
                self.boundary.process_pool.append(proc)

            try:
                while True:
                    line = await proc.stdout.readline()
                    if not line:
                        break
                    await func(self, line.decode("utf-8", errors="replace").strip(), *args, **kwargs)
            except asyncio.CancelledError:
                pass
            finally:
                if proc.returncode is None:
                    try:
                        proc.terminate()
                    except ProcessLookupError:
                        pass
                if hasattr(self, "boundary") and hasattr(self.boundary, "process_pool"):
                    if proc in self.boundary.process_pool:
                        self.boundary.process_pool.remove(proc)
        return wrapper
    return decorator