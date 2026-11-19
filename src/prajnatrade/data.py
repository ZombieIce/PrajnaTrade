from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

ETF_SYMBOLS = ("511260", "518880", "515080", "513300", "159612")
CASH = "CASH"
PRICE_BASIS = "total_return"
REQUIRED_COLUMNS = ("date", "symbol", "open", "high", "low", "close", "volume")
PRICE_COLUMNS = ("open", "high", "low", "close")


class DataValidationError(ValueError):
    """Raised when source bars cannot be used reproducibly."""


@dataclass(frozen=True)
class ValidatedData:
    bars: pd.DataFrame
    data_hash: str
    report: dict[str, object]


def load_bars(path: str | Path, price_basis: str) -> ValidatedData:
    source = Path(path)
    if not source.is_file():
        raise DataValidationError(f"Data file does not exist: {source}")
    if price_basis != PRICE_BASIS:
        raise DataValidationError(
            f"Unsupported price basis {price_basis!r}; use {PRICE_BASIS!r}."
        )

    suffix = source.suffix.lower()
    if suffix == ".csv":
        bars = pd.read_csv(source, dtype={"symbol": "string"})
    elif suffix in {".parquet", ".pq"}:
        bars = pd.read_parquet(source)
    else:
        raise DataValidationError("Data file must have a .csv, .parquet, or .pq extension.")

    missing_columns = sorted(set(REQUIRED_COLUMNS) - set(bars.columns))
    if missing_columns:
        raise DataValidationError(
            f"Missing required columns: {', '.join(missing_columns)}."
        )
    if bars.empty:
        raise DataValidationError("Data file contains no bars.")

    bars = bars.copy()
    bars["symbol"] = bars["symbol"].astype("string").str.strip().str.upper()
    invalid_symbols = sorted(set(bars["symbol"].dropna()) - set(ETF_SYMBOLS))
    if bars["symbol"].isna().any() or invalid_symbols:
        details = ", ".join(invalid_symbols) if invalid_symbols else "blank symbol"
        raise DataValidationError(f"Unsupported or missing ETF symbol(s): {details}.")

    dates = pd.to_datetime(bars["date"], errors="coerce", utc=True)
    if dates.isna().any():
        raise DataValidationError("Date column contains invalid or missing values.")
    bars["date"] = dates.dt.tz_convert(None).dt.normalize()

    if "price_basis" in bars.columns:
        bases = bars["price_basis"].astype("string").str.strip().str.lower()
        if bases.isna().any() or set(bases) != {price_basis}:
            found = ", ".join(sorted(str(value) for value in bases.dropna().unique()))
            raise DataValidationError(
                f"Mixed or mismatched price basis; expected {price_basis!r}, found {found or 'blank'}."
            )
    bars["price_basis"] = price_basis

    for column in (*PRICE_COLUMNS, "volume"):
        raw = bars[column]
        parsed = pd.to_numeric(raw, errors="coerce")
        if (raw.notna() & parsed.isna()).any():
            raise DataValidationError(f"Column {column!r} contains non-numeric values.")
        if not parsed.dropna().map(math.isfinite).all():
            raise DataValidationError(f"Column {column!r} must contain finite values.")
        bars[column] = parsed
        if (parsed.dropna() <= 0).any() and column != "volume":
            raise DataValidationError(f"Column {column!r} must contain positive prices.")
        if column == "volume" and (parsed.dropna() < 0).any():
            raise DataValidationError("'volume' cannot be negative.")
        if column == "volume" and parsed.isna().any():
            raise DataValidationError("'volume' must be present for every bar.")

    if bars.duplicated(["date", "symbol"]).any():
        duplicates = (
            bars.loc[bars.duplicated(["date", "symbol"], keep=False), ["date", "symbol"]]
            .drop_duplicates()
            .assign(date=lambda frame: frame["date"].dt.strftime("%Y-%m-%d"))
            .to_dict("records")
        )
        raise DataValidationError(f"Duplicate date/symbol bars: {duplicates}.")

    for row in bars.itertuples(index=False):
        open_price, high, low, close = (
            getattr(row, column) for column in PRICE_COLUMNS
        )
        present_prices = [price for price in (open_price, close) if pd.notna(price)]
        if pd.notna(high) and present_prices and high < max(present_prices):
            raise DataValidationError(
                f"Invalid OHLC bar for {row.symbol} on {row.date:%Y-%m-%d}: high is below open/close."
            )
        if pd.notna(low) and present_prices and low > min(present_prices):
            raise DataValidationError(
                f"Invalid OHLC bar for {row.symbol} on {row.date:%Y-%m-%d}: low is above open/close."
            )
        if pd.notna(high) and pd.notna(low) and high < low:
            raise DataValidationError(
                f"Invalid OHLC bar for {row.symbol} on {row.date:%Y-%m-%d}: high is below low."
            )

    bars = bars.sort_values(["date", "symbol"], kind="stable").reset_index(drop=True)
    dates_by_day = bars.groupby("date")["symbol"].agg(set)
    missing_symbols: dict[str, list[str]] = {}
    for day, symbols in dates_by_day.items():
        missing = sorted(set(ETF_SYMBOLS) - set(symbols))
        if missing:
            missing_symbols[day.strftime("%Y-%m-%d")] = missing
    unavailable_opens = int((bars["open"].isna() | (bars["volume"] == 0)).sum())
    unavailable_closes = int(bars["close"].isna().sum())

    data_hash = fingerprint_bars(bars)
    report: dict[str, object] = {
        "rows": len(bars),
        "symbols": sorted(str(value) for value in bars["symbol"].unique()),
        "start_date": bars["date"].min().strftime("%Y-%m-%d"),
        "end_date": bars["date"].max().strftime("%Y-%m-%d"),
        "missing_symbols_by_date": missing_symbols,
        "unavailable_opens": unavailable_opens,
        "unavailable_closes": unavailable_closes,
        "zero_volume_bars": int((bars["volume"] == 0).sum()),
        "price_basis": price_basis,
        "data_hash": data_hash,
    }
    return ValidatedData(bars=bars, data_hash=data_hash, report=report)


