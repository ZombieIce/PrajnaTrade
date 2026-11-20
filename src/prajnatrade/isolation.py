from __future__ import annotations

import json
import math
import hashlib
import os
import platform
import selectors
import socket
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
import uuid
import ctypes.util
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn


class IsolationError(ValueError):
    """Raised when an isolated strategy cannot safely complete."""

    def __init__(self, message: str, *, evidence: dict[str, Any] | None = None) -> None:
        self.evidence = evidence
        if evidence is not None:
            message = f"{message} evidence={json.dumps(evidence, sort_keys=True, separators=(',', ':'))}"
        super().__init__(message)


@dataclass(frozen=True)
class IsolationLimits:
    wall_seconds: float = 10.0
    cpu_seconds: int = 5
    address_space_bytes: int = 2 * 1024**3
    input_bytes: int = 32 * 1024**2
    output_bytes: int = 64 * 1024


DEFAULT_ISOLATION_LIMITS = IsolationLimits()


_WORKER = r"""
import contextlib
import ctypes
import errno
import io
import json
import math
import os
import resource
import socket
import subprocess
import sys

sys.dont_write_bytecode = True


class BoundedOutput(io.TextIOBase):
    def __init__(self, limit):
        self.limit = limit
        self.size = 0
        self.exceeded = False

    def write(self, text):
        self.size += len(text.encode("utf-8", errors="replace"))
        if self.size > self.limit:
            self.exceeded = True
        return len(text)

    def flush(self):
        pass


def emit(value):
    sys.__stdout__.write(json.dumps(value, separators=(",", ":"), allow_nan=False) + "\n")
    sys.__stdout__.flush()


def denied(operation):
    try:
        operation()
    except OSError as error:
        return error.errno in (errno.EACCES, errno.EPERM)
    return False


def main():
    request = json.loads(sys.stdin.buffer.readline())
    canary = request["canary"]
    port = request["port"]
    source = request["source"]
    parameters = request["parameters"]
    symbols = tuple(request["symbols"])
    limits = request["limits"]
    protocol_token = request["protocol_token"]

    required = ("RLIMIT_CPU", "RLIMIT_AS", "RLIMIT_FSIZE", "RLIMIT_CORE", "RLIMIT_NOFILE")
    if any(not hasattr(resource, name) for name in required):
        emit({"ok": False, "reason": "resource_limits_unavailable"})
        return

    def set_hard_limit(limit, maximum):
        soft, hard = resource.getrlimit(limit)
        hard_cap = maximum if hard == resource.RLIM_INFINITY else min(maximum, hard)
        soft_cap = (
            hard_cap if soft == resource.RLIM_INFINITY else min(soft, hard_cap)
        )
        resource.setrlimit(limit, (soft_cap, hard_cap))

    try:
        set_hard_limit(resource.RLIMIT_CPU, limits["cpu_seconds"])
        set_hard_limit(resource.RLIMIT_FSIZE, 0)
        set_hard_limit(resource.RLIMIT_CORE, 0)
        set_hard_limit(resource.RLIMIT_NOFILE, 32)
    except (OSError, ValueError):
        emit({"ok": False, "reason": "resource_limits_unavailable"})
        return

    try:
        sandbox = ctypes.CDLL(request["sandbox_library"])
        sandbox_init = sandbox.sandbox_init
        sandbox_init.argtypes = [
            ctypes.c_char_p,
            ctypes.c_uint,
            ctypes.POINTER(ctypes.c_char_p),
        ]
        sandbox_init.restype = ctypes.c_int
        error = ctypes.c_char_p()
        if sandbox_init(
            request["sandbox_profile"].encode("utf-8"), 0, ctypes.byref(error)
        ) != 0:
            emit({"ok": False, "reason": "sandbox_install_failed"})
            return
    except (OSError, AttributeError):
        emit({"ok": False, "reason": "sandbox_install_failed"})
        return

    file_denied = denied(lambda: open(canary, "rb").read(1))
    write_denied = denied(lambda: open(canary, "wb").close())

    def connect():
        with socket.socket() as client:
            client.connect(("127.0.0.1", port))

    network_denied = denied(connect)

    def spawn():
        child = subprocess.Popen(
            ["/usr/bin/true"], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        child.wait(timeout=1)

    process_denied = denied(spawn)
    evidence = {
        "protected_read_denied": file_denied,
        "protected_write_denied": write_denied,
        "network_denied": network_denied,
        "process_creation_denied": process_denied,
        "strategy_started": False,
        "sandbox_preflight_verified": False,
    }
    if not all(evidence[key] for key in (
        "protected_read_denied", "protected_write_denied", "network_denied",
        "process_creation_denied",
    )):
        emit({"ok": False, "reason": "sandbox_preflight_failed", "evidence": evidence})
        return
    try:
        set_hard_limit(resource.RLIMIT_AS, limits["address_space_bytes"])
    except (OSError, ValueError):
        emit({"ok": False, "reason": "memory_limit_unavailable", "evidence": evidence})
        return
    try:
        import pandas as pd

        history = pd.DataFrame(request["history"])
        if "date" in history:
            history["date"] = pd.to_datetime(history["date"])
    except MemoryError:
        emit({"ok": False, "reason": "memory_limit", "evidence": evidence})
        return
    except BaseException:
        emit({"ok": False, "reason": "worker_setup_failed", "evidence": evidence})
        return
    status = {
        "protected_read_denied": file_denied,
        "protected_write_denied": write_denied,
        "network_denied": network_denied,
        "process_creation_denied": process_denied,
        "strategy_started": False,
    }
    sys.__stdout__.buffer.write(
        b"\x00PRAJNATRADE_PREFLIGHT:"
        + protocol_token.encode("ascii")
        + b":"
        + json.dumps(status, separators=(",", ":")).encode("ascii")
        + b"\n"
    )
    sys.__stdout__.flush()
    namespace = {
        "ETF_SYMBOLS": symbols,
        "CASH": "CASH",
        "parameters": parameters,
    }
    try:
        evidence["strategy_started"] = True
        sys.__stdout__.buffer.write(
            b"\x00PRAJNATRADE_STARTED:" + protocol_token.encode("ascii") + b"\n"
        )
        sys.__stdout__.flush()
        exec(compile(source, "<generated-strategy>", "exec"), namespace)
        strategy = namespace.get("strategy")
        if not callable(strategy):
            emit({"ok": False, "reason": "strategy_entrypoint_missing", "evidence": evidence})
            return
        output = BoundedOutput(limits["output_bytes"])
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            result = strategy(history, request["state"])
        evidence["strategy_output_bytes"] = output.size
        if output.exceeded:
            emit({"ok": False, "reason": "output_limit", "evidence": evidence})
            return
        if result is None:
            weights = None
        elif isinstance(result, dict):
            weights = {str(key): float(value) for key, value in result.items()}
        else:
            emit({"ok": False, "reason": "invalid_strategy_output", "evidence": evidence})
            return
        emit({"ok": True, "weights": weights})
    except KeyboardInterrupt:
        emit({"ok": False, "reason": "strategy_interrupted", "evidence": evidence})
    except MemoryError:
        emit({"ok": False, "reason": "memory_limit", "evidence": evidence})
    except BaseException:
        emit({"ok": False, "reason": "strategy_failed", "evidence": evidence})


main()
"""


