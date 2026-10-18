//! Fixture-only project-service harness; never defaults to the current project.
use mcu_contracts::ScaffoldOptions;
use mcu_core::ProjectService;
use std::io::Read;
use std::path::PathBuf;

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut args = std::env::args_os().skip(1);
    let application_root =
        PathBuf::from(args.next().ok_or("Explicit application root is required")?);
    let fixture_root = PathBuf::from(
        args.next()
            .ok_or("Explicit audit fixture root is required")?,
    );
    if args.next().is_some() {
        return Err("Unexpected harness arguments".into());
    }
    let mut input = Vec::new();
    std::io::stdin().take(64_001).read_to_end(&mut input)?;
    if input.len() > 64_000 {
        return Err("Scaffold fixture input exceeds limit".into());
    }
    let cases: Vec<ScaffoldOptions> = serde_json::from_slice(&input)?;
    if cases.len() > 64 {
        return Err("Too many scaffold cases".into());
    }
    let mut service = ProjectService::fixture(&application_root, &fixture_root)?;
    let mut results = Vec::new();
    for (index, options) in cases.into_iter().enumerate() {
        let parent = fixture_root.join(format!("rust-case-{index}"));
        std::fs::create_dir(&parent)?;
        let registration = service.create_project("scaffold-verification", &parent, options)?;
        let mut sources = serde_json::Map::new();
        for source in &registration.manifest.files {
            let snapshot = service.read_document(
                "scaffold-verification",
                &registration.workspace_id,
                &source.document_id,
            )?;
            sources.insert(source.name.clone(), snapshot.content.into());
        }
        results.push(sources);
        service.unregister_project("scaffold-verification", &registration.workspace_id)?;
    }
    println!("{}", serde_json::to_string(&results)?);
    Ok(())
}
