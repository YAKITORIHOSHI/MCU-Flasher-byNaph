//! Shared native-widget design, ported from the existing Python workspace.
//! All dimensions are logical pixels: the native toolkit applies display scaling once.

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum Theme {
    #[default]
    Glass,
    FrostedLight,
    SolarizedDark,
}

impl Theme {
    // main/core/theme.py: Theme.apply_theme / get_palette aliases.
    pub fn parse(value: &str) -> Self {
        match value.trim().to_lowercase().as_str() {
            "light" | "clean" => Self::FrostedLight,
            "solarized" | "solarize" | "solarized_dark" | "solarize_dark" | "solarized-dark" => {
                Self::SolarizedDark
            }
            _ => Self::Glass,
        }
    }
    pub fn key(&self) -> &'static str {
        ["default", "light", "solarized_dark"][self.index()]
    }
    pub fn label(&self) -> &'static str {
        ["Glass Smoked Dark", "Glass Frosted Light", "Solarized Dark"][self.index()]
    }
    fn index(&self) -> usize {
        match self {
            Self::Glass => 0,
            Self::FrostedLight => 1,
            Self::SolarizedDark => 2,
        }
    }
    /// Exact framework-neutral token; an unknown key uses this theme's text ink.
    pub fn color(&self, key: &str) -> &'static str {
        PALETTE
            .iter()
            .find(|(name, _)| *name == key)
            .map_or(PALETTE[7].1[self.index()], |(_, colors)| {
                colors[self.index()]
            })
    }
    /// RGB in 0xRRGGBB order, suitable for native platform color conversion.
    pub fn rgb(&self, key: &str) -> u32 {
        u32::from_str_radix(&self.color(key)[1..], 16).unwrap_or(0)
    }
    pub fn rgba(&self, key: &str, alpha: u8) -> [u8; 4] {
        let rgb = self.rgb(key);
        [(rgb >> 16) as u8, (rgb >> 8) as u8, rgb as u8, alpha]
    }
}

// main/core/theme.py: Theme.PALETTES, columns default / light / solarized_dark.
// Do not substitute the older class-level defaults: apply_theme replaces them.
pub const PALETTE: &[(&str, [&str; 3])] = &[
    ("BG_DARKEST", ["#101b2a", "#f1f7fc", "#001b22"]),
    ("BG_DARK", ["#18283b", "#e8f0f7", "#002b36"]),
    ("BG_MID", ["#20344b", "#f8fcff", "#073642"]),
    ("BG_LIGHT", ["#2a415a", "#dce9f3", "#0d4a59"]),
    ("BG_HOVER", ["#324e69", "#d1e4ee", "#115d70"]),
    ("BORDER", ["#425e79", "#a6becf", "#166b80"]),
    ("BORDER_LIT", ["#80d9ce", "#0969da", "#2aa198"]),
    ("TEXT", ["#e3edf6", "#24292f", "#ffffff"]),
    ("TEXT_DIM", ["#adc0d3", "#57606a", "#d0e4e8"]),
    ("TEXT_BRIGHT", ["#f6fbff", "#1f2328", "#ffffff"]),
    ("CYAN", ["#80d9ce", "#0969da", "#2aa198"]),
    ("CYAN_DIM", ["#326b73", "#0550ae", "#208078"]),
    ("GREEN", ["#5ccc6e", "#1a7f37", "#859900"]),
    ("GREEN_DIM", ["#2d6636", "#116329", "#586600"]),
    ("YELLOW", ["#e8b83a", "#9a6700", "#b58900"]),
    ("YELLOW_DIM", ["#7a6020", "#7d4e00", "#7a5d00"]),
    ("RED", ["#f05050", "#cf222e", "#dc322f"]),
    ("RED_DIM", ["#7a2828", "#a40e26", "#93201e"]),
    ("MAGENTA", ["#c678dd", "#8250df", "#d33682"]),
    ("PURPLE", ["#b388ff", "#6639ba", "#6c71c4"]),
    ("PURPLE_DIM", ["#9d7cc4", "#542c9f", "#4d5196"]),
    ("BLUE", ["#61afef", "#0969da", "#268bd2"]),
    ("ORANGE", ["#d19a66", "#bc4c00", "#cb4b16"]),
    ("BTN_COMPILE", ["#284a55", "#1f883d", "#586600"]),
    ("BTN_COMPILE_H", ["#356372", "#1a7f37", "#859900"]),
    ("BTN_UPLOAD", ["#256b72", "#0969da", "#6c71c4"]),
    ("BTN_UPLOAD_H", ["#327e87", "#0858b8", "#8389db"]),
    ("BTN_FULL", ["#273e55", "#0969da", "#268bd2"]),
    ("BTN_FULL_H", ["#36546f", "#0858b8", "#3a9de0"]),
    ("BTN_MONITOR", ["#294c57", "#0e8a7e", "#2aa198"]),
    ("BTN_MONITOR_H", ["#366674", "#0b7066", "#38b8ae"]),
    ("BTN_STOP", ["#693745", "#cf222e", "#dc322f"]),
    ("BTN_STOP_H", ["#864553", "#b61c27", "#e84a47"]),
    ("BTN_CLEAR", ["#273e55", "#eef1f4", "#0a4554"]),
    ("BTN_CLEAR_H", ["#36546f", "#e1e4e8", "#115d70"]),
    ("BTN_DIM", ["#24384c", "#eef1f4", "#073642"]),
    ("BTN_DIM_H", ["#324e69", "#e1e4e8", "#0d4a59"]),
    ("BTN_DANGER", ["#7a2828", "#cf222e", "#93201e"]),
    ("BTN_DANGER_H", ["#a03030", "#b61c27", "#dc322f"]),
];

