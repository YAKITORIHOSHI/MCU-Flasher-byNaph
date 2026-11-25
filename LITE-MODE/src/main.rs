#![cfg_attr(target_os = "windows", windows_subsystem = "windows")]

mod bridge;
mod desktop;
mod preferences;
mod ui;

fn main() -> eframe::Result {
    let args: Vec<_> = std::env::args().collect();
    let startup_error = match desktop::bootstrap(&args) {
        Ok(true) => return Ok(()),
        Ok(false) => None,
        Err(error) => Some(error),
    };
    let preview = args.iter().any(|arg| arg == "--preview");
    // Open as the window was left; previews never read or write that state.
    let left = if preview {
        preferences::Preferences::default()
    } else {
        preferences::directory()
            .map(|folder| preferences::load(&folder))
            .unwrap_or_default()
    };
    let size = if preview {
        args.windows(2)
            .find(|pair| pair[0] == "--preview-size")
            .and_then(|pair| pair[1].split_once('x'))
            .and_then(|(w, h)| Some([w.parse::<f32>().ok()?, h.parse::<f32>().ok()?]))
            .filter(|size| size[0].is_finite() && size[1].is_finite())
            .map(|size| [size[0].clamp(360.0, 3840.0), size[1].clamp(240.0, 2160.0)])
            .unwrap_or(preferences::DEFAULT_SIZE)
    } else {
        [left.width, left.height]
    };
    let options = eframe::NativeOptions {
        viewport: eframe::egui::ViewportBuilder::default()
            .with_title("MCU Flasher / Lite")
            .with_app_id("dev.naph.mcuflasher.lite")
            .with_inner_size(size)
            .with_maximized(left.maximized)
            .with_min_inner_size([360.0, 240.0])
            .with_clamp_size_to_monitor_size(true)
            .with_icon(ui::app_icon()),
        renderer: eframe::Renderer::Glow,
        ..Default::default()
    };
    eframe::run_native(
        "MCU Flasher Lite",
        options,
        Box::new(move |cc| Ok(Box::new(ui::LiteApp::new(cc, startup_error)))),
    )
}
