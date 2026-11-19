from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from prajnatrade.store import SQLiteStore


def write_campaign_report(
    database: str | Path, campaign_id: str, output: str | Path
) -> dict[str, Any]:
    store = SQLiteStore(database)
    campaign = store.get_campaign(campaign_id)
    experiments = store.list_experiments(campaign_id)
    candidates = store.list_candidates(campaign_id)
    holdout_receipt = store.get_holdout_receipt(campaign_id)
    provider_tokens = sum(
        int(item["provider_usage"].get("total_tokens", 0)) for item in experiments
    )
    provider_cost = sum(
        float(item["provider_usage"].get("estimated_cost_usd", 0.0))
        for item in experiments
    )
    failed_attempts = sum(item["status"] == "failed" for item in experiments)
    provenance = campaign["baseline"].get("data_provenance")
    evaluations_by_candidate = {
        candidate["candidate_id"]: store.list_evaluations(candidate["candidate_id"])
        for candidate in candidates
    }
    sections = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>PrajnaTrade research report — {html.escape(campaign_id)}</title>",
        _style(),
        "</head><body>",
        "<main>",
        "<header><h1>PrajnaTrade research report</h1>",
        f"<p class=\"badge\">{html.escape(campaign['data_kind'].upper())} DATA</p>",
        "<p>Synthetic runs are engineering evidence only, not market performance.</p>",
        "</header>",
        "<section><h2>Campaign</h2>",
        _definition_list(
            {
                "Objective": campaign["objective"],
                "Campaign": campaign_id,
                "Status": campaign["status"],
                "Stop reason": campaign["stop_reason"] or "—",
                "Provider": f"{campaign['provider']} / {campaign['provider_model']}",
                "Data fingerprint": campaign["data_hash"],
                "Data source path": campaign["data_path"],
                "Data date range": (
                    f"{provenance['start_date']} — {provenance['end_date']}"
                    if provenance else "Unavailable for this older campaign"
                ),
                "Data rows": (
                    str(provenance["rows"]) if provenance
                    else "Unavailable for this older campaign"
                ),
                "Price basis": campaign["price_basis"],
                "Universe": "511260, 518880, 515080, 513300, 159612, CASH",
                "Total research attempts": str(len(experiments)),
                "Failed attempts": str(failed_attempts),
                "Provider tokens": str(provider_tokens),
                "Estimated provider cost": f"${provider_cost:.6f}",
                "Initial cash": _format(campaign["initial_cash"]),
                "Discovery interval": (
                    f"{campaign['split']['discovery_start']} — "
                    f"{campaign['split']['discovery_end']}"
                ),
                "Validation interval": (
                    f"{campaign['split']['validation_start']} — "
                    f"{campaign['split']['validation_end']}"
                ),
                "Locked holdout interval": (
                    f"{campaign['split']['holdout_start']} — "
                    f"{campaign['split']['holdout_end']}"
                ),
                "Holdout status": (
                    holdout_receipt["status"] if holdout_receipt else "locked"
                ),
            }
        ),
        "</section>",
        "<section><h2>Equal-weight buy-and-hold baseline</h2>",
        _metrics_table(campaign["baseline"]["metrics"]),
        _equity_and_drawdown(
            campaign["baseline"]["equity_curve"], "Discovery baseline"
        ),
        "</section>",
        "<section><h2>Experiment history</h2>",
        _experiment_table(experiments),
        "</section>",
        "<section><h2>Frozen candidates and independent evaluations</h2>",
        _candidate_sections(candidates, evaluations_by_candidate),
        "</section>",
        "<section><h2>Limitations and next steps</h2>",
        "<ul><li>Discovery results are exploratory and selected from multiple attempts.</li>",
        "<li>Validation uses a separate interval; the final holdout is one-use and requires explicit confirmation.</li>",
        "<li>Mock provider runs test orchestration, not AI novelty. Synthetic data is never market evidence.</li>",
        "<li>Generated Python requires explicit trusted-local opt-in and is not sandboxed; "
        "it can access files, credentials, network, and holdout data.</li>",
        "<li>Historical backtests do not establish future profitability.</li></ul>",
        "</section>",
        "</main></body></html>",
    ]
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(sections), encoding="utf-8")
    return {
        "campaign_id": campaign_id,
        "output": str(target),
        "experiments": len(experiments),
        "candidates": len(candidates),
        "data_kind": campaign["data_kind"],
    }


