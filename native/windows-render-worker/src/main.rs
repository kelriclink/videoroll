#![cfg_attr(target_os = "windows", windows_subsystem = "windows")]

mod api;
mod config;
mod ffmpeg;
mod hardware;
mod logging;
mod paths;
mod protocol;
mod worker;

use config::{AppConfig, Credential};
use eframe::egui;
use hardware::Device;
use logging::SharedLog;
use paths::AppPaths;
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;
use worker::WorkerHandle;

const APP_TITLE: &str = "VideoRoll Render Worker";

fn main() -> eframe::Result {
    let paths = match AppPaths::discover().and_then(|paths| {
        paths.ensure()?;
        Ok(paths)
    }) {
        Ok(paths) => paths,
        Err(error) => {
            eprintln!("VideoRoll Render Worker startup failed: {error:#}");
            return Ok(());
        }
    };

    let options = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default()
            .with_title(APP_TITLE)
            .with_inner_size([940.0, 720.0])
            .with_min_inner_size([760.0, 560.0]),
        ..Default::default()
    };

    eframe::run_native(
        APP_TITLE,
        options,
        Box::new(move |_cc| Ok(Box::new(RenderWorkerApp::new(paths)))),
    )
}

struct RenderWorkerApp {
    paths: AppPaths,
    log: SharedLog,
    config: AppConfig,
    enrollment_token: String,
    worker: Option<WorkerHandle>,
    scan_result: Arc<Mutex<Option<Result<Vec<Device>, String>>>>,
    detected_devices: Vec<Device>,
    ui_message: String,
}

impl RenderWorkerApp {
    fn new(paths: AppPaths) -> Self {
        let log = SharedLog::new(&paths);
        log.info(format!("application directory: {}", paths.root.display()));
        Self {
            config: AppConfig::load(&paths),
            paths,
            log,
            enrollment_token: String::new(),
            worker: None,
            scan_result: Arc::new(Mutex::new(None)),
            detected_devices: Vec::new(),
            ui_message: String::new(),
        }
    }

    fn start_scan(&mut self) {
        if self
            .scan_result
            .lock()
            .ok()
            .and_then(|result| result.as_ref().map(|_| ()))
            .is_some()
        {
            return;
        }
        self.ui_message = "Scanning hardware...".to_string();
        let slot = Arc::clone(&self.scan_result);
        let ffmpeg = self.paths.ffmpeg();
        let log = self.log.clone();
        thread::spawn(move || {
            let result = hardware::scan(&ffmpeg)
                .map(|snapshot| snapshot.devices)
                .map_err(|error| format!("{error:#}"));
            if let Ok(ref devices) = result {
                for device in devices {
                    log.info(format!(
                        "hardware scan: {} [{}] {}",
                        device.name,
                        device.backend,
                        device.encoders.join(", ")
                    ));
                }
            }
            if let Ok(mut target) = slot.lock() {
                *target = Some(result);
            }
        });
    }

    fn consume_scan(&mut self) {
        let result = self
            .scan_result
            .lock()
            .ok()
            .and_then(|mut slot| slot.take());
        if let Some(result) = result {
            match result {
                Ok(devices) => {
                    self.detected_devices = devices;
                    self.ui_message = format!("Detected {} render device(s).", self.detected_devices.len());
                }
                Err(error) => {
                    self.detected_devices.clear();
                    self.ui_message = format!("Hardware scan failed: {error}");
                }
            }
        }
    }

    fn start_worker(&mut self) {
        if self.worker.as_ref().is_some_and(WorkerHandle::is_running) {
            return;
        }
        self.config.max_concurrency = self.config.max_concurrency.clamp(1, 32);
        if self.config.normalized_server_url().is_empty() {
            self.ui_message = "Server URL is required.".to_string();
            return;
        }
        if Credential::load(&self.paths).is_none() && self.enrollment_token.trim().is_empty() {
            self.ui_message =
                "This PC is not paired. Paste a one-time vre_* enrollment token first.".to_string();
            return;
        }
        if let Err(error) = self.config.save(&self.paths) {
            self.ui_message = format!("Cannot save configuration: {error:#}");
            return;
        }
        self.log.info(format!(
            "starting worker: server={} node={} max_concurrency={}",
            self.config.normalized_server_url(),
            self.config.node_name,
            self.config.max_concurrency
        ));
        self.worker = Some(WorkerHandle::start(
            self.config.clone(),
            self.enrollment_token.trim().to_string(),
            self.paths.clone(),
            self.log.clone(),
        ));
        self.enrollment_token.clear();
        self.ui_message = "Worker starting...".to_string();
    }

    fn stop_worker(&mut self) {
        if let Some(worker) = &self.worker {
            worker.stop_accepting();
            self.ui_message =
                "Drain requested. No new jobs will be claimed; active jobs may finish.".to_string();
        }
    }

