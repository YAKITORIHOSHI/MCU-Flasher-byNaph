use mcu_core::resource_budget::{optimal_compiler_jobs, safe_worker_count, WorkerBudget};
use serde::Deserialize;
use std::io::Read;

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct Input {
    mode: String,
    logical_cpus: u32,
    physical_cpus: Option<u32>,
    available_gib: Option<f64>,
    storage_limit: Option<u32>,
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut input = Vec::new();
    std::io::stdin().take(4_000_001).read_to_end(&mut input)?;
    if input.len() > 4_000_000 {
        return Err("Resource fixture exceeds limit".into());
    }
    let cases: Vec<Input> = serde_json::from_slice(&input)?;
    if cases.len() > 24_000 {
        return Err("Too many resource cases".into());
    }
    let results: Vec<_> = cases
        .iter()
        .map(|case| {
            let input = WorkerBudget {
                mode: &case.mode,
                logical_cpus: case.logical_cpus,
                physical_cpus: case.physical_cpus,
                available_gib: case.available_gib,
                storage_limit: case.storage_limit,
            };
            serde_json::json!({
                "safe": safe_worker_count(input).expect("JSON has finite memory metrics"),
                "optimal": optimal_compiler_jobs(input).expect("JSON has finite memory metrics"),
            })
        })
        .collect();
    println!("{}", serde_json::to_string(&results)?);
    Ok(())
}
