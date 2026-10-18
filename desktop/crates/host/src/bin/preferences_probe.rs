use std::io::Read;

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut input = Vec::new();
    std::io::stdin().take(2_000_001).read_to_end(&mut input)?;
    if input.len() > 2_000_000 {
        return Err("Preference fixture exceeds limit".into());
    }
    let cases: Vec<[serde_json::Value; 3]> = serde_json::from_slice(&input)?;
    if cases.len() > 2048 {
        return Err("Too many preference cases".into());
    }
    let results: Vec<_> = cases
        .iter()
        .map(|[current, original, edited]| {
            mcu_core::preferences::merge_changes(current, original, edited)
                .map(|merged| serde_json::json!({"success":true,"merged":merged}))
                .unwrap_or_else(|_| serde_json::json!({"success":false}))
        })
        .collect();
    println!("{}", serde_json::to_string(&results)?);
    Ok(())
}
