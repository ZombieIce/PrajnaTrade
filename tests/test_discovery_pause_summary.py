import copy

import pytest

from prajnatrade.discovery_summary import summarize_signal_pauses


def test_insufficient_history_is_part_of_frozen_decision_calendar() -> None:
    report = summarize_signal_pauses({
        "start_date": "2024-01-02",
        "end_date": "2024-01-04",
        "decision_records": [
            {"date": "2024-01-02", "signal_status": "insufficient_history"},
            {"date": "2024-01-03", "signal_status": "no_target"},
            {"date": "2024-01-04", "signal_status": "generated"},
        ],
    })
    assert report["decision_days"] == 3
    assert report["decision_dates"] == ["2024-01-02", "2024-01-03", "2024-01-04"]


@pytest.mark.parametrize("defect", ["date", "symbols", "reason", "evidence"])
def test_malformed_pause_metadata_cannot_be_verified(defect: str) -> None:
    day = "2024-01-02"
    pause = {
        "date": day,
        "symbols": ["ETF"],
        "reasons": [{"symbol": "ETF", "reason": "derived_after_cutoff"}],
        "evidence": [{
            "symbol": "ETF",
            "reason": "derived_after_cutoff",
            "source": "derived",
            "evidence_available_at": f"{day}T16:00:00+08:00",
            "availability_evidence": "synthetic",
        }],
    }
    if defect == "date":
        pause["date"] = "2024-01-03"
    elif defect == "symbols":
        pause["symbols"] = ["OTHER"]
    elif defect == "reason":
        pause["reasons"].append("invalid")
    else:
        pause["evidence"].append(copy.deepcopy(pause["evidence"][0]))
    run = {
        "start_date": day,
        "end_date": day,
        "decision_records": [{
            "date": day, "signal_status": "paused",
            "signal_cutoff": f"{day}T15:30:00+08:00", "signal_pause": pause,
        }],
        "signal_gaps": [copy.deepcopy(pause)],
        "equity_curve": [{
            "date": day, "cash": 100.0, "market_value": 0.0,
            "dividend_receivables": 0.0, "nav": 100.0, "holdings": {},
        }],
    }
    report = summarize_signal_pauses(run)
    assert report["status"] == "insufficient_evidence"
    assert report["events"][0]["rule_compliant"] is False