// main/qt/theme.py: build_stylesheet global, toolbar and portable-glass rules.
pub const FONT_FAMILIES: &[&str] = &["Montserrat", "Segoe UI", "Ubuntu", "sans-serif"];
pub const UI_FONT_PX: u32 = 12;
pub const LOGO_FONT_PX: u32 = 14;
pub const LOGO_INK: &str = "#56cfbf"; // main/qt/toolbar.py: PrimaryToolbar._setup_widgets
pub const GLASS_ALPHA: u8 = 225;
pub const GLASS_HIGHLIGHT_ALPHA: u8 = 220;
pub const RIM_ALPHA: u8 = 55;
pub const SELECTED_ALPHA: u8 = 35;
pub const ACTION_RADIUS: u32 = 8;
pub const TAB_RADIUS: u32 = 9;
pub const TAB_PADDING: [u32; 2] = [12, 6]; // horizontal, vertical
pub const FOCUS_BORDER: u32 = 2;
pub const ACTION_SPACING: u32 = 4;
pub const PRIMARY_PADDING: [u32; 4] = [10, 7, 10, 7]; // left, top, right, bottom
pub const CENTRAL_MARGIN: u32 = 0;
pub const CONTROL_HEIGHT: u32 = 26;
pub const ACTION_ICON_SIZE: u32 = 16;
pub const SPLITTER_WIDTH: u32 = 8;
pub const EDITOR_MIN_HEIGHT: u32 = 150;

