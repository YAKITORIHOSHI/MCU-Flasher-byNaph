//! Development-only installed-recipe snapshots. A snapshot is not a lease,
//! immutable package generation, executable allowlist, or permission to build.
//! Native callers own the fixture scope, target record, host and reader.
//! The fixture parser accepts bounded ordinary JSON. Unlike Python's decoder,
//! it explicitly refuses NaN/Infinity, out-of-range numbers and excessive depth.
use serde_json::Value;
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

const Q_BOARD: &str = "8cbeb02c478524a13cb79267212ae4a768fc4afdf5ae9b7ad5e1084ed5943832";
const Q_PLATFORM: &str = "2ae4b31c96c795424f3b38579530b62aa0251004d1f2b28ed10f1ddcc7e7c8c0";
const Q_LINKER: &str = "d5547a1d22fa78a5c84a44ec2d53e1dade6d29d37f56c999299b1de789437ee1";

pub const MAX_METADATA_BYTES: usize = 65_536;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SnapshotRefusal {
    InvalidFixture,
    UnsafePath,
    NonRegular,
    Changed,
    TooLarge,
    InvalidMetadata,
    Io,
    UnsupportedHost,
}

/// Implementations must return descriptor-read regular-file bytes, refusing
/// aliases, observable replacements and more than MAX_METADATA_BYTES. Directory
/// checks are observations; the caller must not treat them as future authority.
pub trait SnapshotReader {
    fn directory(&mut self, path: &Path) -> Result<PathBuf, SnapshotRefusal>;
    fn read_regular(&mut self, path: &Path, boundary: &Path) -> Result<Vec<u8>, SnapshotRefusal>;
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum NativeHost {
    Linux,
    Windows,
    Unsupported,
}

pub struct NativeRecipeSelection<'a> {
    pub backend: Option<&'a str>,
    pub platform: &'a str,
    pub board: &'a str,
    pub framework: &'a str,
    pub manifest: &'a Path,
}

#[derive(Debug, Default, PartialEq, Eq)]
pub struct CompatibilityDecision {
    pub rule: Option<&'static str>,
    pub options: BTreeMap<&'static str, String>,
}

/// Explicit development authority, never deserialized from an editor message.
/// Both package inputs and the application-hook replica stay in the fixture.
pub struct FixtureCompatibilityScope {
    root: PathBuf,
}

impl FixtureCompatibilityScope {
    pub fn new(checkout_root: &Path, fixture_root: &Path) -> Result<Self, SnapshotRefusal> {
        let checkout = checkout_root
            .canonicalize()
            .map_err(|_| SnapshotRefusal::InvalidFixture)?;
        let root = fixture_root
            .canonicalize()
            .map_err(|_| SnapshotRefusal::InvalidFixture)?;
        let scratch = checkout.join("audit/migration/scratch");
        if root == scratch || !root.starts_with(&scratch) {
            return Err(SnapshotRefusal::InvalidFixture);
        }
        Ok(Self { root })
    }