def _style() -> str:
    return (
        "<style>body{font:16px/1.5 system-ui,sans-serif;background:#f5f7fa;color:#17212b;"
        "margin:0}main{max-width:1100px;margin:2rem auto;padding:0 1.25rem}section,header"
        "{background:#fff;border:1px solid #dce3ea;border-radius:10px;padding:1.2rem;margin:1rem 0}"
        "h1,h2{line-height:1.2}table{width:100%;border-collapse:collapse;font-size:.92rem}"
        "th,td{text-align:left;padding:.55rem;border-bottom:1px solid #e6ebf0;vertical-align:top}"
        "th{background:#f0f4f8}.badge{display:inline-block;background:#fff0c2;padding:.2rem .65rem;"
        "border-radius:1rem;font-weight:700}.error{color:#9e2525}.muted{color:#607080}"
        "pre{white-space:pre-wrap;overflow-wrap:anywhere}svg{width:100%;height:auto}"
        ".metric-grid{display:grid;grid-template-columns:repeat(auto-fit,"
        "minmax(190px,1fr));gap:.8rem}.metric{background:#f4f7fa;padding:.7rem;border-radius:6px}"
        "</style>"
    )


def _definition_list(values: dict[str, str]) -> str:
    items = "".join(
        f"<dt>{html.escape(label)}</dt><dd>{html.escape(value)}</dd>"
        for label, value in values.items()
    )
    return f"<dl>{items}</dl>"


def _metrics_table(metrics: dict[str, Any]) -> str:
    keys = (
        ("Total return", "total_return"),
        ("CAGR", "cagr"),
        ("Annualized volatility", "annualized_volatility"),
        ("Sharpe", "sharpe"),
        ("Calmar", "calmar"),
        ("Max drawdown", "max_drawdown"),
        ("Turnover", "turnover"),
    )
    cells = "".join(
        f'<div class="metric"><strong>{html.escape(label)}</strong><br>{_format(metrics.get(key))}</div>'
        for label, key in keys
    )
    return (
        f'<div class="metric-grid">{cells}</div>'
        f"<p>Fees: {_format(metrics['transaction_costs']['fees'])}; "
        f"slippage: {_format(metrics['transaction_costs']['slippage'])}; "
        f"total transaction costs: {_format(metrics['transaction_costs']['total'])}</p>"
        f"<p>Yearly returns: {html.escape(str(metrics['yearly_returns']))}</p>"
    )


def _experiment_table(experiments: list[dict[str, Any]]) -> str:
    rows = []
    details = []
    for experiment in experiments:
        metrics = experiment["metrics"] or {}
        error = experiment["error"]
        rows.append(
            "<tr>"
            f"<td>{experiment['ordinal']}</td>"
            f"<td>{html.escape(experiment['hypothesis'])}</td>"
            f"<td>{html.escape(experiment['strategy'].get('family', 'unknown'))}</td>"
            f"<td>{html.escape(experiment['status'])}</td>"
            f"<td>{html.escape(experiment['decision'])}</td>"
            f"<td>{_format(metrics.get('total_return'))}</td>"
            f'<td class="error">{html.escape(error or "")}</td>'
            "</tr>"
        )
        audit = {
            "experiment_id": experiment["experiment_id"],
            "data_hash": experiment["data_hash"],
            "strategy_hash": experiment["strategy_hash"],
            "strategy": experiment["strategy"],
            "parameters": experiment["parameters"],
            "rationale": experiment["rationale"],
            "reflection": experiment["reflection"],
            "provider_usage": experiment["provider_usage"],
            "metrics": {
                key: value
                for key, value in metrics.items()
                if key not in {"equity_curve", "fills"}
            },
        }
        details.append(
            f"<details><summary>Experiment {experiment['ordinal']} audit details</summary>"
            f"<pre>{html.escape(json.dumps(audit, indent=2, sort_keys=True, ensure_ascii=False))}</pre>"
            "</details>"
        )
    return (
        "<table><thead><tr><th>#</th><th>Hypothesis</th><th>Strategy</th>"
        "<th>Status</th><th>Decision</th><th>Total return</th><th>Error</th>"
        "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
        + "".join(details)
    )


