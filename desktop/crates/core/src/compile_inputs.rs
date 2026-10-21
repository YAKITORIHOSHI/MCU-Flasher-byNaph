//! Pure, bounded compile-input planning for explicitly prepared native fixtures.
//! A syntactically exact target is not installed-target evidence, package
//! readiness, a compatibility-recipe result, a filesystem lease or permission
//! to spawn. Native host evidence must bind the target, host and admitted tools.

pub const MAX_COMPILE_SOURCES: usize = 128;
pub const MAX_COMPILE_SOURCE_BYTES: usize = 2 * 1024 * 1024;
pub const MAX_COMPILE_TOTAL_BYTES: usize = 16 * 1024 * 1024;
pub const COMPILE_SOURCE_DIRECTORY: &str = "sources";
pub const COMPILE_ENVIRONMENT: &str = "native_compile";

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CompileInputError {
    InvalidPlatformSpec,
    InvalidBoardId,
    UnsupportedFramework,
    InvalidSourceName,
    UnsupportedSource,
    SourceCollision,
    MissingEntrypoint,
    TooManySources,
    SourceTooLarge,
    SourcesTooLarge,
}

impl std::fmt::Display for CompileInputError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let message = match self {
            Self::InvalidPlatformSpec => "Use an exact registry platform name and pinned version.",
            Self::InvalidBoardId => "Use one bounded literal board ID.",
            Self::UnsupportedFramework => "This compile-input slice supports Arduino only.",
            Self::InvalidSourceName => "Use a portable visible root source filename.",
            Self::UnsupportedSource => "Only .cpp, .c, .h and .hpp compile inputs are supported.",
            Self::SourceCollision => "Compile source names must not alias another source.",
            Self::MissingEntrypoint => "A literal main.cpp entrypoint is required.",
            Self::TooManySources => "Too many compile source files.",
            Self::SourceTooLarge => "A compile source exceeds its byte limit.",
            Self::SourcesTooLarge => "Compile sources exceed their total byte limit.",
        };
        f.write_str(message)
    }
}

impl std::error::Error for CompileInputError {}

/// An exact literal selection, never a claim that it exists or is buildable.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ExactCompileTarget {
    platform_spec: String,
    board_id: String,
    framework: String,
}

impl ExactCompileTarget {
    /// Accept `name@x.y.z` or `owner/name@x.y.z`, with optional exact SemVer
    /// prerelease/build identifiers. Registry/path/URL/range resolution is not
    /// performed. Only the explicit lowercase `arduino` framework is admitted.
    pub fn new(
        platform_spec: &str,
        board_id: &str,
        framework: &str,
    ) -> Result<Self, CompileInputError> {
        if !exact_platform_spec(platform_spec) {
            return Err(CompileInputError::InvalidPlatformSpec);
        }
        if !literal_id(board_id, true) {
            return Err(CompileInputError::InvalidBoardId);
        }
        if framework != "arduino" {
            return Err(CompileInputError::UnsupportedFramework);
        }
        Ok(Self {
            platform_spec: platform_spec.into(),
            board_id: board_id.into(),
            framework: framework.into(),
        })
    }

    pub fn platform_spec(&self) -> &str {
        &self.platform_spec
    }

    pub fn board_id(&self) -> &str {
        &self.board_id
    }

    pub fn framework(&self) -> &str {
        &self.framework
    }
}

fn literal_id(value: &str, board: bool) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value.as_bytes()[0].is_ascii_alphanumeric()
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"_-".contains(&b) || board && b"+.".contains(&b))
}

fn exact_platform_spec(spec: &str) -> bool {
    if spec.len() > 256 {
        return false;
    }
    let Some((package, version)) = spec.split_once('@') else {
        return false;
    };
    let mut names = package.split('/');
    if !names.next().is_some_and(|name| literal_id(name, false))
        || names.next().is_some_and(|name| !literal_id(name, false))
        || names.next().is_some()
    {
        return false;
    }
    exact_version(version)
}

fn exact_version(version: &str) -> bool {
    if version.is_empty() || version.len() > 128 {
        return false;
    }
    let without_build = if let Some((base, build)) = version.split_once('+') {
        if !version_identifiers(build, false) {
            return false;
        }
        base
    } else {
        version
    };
    let release = if let Some((base, prerelease)) = without_build.split_once('-') {
        if !version_identifiers(prerelease, true) {
            return false;
        }
        base
    } else {
        without_build
    };
    let mut components = release.split('.');
    (0..3).all(|_| components.next().is_some_and(exact_decimal)) && components.next().is_none()
}

fn exact_decimal(component: &str) -> bool {
    !component.is_empty()
        && component.bytes().all(|b| b.is_ascii_digit())
        && (component.len() == 1 || !component.starts_with('0'))
}