def run_isolated_strategy(
    source: str,
    parameters: dict[str, Any],
    history: Any,
    state: dict[str, Any],
    *,
    symbols: tuple[str, ...],
    limits: IsolationLimits | None = None,
    evidence_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, float] | None:
    limits = limits or DEFAULT_ISOLATION_LIMITS
    if limits.wall_seconds <= 0 or limits.cpu_seconds <= 0:
        raise ValueError("Isolation time limits must be greater than zero.")
    if min(limits.address_space_bytes, limits.input_bytes, limits.output_bytes) <= 0:
        raise ValueError("Isolation memory and I/O limits must be greater than zero.")

    evidence = {
        "run_id": str(uuid.uuid4()),
        "runtime": "macOS sandbox_init" if sys.platform == "darwin" else "unavailable",
        "os_version": platform.mac_ver()[0] or platform.system(),
        "python_version": platform.python_version(),
        "uid": os.geteuid() if hasattr(os, "geteuid") else None,
        "profile_sha256": None,
        "limits": {
            "wall_seconds": limits.wall_seconds,
            "cpu_seconds": limits.cpu_seconds,
            "address_space_bytes": limits.address_space_bytes,
            "input_bytes": limits.input_bytes,
            "output_bytes": limits.output_bytes,
        },
        "strategy_started": False,
        "opened_protected_input": False,
        "output_destination": "unavailable",
        "input_bytes": 0,
        "output_bytes": 0,
        "strategy_output_bytes": None,
        "wall_seconds_used": None,
        "cpu_seconds_used": None,
        "peak_rss_bytes": None,
    }

    def reject(message: str) -> NoReturn:
        if evidence_callback is not None:
            evidence_callback(dict(evidence))
        raise IsolationError(message, evidence=evidence)

    if sys.platform != "darwin":
        reject("OS isolation unavailable: only the tested macOS boundary is supported.")
    sandbox_library = ctypes.util.find_library("sandbox")
    if not sandbox_library:
        reject("OS isolation unavailable: macOS sandbox API was not found.")

    try:
        profile = _sandbox_profile()
    except (OSError, ValueError):
        reject("OS isolation unavailable: sandbox profile could not be built.")
    evidence["profile_sha256"] = hashlib.sha256(profile.encode("utf-8")).hexdigest()
    evidence["output_destination"] = "trusted controller pipe"
    protocol_token = uuid.uuid4().hex

    with tempfile.TemporaryDirectory(prefix="prajnatrade-isolation-") as directory:
        canary = Path(directory) / "protected-input"
        canary.write_text("synthetic isolation canary", encoding="utf-8")
        listener = socket.socket()
        try:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            listener.setblocking(False)
            request = {
                "source": source,
                "parameters": parameters,
                "symbols": symbols,
                "history": _records(history),
                "state": state,
                "canary": str(canary),
                "port": listener.getsockname()[1],
                "protocol_token": protocol_token,
                "limits": {
                    "cpu_seconds": limits.cpu_seconds,
                    "address_space_bytes": limits.address_space_bytes,
                    "output_bytes": limits.output_bytes,
                },
            }
            request["sandbox_profile"] = profile
            request["sandbox_library"] = sandbox_library
            encoded = json.dumps(request, separators=(",", ":"), allow_nan=False).encode()
            if len(encoded) > limits.input_bytes:
                raise IsolationError("Strategy input exceeded the configured isolation limit.")
            evidence["input_bytes"] = len(encoded)
            command = [sys.executable, "-I", "-c", _WORKER]
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                cwd="/",
                env={
                    "HOME": "/nonexistent",
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "OPENBLAS_NUM_THREADS": "1",
                    "OMP_NUM_THREADS": "1",
                },
                close_fds=True,
                bufsize=0,
            )
            response = _exchange(process, encoded + b"\n", limits, evidence, protocol_token)
        except IsolationError:
            if evidence_callback is not None:
                evidence_callback(dict(evidence))
            raise
        except (OSError, subprocess.SubprocessError) as error:
            isolation_error = IsolationError(
                "OS-isolated strategy could not start or complete.", evidence=evidence
            )
            if evidence_callback is not None:
                evidence_callback(dict(evidence))
            raise isolation_error from error
        finally:
            listener.close()

    try:
        result = json.loads(response)
    except (json.JSONDecodeError, UnboundLocalError) as error:
        isolation_error = IsolationError(
            "OS-isolated strategy returned no valid result.", evidence=evidence
        )
        if evidence_callback is not None:
            evidence_callback(dict(evidence))
        raise isolation_error from error
    if not isinstance(result, dict):
        isolation_error = IsolationError(
            "OS-isolated strategy returned no valid result.", evidence=evidence
        )
        if evidence_callback is not None:
            evidence_callback(dict(evidence))
        raise isolation_error
    if not evidence["strategy_started"]:
        evidence.update(result.get("evidence") or {})
    elif not evidence["sandbox_preflight_verified"]:
        isolation_error = IsolationError(
            "OS-isolated strategy started without verified preflight evidence.",
            evidence=evidence,
        )
        if evidence_callback is not None:
            evidence_callback(dict(evidence))
        raise isolation_error
    if not result.get("ok"):
        reason = result.get("reason", "sandbox_runtime_failed")
        if reason == "strategy_interrupted":
            if evidence_callback is not None:
                evidence_callback(dict(evidence))
            raise KeyboardInterrupt
        if evidence_callback is not None:
            evidence_callback(dict(evidence))
        raise IsolationError(
            f"OS-isolated strategy aborted: {reason}.",
            evidence=evidence,
        )
    if evidence_callback is not None:
        evidence_callback(dict(evidence))
    return result["weights"]