    fn reset_pairing(&mut self) {
        if self.worker.as_ref().is_some_and(WorkerHandle::is_running) {
            self.ui_message = "Stop/drain the worker before resetting pairing.".to_string();
            return;
        }
        match Credential::remove(&self.paths) {
            Ok(()) => {
                self.enrollment_token.clear();
                self.ui_message = "Local credential removed. Generate a new vre_* token.".to_string();
                self.log.info("local pairing credential removed");
            }
            Err(error) => {
                self.ui_message = format!("Cannot remove credential: {error:#}");
            }
        }
    }

    fn paired(&self) -> bool {
        Credential::load(&self.paths).is_some()
    }
}

impl eframe::App for RenderWorkerApp {
    fn update(&mut self, ctx: &egui::Context, _frame: &mut eframe::Frame) {
        self.consume_scan();
        ctx.request_repaint_after(Duration::from_millis(500));

        let status = self.worker.as_ref().map(WorkerHandle::status);
        if let Some(state) = &status {
            if !state.last_error.is_empty() {
                self.ui_message = state.last_error.clone();
            }
            if !state.devices.is_empty() {
                self.detected_devices = state.devices.clone();
            }
        }

        egui::CentralPanel::default().show(ctx, |ui| {
            ui.heading("VideoRoll Native Render Worker");
            ui.label("Rust worker · self-contained install directory · Intel QSV / NVIDIA NVENC / CPU fallback");
            ui.add_space(8.0);

            egui::Grid::new("configuration")
                .num_columns(2)
                .spacing([12.0, 8.0])
                .show(ui, |ui| {
                    ui.label("Server API");
                    ui.text_edit_singleline(&mut self.config.server_url);
                    ui.end_row();

                    ui.label("One-time token");
                    ui.add(
                        egui::TextEdit::singleline(&mut self.enrollment_token)
                            .password(true)
                            .hint_text("vre_* (first pairing only)"),
                    );
                    ui.end_row();

                    ui.label("Node name");
                    ui.text_edit_singleline(&mut self.config.node_name);
                    ui.end_row();

                    ui.label("Max concurrency");
                    ui.add(egui::DragValue::new(&mut self.config.max_concurrency).range(1..=32));
                    ui.end_row();

                    ui.label("Pairing");
                    ui.label(if self.paired() { "Paired" } else { "Not paired" });
                    ui.end_row();

                    ui.label("Install directory");
                    ui.monospace(self.paths.root.display().to_string());
                    ui.end_row();
                });

            ui.add_space(10.0);
            ui.horizontal(|ui| {
                if ui.button("Scan hardware").clicked() {
                    self.start_scan();
                }
                let running = status.as_ref().is_some_and(|state| state.running);
                if ui
                    .add_enabled(!running, egui::Button::new("Start worker"))
                    .clicked()
                {
                    self.start_worker();
                }
                if ui
                    .add_enabled(running, egui::Button::new("Stop accepting"))
                    .clicked()
                {
                    self.stop_worker();
                }
                if ui
                    .add_enabled(!running, egui::Button::new("Reset pairing"))
                    .clicked()
                {
                    self.reset_pairing();
                }
            });

            if let Some(state) = &status {
                ui.add_space(6.0);
                ui.horizontal(|ui| {
                    ui.label(format!("State: {}", state.phase));
                    ui.separator();
                    ui.label(format!("Connected: {}", state.connected));
                    ui.separator();
                    ui.label(format!("Active jobs: {}", state.active_jobs));
                    if let Some(worker_id) = state.worker_id {
                        ui.separator();
                        ui.monospace(worker_id.to_string());
                    }
                });
            }

            if !self.ui_message.is_empty() {
                ui.add_space(6.0);
                ui.label(&self.ui_message);
            }

            ui.separator();
            ui.heading("Render devices");
            if self.detected_devices.is_empty() {
                ui.label("No scan result yet.");
            } else {
                egui::Grid::new("devices")
                    .striped(true)
                    .num_columns(4)
                    .show(ui, |ui| {
                        ui.strong("Device");
                        ui.strong("Backend");
                        ui.strong("Index");
                        ui.strong("Encoders");
                        ui.end_row();
                        for device in &self.detected_devices {
                            ui.label(&device.name);
                            ui.label(&device.backend);
                            ui.label(
                                device
                                    .index
                                    .map(|value| value.to_string())
                                    .unwrap_or_else(|| "-".to_string()),
                            );
                            ui.monospace(device.encoders.join(", "));
                            ui.end_row();
                        }
                    });
            }

            ui.separator();
            ui.heading("Runtime files");
            ui.monospace("bin/     ffmpeg.exe, ffprobe.exe");
            ui.monospace("config/  config.json, credential.json");
            ui.monospace("logs/    render-worker.log");
            ui.monospace("cache/   local cache");
            ui.monospace("work/    active execution scratch space");

            ui.separator();
            ui.heading("Recent log");
            let lines = self.log.snapshot();
            egui::ScrollArea::vertical()
                .max_height(220.0)
                .stick_to_bottom(true)
                .show(ui, |ui| {
                    for line in lines.iter().rev().take(100).rev() {
                        ui.monospace(line);
                    }
                });
        });
    }

    fn on_exit(&mut self, _gl: Option<&eframe::glow::Context>) {
        if let Some(worker) = &self.worker {
            worker.stop_accepting();
        }
    }
}