fn version_identifiers(value: &str, prerelease: bool) -> bool {
    value.split('.').all(|part| {
        !part.is_empty()
            && part.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-')
            && (!prerelease || !part.bytes().all(|b| b.is_ascii_digit()) || exact_decimal(part))
    })
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CompileSource {
    pub name: String,
    pub bytes: Vec<u8>,
}

/// Immutable, deterministic inputs only; no caller options, hooks or commands.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CompileInputPlan {
    target: ExactCompileTarget,
    sources: Vec<CompileSource>,
    config_bytes: Vec<u8>,
}

impl CompileInputPlan {
    /// Own the complete admitted source set, sorted by literal filename without
    /// changing its names or bytes. This initial slice requires `main.cpp` and
    /// refuses `.ino` rather than pretending Arduino preprocessing occurred.
    pub fn new(
        target: ExactCompileTarget,
        mut sources: Vec<CompileSource>,
    ) -> Result<Self, CompileInputError> {
        if sources.len() > MAX_COMPILE_SOURCES {
            return Err(CompileInputError::TooManySources);
        }
        let mut total = 0usize;
        let mut names = std::collections::HashSet::with_capacity(sources.len());
        for source in &sources {
            validate_source_name(&source.name)?;
            // Conservatively catch Unicode case expansions too (for example
            // sharp-s/SS and Kelvin-sign/K), without changing literal paths.
            let key = source.name.to_lowercase().to_uppercase();
            if !names.insert(key) {
                return Err(CompileInputError::SourceCollision);
            }
            if source.bytes.len() > MAX_COMPILE_SOURCE_BYTES {
                return Err(CompileInputError::SourceTooLarge);
            }
            total = total
                .checked_add(source.bytes.len())
                .filter(|&total| total <= MAX_COMPILE_TOTAL_BYTES)
                .ok_or(CompileInputError::SourcesTooLarge)?;
        }
        if !sources.iter().any(|source| source.name == "main.cpp") {
            return Err(CompileInputError::MissingEntrypoint);
        }
        sources.sort_by(|left, right| left.name.cmp(&right.name));
        let config_bytes = format!(
            "[platformio]\nsrc_dir = {COMPILE_SOURCE_DIRECTORY}\ninclude_dir = {COMPILE_SOURCE_DIRECTORY}\n\n[env:{COMPILE_ENVIRONMENT}]\nplatform = {}\nboard = {}\nframework = {}\n",
            target.platform_spec(), target.board_id(), target.framework()
        )
        .into_bytes();
        Ok(Self {
            target,
            sources,
            config_bytes,
        })
    }

    pub fn target(&self) -> &ExactCompileTarget {
        &self.target
    }

    pub fn sources(&self) -> &[CompileSource] {
        &self.sources
    }

    pub fn config_bytes(&self) -> &[u8] {
        &self.config_bytes
    }
}

