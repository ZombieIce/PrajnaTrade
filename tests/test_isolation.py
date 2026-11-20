from __future__ import annotations

import socket
import subprocess
from pathlib import Path

import pandas as pd
import pytest

from prajnatrade.isolation import IsolationError, IsolationLimits
from prajnatrade.strategies import make_strategy


def _strategy(source: str) -> dict[str, object]:
    return {"family": "python", "parameters": {}, "source": source}


def _history() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "date": pd.Timestamp("2024-01-02"),
                "symbol": "511260",
                "open": 10.0,
                "high": 10.0,
                "low": 10.0,
                "close": 10.0,
                "volume": 1000,
                "price_basis": "total_return",
            }
        ]
    )


def _sandbox_test_limits(**overrides: object) -> IsolationLimits:
    values = {
        "wall_seconds": 10.0,
        "cpu_seconds": 5,
        "address_space_bytes": 1_000_000_000_000_000,
        "input_bytes": 1024 * 1024,
        "output_bytes": 64 * 1024,
    }
    values.update(overrides)
    return IsolationLimits(**values)


def test_isolated_python_cannot_read_a_protected_synthetic_file(
    tmp_path: Path,
) -> None:
    protected = tmp_path / "synthetic-protected-input.txt"
    protected.write_text("synthetic-secret", encoding="utf-8")
    strategy = make_strategy(
        _strategy(
            "def strategy(history, state):\n"
            "    try:\n"
            f"        open({str(protected)!r}, encoding='utf-8').read()\n"
            "    except OSError:\n"
            "        return {CASH: 1.0}\n"
            "    return {'511260': 1.0}\n"
        ),
        isolated_python=True,
        isolation_limits=_sandbox_test_limits(),
    )

    assert strategy(_history(), {}) == {"CASH": 1.0}


def test_isolated_python_cannot_connect_to_a_synthetic_listener() -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(0.2)
    port = listener.getsockname()[1]
    strategy = make_strategy(
        _strategy(
            "def strategy(history, state):\n"
            "    import socket\n"
            "    try:\n"
            "        with socket.socket() as client:\n"
            f"            client.connect(('127.0.0.1', {port}))\n"
            "    except OSError:\n"
            "        return {CASH: 1.0}\n"
            "    return {'511260': 1.0}\n"
            "    return {CASH: 1.0}\n"
        ),
        isolated_python=True,
        isolation_limits=_sandbox_test_limits(),
    )

    try:
        assert strategy(_history(), {}) == {"CASH": 1.0}
        with pytest.raises(TimeoutError):
            listener.accept()
    finally:
        listener.close()


def test_isolated_python_cannot_create_a_process_and_records_success_evidence() -> None:
    evidence: list[dict[str, object]] = []
    strategy = make_strategy(
        _strategy(
            "import subprocess\n"
            "def strategy(history, state):\n"
            "    try:\n"
            "        subprocess.Popen(['/usr/bin/true'])\n"
            "    except OSError:\n"
            "        return {CASH: 1.0}\n"
            "    return {'511260': 1.0}\n"
        ),
        isolated_python=True,
        isolation_limits=_sandbox_test_limits(),
        evidence_callback=evidence.append,
    )

    assert strategy(_history(), {}) == {"CASH": 1.0}
    assert evidence[0]["strategy_started"] is True
    assert evidence[0]["protected_read_denied"] is True
    assert evidence[0]["protected_write_denied"] is True
    assert evidence[0]["network_denied"] is True
    assert evidence[0]["process_creation_denied"] is True
    assert evidence[0]["opened_protected_input"] is False
    assert evidence[0]["output_destination"] == "trusted controller pipe"
    assert evidence[0]["uid"] >= 0
    assert evidence[0]["profile_sha256"]
    assert evidence[0]["sandbox_preflight_verified"] is True
    assert evidence[0]["input_bytes"] > 0
    assert evidence[0]["output_bytes"] > 0
    assert evidence[0]["wall_seconds_used"] >= 0
    assert evidence[0]["cpu_seconds_used"] is None
    assert evidence[0]["peak_rss_bytes"] is None


def test_unenforceable_memory_limit_rejects_before_strategy_starts() -> None:
    evidence: list[dict[str, object]] = []
    strategy = make_strategy(
        _strategy(
            "def strategy(history, state):\n"
            "    return {'511260': 1.0}\n"
        ),
        isolated_python=True,
        isolation_limits=_sandbox_test_limits(address_space_bytes=1),
        evidence_callback=evidence.append,
    )

    with pytest.raises(IsolationError, match="memory_limit") as error:
        strategy(_history(), {})

    assert error.value.evidence is not None
    assert error.value.evidence["strategy_started"] is False
    assert error.value.evidence["opened_protected_input"] is False
    assert evidence[0]["strategy_started"] is False


def test_strategy_output_limit_aborts_with_execution_evidence() -> None:
    evidence: list[dict[str, object]] = []
    strategy = make_strategy(
        _strategy(
            "def strategy(history, state):\n"
            "    print('synthetic output' * 200)\n"
            "    return {CASH: 1.0}\n"
        ),
        isolated_python=True,
        isolation_limits=_sandbox_test_limits(output_bytes=1024),
        evidence_callback=evidence.append,
    )

    with pytest.raises(IsolationError, match="output_limit") as error:
        strategy(_history(), {})

    assert error.value.evidence is not None
    assert error.value.evidence["strategy_started"] is True
    assert evidence[0]["output_destination"] == "trusted controller pipe"
    assert evidence[0]["output_bytes"] > 0


def test_strategy_timeout_aborts_without_returning_a_score() -> None:
    evidence: list[dict[str, object]] = []
    strategy = make_strategy(
        _strategy(
            "import time\n"
            "def strategy(history, state):\n"
            "    time.sleep(10)\n"
            "    return {CASH: 1.0}\n"
        ),
        isolated_python=True,
        isolation_limits=_sandbox_test_limits(wall_seconds=0.25),
        evidence_callback=evidence.append,
    )

    with pytest.raises(IsolationError, match="timeout") as error:
        strategy(_history(), {})
    assert error.value.evidence is not None
    assert evidence[0]["output_destination"] == "trusted controller pipe"
