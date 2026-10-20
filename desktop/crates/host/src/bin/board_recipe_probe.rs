//! Verification-only stdin/stdout fixture. No path, process or device authority.
use mcu_core::board_recipe::{recipe_options, RecipeInput};
use serde_json::Value;
use std::io::Read;

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut input = Vec::new();
    std::io::stdin().take(8_000_001).read_to_end(&mut input)?;
    if input.len() > 8_000_000 {
        return Err("Recipe fixture input exceeds limit".into());
    }
    let cases: Vec<Value> = serde_json::from_slice(&input)?;
    if cases.len() > 32_768 {
        return Err("Too many recipe fixture cases".into());
    }
    let output: Vec<_> = cases
        .iter()
        .map(|case| {
            let decision = recipe_options(&RecipeInput {
                platform_name: case["platformName"].as_str().unwrap_or(""),
                board_id: case["boardId"].as_str().unwrap_or(""),
                framework: case["framework"].as_str().unwrap_or(""),
                board: &case["board"],
                platform: &case["platform"],
                framework_package: &case["frameworkPackage"],
                sdk_package: &case["sdkPackage"],
                board_sha256: case["boardSha256"].as_str().unwrap_or(""),
            });
            serde_json::json!({"rule":decision.rule,"options":decision.options})
        })
        .collect();
    let output = serde_json::to_string(&output)?;
    if output.len() > 32_000_000 {
        return Err("Recipe fixture output exceeds limit".into());
    }
    println!("{output}");
    Ok(())
}