def _candidate_sections(
    candidates: list[dict[str, Any]],
    evaluations_by_candidate: dict[str, list[dict[str, Any]]],
) -> str:
    if not candidates:
        return "<p>No candidates have been frozen.</p>"
    sections = []
    for candidate in candidates:
        split = candidate["split"]
        split_summary = " | ".join(
            f"{period}: {split[f'{period}_start']} — {split[f'{period}_end']}"
            for period in ("discovery", "validation", "holdout")
        )
        sections.extend(
            [
                f"<h3>{html.escape(candidate['name'])}</h3>",
                f"<p>{html.escape(candidate['conclusion'])}</p>",
                _definition_list(
                    {
                        "Candidate fingerprint": candidate["candidate_hash"],
                        "Frozen strategy fingerprint": candidate["strategy_hash"],
                        "Frozen data fingerprint": candidate["data_hash"],
                        "Source experiment": candidate["experiment_id"],
                        "Frozen at": candidate["frozen_at"],
                        "Frozen data split": split_summary,
                    }
                ),
            ]
        )
        for evaluation in evaluations_by_candidate[candidate["candidate_id"]]:
            result = evaluation["result"]
            sections.append(
                f"<h4>{html.escape(evaluation['kind'].title())}: "
                f"{html.escape(evaluation['start_date'])} — {html.escape(evaluation['end_date'])}</h4>"
            )
            sections.append(
                f"<p>Data kind: {html.escape(result['data_kind'])}; "
                f"benchmark excess return: {_format(result['benchmark_excess_return'])}</p>"
            )
            sections.append(_metrics_table(result["candidate"]["metrics"]))
            sections.append(
                _equity_and_drawdown(
                    result["candidate"]["equity_curve"],
                    f"{evaluation['kind'].title()} candidate",
                )
            )
            if evaluation["kind"] == "validation":
                sections.append(
                    f"<h5>Cost sensitivity at "
                    f"{_format(result['cost_sensitivity']['transaction_cost_bps'])} bps</h5>"
                )
                sections.append(_metrics_table(result["cost_sensitivity"]["metrics"]))
                sections.append(
                    "<p>Parameter robustness: "
                    + html.escape(str(result["parameter_robustness"]))
                    + "</p>"
                )
                sections.append(
                    _period_breakdown(result["market_period_breakdown"])
                )
            else:
                sections.append(
                    "<p>Holdout unlocked at "
                    + html.escape(result["unlocked_at"])
                    + " after explicit confirmation.</p>"
                )
    return "".join(sections)


def _period_breakdown(periods: dict[str, float]) -> str:
    if not periods:
        return (
            '<h5>Market-period breakdown</h5>'
            '<p class="muted">Unavailable for this interval.</p>'
        )
    rows = "".join(
        f"<tr><td>{html.escape(period)}</td><td>{_format(value)}</td></tr>"
        for period, value in periods.items()
    )
    return (
        "<h5>Market-period breakdown</h5>"
        "<table><thead><tr><th>Period</th><th>Return</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )


def _equity_and_drawdown(curve: list[dict[str, Any]], title: str) -> str:
    if not curve:
        return f"<p class=\"muted\">No equity curve available for {html.escape(title)}.</p>"
    equities = [float(point["equity"]) for point in curve]
    peaks = []
    peak = equities[0]
    for equity in equities:
        peak = max(peak, equity)
        peaks.append(peak)
    drawdowns = [equity / peak - 1.0 for equity, peak in zip(equities, peaks)]
    return (
        f"<h5>{html.escape(title)} equity</h5>{_svg_plot(equities, "#2665a8", "Equity")}"
        f"<h5>{html.escape(title)} drawdown</h5>{_svg_plot(drawdowns, "#c14444", "Drawdown")}"
    )


def _svg_plot(values: list[float], color: str, label: str) -> str:
    width, height, margin = 700, 180, 12
    low, high = min(values), max(values)
    spread = high - low or 1.0
    points = []
    for index, value in enumerate(values):
        x = margin + (width - 2 * margin) * index / max(1, len(values) - 1)
        y = height - margin - (height - 2 * margin) * (value - low) / spread
        points.append(f"{x:.1f},{y:.1f}")
    polyline = " ".join(points)
    return (
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(label)}">'
        f'<polyline points="{polyline}" fill="none" stroke="{color}" stroke-width="2.5"/>'
        "</svg>"
    )


def _format(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.4f}"
    return html.escape(str(value))