fn validate_source_name(name: &str) -> Result<(), CompileInputError> {
    if name.is_empty()
        || name.len() > 255
        || name != name.trim()
        || name.starts_with('.')
        || name.ends_with('.')
        || name
            .chars()
            .any(|c| c.is_control() || "\\/:<>\"|?*".contains(c))
        || crate::source_names::is_windows_reserved_device_name(name)
    {
        return Err(CompileInputError::InvalidSourceName);
    }
    let extension = name
        .rsplit_once('.')
        .map(|(_, ext)| ext.to_ascii_lowercase());
    if !matches!(extension.as_deref(), Some("cpp" | "c" | "h" | "hpp")) {
        return Err(CompileInputError::UnsupportedSource);
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn target() -> ExactCompileTarget {
        ExactCompileTarget::new("platformio/atmelavr@5.1.0", "uno", "arduino").unwrap()
    }

    fn source(name: &str, bytes: &[u8]) -> CompileSource {
        CompileSource {
            name: name.into(),
            bytes: bytes.into(),
        }
    }

    #[test]
    fn exact_target_preserves_pinned_literal_selection() {
        for spec in [
            "platformio/atmelavr@5.1.0",
            "espressif32@8.0.0",
            "arduinoq@1.0.0-rc.4+tool.1",
        ] {
            let target = ExactCompileTarget::new(spec, "mcu+sdk-v2.0_3", "arduino").unwrap();
            assert_eq!(target.platform_spec(), spec);
            assert_eq!(target.board_id(), "mcu+sdk-v2.0_3");
            assert_eq!(target.framework(), "arduino");
        }
    }

    #[test]
    fn unpinned_ranges_paths_and_ini_injection_cannot_become_targets() {
        for spec in [
            "",
            "atmelavr",
            "atmelavr@latest",
            "atmelavr@^5.1.0",
            "atmelavr@~5.1.0",
            "atmelavr@>=5.1.0",
            "atmelavr@5.*",
            "atmelavr@5.1",
            "atmelavr@05.1.0",
            "atmelavr@5.01.0",
            "atmelavr@5.1.00",
            "atmelavr@5.1.0-01",
            "atmelavr@5.1.0-",
            "atmelavr@5.1.0+",
            "atmelavr@5.1.0+a..b",
            "atmelavr@5.1.0@6.0.0",
            "../atmelavr@5.1.0",
            "a/b/c@5.1.0",
            "file:///tools/atmelavr",
            "https://example.test/platform.zip",
            "git+https://example.test/platform#v5.1.0",
            "atmelavr@5.1.0\nextra_scripts = evil.py",
            "atmelavr@5.1.0;comment",
            "atmelavr@${sysenv.VERSION}",
            "atmelavr@5.1.0 ",
            " atmelavr@5.1.0",
        ] {
            assert_eq!(
                ExactCompileTarget::new(spec, "uno", "arduino"),
                Err(CompileInputError::InvalidPlatformSpec),
                "{spec:?}"
            );
        }
    }

    #[test]
    fn board_literals_and_frameworks_refuse_ambiguity_or_injection() {
        for board in [
            "",
            ".",
            "..",
            "../uno",
            "a/b",
            "a\\b",
            "uno ",
            " uno",
            "uno\nextra_scripts=evil",
            "${sysenv.BOARD}",
            "a;b",
            "[env:other]",
            "uno:stream",
        ] {
            assert_eq!(
                ExactCompileTarget::new("atmelavr@5.1.0", board, "arduino"),
                Err(CompileInputError::InvalidBoardId),
                "{board:?}"
            );
        }
        for framework in [
            "",
            "Arduino",
            "espidf",
            "arduino, espidf",
            "arduino\nextra_scripts=evil",
        ] {
            assert_eq!(
                ExactCompileTarget::new("atmelavr@5.1.0", "uno", framework),
                Err(CompileInputError::UnsupportedFramework),
                "{framework:?}"
            );
        }
        assert_eq!(
            ExactCompileTarget::new(&format!("{}@1.2.3", "x".repeat(129)), "uno", "arduino"),
            Err(CompileInputError::InvalidPlatformSpec)
        );
        assert_eq!(
            ExactCompileTarget::new("atmelavr@5.1.0", &"x".repeat(129), "arduino"),
            Err(CompileInputError::InvalidBoardId)
        );
    }

    #[test]
    fn generated_config_and_source_order_are_fixed_and_deterministic() {
        let inputs = vec![
            source(
                "main.cpp",
                b"#include <Arduino.h>\nvoid setup() {}\nvoid loop() {}\n",
            ),
            source("helper.h", b"#pragma once\n"),
        ];
        let plan = CompileInputPlan::new(target(), inputs.clone()).unwrap();
        let mut reversed = inputs;
        reversed.reverse();
        assert_eq!(CompileInputPlan::new(target(), reversed).unwrap(), plan);
        assert_eq!(plan.target(), &target());
        assert_eq!(plan.sources()[0].name, "helper.h");
        assert_eq!(plan.config_bytes(), b"[platformio]\nsrc_dir = sources\ninclude_dir = sources\n\n[env:native_compile]\nplatform = platformio/atmelavr@5.1.0\nboard = uno\nframework = arduino\n");
    }

    #[test]
    fn plan_preserves_bom_newlines_non_utf8_and_unicode_filenames() {
        let main = b"\xef\xbb\xbf// literal \xff bytes\r\nvoid setup() {}\nvoid loop() {}\r";
        let plan = CompileInputPlan::new(
            target(),
            vec![
                source("main.cpp", main),
                source("caf\u{e9} \u{65e5}\u{672c}\u{8a9e}.hpp", b"// header\r\n"),
            ],
        )
        .unwrap();
        assert_eq!(plan.sources()[1].name, "main.cpp");
        assert_eq!(plan.sources()[1].bytes, main);
        assert_eq!(
            plan.sources()[0].name,
            "caf\u{e9} \u{65e5}\u{672c}\u{8a9e}.hpp"
        );
    }

    #[test]
    fn root_source_subset_admits_c_cpp_and_headers_without_transforming_them() {
        let plan = CompileInputPlan::new(
            target(),
            vec![
                source("main.cpp", b"entry"),
                source("driver.c", b"c"),
                source("other.CPP", b"cpp"),
                source("bits.H", b"h"),
                source("bits.hpp", b"hpp"),
            ],
        )
        .unwrap();
        assert_eq!(plan.sources().len(), 5);
        assert_eq!(
            plan.sources()
                .iter()
                .find(|s| s.name == "other.CPP")
                .unwrap()
                .bytes,
            b"cpp"
        );
    }

    #[test]
    fn ino_notes_config_hooks_and_non_source_inputs_are_explicitly_unsupported() {
        for name in [
            "Sketch.ino",
            "Sketch.INO",
            "NOTE.txt",
            "platformio.ini",
            "extra.py",
            "build.rs",
            "data.bin",
        ] {
            assert_eq!(
                CompileInputPlan::new(
                    target(),
                    vec![source("main.cpp", b"entry"), source(name, b"payload")]
                ),
                Err(CompileInputError::UnsupportedSource),
                "{name}"
            );
        }
    }

    #[test]
    fn unsafe_root_names_and_windows_devices_are_refused_on_every_host() {
        for name in [
            "",
            ".cpp",
            "../extra.cpp",
            "sub/extra.cpp",
            "sub\\extra.cpp",
            "C:extra.cpp",
            "extra.cpp:stream",
            "hidden.cpp ",
            " leading.cpp",
            "extra.cpp.",
            "bad\n.cpp",
            "bad\0.cpp",
            "bad?.cpp",
            "bad*.cpp",
            "bad|.cpp",
            "bad<.cpp",
            "bad>.cpp",
            "bad\".cpp",
            "CON.cpp",
            "PrN.hpp",
            "NUL.h",
            "AUX .cpp",
            "COM1.c",
            "LPT9.cpp",
            "COM\u{b9}.hpp",
            "LPT\u{b2}.h",
        ] {
            assert_eq!(
                CompileInputPlan::new(
                    target(),
                    vec![source("main.cpp", b"entry"), source(name, b"payload")]
                ),
                Err(CompileInputError::InvalidSourceName),
                "{name:?}"
            );
        }
        assert_eq!(
            CompileInputPlan::new(
                target(),
                vec![
                    source("main.cpp", b"entry"),
                    source(&format!("{}.h", "x".repeat(254)), b"")
                ]
            ),
            Err(CompileInputError::InvalidSourceName)
        );
    }

    #[test]
    fn collisions_refuse_exact_ascii_and_unicode_case_aliases() {
        for (first, second) in [
            ("helper.h", "helper.h"),
            ("Helper.h", "HELPER.H"),
            ("\u{c9}lan.h", "\u{e9}lan.h"),
            ("Stra\u{df}e.h", "STRASSE.h"),
            ("\u{212a}.h", "k.h"),
        ] {
            assert_eq!(
                CompileInputPlan::new(
                    target(),
                    vec![
                        source("main.cpp", b"entry"),
                        source(first, b"first"),
                        source(second, b"second")
                    ]
                ),
                Err(CompileInputError::SourceCollision),
                "{first} / {second}"
            );
        }
    }

    #[test]
    fn literal_main_cpp_is_required_without_a_fuzzy_fallback() {
        for sources in [
            vec![],
            vec![source("other.cpp", b"entry")],
            vec![source("Main.cpp", b"entry")],
            vec![source("MAIN.CPP", b"entry")],
            vec![source("main.c", b"entry")],
        ] {
            assert_eq!(
                CompileInputPlan::new(target(), sources),
                Err(CompileInputError::MissingEntrypoint)
            );
        }
    }

    #[test]
    fn source_count_bound_accepts_128_and_refuses_129() {
        let mut sources = vec![source("main.cpp", b"entry")];
        sources.extend((0..127).map(|i| source(&format!("header{i}.h"), b"")));
        assert!(CompileInputPlan::new(target(), sources.clone()).is_ok());
        sources.push(source("one_more.h", b""));
        assert_eq!(
            CompileInputPlan::new(target(), sources),
            Err(CompileInputError::TooManySources)
        );
    }

    #[test]
    fn per_source_byte_bound_accepts_2_mib_and_refuses_one_extra_byte() {
        assert!(CompileInputPlan::new(
            target(),
            vec![source("main.cpp", &vec![0x80; 2 * 1024 * 1024])]
        )
        .is_ok());
        assert_eq!(
            CompileInputPlan::new(
                target(),
                vec![source("main.cpp", &vec![0x80; 2 * 1024 * 1024 + 1])]
            ),
            Err(CompileInputError::SourceTooLarge)
        );
    }

    #[test]
    fn total_byte_bound_accepts_16_mib_and_refuses_one_extra_byte() {
        let mut sources = [
            "main.cpp",
            "one.cpp",
            "two.cpp",
            "three.cpp",
            "four.cpp",
            "five.cpp",
            "six.cpp",
            "seven.cpp",
        ]
        .into_iter()
        .enumerate()
        .map(|(index, name)| source(name, &vec![index as u8; 2 * 1024 * 1024]))
        .collect::<Vec<_>>();
        assert!(CompileInputPlan::new(target(), sources.clone()).is_ok());
        sources.push(source("extra.h", b"x"));
        assert_eq!(
            CompileInputPlan::new(target(), sources),
            Err(CompileInputError::SourcesTooLarge)
        );
    }
}
