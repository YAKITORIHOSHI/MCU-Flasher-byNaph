//! Bounded pure target-transport differential probe; no tool/device operations.
use mcu_core::target_transport::{self, TargetTransportError};
use serde::Serialize;
use serde_json::Value;
use std::io::Read;

fn value<T: Serialize>(result: Result<T, TargetTransportError>) -> Value {
    match result {
        Ok(value) => serde_json::json!({"success":true,"value":value}),
        Err(error) => {
            serde_json::json!({"success":false,"error":{"code":"invalid-target-field","field":error.field,"message":error.message}})
        }
    }
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut input = Vec::new();
    std::io::stdin().take(8_000_001).read_to_end(&mut input)?;
    if input.len() > 8_000_000 {
        return Err("Target fixture input exceeds limit".into());
    }
    let cases: Vec<[Value; 3]> = serde_json::from_slice(&input)?;
    if cases.len() > 32_768 {
        return Err("Too many target-transport cases".into());
    }
    let output: Vec<_> = cases.iter().map(|[info, requested, port]| serde_json::json!({
        "valid_platformio_board_id":target_transport::valid_platformio_board_id(info.get("board").unwrap_or(&Value::Null)),
        "requires_upload_port":value(target_transport::requires_upload_port(info)),
        "serial_upload_speed":value(target_transport::serial_upload_speed(info, requested)),
        "upload_configuration":value(target_transport::upload_configuration(info, requested)),
        "upload_target_ready":value(target_transport::upload_target_ready(info, port))
    })).collect();
    let output = serde_json::to_string(&output)?;
    if output.len() > 32_000_000 {
        return Err("Target fixture output exceeds limit".into());
    }
    println!("{output}");
    Ok(())
}
