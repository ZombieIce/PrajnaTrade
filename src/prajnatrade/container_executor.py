"""Container-isolated execution of generated Python strategies.

The untrusted strategy never runs in the trusted controller process.  Every
invocation starts a fresh Docker container with no mounts, no network, a
read-only root filesystem, an unprivileged UID, no Linux capabilities, and
CPU/memory/PID/tmpfs limits.  The container receives one JSON request on
stdin and produces JSON protocol lines on stdout.  The trusted side inspects
the running container before acknowledging it, verifies the actual isolation
settings, validates the returned weights, and records the evidence.

There is deliberately no trusted-local fallback: if Docker/Colima or the
configured image is unavailable the invocation fails with
:class:`prajnatrade.isolation.IsolationError`.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import selectors
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from prajnatrade.backtest import _validate_weights
from prajnatrade.data import ETF_SYMBOLS
from prajnatrade.isolation import IsolationError

_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_CONTAINER_ID = re.compile(r"^[0-9a-f]{12,64}$")
_MEMORY = re.compile(r"^([0-9]+)([kmg])?$", re.IGNORECASE)
_MEMORY_MULTIPLIERS = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3}
_SECURITY_OPTION = "no-new-privileges"


@dataclass(frozen=True)
class ContainerLimits:
    """Trusted-side limits for one container invocation."""

    wall_seconds: float = 30.0
    input_bytes: int = 32 * 1024**2
    output_bytes: int = 64 * 1024
    memory: str = "512m"
    cpus: str = "1"
    pids_limit: int = 64
    tmpfs_size: str = "16m"


DEFAULT_CONTAINER_LIMITS = ContainerLimits()


@dataclass(frozen=True)
class ImageIdentity:
    reference: str
    digest: str


@dataclass(frozen=True)
class ExecutionResult:
    """Validated weights plus the evidence for one container invocation."""

    weights: dict[str, float] | None
    evidence: dict[str, Any]


_WORKER = r"""
import contextlib
import io
import json
import signal
import sys

sys.dont_write_bytecode = True


class _Sink(io.TextIOBase):
    def __init__(self, limit):
        self.limit = limit
        self.size = 0
        self.exceeded = False

    def write(self, text):
        try:
            self.size += len(text.encode("utf-8", errors="replace"))
        except AttributeError:
            self.size += len(text)
        if self.size > self.limit:
            self.exceeded = True
        return len(text)

    def flush(self):
        pass


def emit(value):
    sys.__stdout__.write(json.dumps(value, separators=(",", ":"), allow_nan=False) + "\n")
    sys.__stdout__.flush()


def fail(reason):
    emit({"ok": False, "reason": reason})


def expire(_signum, _frame):
    raise SystemExit(3)


def main():
    try:
        request = json.loads(sys.stdin.buffer.readline())
        if not isinstance(request, dict):
            fail("invalid_request")
            return
        source = request["source"]
        parameters = request["parameters"]
        symbols = tuple(str(symbol) for symbol in request["symbols"])
        state = request["state"]
        history_rows = request["history"]
        output_limit = int(request["limits"]["output_bytes"])
        wall_seconds = float(request["limits"]["wall_seconds"])
    except BaseException:
        fail("invalid_request")
        return
    emit({"event": "ready"})
    try:
        acknowledgement = json.loads(sys.stdin.buffer.readline())
    except BaseException:
        fail("inspection_not_acknowledged")
        return
    if not isinstance(acknowledgement, dict) or acknowledgement.get("ack") is not True:
        fail("inspection_not_acknowledged")
        return
    try:
        signal.signal(signal.SIGALRM, expire)
        signal.setitimer(signal.ITIMER_REAL, wall_seconds)
    except BaseException:
        fail("worker_setup_failed")
        return
    try:
        import pandas as pd

        history = pd.DataFrame(history_rows)
        if "date" in history:
            history["date"] = pd.to_datetime(history["date"])
    except BaseException:
        fail("worker_setup_failed")
        return
    namespace = {
        "ETF_SYMBOLS": symbols,
        "CASH": "CASH",
        "parameters": parameters,
    }
    sink = _Sink(output_limit)
    emit({"event": "source_started"})
    try:
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            exec(compile(source, "<generated-strategy>", "exec"), namespace)
            strategy = namespace.get("strategy")
            if not callable(strategy):
                emit({"ok": False, "reason": "strategy_entrypoint_missing"})
                return
            emit({"event": "strategy_started"})
            result = strategy(history, state)
    except BaseException:
        emit({"ok": False, "reason": "strategy_failed"})
        return
    if sink.exceeded:
        emit({"ok": False, "reason": "output_limit"})
        return
    if result is None:
        weights = None
    elif isinstance(result, dict) and all(isinstance(key, str) for key in result):
        weights = {}
        for key, value in result.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                emit({"ok": False, "reason": "invalid_strategy_output"})
                return
            weights[key] = float(value)
    else:
        emit({"ok": False, "reason": "invalid_strategy_output"})
        return
    try:
        emit({"ok": True, "weights": weights})
    except BaseException:
        emit({"ok": False, "reason": "invalid_strategy_output"})


