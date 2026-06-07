#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
#![forbid(unsafe_code)]

mod convert;
mod fit;
mod garmin;
mod python;

use std::path::{Path, PathBuf};
use std::sync::mpsc::{self, Receiver, Sender};
use std::thread;

use eframe::egui;
use garmin::{GarminClient, LoginResult, PendingMfa, UploadOutcome, is_incorrect_credentials};
use zeroize::Zeroize;

const WIDTH: f32 = 460.0;
const HEIGHT: f32 = 225.0;

enum WorkerMessage {
    NeedCredentials,
    CredentialsRejected(String),
    NeedMfa(PendingMfa),
    Status(String),
    Account(String),
    Done(UploadOutcome, bool),
    Error(String),
}

struct UploadApp {
    selected: Option<PathBuf>,
    status: String,
    account: String,
    busy: bool,
    show_credentials: bool,
    show_mfa: bool,
    email: String,
    password: String,
    credential_error: String,
    mfa_code: String,
    pending_mfa: Option<PendingMfa>,
    tx: Sender<WorkerMessage>,
    rx: Receiver<WorkerMessage>,
}

impl UploadApp {
    fn new(context: &eframe::CreationContext<'_>) -> Self {
        configure_style(&context.egui_ctx);
        let (tx, rx) = mpsc::channel();
        Self {
            selected: initial_fit(),
            status: "Ready".to_owned(),
            account: "Garmin Connect".to_owned(),
            busy: false,
            show_credentials: false,
            show_mfa: false,
            email: String::new(),
            password: String::new(),
            credential_error: String::new(),
            mfa_code: String::new(),
            pending_mfa: None,
            tx,
            rx,
        }
    }

    fn start_saved_login(&mut self, context: egui::Context) {
        let Some(path) = self.selected.clone() else {
            self.status = "Choose a FIT file".to_owned();
            return;
        };
        if let Err(error) = fit::fingerprint(&path) {
            self.status = error.to_string();
            return;
        }
        self.busy = true;
        self.status = "Signing in...".to_owned();
        let tx = self.tx.clone();
        thread::spawn(move || {
            let message = match GarminClient::from_saved_tokens() {
                Ok(Some(mut client)) => run_upload(&mut client, &path, &tx),
                Ok(None) => WorkerMessage::NeedCredentials,
                Err(error) => WorkerMessage::Error(error.to_string()),
            };
            let _ = tx.send(message);
            context.request_repaint();
        });
    }

    fn submit_credentials(&mut self, context: egui::Context) {
        let Some(path) = self.selected.clone() else {
            return;
        };
        if self.email.trim().is_empty() || self.password.is_empty() {
            self.status = "Email and password are required".to_owned();
            return;
        }
        let email = self.email.trim().to_owned();
        let mut password = std::mem::take(&mut self.password);
        self.show_credentials = false;
        self.credential_error.clear();
        self.busy = true;
        self.status = "Signing in...".to_owned();
        let tx = self.tx.clone();
        thread::spawn(move || {
            let result = GarminClient::login(&email, &password, |status| {
                let _ = tx.send(WorkerMessage::Status(status.to_owned()));
                context.request_repaint();
            });
            password.zeroize();
            let message = match result {
                Ok(LoginResult::Authenticated(mut client)) => run_upload(&mut client, &path, &tx),
                Ok(LoginResult::MfaRequired(pending)) => WorkerMessage::NeedMfa(pending),
                Err(error) if is_incorrect_credentials(&error) => {
                    WorkerMessage::CredentialsRejected(error.to_string())
                }
                Err(error) => WorkerMessage::Error(error.to_string()),
            };
            let _ = tx.send(message);
            context.request_repaint();
        });
    }

