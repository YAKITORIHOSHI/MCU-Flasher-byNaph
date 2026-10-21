//! Pure exact descriptor checks for an explicitly owned prepared fixture.
//! The platform/board pair does not prove registry-owner identity (ordinary
//! platform metadata contains no owner), transitive dependencies, executable
//! identity, installed readiness, compatibility overrides or permission to run.
//! The native host must supply exactly one role of each kind and bind raw bytes.

use crate::compile_inputs::ExactCompileTarget;
use serde_json::Value;
use std::collections::HashSet;

pub const MAX_PREPARED_DESCRIPTOR_BYTES: usize = 64 * 1024;
pub const MAX_PREPARED_DESCRIPTOR_DEPTH: usize = 32;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PreparedTargetRefusal {
    InvalidPath,
    TooLarge,
    InvalidMetadata,
    TargetMismatch,
    UnsupportedFramework,
}

impl std::fmt::Display for PreparedTargetRefusal {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(match self {
            Self::InvalidPath => {
                "Prepared descriptor paths do not identify one exact platform/board pair."
            }
            Self::TooLarge => "A prepared descriptor exceeds its byte limit.",
            Self::InvalidMetadata => {
                "Prepared descriptors require bounded unambiguous JSON objects."
            }
            Self::TargetMismatch => "Prepared platform metadata differs from the exact target.",
            Self::UnsupportedFramework => {
                "Prepared descriptors do not explicitly declare the target framework."
            }
        })
    }
}

impl std::error::Error for PreparedTargetRefusal {}

/// Validate one platform/board role pair without filesystem access or mutation.
pub fn validate_prepared_descriptors(
    target: &ExactCompileTarget,
    platform_path: &str,
    platform_bytes: &[u8],
    board_path: &str,
    board_bytes: &[u8],
) -> Result<(), PreparedTargetRefusal> {
    let parent = platform_path
        .strip_suffix("/platform.json")
        .ok_or(PreparedTargetRefusal::InvalidPath)?;
    if !portable_relative_path(platform_path)
        || !portable_relative_path(board_path)
        || board_path != format!("{parent}/boards/{}.json", target.board_id())
    {
        return Err(PreparedTargetRefusal::InvalidPath);
    }
    let platform = metadata(platform_bytes)?;
    let board = metadata(board_bytes)?;
    let (package, version) = target
        .platform_spec()
        .rsplit_once('@')
        .ok_or(PreparedTargetRefusal::TargetMismatch)?;
    let name = package
        .rsplit('/')
        .next()
        .ok_or(PreparedTargetRefusal::TargetMismatch)?;
    if platform.get("name").and_then(Value::as_str) != Some(name)
        || platform.get("version").and_then(Value::as_str) != Some(version)
    {
        return Err(PreparedTargetRefusal::TargetMismatch);
    }
    let framework = target.framework();
    if !platform
        .get("frameworks")
        .and_then(Value::as_object)
        .and_then(|frameworks| frameworks.get(framework))
        .is_some_and(Value::is_object)
    {
        return Err(PreparedTargetRefusal::UnsupportedFramework);
    }
    let declared = board
        .get("frameworks")
        .and_then(Value::as_array)
        .ok_or(PreparedTargetRefusal::UnsupportedFramework)?;
    let mut frameworks = HashSet::with_capacity(declared.len());
    for item in declared {
        let item = item
            .as_str()
            .ok_or(PreparedTargetRefusal::UnsupportedFramework)?;
        if !frameworks.insert(item) {
            return Err(PreparedTargetRefusal::UnsupportedFramework);
        }
    }
    if !frameworks.contains(framework) {
        return Err(PreparedTargetRefusal::UnsupportedFramework);
    }
    Ok(())
}

