use std::fs::File;
use std::path::Path;

use arrow_array::{ArrayRef, Float64Array, Int64Array, RecordBatch, StringArray};
use arrow_schema::{DataType, Field, Schema};
use chrono::{DateTime, Utc};
use parquet::arrow::ArrowWriter;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Market {
    AShare,
    BinancePerp,
}

impl Market {
    fn as_str(self) -> &'static str {
        match self {
            Market::AShare => "ASHARE",
            Market::BinancePerp => "BINANCE_PERP",
        }
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct DailyBar {
    pub symbol: String,
    pub market: Market,
    pub timestamp_utc: DateTime<Utc>,
    pub open: f64,
    pub high: f64,
    pub low: f64,
    pub close: f64,
    pub volume: f64,
    pub turnover_rate: Option<f64>,
    pub adj_close_hfq: Option<f64>,
    pub open_interest: Option<f64>,
    pub position_size: Option<f64>,
}

pub fn normalize_ashare_symbol(raw_code: &str) -> Option<String> {
    let normalized = raw_code.trim().to_uppercase();
    if normalized.ends_with(".SH") || normalized.ends_with(".SZ") {
        return Some(normalized);
    }

    if normalized.len() != 6 || !normalized.chars().all(|c| c.is_ascii_digit()) {
        return None;
    }

    let exchange = if normalized.starts_with('6') || normalized.starts_with('9') {
        "SH"
    } else {
        "SZ"
    };
    Some(format!("{normalized}.{exchange}"))
}

pub fn normalize_binance_perp_symbol(raw_symbol: &str) -> Option<String> {
    let normalized = raw_symbol.trim().to_uppercase();
    if normalized.ends_with(".BINANCE_PERP") {
        return Some(normalized);
    }

    let contract: String = normalized
        .chars()
        .filter(|c| c.is_ascii_alphanumeric())
        .collect();
    if contract.is_empty() {
        return None;
    }
    Some(format!("{contract}.BINANCE_PERP"))
}

pub fn daily_bars_to_record_batch(
    bars: &[DailyBar],
) -> Result<RecordBatch, Box<dyn std::error::Error + Send + Sync>> {
    let schema = Schema::new(vec![
        Field::new("symbol", DataType::Utf8, false),
        Field::new("market", DataType::Utf8, false),
        Field::new("ts_utc_us", DataType::Int64, false),
        Field::new("open", DataType::Float64, false),
        Field::new("high", DataType::Float64, false),
        Field::new("low", DataType::Float64, false),
        Field::new("close", DataType::Float64, false),
        Field::new("volume", DataType::Float64, false),
        Field::new("turnover_rate", DataType::Float64, true),
        Field::new("adj_close_hfq", DataType::Float64, true),
        Field::new("open_interest", DataType::Float64, true),
        Field::new("position_size", DataType::Float64, true),
    ]);

    let symbol = StringArray::from(
        bars.iter()
            .map(|b| Some(b.symbol.as_str()))
            .collect::<Vec<Option<&str>>>(),
    );
    let market = StringArray::from(
        bars.iter()
            .map(|b| Some(b.market.as_str()))
            .collect::<Vec<Option<&str>>>(),
    );
    let ts_utc_us = Int64Array::from(
        bars.iter()
            .map(|b| b.timestamp_utc.timestamp_micros())
            .collect::<Vec<i64>>(),
    );
    let open = Float64Array::from(bars.iter().map(|b| b.open).collect::<Vec<f64>>());
    let high = Float64Array::from(bars.iter().map(|b| b.high).collect::<Vec<f64>>());
    let low = Float64Array::from(bars.iter().map(|b| b.low).collect::<Vec<f64>>());
    let close = Float64Array::from(bars.iter().map(|b| b.close).collect::<Vec<f64>>());
    let volume = Float64Array::from(bars.iter().map(|b| b.volume).collect::<Vec<f64>>());
    let turnover_rate = Float64Array::from(
        bars.iter()
            .map(|b| b.turnover_rate)
            .collect::<Vec<Option<f64>>>(),
    );
    let adj_close_hfq = Float64Array::from(
        bars.iter()
            .map(|b| b.adj_close_hfq)
            .collect::<Vec<Option<f64>>>(),
    );
    let open_interest = Float64Array::from(
        bars.iter()
            .map(|b| b.open_interest)
            .collect::<Vec<Option<f64>>>(),
    );
    let position_size = Float64Array::from(
        bars.iter()
            .map(|b| b.position_size)
            .collect::<Vec<Option<f64>>>(),
    );

    let columns: Vec<ArrayRef> = vec![
        std::sync::Arc::new(symbol),
        std::sync::Arc::new(market),
        std::sync::Arc::new(ts_utc_us),
        std::sync::Arc::new(open),
        std::sync::Arc::new(high),
        std::sync::Arc::new(low),
        std::sync::Arc::new(close),
        std::sync::Arc::new(volume),
        std::sync::Arc::new(turnover_rate),
        std::sync::Arc::new(adj_close_hfq),
        std::sync::Arc::new(open_interest),
        std::sync::Arc::new(position_size),
    ];

    Ok(RecordBatch::try_new(std::sync::Arc::new(schema), columns)?)
}

pub fn write_daily_bars_parquet<P: AsRef<Path>>(
    bars: &[DailyBar],
    output_path: P,
) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    let batch = daily_bars_to_record_batch(bars)?;
    let file = File::create(output_path)?;
    let mut writer = ArrowWriter::try_new(file, batch.schema(), None)?;
    writer.write(&batch)?;
    writer.close()?;
    Ok(())
}