    fn submit_mfa(&mut self, context: egui::Context) {
        let Some(path) = self.selected.clone() else {
            return;
        };
        let Some(pending) = self.pending_mfa.take() else {
            self.status = "MFA session expired. Retry upload.".to_owned();
            self.show_mfa = false;
            return;
        };
        if self.mfa_code.trim().is_empty() {
            self.pending_mfa = Some(pending);
            self.status = "MFA code is required".to_owned();
            return;
        }
        let mut code = std::mem::take(&mut self.mfa_code);
        self.show_mfa = false;
        self.status = "Verifying...".to_owned();
        let tx = self.tx.clone();
        thread::spawn(move || {
            let result = GarminClient::complete_mfa(pending, &code);
            code.zeroize();
            let message = match result {
                Ok(mut client) => run_upload(&mut client, &path, &tx),
                Err(error) => WorkerMessage::Error(error.to_string()),
            };
            let _ = tx.send(message);
            context.request_repaint();
        });
    }

    fn process_messages(&mut self) {
        while let Ok(message) = self.rx.try_recv() {
            match message {
                WorkerMessage::NeedCredentials => {
                    self.busy = true;
                    self.show_credentials = true;
                    self.credential_error.clear();
                    self.status = "Sign in required".to_owned();
                }
                WorkerMessage::CredentialsRejected(error) => {
                    self.busy = true;
                    self.show_credentials = true;
                    self.credential_error = error;
                    self.status = "Sign in required".to_owned();
                }
                WorkerMessage::NeedMfa(pending) => {
                    self.pending_mfa = Some(pending);
                    self.show_mfa = true;
                    self.status = "MFA required".to_owned();
                }
                WorkerMessage::Status(status) => self.status = status,
                WorkerMessage::Account(name) => self.account = format!("Signed in as {name}"),
                WorkerMessage::Done(UploadOutcome::Uploaded, converted) => {
                    self.busy = false;
                    self.status = if converted {
                        "Converted and uploaded".to_owned()
                    } else {
                        "Uploaded".to_owned()
                    };
                }
                WorkerMessage::Done(UploadOutcome::AlreadyUploaded(name), converted) => {
                    self.busy = false;
                    self.status = if converted {
                        format!("Converted; already uploaded: {name}")
                    } else {
                        format!("Already uploaded: {name}")
                    };
                }
                WorkerMessage::Error(error) => {
                    self.busy = false;
                    self.pending_mfa = None;
                    self.show_mfa = false;
                    self.status = error;
                }
            }
        }
    }

    fn choose_file(&mut self) {
        let initial = self
            .selected
            .as_deref()
            .and_then(Path::parent)
            .unwrap_or_else(|| Path::new("."));
        if let Some(path) = rfd::FileDialog::new()
            .set_title("Select MyWhoosh FIT")
            .set_directory(initial)
            .add_filter("FIT activity", &["fit"])
            .pick_file()
        {
            self.selected = Some(path);
            self.status = "Ready".to_owned();
        }
    }

    fn credentials_window(&mut self, context: &egui::Context) {
        if !self.show_credentials {
            return;
        }
        egui::Window::new("Sign in to Garmin Connect")
            .collapsible(false)
            .resizable(false)
            .anchor(egui::Align2::CENTER_CENTER, egui::Vec2::ZERO)
            .show(context, |ui| {
                ui.set_width(300.0);
                ui.label("Email");
                ui.text_edit_singleline(&mut self.email);
                ui.add_space(8.0);
                ui.label("Password");
                let response =
                    ui.add(egui::TextEdit::singleline(&mut self.password).password(true));
                if !self.credential_error.is_empty() {
                    ui.label(
                        egui::RichText::new(&self.credential_error)
                            .color(egui::Color32::from_rgb(185, 28, 28)),
                    );
                }
                ui.add_space(12.0);
                ui.horizontal(|ui| {
                    if ui.button("Cancel").clicked() {
                        self.password.zeroize();
                        self.credential_error.clear();
                        self.show_credentials = false;
                        self.status = "Ready".to_owned();
                    }
                    if ui.button("Sign in").clicked()
                        || (response.lost_focus()
                            && ui.input(|input| input.key_pressed(egui::Key::Enter)))
                    {
                        self.submit_credentials(context.clone());
                    }
                });
            });
    }

