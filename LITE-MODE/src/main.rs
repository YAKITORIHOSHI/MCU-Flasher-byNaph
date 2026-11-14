#![cfg_attr(target_os = "windows", windows_subsystem = "windows")]

mod bridge;
mod desktop;
mod ui;

fn main() -> eframe::Result {
    let args: Vec<_> = std::env::args().collect();
    let size = if args.iter().any(|arg| arg == "--preview") {
        args.windows(2)
            .find(|pair| pair[0] == "--preview-size")
            .and_then(|pair| pair[1].split_once('x'))
            .and_then(|(w, h)| Some([w.parse::<f32>().ok()?, h.parse::<f32>().ok()?]))
            .filter(|size| size[0].is_finite() && size[1].is_finite())
            .map(|size| [size[0].clamp(360.0, 3840.0), size[1].clamp(240.0, 2160.0)])
            .unwrap_or([1140.0, 800.0])
    } else {
        [1140.0, 800.0]
    };
    let options = eframe::NativeOptions {
        viewport: eframe::egui::ViewportBuilder::default()
            .with_title("MCU Flasher / Lite")
            .with_app_id("dev.naph.mcuflasher.lite")
            .with_inner_size(size)
            .with_min_inner_size([360.0, 240.0])
            .with_clamp_size_to_monitor_size(true)
            .with_icon(ui::app_icon()),
        renderer: eframe::Renderer::Glow,
        ..Default::default()
    };
    eframe::run_native(
        "MCU Flasher Lite",
        options,
        Box::new(|cc| Ok(Box::new(ui::LiteApp::new(cc)))),
    )
}
