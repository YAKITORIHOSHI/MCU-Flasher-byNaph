//! Portable filename policy shared by fixture source and native metadata paths.
//! This is name validation only; it grants no filesystem access or save lease.

/// Windows reserves device stems even before extensions. Refuse ASCII spaces
/// immediately before the first extension conservatively, as well as the three
/// ISO-8859-1 superscript digits Windows recognizes in COM/LPT device names.
/// Apply the same rule on Linux so accepted sources remain portable.
pub fn is_windows_reserved_device_name(name: &str) -> bool {
    let stem = name
        .split('.')
        .next()
        .unwrap_or_default()
        .trim_end_matches([' ', '.'])
        .to_ascii_uppercase();
    matches!(stem.as_str(), "CON" | "PRN" | "AUX" | "NUL")
        || stem
            .strip_prefix("COM")
            .or_else(|| stem.strip_prefix("LPT"))
            .is_some_and(|suffix| {
                matches!(
                    suffix,
                    "1" | "2" | "3" | "4" | "5" | "6" | "7" | "8" | "9" | "¹" | "²" | "³"
                )
            })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn windows_device_stems_include_extensions_spaces_case_and_superscript_digits() {
        for stem in ["CON", "prn", "AuX", "nul"] {
            for suffix in ["", ".ino", " .cpp", "   .hpp", ".backup.txt"] {
                assert!(is_windows_reserved_device_name(&format!("{stem}{suffix}")));
            }
        }
        for prefix in ["COM", "com", "LPT", "lPt"] {
            for digit in ["1", "2", "3", "4", "5", "6", "7", "8", "9", "¹", "²", "³"] {
                for suffix in ["", ".ino", " .cpp", "   .hpp", ".backup.txt"] {
                    let name = format!("{prefix}{digit}{suffix}");
                    assert!(is_windows_reserved_device_name(&name), "{name}");
                }
            }
        }
    }

    #[test]
    fn similar_ordinary_names_are_not_silently_normalized_to_devices() {
        for name in [
            "Console.ino",
            "AUXiliary.cpp",
            "COM0.h",
            "COM10.h",
            "LPT10.txt",
            "COM¹x.ino",
            "LPT⁴.hpp",
            "COM１.cpp",
            "Sketch.COM1.ino",
            "café 日本語.hpp",
        ] {
            assert!(!is_windows_reserved_device_name(name), "{name}");
        }
    }
}