    pub fn options(
        &self,
        selection: &NativeRecipeSelection<'_>,
        host: NativeHost,
        reader: &mut impl SnapshotReader,
    ) -> Result<CompatibilityDecision, SnapshotRefusal> {
        let q = (selection.platform, selection.board, selection.framework)
            == ("arduinoq", "uno_q", "arduino");
        let esp = selection.platform == "espressif32"
            && selection.framework == "arduino"
            && matches!(selection.board, "onehorse32dev" | "minimain_esp32s2");
        if selection.backend == Some("arduino-cli") || !(esp || q && host == NativeHost::Linux) {
            return Ok(CompatibilityDecision::default());
        }
        let core = reader.directory(&self.root.join("core"))?;
        let platforms = reader.directory(&core.join("platforms"))?;
        let packages = reader.directory(&core.join("packages"))?;
        if core.parent() != Some(self.root.as_path())
            || platforms.parent() != Some(core.as_path())
            || packages.parent() != Some(core.as_path())
        {
            return Err(SnapshotRefusal::UnsafePath);
        }
        let manifest = selection.manifest;
        let expected_name = format!("{}.json", selection.board);
        let boards = manifest.parent().ok_or(SnapshotRefusal::UnsafePath)?;
        let platform_dir = boards.parent().ok_or(SnapshotRefusal::UnsafePath)?;
        let platform_parent = platform_dir.parent().ok_or(SnapshotRefusal::UnsafePath)?;
        if manifest.file_name().and_then(|s| s.to_str()) != Some(expected_name.as_str())
            || boards.file_name().and_then(|s| s.to_str()) != Some("boards")
            || reader.directory(platform_parent)? != platforms
        {
            return Err(SnapshotRefusal::UnsafePath);
        }
        let raw = bounded_read(reader, manifest, &platforms)?;
        let board = metadata(&raw)?;
        let board_sha = crate::source_digest(&raw).0;
        let raw = bounded_read(reader, &platform_dir.join("platform.json"), &platforms)?;
        let platform = metadata(&raw)?;
        if q {
            let platform_sha = crate::source_digest(&raw).0;
            let raw = bounded_read(
                reader,
                &platform_dir.join("builder/artifacts/zephyr-llext.py"),
                &platforms,
            )?;
            let linker_sha = crate::source_digest(&raw).0;
            if (
                board_sha.as_str(),
                platform_sha.as_str(),
                linker_sha.as_str(),
            ) != (Q_BOARD, Q_PLATFORM, Q_LINKER)
            {
                return Ok(CompatibilityDecision::default());
            }
            let raw = bounded_read(
                reader,
                &packages.join("framework-arduino-zephyr/package.json"),
                &packages,
            )?;
            let framework = metadata(&raw)?;
            if framework["name"] != "framework-arduino-zephyr"
                || framework["version"] != "0.56.0-leelabskku+unoq"
            {
                return Ok(CompatibilityDecision::default());
            }
            // A replica of the immutable application's fixed hook location;
            // the fixture caller cannot supply an alternative script leaf.
            let application = self.root.join("application");
            let hook = application.join("src/modules/arduinoq_link_paths.py");
            let hook_raw = bounded_read(reader, &hook, &application)?;
            return Ok(arduinoq_recipe_options(&ArduinoQInput {
                host,
                selection,
                board_sha256: &board_sha,
                platform_sha256: &platform_sha,
                linker_sha256: &linker_sha,
                framework_package: &framework,
                hook_path: &hook,
                hook_sha256: &crate::source_digest(&hook_raw).0,
            }));
        }
        let arduino = "framework-arduinoespressif32";
        let sdk = if selection.board == "onehorse32dev" {
            "framework-arduinoespressif32-esp32-libs"
        } else {
            "framework-arduinoespressif32-esp32s2-libs"
        };
        let framework = metadata(&bounded_read(
            reader,
            &packages.join(arduino).join("package.json"),
            &packages,
        )?)?;
        let sdk = metadata(&bounded_read(
            reader,
            &packages.join(sdk).join("package.json"),
            &packages,
        )?)?;
        let result = crate::board_recipe::recipe_options(&crate::board_recipe::RecipeInput {
            platform_name: selection.platform,
            board_id: selection.board,
            framework: selection.framework,
            board: &board,
            platform: &platform,
            framework_package: &framework,
            sdk_package: &sdk,
            board_sha256: &board_sha,
        });
        Ok(CompatibilityDecision {
            rule: result.rule,
            options: result
                .options
                .into_iter()
                .map(|(key, value)| (key, value.into()))
                .collect(),
        })
    }
}

pub struct ArduinoQInput<'a> {
    pub host: NativeHost,
    pub selection: &'a NativeRecipeSelection<'a>,
    pub board_sha256: &'a str,
    pub platform_sha256: &'a str,
    pub linker_sha256: &'a str,
    pub framework_package: &'a Value,
    pub hook_path: &'a Path,
    pub hook_sha256: &'a str,
}