def probe_isolation(limits: IsolationLimits | None = None) -> None:
    limits = limits or DEFAULT_ISOLATION_LIMITS
    import pandas as pd

    from prajnatrade.data import ETF_SYMBOLS

    run_isolated_strategy(
        "def strategy(history, state):\n    return {CASH: 1.0}\n",
        {},
        pd.DataFrame(columns=("date", "symbol", "close")),
        {},
        symbols=ETF_SYMBOLS,
        limits=limits,
    )


def _records(history: Any) -> list[dict[str, Any]]:
    records = history.to_dict(orient="records")
    normalized = []
    for row in records:
        normalized.append(
            {
                key: _normalize_value(value) for key, value in row.items()
            }
        )
    return normalized


def _normalize_value(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _sandbox_profile() -> str:
    paths = sysconfig.get_paths()
    read_roots = {
        Path("/System/Library"),
        Path("/usr/lib"),
    }
    for key in ("stdlib", "platstdlib", "purelib", "platlib"):
        value = paths.get(key)
        if value:
            original = Path(value).absolute()
            candidate = original.resolve()
            if candidate.exists() and candidate.is_dir():
                read_roots.add(candidate)
            if original.exists() and original.is_dir():
                read_roots.add(original)
    rules = [
        "(version 1)",
        "(deny default)",
        "(deny network*)",
        "(deny file-write*)",
        "(deny process-fork)",
        "(deny process-exec)",
        "(allow system-info)",
        "(allow sysctl-read)",
        "(allow file-write-data (literal \"/dev/fd/1\"))",
    ]
    rules.extend(
        f"(allow file-read* (subpath {_profile_string(str(root))}))"
        for root in sorted(read_roots)
    )
    return "".join(rules)


def _profile_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _exchange(
    process: subprocess.Popen[bytes],
    request: bytes,
    limits: IsolationLimits,
    evidence: dict[str, Any],
    protocol_token: str,
) -> bytes:
    assert process.stdin is not None
    assert process.stdout is not None
    write_error: list[OSError] = []

    def write_request() -> None:
        try:
            remaining = memoryview(request)
            while remaining:
                written = process.stdin.write(remaining)
                if not written:
                    raise OSError("input pipe closed")
                remaining = remaining[written:]
            process.stdin.close()
        except OSError as error:
            write_error.append(error)

    writer = threading.Thread(target=write_request, daemon=True)
    writer.start()
    output = bytearray()
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    preflight_prefix = f"\x00PRAJNATRADE_PREFLIGHT:{protocol_token}:".encode("ascii")
    started_marker = f"\x00PRAJNATRADE_STARTED:{protocol_token}\n".encode("ascii")
    preflight_frame = b""
    started = time.monotonic()
    deadline = started + limits.wall_seconds
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                process.kill()
                preflight_frame = _record_protocol(output, evidence, preflight_prefix, started_marker)
                evidence["wall_seconds_used"] = time.monotonic() - started
                raise IsolationError("OS-isolated strategy aborted: timeout.", evidence=evidence)
            events = selector.select(min(remaining, 0.1))
            if not events:
                if process.poll() is not None:
                    break
                continue
            chunk = os.read(process.stdout.fileno(), 4096)
            if not chunk:
                break
            output.extend(chunk)
            preflight_frame = _record_protocol(
                output, evidence, preflight_prefix, started_marker, preflight_frame
            )
            if len(output) > limits.output_bytes:
                process.kill()
                evidence["wall_seconds_used"] = time.monotonic() - started
                raise IsolationError(
                    "OS-isolated strategy aborted: output_limit.", evidence=evidence
                )
        return_code = process.wait(timeout=max(0.1, deadline - time.monotonic()))
        if return_code != 0:
            raise IsolationError(
                "OS-isolated strategy aborted: sandbox_runtime_failed.", evidence=evidence
            )
        if write_error:
            raise IsolationError(
                "OS-isolated strategy aborted: input_pipe_failed.", evidence=evidence
            )
        evidence["wall_seconds_used"] = time.monotonic() - started
        clean_output = bytes(output).replace(started_marker, b"", 1)
        if preflight_frame:
            clean_output = clean_output.replace(preflight_frame, b"", 1)
        return clean_output.strip()
    except subprocess.TimeoutExpired as error:
        process.kill()
        preflight_frame = _record_protocol(
            output, evidence, preflight_prefix, started_marker, preflight_frame
        )
        evidence["wall_seconds_used"] = time.monotonic() - started
        raise IsolationError(
            "OS-isolated strategy aborted: timeout.", evidence=evidence
        ) from error
    finally:
        selector.close()
        writer.join(timeout=1)
        if process.poll() is None:
            process.kill()
            process.wait()


def _record_protocol(
    output: bytearray,
    evidence: dict[str, Any],
    preflight_prefix: bytes,
    started_marker: bytes,
    preflight_frame: bytes = b"",
) -> bytes:
    evidence["output_bytes"] = len(output)
    if not preflight_frame:
        start = output.find(preflight_prefix)
        end = output.find(b"\n", start) if start >= 0 else -1
        if end >= 0:
            preflight_frame = bytes(output[start : end + 1])
            try:
                status = json.loads(preflight_frame[len(preflight_prefix) : -1])
            except json.JSONDecodeError:
                status = None
            if isinstance(status, dict) and all(
                status.get(key) is True
                for key in (
                    "protected_read_denied",
                    "protected_write_denied",
                    "network_denied",
                    "process_creation_denied",
                )
            ):
                evidence.update(status)
                evidence["sandbox_preflight_verified"] = True
    if started_marker in output:
        evidence["strategy_started"] = True
    return preflight_frame