pub const ETL_PHASE1_ARCHITECTURE: &str = r#"Phase-1 ETL (Rust):
1) Extract:
   - A-share: pull AkShare/Tushare daily bars, map trade date to UTC close timestamp.
   - Binance Perp: stream local/remote gzip CSV files with buffered readers.
2) Transform:
   - Normalize symbols (e.g. 600519.SH, BTCUSDT.BINANCE_PERP).
   - Convert all times to DateTime<Utc>, persist epoch microseconds.
   - Map market-specific fields to nullable columns in DailyBar.
3) Load:
   - Batch by market/symbol/month into Arrow RecordBatch.
   - Write partitioned parquet paths like market=ASHARE/symbol=600519.SH/yyyymm=202609.parquet.
4) Parallelism:
   - Use Rayon/Tokio task fan-out per source file/symbol partition.
   - Keep bounded channels between parse -> normalize -> writer stages for backpressure.
"#;

#[cfg(test)]
mod tests {
    use std::fs;
    use std::time::{SystemTime, UNIX_EPOCH};

    use chrono::TimeZone;

    use super::*;

    fn sample_bar() -> DailyBar {
        DailyBar {
            symbol: "600519.SH".to_string(),
            market: Market::AShare,
            timestamp_utc: Utc
                .with_ymd_and_hms(2024, 6, 3, 7, 0, 0)
                .single()
                .expect("valid datetime"),
            open: 100.0,
            high: 110.0,
            low: 99.0,
            close: 108.5,
            volume: 1_000_000.0,
            turnover_rate: Some(1.2),
            adj_close_hfq: Some(108.5),
            open_interest: None,
            position_size: None,
        }
    }

    #[test]
    fn normalizes_symbols() {
        assert_eq!(
            normalize_ashare_symbol("600519"),
            Some("600519.SH".to_string())
        );
        assert_eq!(
            normalize_ashare_symbol("000001"),
            Some("000001.SZ".to_string())
        );
        assert_eq!(
            normalize_binance_perp_symbol("btc/usdt"),
            Some("BTCUSDT.BINANCE_PERP".to_string())
        );
    }

    #[test]
    fn builds_record_batch() {
        let bars = vec![sample_bar()];
        let batch = daily_bars_to_record_batch(&bars).expect("batch");

        assert_eq!(batch.num_rows(), 1);
        assert_eq!(batch.num_columns(), 12);
        assert_eq!(
            batch
                .schema()
                .field_with_name("symbol")
                .expect("symbol field")
                .data_type(),
            &DataType::Utf8
        );
    }

    #[test]
    fn writes_parquet_file() {
        let bars = vec![sample_bar()];
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("time")
            .as_nanos();
        let path = std::env::temp_dir().join(format!("daily_bar_test_{unique}.parquet"));

        write_daily_bars_parquet(&bars, &path).expect("write parquet");
        let metadata = fs::metadata(&path).expect("metadata");
        assert!(metadata.len() > 0);

        fs::remove_file(path).expect("cleanup");
    }
}