/// Pure policy only. Hash strings/paths are evidence supplied by the native
/// snapshot orchestrator; passing this check grants no script execution right.
pub fn arduinoq_recipe_options(input: &ArduinoQInput<'_>) -> CompatibilityDecision {
    let selected = input.selection;
    let Some(hook) = input.hook_path.to_str() else {
        return CompatibilityDecision::default();
    };
    if input.host != NativeHost::Linux
        || selected.backend == Some("arduino-cli")
        || (selected.platform, selected.board, selected.framework)
            != ("arduinoq", "uno_q", "arduino")
        || (
            input.board_sha256,
            input.platform_sha256,
            input.linker_sha256,
        ) != (Q_BOARD, Q_PLATFORM, Q_LINKER)
        || input.framework_package["name"] != "framework-arduino-zephyr"
        || input.framework_package["version"] != "0.56.0-leelabskku+unoq"
        || hook.chars().any(|c| (c as u32) < 32)
    {
        return CompatibilityDecision::default();
    }
    CompatibilityDecision {
        rule: Some("arduinoq-rc4-zephyr-0.56-linker-paths"),
        options: BTreeMap::from([
            ("extra_scripts", format!("post:{hook}")),
            ("custom_mcu_arduinoq_hook_sha256", input.hook_sha256.into()),
        ]),
    }
}

fn bounded_read(
    reader: &mut impl SnapshotReader,
    path: &Path,
    boundary: &Path,
) -> Result<Vec<u8>, SnapshotRefusal> {
    let raw = reader.read_regular(path, boundary)?;
    if raw.len() > MAX_METADATA_BYTES {
        return Err(SnapshotRefusal::TooLarge);
    }
    Ok(raw)
}

