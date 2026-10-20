//! Development-only fixture reader. Native command-line authority selects one
//! existing owned scratch root; stdin supplies test records, never live grants.
//! No package installation, tool execution, process launch or filesystem write.
use mcu_core::board_compatibility::{FixtureCompatibilityScope, NativeHost, NativeRecipeSelection};
use mcu_host::recipe_snapshot::NativeReader;
use serde_json::Value;
use std::io::Read;
use std::path::Path;

const INPUT_LIMIT: u64 = 1_000_000;
const CASE_LIMIT: usize = 256;

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<_> = std::env::args_os().collect();
    if args.len() != 3 || args[1] != "--fixture-root" {
        return Err("Use --fixture-root with an existing owned audit scratch directory".into());
    }
    let checkout = Path::new(env!("CARGO_MANIFEST_DIR"))
        .ancestors()
        .nth(3)
        .ok_or("Missing checkout")?;
    let scope = FixtureCompatibilityScope::new(checkout, Path::new(&args[2]))
        .map_err(|r| format!("Invalid development fixture: {r:?}"))?;
    let mut raw = Vec::new();
    std::io::stdin()
        .take(INPUT_LIMIT + 1)
        .read_to_end(&mut raw)?;
    if raw.len() as u64 > INPUT_LIMIT {
        return Err("Compatibility input exceeds byte limit".into());
    }
    let cases: Vec<Value> = serde_json::from_slice(&raw)?;
    if cases.len() > CASE_LIMIT {
        return Err("Compatibility input exceeds case limit".into());
    }
    let host = if cfg!(target_os = "linux") {
        NativeHost::Linux
    } else if cfg!(windows) {
        NativeHost::Windows
    } else {
        NativeHost::Unsupported
    };
    let results: Vec<_> = cases
        .iter()
        .map(|case| {
            if case.as_object().is_some_and(|fields| {
                ["platform", "board", "framework", "pio_manifest"]
                    .iter()
                    .any(|key| fields.get(*key).is_some_and(|value| !value.is_string()))
            }) {
                return serde_json::json!({"rule":null,"options":{},"refusal":"InvalidMetadata",
                "readAttempts":0,"bytesRead":0});
            }
            let target = NativeRecipeSelection {
                backend: case["backend"].as_str(),
                platform: case["platform"].as_str().unwrap_or(""),
                board: case["board"].as_str().unwrap_or(""),
                framework: case["framework"].as_str().unwrap_or(""),
                manifest: Path::new(case["pio_manifest"].as_str().unwrap_or("")),
            };
            let mut reader = NativeReader::default();
            match scope.options(&target, host, &mut reader) {
                Ok(value) => serde_json::json!({"rule":value.rule,"options":value.options,
                "refusal":null,"readAttempts":reader.attempts,"bytesRead":reader.bytes}),
                Err(refusal) => {
                    serde_json::json!({"rule":null,"options":{},"refusal":format!("{refusal:?}"),
                "readAttempts":reader.attempts,"bytesRead":reader.bytes})
                }
            }
        })
        .collect();
    let output = serde_json::to_string(&results)?;
    if output.len() > 4_000_000 {
        return Err("Compatibility output exceeds limit".into());
    }
    println!("{output}");
    Ok(())
}