def fingerprint_bars(bars: pd.DataFrame) -> str:
    canonical = bars.sort_values(["date", "symbol"], kind="stable").copy()
    canonical["date"] = canonical["date"].dt.strftime("%Y-%m-%d")
    canonical["symbol"] = canonical["symbol"].astype(str)
    canonical_payload = canonical.to_csv(
        index=False, na_rep="", float_format="%.17g", lineterminator="\n"
    )
    return hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()


def validate_calendar(path: str | Path, bars: pd.DataFrame) -> dict[str, object]:
    source = Path(path)
    if not source.is_file():
        raise DataValidationError(f"Calendar file does not exist: {source}")
    try:
        calendar = pd.read_csv(source, dtype={"date": "string"})
    except pd.errors.EmptyDataError as error:
        raise DataValidationError("Calendar file is empty.") from error
    if "date" not in calendar.columns or calendar.empty:
        raise DataValidationError("Calendar CSV must contain a nonempty date column.")
    dates = pd.to_datetime(calendar["date"], errors="coerce", utc=True)
    if dates.isna().any():
        raise DataValidationError("Calendar contains invalid or missing dates.")
    dates = dates.dt.tz_convert(None).dt.normalize()
    if dates.duplicated().any():
        raise DataValidationError("Calendar contains duplicate dates.")
    expected = set(dates)
    actual = set(bars["date"])
    return {
        "status": "checked",
        "missing_dates": [day.strftime("%Y-%m-%d") for day in sorted(expected - actual)],
        "unexpected_dates": [day.strftime("%Y-%m-%d") for day in sorted(actual - expected)],
    }


def report_json(report: dict[str, object]) -> str:
    return json.dumps(report, indent=2, sort_keys=True, allow_nan=False)