main()
"""


class _ContainerTimeout(Exception):
    pass


class _ContainerOutputLimit(Exception):
    pass


class _ContainerProtocolError(Exception):
    pass


class _ContainerBoundaryError(Exception):
    def __init__(self, checks: dict[str, bool]) -> None:
        super().__init__(", ".join(sorted(name for name, ok in checks.items() if not ok)))
        self.checks = checks


class _ContainerRuntimeError(Exception):
    pass


def _validate_limits(limits: ContainerLimits) -> None:
    if isinstance(limits.wall_seconds, bool) or not math.isfinite(limits.wall_seconds):
        raise ValueError("Container wall_seconds must be finite.")
    if limits.wall_seconds <= 0:
        raise ValueError("Container wall_seconds must be greater than zero.")
    if min(limits.input_bytes, limits.output_bytes) <= 0:
        raise ValueError("Container input and output limits must be greater than zero.")
    if limits.pids_limit <= 0:
        raise ValueError("Container pids_limit must be greater than zero.")
    _memory_bytes(limits.memory)
    _nano_cpus(limits.cpus)
    if not isinstance(limits.tmpfs_size, str) or not limits.tmpfs_size.strip():
        raise ValueError("Container tmpfs_size must be a non-empty string.")


def _memory_bytes(value: str) -> int:
    match = _MEMORY.fullmatch(str(value))
    if match is None:
        raise ValueError(f"Invalid container memory limit: {value!r}.")
    amount = int(match.group(1))
    multiplier = _MEMORY_MULTIPLIERS[(match.group(2) or "").lower()]
    if amount <= 0:
        raise ValueError("Container memory limit must be greater than zero.")
    return amount * multiplier


def _nano_cpus(value: str) -> int:
    try:
        cpus = Decimal(str(value))
    except ArithmeticError as error:
        raise ValueError(f"Invalid container CPU limit: {value!r}.") from error
    if not cpus.is_finite() or cpus <= 0:
        raise ValueError("Container CPU limit must be greater than zero.")
    return int(cpus * 1_000_000_000)


class ContainerStrategyExecutor:
    """Execute generated Python inside a verified Docker container boundary.

    ``DOCKER_HOST`` (or ``PRAJNA_DOCKER_HOST``) selects the daemon; the default
    is the ``docker`` CLI's configured daemon.  ``PRAJNA_DOCKER_BIN`` overrides
    the CLI binary.  The image digest is resolved once and is checked against
    ``expected_digest`` before any strategy runs.
    """

    def __init__(
        self,
        image: str,
        *,
        limits: ContainerLimits | None = None,
        docker_host: str | None = None,
        docker_binary: str | None = None,
        expected_digest: str | None = None,
    ) -> None:
        if not isinstance(image, str) or not image.strip():
            raise ValueError("A container image reference is required.")
        self.image = image.strip()
        self.limits = limits or DEFAULT_CONTAINER_LIMITS
        _validate_limits(self.limits)
        self._docker_host = (
            docker_host
            or os.environ.get("PRAJNA_DOCKER_HOST")
            or os.environ.get("DOCKER_HOST")
            or None
        )
        self._docker_binary = docker_binary or os.environ.get("PRAJNA_DOCKER_BIN") or "docker"
        if expected_digest is not None and _SHA256.fullmatch(expected_digest) is None:
            raise ValueError("The expected container image digest is invalid.")
        self._expected_digest = expected_digest
        self._identity: ImageIdentity | None = None

    def limits_config(self) -> dict[str, Any]:
        return asdict(self.limits)

    def resolve_configuration(self) -> dict[str, Any]:
        """Resolve and return the campaign evaluation configuration."""
        identity = self.resolve_image()
        return {
            "kind": "container",
            "image": identity.reference,
            "digest": identity.digest,
            "limits": self.limits_config(),
        }

    def resolve_image(self) -> ImageIdentity:
        if self._identity is not None:
            return self._identity
        completed = self._run_docker(
            ["image", "inspect", "--format", "{{.Id}}", self.image], timeout=30.0
        )
        digest = completed.stdout.strip()
        if completed.returncode != 0 or _SHA256.fullmatch(digest) is None:
            detail = completed.stderr.strip() or "image not found"
            raise IsolationError(
                f"Container image {self.image!r} could not be resolved: {detail}."
            )
        if self._expected_digest is not None and digest != self._expected_digest:
            raise IsolationError(
                "Container image digest changed after campaign creation: "
                f"expected {self._expected_digest}, resolved {digest}."
            )
        self._identity = ImageIdentity(reference=self.image, digest=digest)
        return self._identity

    def run(
        self,
        source: str,
        parameters: Mapping[str, Any],
        history: Any,
        state: Mapping[str, Any],
    ) -> ExecutionResult:
        if not isinstance(source, str) or not source.strip():
            raise IsolationError("Container strategy source must be a non-empty string.")
        identity = self.resolve_image()
        run_id = uuid.uuid4().hex
        request = {
            "source": source,
            "parameters": dict(parameters),
            "symbols": list(ETF_SYMBOLS),
            "history": _history_records(history),
            "state": dict(state),
            "limits": {
                "output_bytes": self.limits.output_bytes,
                "wall_seconds": self.limits.wall_seconds,
            },
        }
        try:
            encoded = json.dumps(
                request, separators=(",", ":"), allow_nan=False
            ).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise IsolationError(
                f"Container strategy request is not JSON serializable: {error}."
            ) from error
        evidence: dict[str, Any] = {
            "run_id": run_id,
            "image_reference": identity.reference,
            "image_digest": identity.digest,
            "container_id": None,
            "inspect": None,
            "isolation_checks": {},
            "input_sha256": hashlib.sha256(encoded).hexdigest(),
            "input_bytes": len(encoded),
            "output_sha256": hashlib.sha256(b"").hexdigest(),
            "output_bytes": 0,
            "wall_seconds": 0.0,
            "exit_code": None,
            "source_started": False,
            "strategy_started": False,
            "result": "error",
        }
        if len(encoded) > self.limits.input_bytes:
            evidence["reason"] = "input_limit"
            raise IsolationError(
                "Container strategy input exceeded the configured limit.",
                evidence=evidence,
            )
        with tempfile.TemporaryDirectory(prefix="prajnatrade-container-") as directory:
            cidfile = Path(directory) / "container-id"
            command = self._run_command(identity.reference, run_id, cidfile)
            final = self._exchange(command, encoded, evidence, cidfile)
        exit_code = evidence["exit_code"]
        if exit_code != 0:
            evidence["reason"] = "container_exit"
            raise IsolationError(
                f"Container strategy container exited with code {exit_code}.",
                evidence=evidence,
            )
        if not final.get("ok"):
            reason = final.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                reason = "strategy_failed"
            evidence["reason"] = reason
            raise IsolationError(f"Container strategy aborted: {reason}.", evidence=evidence)
        raw_weights = final.get("weights")
        if raw_weights is None:
            weights = None
        else:
            try:
                weights = _validate_container_weights(raw_weights)
            except ValueError as error:
                evidence["weights_valid"] = False
                evidence["reason"] = "invalid_weights"
                raise IsolationError(
                    f"Container strategy returned invalid weights: {error}",
                    evidence=evidence,
                ) from error
        evidence["weights_valid"] = True
        evidence["result"] = "weights" if weights is not None else "none"
        return ExecutionResult(weights=weights, evidence=evidence)

    def _run_command(
        self, image: str, run_id: str, cidfile: Path
    ) -> list[str]:
        limits = self.limits
        return [
            self._docker_path(),
            "run",
            "--rm",
            "-i",
            "--cidfile",
            str(cidfile),
            "--label",
            f"prajnatrade.run-id={run_id}",
            "--network",
            "none",
            "--read-only",
            "--user",
            "65534:65534",
            "--cap-drop",
            "ALL",
            "--security-opt",
            _SECURITY_OPTION,
            "--pids-limit",
            str(limits.pids_limit),
            "--memory",
            limits.memory,
            "--cpus",
            limits.cpus,
            "--tmpfs",
            f"/tmp:rw,size={limits.tmpfs_size}",
            "-e",
            "PYTHONDONTWRITEBYTECODE=1",
            image,
            "python",
            "-I",
            "-c",
            _WORKER,
        ]

    def _exchange(
        self,
        command: list[str],
        request: bytes,
        evidence: dict[str, Any],
        cidfile: Path,
    ) -> dict[str, Any]:
        limits = self.limits
        started = time.monotonic()
        deadline = started + limits.wall_seconds
        process: subprocess.Popen[bytes] | None = None
        selector: selectors.BaseSelector | None = None
        container_id: str | None = None
        stdout = bytearray()
        stderr = bytearray()
        writer_errors: list[BaseException] = []
        final: dict[str, Any] | None = None
        failure: tuple[str, str] | None = None
        source_started = False
        strategy_started = False
        killed = False
        try:
            try:
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=self._docker_env(),
                    bufsize=0,
                )
            except OSError as error:
                raise _ContainerRuntimeError(f"cannot start Docker CLI ({error})") from error
            writer = threading.Thread(
                target=_write_request, args=(process, request, writer_errors), daemon=True
            )
            writer.start()
            stderr_reader = threading.Thread(
                target=_drain_stream, args=(process.stderr, stderr), daemon=True
            )
            stderr_reader.start()
            selector = selectors.DefaultSelector()
            selector.register(process.stdout, selectors.EVENT_READ)
            buffer = bytearray()
            ready = False
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _ContainerTimeout
                if not selector.select(min(remaining, 0.05)):
                    if process.poll() is not None:
                        break
                    continue
                chunk = os.read(process.stdout.fileno(), 4096)
                if not chunk:
                    break
                stdout.extend(chunk)
                if len(stdout) > limits.output_bytes:
                    raise _ContainerOutputLimit
                buffer.extend(chunk)
                while True:
                    index = buffer.find(b"\n")
                    if index < 0:
                        break
                    line = bytes(buffer[:index])
                    del buffer[: index + 1]
                    message = _decode_message(line)
                    if "event" in message:
                        event = message["event"]
                        if event == "ready":
                            if ready:
                                raise _ContainerProtocolError("duplicate ready event")
                            ready = True
                            container_id = _container_id(cidfile, process)
                            evidence["container_id"] = container_id
                            inspection = self._inspect(container_id)
                            checks = _isolation_checks(inspection, limits)
                            checks["image_digest_matches"] = (
                                inspection.get("image_id") == evidence["image_digest"]
                            )
                            evidence["inspect"] = inspection
                            evidence["isolation_checks"] = checks
                            if not all(checks.values()):
                                raise _ContainerBoundaryError(checks)
                            _acknowledge(process)
                        elif event == "source_started":
                            source_started = True
                        elif event == "strategy_started":
                            strategy_started = True
                        else:
                            raise _ContainerProtocolError(
                                f"unknown protocol event {event!r}"
                            )
                    elif "ok" in message:
                        if final is not None:
                            raise _ContainerProtocolError("duplicate result")
                        if not isinstance(message["ok"], bool):
                            raise _ContainerProtocolError("invalid result flag")
                        final = message
                    else:
                        raise _ContainerProtocolError("unexpected output line")
            if process.poll() is None:
                try:
                    process.wait(timeout=max(0.1, deadline - time.monotonic()))
                except subprocess.TimeoutExpired as error:
                    raise _ContainerTimeout from error
            if buffer.strip():
                raise _ContainerProtocolError("truncated output")
            if writer_errors:
                raise _ContainerRuntimeError("input pipe failed")
            if not ready:
                raise _ContainerProtocolError("container never became ready")
            if final is None:
                raise _ContainerProtocolError("container returned no result")
        except _ContainerTimeout:
            failure = ("Container strategy aborted: timeout.", "timeout")
        except _ContainerOutputLimit:
            failure = ("Container strategy aborted: output_limit.", "output_limit")
        except _ContainerBoundaryError as error:
            failure = (
                "Container isolation boundary verification failed: "
                f"{error}.",
                "isolation_boundary_failed",
            )
        except _ContainerProtocolError as error:
            failure = (
                f"Container strategy returned no valid result: {error}.",
                "invalid_result",
            )
        except _ContainerRuntimeError as error:
            failure = (f"Container runtime failed: {error}.", "container_runtime_failed")
        except IsolationError as error:
            if error.evidence:
                evidence.update(error.evidence)
            failure = (str(error), "container_runtime_failed")
        finally:
            if process is not None:
                if process.poll() is None:
                    process.kill()
                    killed = True
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                identifier = container_id
                if killed and identifier is None:
                    identifier = _read_container_id(cidfile)
                if killed and identifier is not None:
                    self._remove_container(identifier)
            if selector is not None:
                selector.close()
            evidence["wall_seconds"] = round(time.monotonic() - started, 6)
            evidence["exit_code"] = process.returncode if process is not None else None
            evidence["output_bytes"] = len(stdout)
            evidence["output_sha256"] = hashlib.sha256(stdout).hexdigest()
            evidence["source_started"] = source_started
            evidence["strategy_started"] = strategy_started
        if failure is not None:
            message, reason = failure
            tail = stderr[-512:].decode("utf-8", "replace").strip()
            if tail:
                message = f"{message} Docker stderr: {tail}"
            evidence["reason"] = reason
            evidence["result"] = "error"
            raise IsolationError(message, evidence=evidence)
        assert final is not None
        return final

    def _inspect(self, container_id: str) -> dict[str, Any]:
        completed = self._run_docker(
            ["inspect", "--type", "container", container_id], timeout=15.0
        )
        if completed.returncode != 0:
            raise IsolationError(
                "Container inspection failed: "
                f"{completed.stderr.strip() or 'inspect returned nonzero'}."
            )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise IsolationError("Container inspection returned malformed JSON.") from error
        if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
            raise IsolationError("Container inspection returned an unexpected document.")
        return _inspect_summary(payload[0])

    def _remove_container(self, container_id: str) -> None:
        try:
            self._run_docker(["rm", "-f", container_id], timeout=10.0)
        except IsolationError:
            pass

    def _run_docker(
        self, arguments: list[str], *, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                [self._docker_path(), *arguments],
                capture_output=True,
                text=True,
                timeout=timeout,
                env=self._docker_env(),
                check=False,
            )
        except FileNotFoundError as error:
            raise IsolationError(
                "Container executor unavailable: Docker CLI was not found."
            ) from error
        except (OSError, subprocess.SubprocessError) as error:
            raise IsolationError(
                f"Container executor unavailable: {type(error).__name__}."
            ) from error

    def _docker_path(self) -> str:
        path = shutil.which(self._docker_binary)
        if path is None:
            raise IsolationError(
                "Container executor unavailable: Docker CLI was not found. "
                "Start Colima and set DOCKER_HOST (or PRAJNA_DOCKER_HOST)."
            )
        return path

    def _docker_env(self) -> dict[str, str]:
        names = (
            "PATH",
            "HOME",
            "TMPDIR",
            "DOCKER_CONFIG",
            "DOCKER_CERT_PATH",
            "DOCKER_TLS_VERIFY",
        )
        environment = {name: os.environ[name] for name in names if name in os.environ}
        if self._docker_host:
            environment["DOCKER_HOST"] = self._docker_host
        return environment


def executor_from_config(config: Mapping[str, Any]) -> ContainerStrategyExecutor:
    """Build the trusted executor frozen in a Campaign evaluation config."""
    if not isinstance(config, Mapping) or config.get("kind") != "container":
        raise ValueError("The Campaign does not configure a container executor.")
    image = config.get("image")
    digest = config.get("digest")
    limits = config.get("limits")
    if not isinstance(image, str) or not image.strip():
        raise ValueError("The Campaign container image is invalid.")
    if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
        raise ValueError("The Campaign container image digest is invalid.")
    if not isinstance(limits, Mapping):
        raise ValueError("The Campaign container limits are invalid.")
    try:
        parsed = ContainerLimits(
            wall_seconds=float(limits["wall_seconds"]),
            input_bytes=int(limits["input_bytes"]),
            output_bytes=int(limits["output_bytes"]),
            memory=str(limits["memory"]),
            cpus=str(limits["cpus"]),
            pids_limit=int(limits["pids_limit"]),
            tmpfs_size=str(limits["tmpfs_size"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"The Campaign container limits are invalid: {error}.") from error
    _validate_limits(parsed)
    return ContainerStrategyExecutor(image, limits=parsed, expected_digest=digest)


def _write_request(
    process: subprocess.Popen[bytes], request: bytes, errors: list[BaseException]
) -> None:
    try:
        assert process.stdin is not None
        process.stdin.write(request + b"\n")
        process.stdin.flush()
    except (OSError, ValueError) as error:
        errors.append(error)


def _acknowledge(process: subprocess.Popen[bytes]) -> None:
    try:
        assert process.stdin is not None
        process.stdin.write(b'{"ack":true}\n')
        process.stdin.flush()
        process.stdin.close()
    except (OSError, ValueError) as error:
        raise _ContainerRuntimeError("cannot acknowledge container inspection") from error


def _drain_stream(stream: Any, target: bytearray) -> None:
    try:
        while True:
            chunk = stream.read(4096)
            if not chunk:
                break
            if len(target) < 8192:
                target.extend(chunk[: 8192 - len(target)])
    except (OSError, ValueError):
        pass


def _decode_message(line: bytes) -> dict[str, Any]:
    try:
        message = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise _ContainerProtocolError("non-JSON output") from error
    if not isinstance(message, dict):
        raise _ContainerProtocolError("output is not a JSON object")
    return message


def _container_id(cidfile: Path, process: subprocess.Popen[bytes]) -> str:
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        identifier = _read_container_id(cidfile)
        if identifier is not None:
            return identifier
        if process.poll() is not None:
            break
        time.sleep(0.01)
    raise _ContainerProtocolError("container id is unavailable")


def _read_container_id(cidfile: Path) -> str | None:
    try:
        value = cidfile.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        return None
    if _CONTAINER_ID.fullmatch(value) is None:
        return None
    return value


def _isolation_checks(
    inspection: Mapping[str, Any], limits: ContainerLimits
) -> dict[str, bool]:
    security = [str(option) for option in inspection.get("security_opt") or []]
    tmpfs = inspection.get("tmpfs") or {}
    tmpfs_value = str(tmpfs.get("/tmp", "")) if isinstance(tmpfs, Mapping) else ""
    return {
        "no_mounts": inspection.get("mount_count") == 0,
        "no_binds": inspection.get("bind_count") == 0,
        "network_none": inspection.get("network_mode") == "none",
        "no_published_ports": inspection.get("port_count") == 0,
        "root_read_only": inspection.get("read_only") is True,
        "non_root_user": inspection.get("user") == "65534:65534",
        "all_capabilities_dropped": "ALL" in (inspection.get("cap_drop") or []),
        "no_added_capabilities": not inspection.get("cap_add"),
        "no_new_privileges": any(
            option.split(":")[0] == _SECURITY_OPTION for option in security
        ),
        "not_privileged": inspection.get("privileged") is False,
        "memory_limited": inspection.get("memory_bytes") == _memory_bytes(limits.memory),
        "pids_limited": inspection.get("pids_limit") == limits.pids_limit,
        "cpu_limited": inspection.get("nano_cpus") == _nano_cpus(limits.cpus),
        "tmpfs_limited": (
            "/tmp" in tmpfs
            and f"size={limits.tmpfs_size}" in tmpfs_value.replace(" ", "")
        ),
    }


def _inspect_summary(payload: Mapping[str, Any]) -> dict[str, Any]:
    host = payload.get("HostConfig") or {}
    config = payload.get("Config") or {}
    mounts = payload.get("Mounts") or []
    binds = host.get("Binds") or []
    ports = host.get("PortBindings") or {}
    environment = config.get("Env") or []
    return {
        "container_id": payload.get("Id"),
        "image_id": payload.get("Image"),
        "mount_count": len(mounts),
        "mounts": [
            {
                "type": mount.get("Type"),
                "destination": mount.get("Destination"),
                "read_only": mount.get("RW") is False,
            }
            for mount in mounts
            if isinstance(mount, Mapping)
        ],
        "bind_count": len(binds),
        "network_mode": host.get("NetworkMode"),
        "port_count": len(ports),
        "read_only": host.get("ReadonlyRootfs"),
        "user": config.get("User"),
        "cap_drop": list(host.get("CapDrop") or []),
        "cap_add": list(host.get("CapAdd") or []),
        "security_opt": list(host.get("SecurityOpt") or []),
        "privileged": host.get("Privileged"),
        "memory_bytes": host.get("Memory"),
        "memory_swap": host.get("MemorySwap"),
        "pids_limit": host.get("PidsLimit"),
        "nano_cpus": host.get("NanoCpus"),
        "cpu_quota": host.get("CpuQuota"),
        "tmpfs": dict(host.get("Tmpfs") or {}),
        "pid_mode": host.get("PidMode"),
        "ipc_mode": host.get("IpcMode"),
        "environment_names": sorted(
            str(item).split("=", 1)[0] for item in environment
        ),
    }


def _validate_container_weights(raw: Any) -> dict[str, float]:
    if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
        raise ValueError("weights must be an object with string asset names")
    for key, value in raw.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"weight for {key} must be numeric")
        if not math.isfinite(float(value)):
            raise ValueError(f"weight for {key} must be finite")
    return _validate_weights(raw)


def _history_records(history: Any) -> list[dict[str, Any]]:
    try:
        rows = history.to_dict(orient="records")
    except AttributeError as error:
        raise IsolationError("Container strategy history is not tabular.") from error
    records: list[dict[str, Any]] = []
    for row in rows:
        records.append({str(key): _normalize_value(value) for key, value in row.items()})
    return records


def _normalize_value(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


__all__ = [
    "DEFAULT_CONTAINER_LIMITS",
    "ETF_SYMBOLS",
    "ContainerLimits",
    "ContainerStrategyExecutor",
    "ExecutionResult",
    "ImageIdentity",
    "executor_from_config",
]