fn portable_relative_path(path: &str) -> bool {
    !path.is_empty()
        && path.len() <= 4096
        && path.split('/').all(|part| {
            !part.is_empty()
                && part.len() <= 255
                && part == part.trim()
                && !part.starts_with('.')
                && !part.ends_with('.')
                && !part
                    .chars()
                    .any(|c| c.is_control() || "\\:<>\"|?*".contains(c))
                && !crate::source_names::is_windows_reserved_device_name(part)
        })
}

fn metadata(raw: &[u8]) -> Result<Value, PreparedTargetRefusal> {
    if raw.len() > MAX_PREPARED_DESCRIPTOR_BYTES {
        return Err(PreparedTargetRefusal::TooLarge);
    }
    // serde_json owns JSON syntax/UTF-8/number validation. Its standard Value
    // parser overwrites duplicate keys, so inspect the original tokens too.
    // No BOM or nonstandard JSON constants are accepted in this fixture slice.
    let value: Value =
        serde_json::from_slice(raw).map_err(|_| PreparedTargetRefusal::InvalidMetadata)?;
    if !value.is_object() {
        return Err(PreparedTargetRefusal::InvalidMetadata);
    }
    let mut unique = UniqueKeys { raw, cursor: 0 };
    unique.value(0)?;
    unique.whitespace();
    if unique.cursor != raw.len() {
        return Err(PreparedTargetRefusal::InvalidMetadata);
    }
    Ok(value)
}

/// This second pass visits only already syntax-validated JSON. It retains each
/// object's decoded keys independently so escaped aliases are refused as well.
/// All indexing remains checked and recursion is bounded even on invalid input.
struct UniqueKeys<'a> {
    raw: &'a [u8],
    cursor: usize,
}

impl<'a> UniqueKeys<'a> {
    fn whitespace(&mut self) {
        while self
            .raw
            .get(self.cursor)
            .is_some_and(|b| b.is_ascii_whitespace())
        {
            self.cursor += 1;
        }
    }

    fn consume(&mut self, expected: u8) -> Result<(), PreparedTargetRefusal> {
        self.whitespace();
        if self.raw.get(self.cursor) != Some(&expected) {
            return Err(PreparedTargetRefusal::InvalidMetadata);
        }
        self.cursor += 1;
        Ok(())
    }