fn metadata(raw: &[u8]) -> Result<Value, SnapshotRefusal> {
    if raw.len() > MAX_METADATA_BYTES {
        return Err(SnapshotRefusal::TooLarge);
    }
    let raw = raw.strip_prefix(b"\xef\xbb\xbf").unwrap_or(raw);
    let value: Value = serde_json::from_slice(raw).map_err(|_| SnapshotRefusal::InvalidMetadata)?;
    if !value.is_object() {
        return Err(SnapshotRefusal::InvalidMetadata);
    }
    Ok(value)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[derive(Default)]
    struct FakeReader {
        reads: usize,
        files: BTreeMap<PathBuf, Vec<u8>>,
    }
    impl SnapshotReader for FakeReader {
        fn directory(&mut self, path: &Path) -> Result<PathBuf, SnapshotRefusal> {
            Ok(path.into())
        }
        fn read_regular(&mut self, path: &Path, _: &Path) -> Result<Vec<u8>, SnapshotRefusal> {
            self.reads += 1;
            self.files.get(path).cloned().ok_or(SnapshotRefusal::Io)
        }
    }
    fn scope() -> FixtureCompatibilityScope {
        FixtureCompatibilityScope {
            root: PathBuf::from("/owned-fixture"),
        }
    }
    fn selection() -> NativeRecipeSelection<'static> {
        NativeRecipeSelection {
            backend: None,
            platform: "arduinoq",
            board: "uno_q",
            framework: "arduino",
            manifest: Path::new("/owned-fixture/core/platforms/arduinoq/boards/uno_q.json"),
        }
    }
    #[test]
    fn unrelated_backend_and_non_linux_q_do_not_read_metadata() {
        let mut reader = FakeReader::default();
        let mut target = selection();
        assert_eq!(
            scope()
                .options(&target, NativeHost::Windows, &mut reader)
                .unwrap(),
            CompatibilityDecision::default()
        );
        target.backend = Some("arduino-cli");
        assert_eq!(
            scope()
                .options(&target, NativeHost::Linux, &mut reader)
                .unwrap(),
            CompatibilityDecision::default()
        );
        target.backend = None;
        target.board = "future-board";
        assert_eq!(
            scope()
                .options(&target, NativeHost::Linux, &mut reader)
                .unwrap(),
            CompatibilityDecision::default()
        );
        assert_eq!(reader.reads, 0);
    }
    #[test]
    fn exact_layout_is_required_before_file_reads() {
        let mut reader = FakeReader::default();
        let mut target = selection();
        target.manifest = Path::new("/outside/boards/uno_q.json");
        assert_eq!(
            scope().options(&target, NativeHost::Linux, &mut reader),
            Err(SnapshotRefusal::UnsafePath)
        );
        assert_eq!(reader.reads, 0);
    }
    #[test]
    fn metadata_parser_accepts_bom_objects_and_hashes_original_bytes() {
        let bytes = b"\xef\xbb\xbf{\"value\":1}";
        assert_eq!(metadata(bytes).unwrap(), json!({"value":1}));
        assert_ne!(
            crate::source_digest(bytes),
            crate::source_digest(&bytes[3..])
        );
        for raw in [
            b"[]".as_slice(),
            b"null",
            b"\xff",
            b"{",
            b"{} trailing",
            b"{\"unused\":NaN}",
            b"{\"unused\":1e10000}",
        ] {
            assert_eq!(metadata(raw), Err(SnapshotRefusal::InvalidMetadata));
        }
    }
    #[test]
    fn even_injected_reader_cannot_exceed_the_metadata_bound() {
        let mut reader = FakeReader::default();
        reader.files.insert(
            selection().manifest.into(),
            vec![b' '; MAX_METADATA_BYTES + 1],
        );
        assert_eq!(
            scope().options(&selection(), NativeHost::Linux, &mut reader),
            Err(SnapshotRefusal::TooLarge)
        );
        assert_eq!(reader.reads, 1);
    }
    #[test]
    fn q_exact_policy_preserves_owned_path_and_hook_digest() {
        let target = selection();
        let package = json!({"name":"framework-arduino-zephyr","version":"0.56.0-leelabskku+unoq"});
        let input = ArduinoQInput {
            host: NativeHost::Linux,
            selection: &target,
            board_sha256: Q_BOARD,
            platform_sha256: Q_PLATFORM,
            linker_sha256: Q_LINKER,
            framework_package: &package,
            hook_path: Path::new("/application café 日本語/src/modules/arduinoq_link_paths.py"),
            hook_sha256: "native-observed-digest",
        };
        let decision = arduinoq_recipe_options(&input);
        assert_eq!(decision.rule, Some("arduinoq-rc4-zephyr-0.56-linker-paths"));
        assert_eq!(
            decision.options["extra_scripts"],
            "post:/application café 日本語/src/modules/arduinoq_link_paths.py"
        );
        assert_eq!(
            decision.options["custom_mcu_arduinoq_hook_sha256"],
            "native-observed-digest"
        );
    }
    #[test]
    fn q_changed_hashes_framework_hosts_and_control_paths_do_not_select_hook() {
        let target = selection();
        let package = json!({"name":"framework-arduino-zephyr","version":"0.56.0-leelabskku+unoq"});
        let mut input = ArduinoQInput {
            host: NativeHost::Linux,
            selection: &target,
            board_sha256: Q_BOARD,
            platform_sha256: Q_PLATFORM,
            linker_sha256: Q_LINKER,
            framework_package: &package,
            hook_path: Path::new("/application/hook.py"),
            hook_sha256: "digest",
        };
        for host in [NativeHost::Windows, NativeHost::Unsupported] {
            input.host = host;
            assert_eq!(
                arduinoq_recipe_options(&input),
                CompatibilityDecision::default()
            );
        }
        input.host = NativeHost::Linux;
        for part in 0..3 {
            (
                input.board_sha256,
                input.platform_sha256,
                input.linker_sha256,
            ) = (Q_BOARD, Q_PLATFORM, Q_LINKER);
            match part {
                0 => input.board_sha256 = "changed",
                1 => input.platform_sha256 = "changed",
                _ => input.linker_sha256 = "changed",
            }
            assert_eq!(
                arduinoq_recipe_options(&input),
                CompatibilityDecision::default()
            );
        }
        (
            input.board_sha256,
            input.platform_sha256,
            input.linker_sha256,
        ) = (Q_BOARD, Q_PLATFORM, Q_LINKER);
        for package in [
            Value::Null,
            json!([]),
            json!({"name":"framework-arduino-zephyr","version":"future"}),
            json!({"name":"foreign","version":"0.56.0-leelabskku+unoq"}),
        ] {
            let changed = ArduinoQInput {
                framework_package: &package,
                ..input
            };
            assert_eq!(
                arduinoq_recipe_options(&changed),
                CompatibilityDecision::default()
            );
        }
        for control in '\0'..='\u{1f}' {
            let path = format!("/application/{control}/hook.py");
            let changed = ArduinoQInput {
                hook_path: Path::new(&path),
                ..input
            };
            assert_eq!(
                arduinoq_recipe_options(&changed),
                CompatibilityDecision::default()
            );
        }
    }
}
