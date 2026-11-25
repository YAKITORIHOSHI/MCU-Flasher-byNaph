//! How this window was left: one small private file beside Lite's other state.
use serde::{Deserialize, Serialize};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};

const FILE: &str = "window.json";
const LIMIT: u64 = 16 * 1024;
pub const BAUD_RATES: [u32; 14] = [
    300, 1200, 2400, 4800, 9600, 19200, 38400, 57600, 74880, 115200, 230400, 250000, 460800, 921600,
];
pub const UPLOAD_SPEEDS: [u32; 7] = [9600, 19200, 57600, 115200, 230400, 460800, 921600];
pub const DEFAULT_SIZE: [f32; 2] = [1140.0, 800.0];

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct Preferences {
    pub dark: bool,
    pub width: f32,
    pub height: f32,
    pub maximized: bool,
    pub serial_tab: bool,
    pub compact_controls: bool,
    pub follow: bool,
    pub line_ending: String,
    pub baud: u32,
    pub upload_speed: u32,
    pub board: String,
}

impl Default for Preferences {
    fn default() -> Self {
        Self {
            dark: false,
            width: DEFAULT_SIZE[0],
            height: DEFAULT_SIZE[1],
            maximized: false,
            serial_tab: false,
            compact_controls: false,
            follow: true,
            line_ending: "both".into(),
            baud: 115200,
            upload_speed: 460800,
            board: String::new(),
        }
    }
}

impl Preferences {
    /// Every value an older, newer or damaged file may hold becomes usable here.
    fn repaired(mut self) -> Self {
        let standard = Self::default();
        if !self.width.is_finite() || !(360.0..=16384.0).contains(&self.width) {
            self.width = standard.width;
        }
        if !self.height.is_finite() || !(240.0..=16384.0).contains(&self.height) {
            self.height = standard.height;
        }
        if !["both", "nl", "cr", "none"].contains(&self.line_ending.as_str()) {
            self.line_ending = standard.line_ending;
        }
        if !BAUD_RATES.contains(&self.baud) {
            self.baud = standard.baud;
        }
        if !UPLOAD_SPEEDS.contains(&self.upload_speed) {
            self.upload_speed = standard.upload_speed;
        }
        if self.board.len() > 512 {
            self.board.clear();
        }
        self
    }
}

/// The same private folder the Python tools use (`isolation.state_directory`).
pub fn directory() -> Option<PathBuf> {
    let absolute = |name: &str| {
        std::env::var_os(name)
            .map(PathBuf::from)
            .filter(|path| path.is_absolute())
    };
    if let Some(path) = std::env::var_os("MCU_LITE_STATE") {
        return Some(PathBuf::from(path)).filter(|path| path.is_absolute());
    }
    #[cfg(windows)]
    let base = absolute("LOCALAPPDATA")
        .or_else(|| absolute("USERPROFILE").map(|home| home.join("AppData").join("Local")));
    #[cfg(not(windows))]
    let base = absolute("XDG_DATA_HOME")
        .or_else(|| absolute("HOME").map(|home| home.join(".local/share")));
    base.map(|base| base.join("mcu-flasher-lite"))
}

pub fn load(directory: &Path) -> Preferences {
    let read = || -> Option<Preferences> {
        let file = std::fs::File::open(directory.join(FILE)).ok()?;
        let mut text = String::new();
        file.take(LIMIT + 1).read_to_string(&mut text).ok()?;
        if text.len() as u64 > LIMIT {
            return None;
        }
        serde_json::from_str(&text).ok()
    };
    read().unwrap_or_default().repaired()
}

/// Replace the file whole, so a window closed mid-write leaves the previous one.
pub fn save(directory: &Path, preferences: &Preferences) -> std::io::Result<()> {
    std::fs::create_dir_all(directory)?;
    let temporary = directory.join(format!("{FILE}.{}.tmp", std::process::id()));
    let result = (|| {
        let mut file = std::fs::File::create(&temporary)?;
        file.write_all(&serde_json::to_vec_pretty(preferences)?)?;
        file.sync_all()?;
        drop(file);
        std::fs::rename(&temporary, directory.join(FILE))
    })();
    if result.is_err() {
        let _ = std::fs::remove_file(&temporary);
    }
    result
}

#[cfg(test)]
mod verification {
    use super::*;

    fn folder(name: &str) -> PathBuf {
        let path =
            std::env::temp_dir().join(format!("lite-preferences-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&path);
        path
    }

    #[test]
    fn dark_mode_size_and_choices_survive_a_restart() {
        let path = folder("round-trip");
        assert_eq!(load(&path), Preferences::default());
        let left = Preferences {
            dark: true,
            width: 900.0,
            height: 640.0,
            maximized: true,
            serial_tab: true,
            compact_controls: true,
            follow: false,
            line_ending: "cr".into(),
            baud: 74880,
            upload_speed: 921600,
            board: "Arduino UNO".into(),
        };
        save(&path, &left).unwrap();
        assert_eq!(load(&path), left);
        let files: Vec<_> = std::fs::read_dir(&path).unwrap().collect();
        assert_eq!(files.len(), 1, "No temporary file may remain");
        std::fs::remove_dir_all(&path).unwrap();
    }

    #[test]
    fn damaged_partial_or_impossible_values_open_the_window_normally() {
        let path = folder("damaged");
        std::fs::create_dir_all(&path).unwrap();
        for (text, dark) in [
            ("", false),
            ("not json", false),
            ("[]", false),
            (r#"{"dark": true}"#, true),
            (
                r#"{"dark": true, "width": -4, "height": 1e30, "baud": 7, "upload_speed": 1, "line_ending": "x"}"#,
                true,
            ),
            (r#"{"dark": true, "from_a_newer_version": {"a": 1}}"#, true),
        ] {
            std::fs::write(path.join(FILE), text).unwrap();
            let expected = Preferences {
                dark,
                ..Preferences::default()
            };
            assert_eq!(load(&path), expected, "{text}");
        }
        std::fs::write(path.join(FILE), vec![b' '; LIMIT as usize + 8]).unwrap();
        assert_eq!(load(&path), Preferences::default());
        std::fs::remove_dir_all(&path).unwrap();
    }
}
