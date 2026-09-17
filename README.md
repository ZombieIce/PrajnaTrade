# PrajnaTrade

Phase-1 implementation now includes:

- Unified cross-market `DailyBar` schema for A-share + Binance Perp.
- UTC microsecond timestamps via `chrono::DateTime<Utc>`.
- Symbol normalization helpers:
  - `600519` -> `600519.SH`
  - `BTCUSDT` / `btc/usdt` -> `BTCUSDT.BINANCE_PERP`
- Arrow `RecordBatch` conversion and Parquet writer for batch storage.
- ETL guidance constant `ETL_PHASE1_ARCHITECTURE` for A-share and Binance pipelines.

Core implementation is in:

- `src/lib.rs`