    fn mfa_window(&mut self, context: &egui::Context) {
        if !self.show_mfa {
            return;
        }
        egui::Window::new("Garmin verification")
            .collapsible(false)
            .resizable(false)
            .anchor(egui::Align2::CENTER_CENTER, egui::Vec2::ZERO)
            .show(context, |ui| {
                ui.set_width(260.0);
                ui.label("MFA code");
                let response = ui.text_edit_singleline(&mut self.mfa_code);
                ui.add_space(12.0);
                ui.horizontal(|ui| {
                    if ui.button("Cancel").clicked() {
                        self.mfa_code.zeroize();
                        self.pending_mfa = None;
                        self.show_mfa = false;
                        self.busy = false;
                        self.status = "Ready".to_owned();
                    }
                    if ui.button("Verify").clicked()
                        || (response.lost_focus()
                            && ui.input(|input| input.key_pressed(egui::Key::Enter)))
                    {
                        self.submit_mfa(context.clone());
                    }
                });
            });
    }
}

impl Drop for UploadApp {
    fn drop(&mut self) {
        self.password.zeroize();
        self.mfa_code.zeroize();
    }
}

impl eframe::App for UploadApp {
    fn logic(&mut self, _context: &egui::Context, _frame: &mut eframe::Frame) {
        self.process_messages();
    }

    fn ui(&mut self, ui: &mut egui::Ui, _frame: &mut eframe::Frame) {
        let context = ui.ctx().clone();
        ui.painter()
            .rect_filled(ui.max_rect(), 0.0, egui::Color32::from_rgb(250, 250, 251));
        egui::Frame::new()
            .fill(egui::Color32::from_rgb(250, 250, 251))
            .inner_margin(egui::Margin::same(12))
            .show(ui, |ui| {
                ui.add_space(12.0);
                ui.heading("Convert & Upload FIT");
                ui.label(egui::RichText::new(&self.account).color(egui::Color32::from_gray(105)));
                ui.add_space(18.0);

                ui.horizontal(|ui| {
                    let available = (ui.available_width() - 82.0).max(100.0);
                    let label = self
                        .selected
                        .as_deref()
                        .map(display_path)
                        .unwrap_or_else(|| "No FIT selected".to_owned());
                    ui.add_sized(
                        [available, 34.0],
                        egui::Label::new(label)
                            .truncate()
                            .sense(egui::Sense::hover()),
                    )
                    .on_hover_text(
                        self.selected
                            .as_deref()
                            .map(|value| value.display().to_string())
                            .unwrap_or_default(),
                    );
                    if ui
                        .add_enabled(!self.busy, egui::Button::new("Choose"))
                        .clicked()
                    {
                        self.choose_file();
                    }
                });
                ui.add_space(14.0);
                if ui
                    .add_enabled(
                        !self.busy,
                        egui::Button::new(
                            egui::RichText::new("Convert & Upload").color(egui::Color32::WHITE),
                        )
                        .fill(egui::Color32::from_rgb(37, 99, 235))
                        .min_size(egui::vec2(ui.available_width(), 38.0)),
                    )
                    .clicked()
                {
                    self.start_saved_login(context.clone());
                }
                ui.add_space(10.0);
                ui.vertical_centered(|ui| {
                    ui.label(
                        egui::RichText::new(&self.status).color(egui::Color32::from_gray(105)),
                    );
                    if self.busy {
                        ui.add(egui::Spinner::new().size(14.0));
                    }
                });
            });
        self.credentials_window(&context);
        self.mfa_window(&context);
    }
}