    fn string(&mut self) -> Result<&'a [u8], PreparedTargetRefusal> {
        self.whitespace();
        let start = self.cursor;
        self.consume(b'"')?;
        while let Some(&byte) = self.raw.get(self.cursor) {
            self.cursor += 1;
            if byte == b'"' {
                return self
                    .raw
                    .get(start..self.cursor)
                    .ok_or(PreparedTargetRefusal::InvalidMetadata);
            }
            if byte == b'\\' {
                self.cursor += 1;
            }
        }
        Err(PreparedTargetRefusal::InvalidMetadata)
    }

    fn value(&mut self, depth: usize) -> Result<(), PreparedTargetRefusal> {
        self.whitespace();
        match self.raw.get(self.cursor) {
            Some(b'{') => {
                if depth >= MAX_PREPARED_DESCRIPTOR_DEPTH {
                    return Err(PreparedTargetRefusal::InvalidMetadata);
                }
                self.cursor += 1;
                let mut keys = HashSet::new();
                self.whitespace();
                if self.raw.get(self.cursor) == Some(&b'}') {
                    self.cursor += 1;
                    return Ok(());
                }
                loop {
                    let key: String = serde_json::from_slice(self.string()?)
                        .map_err(|_| PreparedTargetRefusal::InvalidMetadata)?;
                    if !keys.insert(key) {
                        return Err(PreparedTargetRefusal::InvalidMetadata);
                    }
                    self.consume(b':')?;
                    self.value(depth + 1)?;
                    self.whitespace();
                    match self.raw.get(self.cursor) {
                        Some(b',') => self.cursor += 1,
                        Some(b'}') => {
                            self.cursor += 1;
                            return Ok(());
                        }
                        _ => return Err(PreparedTargetRefusal::InvalidMetadata),
                    }
                }
            }
            Some(b'[') => {
                if depth >= MAX_PREPARED_DESCRIPTOR_DEPTH {
                    return Err(PreparedTargetRefusal::InvalidMetadata);
                }
                self.cursor += 1;
                self.whitespace();
                if self.raw.get(self.cursor) == Some(&b']') {
                    self.cursor += 1;
                    return Ok(());
                }
                loop {
                    self.value(depth + 1)?;
                    self.whitespace();
                    match self.raw.get(self.cursor) {
                        Some(b',') => self.cursor += 1,
                        Some(b']') => {
                            self.cursor += 1;
                            return Ok(());
                        }
                        _ => return Err(PreparedTargetRefusal::InvalidMetadata),
                    }
                }
            }
            Some(b'"') => self.string().map(|_| ()),
            Some(_) => {
                let start = self.cursor;
                while self
                    .raw
                    .get(self.cursor)
                    .is_some_and(|b| !b.is_ascii_whitespace() && !matches!(b, b',' | b']' | b'}'))
                {
                    self.cursor += 1;
                }
                if self.cursor == start {
                    return Err(PreparedTargetRefusal::InvalidMetadata);
                }
                Ok(())
            }
            None => Err(PreparedTargetRefusal::InvalidMetadata),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const PLATFORM_PATH: &str = "platforms/atmelavr/platform.json";
    const BOARD_PATH: &str = "platforms/atmelavr/boards/uno.json";
    const PLATFORM: &[u8] = br#"{"name":"atmelavr","version":"5.1.0","frameworks":{"arduino":{"package":"framework-arduino-avr","script":"builder/frameworks/arduino.py"}}}"#;
    const BOARD: &[u8] = br#"{"name":"Arduino Uno","frameworks":["arduino"]}"#;

    fn target() -> ExactCompileTarget {
        ExactCompileTarget::new("platformio/atmelavr@5.1.0", "uno", "arduino").unwrap()
    }

    fn validate(platform: &[u8], board: &[u8]) -> Result<(), PreparedTargetRefusal> {
        validate_prepared_descriptors(&target(), PLATFORM_PATH, platform, BOARD_PATH, board)
    }

    #[test]
    fn exact_platform_and_literal_board_path_prove_only_the_declared_pair() {
        assert_eq!(validate(PLATFORM, BOARD), Ok(()));
        let owner_not_in_metadata =
            ExactCompileTarget::new("otherowner/atmelavr@5.1.0", "uno", "arduino").unwrap();
        assert_eq!(
            validate_prepared_descriptors(
                &owner_not_in_metadata,
                PLATFORM_PATH,
                PLATFORM,
                BOARD_PATH,
                BOARD
            ),
            Ok(())
        );
        let unknown_literal =
            ExactCompileTarget::new("unlisted@1.2.3", "unlisted+exact", "arduino").unwrap();
        assert_eq!(
            validate_prepared_descriptors(
                &unknown_literal,
                "fixture/platform.json",
                br#"{"name":"unlisted","version":"1.2.3","frameworks":{"arduino":{}}}"#,
                "fixture/boards/unlisted+exact.json",
                BOARD
            ),
            Ok(())
        );
    }

    #[test]
    fn platform_name_and_version_require_exact_strings_without_fuzzy_matching() {
        for platform in [
            br#"{"name":"atmelavr-family","version":"5.1.0","frameworks":{"arduino":{}}}"#
                .as_slice(),
            br#"{"name":"Atmelavr","version":"5.1.0","frameworks":{"arduino":{}}}"#,
            br#"{"name":"atmelavr","version":"5.1.1","frameworks":{"arduino":{}}}"#,
            br#"{"name":"atmelavr","version":"5.1","frameworks":{"arduino":{}}}"#,
            br#"{"name":"atmelavr","version":5.1,"frameworks":{"arduino":{}}}"#,
            br#"{"version":"5.1.0","frameworks":{"arduino":{}}}"#,
            br#"{"name":"atmelavr","frameworks":{"arduino":{}}}"#,
        ] {
            assert_eq!(
                validate(platform, BOARD),
                Err(PreparedTargetRefusal::TargetMismatch),
                "{platform:?}"
            );
        }
        assert_eq!(
            validate(BOARD, PLATFORM),
            Err(PreparedTargetRefusal::TargetMismatch)
        );
    }

    #[test]
    fn both_descriptors_must_explicitly_declare_the_exact_framework() {
        for platform in [
            br#"{"name":"atmelavr","version":"5.1.0"}"#.as_slice(),
            br#"{"name":"atmelavr","version":"5.1.0","frameworks":["arduino"]}"#,
            br#"{"name":"atmelavr","version":"5.1.0","frameworks":{"Arduino":{}}}"#,
            br#"{"name":"atmelavr","version":"5.1.0","frameworks":{"espidf":{}}}"#,
            br#"{"name":"atmelavr","version":"5.1.0","frameworks":{"arduino":null}}"#,
        ] {
            assert_eq!(
                validate(platform, BOARD),
                Err(PreparedTargetRefusal::UnsupportedFramework)
            );
        }
        for board in [
            br#"{}"#.as_slice(),
            br#"{"frameworks":"arduino"}"#,
            br#"{"frameworks":{"arduino":{}}}"#,
            br#"{"frameworks":["Arduino"]}"#,
            br#"{"frameworks":["espidf"]}"#,
            br#"{"frameworks":["arduino",null]}"#,
            br#"{"frameworks":["arduino","arduino"]}"#,
        ] {
            assert_eq!(
                validate(PLATFORM, board),
                Err(PreparedTargetRefusal::UnsupportedFramework)
            );
        }
        assert_eq!(
            validate(PLATFORM, br#"{"frameworks":["espidf","arduino"]}"#),
            Ok(())
        );
    }

    #[test]
    fn board_role_must_be_under_the_same_platform_parent_with_the_exact_id() {
        for board in [
            "platforms/other/boards/uno.json",
            "platforms/atmelavr/boards/Uno.json",
            "platforms/atmelavr/boards/uno-family.json",
            "platforms/atmelavr/boards/uno.json/extra",
            "platforms/atmelavr/uno.json",
            "platforms/atmelavr/boards/platform.json",
            "platforms/atmelavr/platform.json",
        ] {
            assert_eq!(
                validate_prepared_descriptors(&target(), PLATFORM_PATH, PLATFORM, board, BOARD),
                Err(PreparedTargetRefusal::InvalidPath),
                "{board}"
            );
        }
    }

    #[test]
    fn descriptor_roles_refuse_absolute_aliased_unsafe_and_wrong_paths() {
        for path in [
            "platform.json",
            "/platforms/atmelavr/platform.json",
            "./platforms/atmelavr/platform.json",
            "platforms/../atmelavr/platform.json",
            "platforms//atmelavr/platform.json",
            "platforms\\atmelavr\\platform.json",
            "C:/atmelavr/platform.json",
            "platforms/atmelavr/Platform.json",
            "platforms/atmelavr/platform.json:stream",
            "platforms/atmelavr/platform.json\n",
            "platforms/CON/platform.json",
            "platforms/LPT\u{b9}/platform.json",
            "platforms/ trailing/platform.json",
            "platforms/atmelavr./platform.json",
            "platforms/atmelavr/platform.json/",
        ] {
            assert_eq!(
                validate_prepared_descriptors(&target(), path, PLATFORM, BOARD_PATH, BOARD),
                Err(PreparedTargetRefusal::InvalidPath),
                "{path:?}"
            );
        }
        assert_eq!(
            validate_prepared_descriptors(&target(), BOARD_PATH, BOARD, PLATFORM_PATH, PLATFORM),
            Err(PreparedTargetRefusal::InvalidPath)
        );
    }

    #[test]
    fn malformed_non_object_and_nonfinite_json_is_never_accepted() {
        for raw in [
            b"".as_slice(),
            b"[]",
            b"null",
            b"{",
            b"{} trailing",
            b"\xef\xbb\xbf{}",
            b"\xff",
            br#"{"bad":NaN}"#,
            br#"{"bad":Infinity}"#,
            br#"{"bad":1e10000}"#,
            br#"{"bad":"\uD800"}"#,
            br#"{"bad":"\x00"}"#,
        ] {
            assert_eq!(
                validate(raw, BOARD),
                Err(PreparedTargetRefusal::InvalidMetadata),
                "{raw:?}"
            );
            assert_eq!(
                validate(PLATFORM, raw),
                Err(PreparedTargetRefusal::InvalidMetadata),
                "{raw:?}"
            );
        }
    }

    #[test]
    fn duplicate_keys_cannot_overwrite_identity_framework_or_nested_metadata() {
        for raw in [
            br#"{"name":"wrong","name":"atmelavr","version":"5.1.0","frameworks":{"arduino":{}}}"#.as_slice(),
            br#"{"name":"atmelavr","version":"5.0.0","version":"5.1.0","frameworks":{"arduino":{}}}"#,
            br#"{"name":"atmelavr","version":"5.1.0","frameworks":{},"frameworks":{"arduino":{}}}"#,
            br#"{"name":"atmelavr","version":"5.1.0","frameworks":{"arduino":{},"\u0061rduino":{}}}"#,
            br#"{"name":"atmelavr","version":"5.1.0","frameworks":{"arduino":{"script":"one","script":"two"}}}"#,
            br#"{"name":"atmelavr","version":"5.1.0","frameworks":{"arduino":{}},"unused":[{"id":1,"id":1}]}"#,
        ] {
            assert_eq!(validate(raw, BOARD), Err(PreparedTargetRefusal::InvalidMetadata));
        }
        assert_eq!(
            validate(PLATFORM, br#"{"frameworks":[],"frameworks":["arduino"]}"#),
            Err(PreparedTargetRefusal::InvalidMetadata)
        );
    }

    #[test]
    fn escaped_structural_characters_in_strings_do_not_confuse_unique_key_checks() {
        let platform = br#"{"name":"atmelavr","version":"5.1.0","frameworks":{"arduino":{}},"unused":{"a,:[{\"":"value\\\"},]","b":"ordinary"}}"#;
        assert_eq!(validate(platform, BOARD), Ok(()));
    }

    #[test]
    fn byte_bound_accepts_64_kib_and_refuses_one_more_on_either_role() {
        let mut platform = PLATFORM.to_vec();
        platform.resize(64 * 1024, b' ');
        assert_eq!(validate(&platform, BOARD), Ok(()));
        platform.push(b' ');
        assert_eq!(
            validate(&platform, BOARD),
            Err(PreparedTargetRefusal::TooLarge)
        );
        let mut board = BOARD.to_vec();
        board.resize(64 * 1024, b' ');
        assert_eq!(validate(PLATFORM, &board), Ok(()));
        board.push(b' ');
        assert_eq!(
            validate(PLATFORM, &board),
            Err(PreparedTargetRefusal::TooLarge)
        );
    }

    #[test]
    fn nested_json_is_bounded_for_ignored_fields_too() {
        let nested = |arrays: usize| {
            let mut raw = String::from_utf8(PLATFORM[..PLATFORM.len() - 1].to_vec()).unwrap();
            raw.push_str(",\"unused\":");
            raw.push_str(&"[".repeat(arrays));
            raw.push('0');
            raw.push_str(&"]".repeat(arrays));
            raw.push('}');
            raw
        };
        assert_eq!(validate(nested(31).as_bytes(), BOARD), Ok(()));
        assert_eq!(
            validate(nested(32).as_bytes(), BOARD),
            Err(PreparedTargetRefusal::InvalidMetadata)
        );
        assert_eq!(
            validate(nested(256).as_bytes(), BOARD),
            Err(PreparedTargetRefusal::InvalidMetadata)
        );
    }
}
