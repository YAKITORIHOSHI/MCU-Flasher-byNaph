//! Bounded pure ownership-policy differential probe, without native discovery.
use mcu_core::ownership::{self, DirectProcessEvidence, Liveness, ProcessEvidence};
use serde_json::{json, Value};
use std::io::Read;

fn evidence(value: &Value) -> Result<ProcessEvidence, &'static str> {
    let time = |value: &Value| -> Result<Option<f64>, &'static str> {
        match value.get("createTime") {
            Some(Value::Null) | None => Ok(None),
            Some(value) => value.as_f64().map(Some).ok_or("Invalid fixture time"),
        }
    };
    match value["kind"].as_str() {
        Some("inventoryUnavailable") => Ok(ProcessEvidence::InventoryUnavailable),
        Some("present") => Ok(ProcessEvidence::Present {
            create_time: time(value)?,
        }),
        Some("missing") => {
            let direct = &value["direct"];
            let direct = match direct["kind"].as_str() {
                Some("notRunning") => DirectProcessEvidence::NotRunning,
                Some("accessDenied") => DirectProcessEvidence::AccessDenied,
                Some("present") => DirectProcessEvidence::Present {
                    create_time: time(direct)?,
                },
                _ => return Err("Invalid direct fixture evidence"),
            };
            Ok(ProcessEvidence::MissingFromInventory {
                direct: Some(direct),
            })
        }
        _ => Err("Invalid fixture evidence"),
    }
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut input = Vec::new();
    std::io::stdin().take(2_000_001).read_to_end(&mut input)?;
    if input.len() > 2_000_000 {
        return Err("Ownership fixture input exceeds limit".into());
    }
    let cases: Vec<Value> = serde_json::from_slice(&input)?;
    if cases.len() > 4096 {
        return Err("Too many ownership fixture cases".into());
    }
    let mut output = Vec::with_capacity(cases.len());
    for case in cases {
        output.push(match case["operation"].as_str() {
            Some("liveness") => {
                match ownership::liveness(&case["instance"], evidence(&case["evidence"])?) {
                    Ok(value) => {
                        json!({"success":true,"reservesResource":value != Liveness::Stale})
                    }
                    Err(_) => json!({"success":false}),
                }
            }
            Some("port") => match ownership::normalize_port(
                case["port"].as_str().ok_or("Expected fixture port text")?,
            ) {
                Ok(value) => json!({"success":true,"value":value}),
                Err(_) => json!({"success":false}),
            },
            _ => return Err("Unknown ownership fixture operation".into()),
        });
    }
    let output = serde_json::to_string(&output)?;
    if output.len() > 2_000_000 {
        return Err("Ownership fixture output exceeds limit".into());
    }
    println!("{output}");
    Ok(())
}