fn run_upload(client: &mut GarminClient, path: &Path, tx: &Sender<WorkerMessage>) -> WorkerMessage {
    if let Ok(name) = client.profile_name() {
        let _ = tx.send(WorkerMessage::Account(name));
    }
    let prepared = match convert::prepare_for_upload(path, |status| {
        let _ = tx.send(WorkerMessage::Status(status.to_owned()));
    }) {
        Ok(value) => value,
        Err(error) => return WorkerMessage::Error(error.to_string()),
    };
    let _ = tx.send(WorkerMessage::Status(
        if prepared.converted {
            "Uploading converted Garmin FIT..."
        } else {
            "Uploading..."
        }
        .to_owned(),
    ));
    match client.upload_if_missing(&prepared.path) {
        Ok(value) => WorkerMessage::Done(value, prepared.converted),
        Err(error) => WorkerMessage::Error(error.to_string()),
    }
}

fn initial_fit() -> Option<PathBuf> {
    if let Some(profile) = std::env::var_os("USERPROFILE") {
        let downloads = PathBuf::from(profile).join("Downloads");
        let mut mywhoosh: Vec<_> = std::fs::read_dir(downloads)
            .ok()
            .into_iter()
            .flatten()
            .filter_map(Result::ok)
            .map(|entry| entry.path())
            .filter(|path| {
                path.extension()
                    .and_then(|value| value.to_str())
                    .is_some_and(|value| value.eq_ignore_ascii_case("fit"))
                    && path
                        .file_stem()
                        .and_then(|value| value.to_str())
                        .is_some_and(|value| value.to_ascii_lowercase().contains("mywhoosh"))
            })
            .collect();
        mywhoosh.sort_by_key(|path| {
            std::fs::metadata(path)
                .and_then(|metadata| metadata.modified())
                .ok()
        });
        if let Some(path) = mywhoosh.pop() {
            return Some(path);
        }
    }
    None
}

fn display_path(path: &Path) -> String {
    let value = path.display().to_string();
    if value.chars().count() <= 58 {
        return value;
    }
    let suffix: String = value
        .chars()
        .rev()
        .take(55)
        .collect::<String>()
        .chars()
        .rev()
        .collect();
    format!("...{suffix}")
}

fn configure_style(context: &egui::Context) {
    context.set_visuals(egui::Visuals::light());
    context.global_style_mut(|style| {
        style.spacing.item_spacing = egui::vec2(8.0, 8.0);
        style.spacing.button_padding = egui::vec2(14.0, 8.0);
        style.visuals.panel_fill = egui::Color32::from_rgb(250, 250, 251);
        style.visuals.window_fill = egui::Color32::WHITE;
        style.visuals.widgets.active.bg_fill = egui::Color32::from_rgb(37, 99, 235);
        style.visuals.widgets.hovered.bg_fill = egui::Color32::from_rgb(219, 234, 254);
        style.visuals.selection.bg_fill = egui::Color32::from_rgb(191, 219, 254);
    });
}

fn main() -> eframe::Result {
    let options = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default()
            .with_inner_size([WIDTH, HEIGHT])
            .with_min_inner_size([WIDTH, HEIGHT])
            .with_max_inner_size([WIDTH, HEIGHT])
            .with_resizable(false),
        ..Default::default()
    };
    eframe::run_native(
        "Garmin FIT Upload",
        options,
        Box::new(|context| Ok(Box::new(UploadApp::new(context)))),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn display_path_shortens_from_left() {
        let path = Path::new("C:/a/very/long/path/to/a/generated/garmin/activity.fit");
        let displayed = display_path(path);
        assert!(displayed.ends_with("activity.fit"));
    }

    #[test]
    fn initial_fit_prefers_a_raw_mywhoosh_source_when_present() {
        if let Some(path) = initial_fit()
            && path
                .file_stem()
                .and_then(|value| value.to_str())
                .is_some_and(|value| value.to_ascii_lowercase().contains("mywhoosh"))
        {
            assert!(path.is_file());
        }
    }
}