// main/qt/toolbar.py: PrimaryToolbar._setup_widgets, ControlsBar._setup_ui.
#[rustfmt::skip]
pub const PRIMARY_ACTIONS: &[&str] = &["Compile", "Upload", "Stop", "Clean", "Save", "Save All", "Reload", "Modify", "Project", "Cloud", "Resources"];
#[rustfmt::skip]
pub const WORKSPACE_ACTIONS: &[&str] = &["Detach Editor", "Hide Editor", "Hide Monitors", "Settings", "AI Assistant"];
#[rustfmt::skip]
pub const CONTROL_LABELS: &[&str] = &["Board", "Serial port", "Upload speed", "Workspace", "Timestamps", "Skip Compile"];
// main/qt/main_window.py: _update_tab_titles_responsive and tab insertion order.
#[rustfmt::skip]
pub const TOOL_TABS: &[&str] = &["Build Console", "Serial Monitor", "Compatible Devices", "Notifications", "Syntax Check", "Terminal", "AI Changes"];
#[rustfmt::skip]
pub const COMPACT_TOOL_TABS: &[&str] = &["Build", "Serial", "Devices", "Alerts", "Syntax", "Terminal", "AI Changes"];
pub fn tool_tab_labels(width: u32) -> &'static [&'static str] {
    if width >= 1150 {
        TOOL_TABS
    } else {
        COMPACT_TOOL_TABS
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct PrimaryLayout {
    pub compact: bool,
    pub logo: &'static str,
    pub show_project_path: bool,
    pub show_project_icon: bool,
    pub resources_icon_only: bool,
    pub primary_icons_only: bool,
    pub actions_label: &'static str,
    pub actions_min_width: u32,
}

/// Measured widths are native logical size hints, preserving chosen fonts.
/// main/qt/toolbar.py: PrimaryToolbar.set_responsive_width.
pub fn primary_layout(width: u32, side_width: u32, action_width: u32) -> PrimaryLayout {
    let compact =
        width < 1200 || width.saturating_sub(side_width) < action_width.saturating_add(24);
    PrimaryLayout {
        compact,
        logo: if compact && width < 520 {
            "MCU"
        } else if compact && width < 680 {
            "MCU Flasher"
        } else {
            "MCU Flasher by Naph"
        },
        show_project_path: !compact || width >= 1000,
        show_project_icon: width >= 680,
        resources_icon_only: compact && width < 1000,
        primary_icons_only: width < 560,
        actions_label: if width < 420 { "" } else { "Actions " },
        actions_min_width: if width < 420 { 30 } else { 100 },
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ControlsLayout {
    pub compact_actions: bool,
    pub horizontal_groups: bool,
    pub show_labels: [bool; 4],
    pub show_search_and_checkboxes: bool,
    /// Board / port / speed / workspace: [row, column, row span, column span].
    pub grid: [[u32; 4]; 4],
    pub stretch_column: u32,
    pub margins: [u32; 4],
    pub spacing: u32,
    pub minimum_height: u32,
    pub board_min_width: u32,
    pub port_min_width: u32,
    pub timestamp_label: &'static str,
    pub skip_compile_label: &'static str,
    pub speed_label: &'static str,
    pub options_label: &'static str,
}

/// Group widths must be remeasured after applying compact labels to native widgets.
/// main/qt/toolbar.py: ControlsBar._reflow / set_responsive_height / set_responsive_width.
pub fn controls_layout(
    width: u32,
    height: u32,
    groups: [u32; 4],
    adaptive_board: u32,
) -> ControlsLayout {
    let tiny = height < 350 && width < 900;
    let short = tiny || height < 500 && width < 520;
    let required = groups
        .iter()
        .fold(38_u32, |sum, item| sum.saturating_add(*item));
    let single = [[0, 0, 1, 1], [0, 1, 1, 1], [0, 2, 1, 1], [0, 3, 1, 1]];
    let (grid, stretch_column) = if tiny {
        (single, 0)
    } else if short {
        ([[0, 0, 1, 1], [1, 0, 1, 1], [1, 1, 1, 1], [0, 1, 1, 1]], 0)
    } else if width >= required {
        (single, 3)
    } else if width >= 520 {
        ([[0, 0, 1, 1], [0, 1, 1, 1], [0, 2, 1, 1], [1, 0, 1, 3]], 1)
    } else {
        ([[0, 0, 1, 2], [1, 0, 1, 1], [1, 1, 1, 1], [2, 0, 1, 2]], 0)
    };
    let (board_floor, board_percent, port_min) = match width {
        0..950 => (140, 75, 140),
        950..1200 => (150, 85, 170),
        1200..1400 => (160, 95, 200),
        _ => (175, 100, 230),
    };
    ControlsLayout {
        compact_actions: width < 1350,
        horizontal_groups: short,
        show_labels: [!short, !short, !short || !tiny, !short],
        show_search_and_checkboxes: !tiny,
        grid,
        stretch_column,
        margins: if short { [8, 2, 8, 2] } else { [10, 4, 10, 4] },
        spacing: if short { 3 } else { 6 },
        minimum_height: if tiny { 30 } else { 54 },
        board_min_width: if tiny {
            70
        } else {
            board_floor.max(adaptive_board.saturating_mul(board_percent) / 100)
        },
        port_min_width: if tiny { 100 } else { port_min },
        timestamp_label: if width < 1100 { "TS" } else { "Timestamps" },
        skip_compile_label: if width < 1350 { "Skip" } else { "Skip Compile" },
        speed_label: if width < 1100 { "SPD" } else { "Upload speed" },
        options_label: if short { "" } else { "Options " },
    }
}

/// main/qt/main_window.py: _minimum_width_for_display / _update_minimum_window_size.
pub fn minimum_window_size(work_width: u32, work_height: u32) -> [u32; 2] {
    [
        (work_width.max(1) / 2).max(1),
        work_height.saturating_sub(48).clamp(1, 380),
    ]
}

/// main/qt/main_window.py: _fit_panes_for_height, preserving the saved reading font.
pub fn output_reading_min(line_spacing: u32, document_margin: u32, chrome: u32) -> u32 {
    line_spacing
        .saturating_add(document_margin)
        .saturating_add(chrome)
        .saturating_add(1)
        .max(24)
}
/// Returns [editor minimum, tool minimum]; tool tabs and active controls reserve first.
pub fn pane_budget(available: u32, tab_height: u32, active_panel_min: u32, gap: u32) -> [u32; 2] {
    let tools = tab_height
        .saturating_add(active_panel_min.max(24))
        .min(available.saturating_sub(gap));
    [
        EDITOR_MIN_HEIGHT.min(available.saturating_sub(tools).saturating_sub(gap)),
        tools,
    ]
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn palette_modes_preserve_reference_inks_and_aliases() {
        assert_eq!(PALETTE.len(), 39);
        for theme in [Theme::Glass, Theme::FrostedLight, Theme::SolarizedDark] {
            for (key, _) in PALETTE {
                assert!(theme.rgb(key) <= 0xffffff);
            }
            assert_eq!(theme.color("unknown"), theme.color("TEXT"));
        }
        assert_eq!(Theme::parse(" Solarized-dark "), Theme::SolarizedDark);
        assert_eq!(Theme::parse("clean").color("BG_DARKEST"), "#f1f7fc");
        assert_eq!(
            Theme::Glass.rgba("BORDER_LIT", RIM_ALPHA),
            [128, 217, 206, 55]
        );
    }

    #[test]
    fn compact_actions_keep_primary_and_project_access_at_half_screen_widths() {
        let narrow = primary_layout(384, 180, 600);
        assert!(narrow.compact && narrow.primary_icons_only && narrow.resources_icon_only);
        assert_eq!((narrow.logo, narrow.actions_min_width), ("MCU", 30));
        assert!(!primary_layout(1200, 200, 900).compact);
        assert!(primary_layout(1400, 600, 900).compact); // Native font hints take precedence.
        assert_eq!(tool_tab_labels(1149)[2], "Devices");
        assert_eq!(tool_tab_labels(1150)[2], "Compatible Devices");
    }

    #[test]
    fn controls_reflow_without_losing_board_port_or_workspace_cells() {
        for (width, height) in [(384, 700), (800, 700), (1600, 700), (480, 450), (384, 300)] {
            let layout = controls_layout(width, height, [200, 180, 90, 300], 240);
            for (i, [row, col, rows, cols]) in layout.grid.iter().copied().enumerate() {
                for [other_row, other_col, other_rows, other_cols] in
                    layout.grid[..i].iter().copied()
                {
                    assert!(
                        row + rows <= other_row
                            || other_row + other_rows <= row
                            || col + cols <= other_col
                            || other_col + other_cols <= col
                    );
                }
            }
        }
        let tiny = controls_layout(384, 300, [70, 100, 86, 30], 240);
        assert_eq!((tiny.board_min_width, tiny.port_min_width), (70, 100));
        assert!(!tiny.show_search_and_checkboxes);
        assert!(controls_layout(480, 450, [200; 4], 240).show_labels[2]);
    }

    #[test]
    fn logical_minimum_and_reading_budget_fit_short_scaled_work_areas() {
        for work_width in [1, 384, 768, 1280, 1920, 3840] {
            assert_eq!(
                minimum_window_size(work_width, 1080)[0],
                (work_width / 2).max(1)
            );
        }
        assert_eq!(minimum_window_size(768, 300), [384, 252]);
        let panel = output_reading_min(42, 8, 4) + 60;
        for available in 0..600 {
            let [editor, tools] = pane_budget(available, 38, panel, SPLITTER_WIDTH);
            assert!(editor + tools + SPLITTER_WIDTH.min(available) <= available);
            if available >= 38 + panel + SPLITTER_WIDTH {
                assert_eq!(tools, 38 + panel);
            }
        }
    }
}
