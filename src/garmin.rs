use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;
use std::{error::Error, fmt};

#[cfg(windows)]
use std::os::windows::process::CommandExt;

use anyhow::{Context, Result, anyhow, bail};
use base64::{Engine as _, engine::general_purpose};
use chrono::{DateTime, Duration as ChronoDuration, NaiveDateTime, Utc};
use keyring::Entry;
use reqwest::StatusCode;
use reqwest::blocking::{Client, RequestBuilder, multipart};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use zeroize::{Zeroize, ZeroizeOnDrop};

use crate::fit::{ActivityFingerprint, load};

const SERVICE: &str = "fit-fix-garmin-upload";
const ACCOUNT: &str = "garmin-connect-di-tokens";
const SSO: &str = "https://sso.garmin.com";
const CONNECT_API: &str = "https://connectapi.garmin.com";
const DI_TOKEN_URL: &str = "https://diauth.garmin.com/di-oauth2-service/oauth/token";
const IOS_CLIENT_ID: &str = "GCM_IOS_DARK";
const IOS_SERVICE_URL: &str = "https://mobile.integration.garmin.com/gcm/ios";
const PORTAL_CLIENT_ID: &str = "GarminConnect";
const PORTAL_SERVICE_URL: &str = "https://connect.garmin.com/app";
const DI_GRANT_TYPE: &str =
    "https://connectapi.garmin.com/di-oauth2-service/oauth/grant/service_ticket";
const IOS_LOGIN_UA: &str = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148";
const PORTAL_LOGIN_UA: &str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36";
const NATIVE_API_UA: &str = "GCM-Android-5.23";
const NATIVE_GARMIN_UA: &str = "com.garmin.android.apps.connectmobile/5.23; ; Google/sdk_gphone64_arm64/google; Android/33; Dalvik/2.1.0";
const DI_CLIENT_IDS: [&str; 4] = [
    "GARMIN_CONNECT_MOBILE_ANDROID_DI_2025Q2",
    "GARMIN_CONNECT_MOBILE_ANDROID_DI_2024Q4",
    "GARMIN_CONNECT_MOBILE_ANDROID_DI",
    "GARMIN_CONNECT_MOBILE_IOS_DI",
];
const START_TOLERANCE_SECONDS: i64 = 5;
const DURATION_TOLERANCE_SECONDS: f64 = 10.0;
const MAX_ACTIVITY_PAGES: usize = 20;
const CREATE_NO_WINDOW: u32 = 0x0800_0000;
const TOKEN_STORE_FORMAT: u8 = 1;
const TOKEN_CHUNK_BYTES: usize = 2_400;
const MAX_TOKEN_BYTES: usize = 64 * 1024;
const MAX_TOKEN_CHUNKS: usize = MAX_TOKEN_BYTES.div_ceil(TOKEN_CHUNK_BYTES);
static DIRECT_SSO_BLOCKED: AtomicBool = AtomicBool::new(false);

#[derive(Debug)]
struct IncorrectCredentials(String);

impl fmt::Display for IncorrectCredentials {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.0)
    }
}

impl Error for IncorrectCredentials {}

#[derive(Debug)]
struct SsoRateLimited(String);

impl fmt::Display for SsoRateLimited {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.0)
    }
}

impl Error for SsoRateLimited {}

#[derive(Serialize, Deserialize, Zeroize, ZeroizeOnDrop)]
struct Tokens {
    access_token: String,
    refresh_token: String,
    client_id: String,
}

#[derive(Debug, Deserialize, Serialize)]
struct TokenManifest {
    format: u8,
    checksum: String,
    byte_len: usize,
    chunk_count: usize,
}

impl TokenManifest {
    fn for_secret(secret: &[u8]) -> Result<Self> {
        if secret.is_empty() || secret.len() > MAX_TOKEN_BYTES {
            bail!(
                "Garmin token data is an unexpected size ({} bytes)",
                secret.len()
            );
        }
        Ok(Self {
            format: TOKEN_STORE_FORMAT,
            checksum: token_checksum(secret),
            byte_len: secret.len(),
            chunk_count: secret.len().div_ceil(TOKEN_CHUNK_BYTES),
        })
    }

    fn validate(&self) -> Result<()> {
        if self.format != TOKEN_STORE_FORMAT {
            bail!("Unsupported Garmin token storage format");
        }
        if self.byte_len == 0 || self.byte_len > MAX_TOKEN_BYTES {
            bail!("Invalid Garmin token storage length");
        }
        if self.chunk_count == 0
            || self.chunk_count > MAX_TOKEN_CHUNKS
            || self.chunk_count != self.byte_len.div_ceil(TOKEN_CHUNK_BYTES)
        {
            bail!("Invalid Garmin token storage chunk count");
        }
        if self.checksum.len() != 43
            || !self
                .checksum
                .bytes()
                .all(|value| value.is_ascii_alphanumeric() || matches!(value, b'-' | b'_'))
        {
            bail!("Invalid Garmin token storage checksum");
        }
        Ok(())
    }
}

pub struct PendingMfa {
    session: MfaSession,
}

enum MfaSession {
    Direct(DirectMfa),
    Bridge(AuthBridge),
}

struct AuthBridge {
    child: Child,
    stdin: ChildStdin,
    stdout: BufReader<ChildStdout>,
}

#[derive(Deserialize)]
struct BridgeResponse {
    status: String,
    kind: Option<String>,
    message: Option<String>,
    tokens: Option<Tokens>,
}

#[derive(Serialize)]
struct BridgeCredentials<'a> {
    email: &'a str,
    password: &'a str,
}

#[derive(Serialize)]
struct BridgeMfa<'a> {
    mfa_code: &'a str,
}

struct DirectMfa {
    http: Client,
    method: String,
    flow: SsoFlow,
}

#[derive(Clone, Copy)]
enum SsoFlow {
    Mobile,
    Portal,
}

impl SsoFlow {
    fn path(self) -> &'static str {
        match self {
            Self::Mobile => "mobile",
            Self::Portal => "portal",
        }
    }

    fn client_id(self) -> &'static str {
        match self {
            Self::Mobile => IOS_CLIENT_ID,
            Self::Portal => PORTAL_CLIENT_ID,
        }
    }

    fn service_url(self) -> &'static str {
        match self {
            Self::Mobile => IOS_SERVICE_URL,
            Self::Portal => PORTAL_SERVICE_URL,
        }
    }
}

pub enum LoginResult {
    Authenticated(GarminClient),
    MfaRequired(PendingMfa),
}

pub struct GarminClient {
    http: Client,
    tokens: Tokens,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Activity {
    pub activity_id: Option<u64>,
    pub activity_name: Option<String>,
    pub start_time_gmt: Option<String>,
    pub duration: Option<f64>,
    pub elapsed_duration: Option<f64>,
    pub moving_duration: Option<f64>,
    pub distance: Option<f64>,
}

#[derive(Clone, Debug)]
pub enum UploadOutcome {
    Uploaded,
    AlreadyUploaded(String),
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct LoginResponse {
    response_status: Option<ResponseStatus>,
    service_ticket_id: Option<String>,
    customer_mfa_info: Option<MfaInfo>,
    error: Option<Value>,
}

#[derive(Deserialize)]
struct ResponseStatus {
    #[serde(rename = "type")]
    kind: String,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct MfaInfo {
    mfa_last_method_used: Option<String>,
}

#[derive(Deserialize, Zeroize, ZeroizeOnDrop)]
struct TokenResponse {
    access_token: String,
    refresh_token: Option<String>,
}

impl AuthBridge {
    fn start(email: &str, password: &str) -> Result<(Self, BridgeResponse)> {
        let mut child = spawn_auth_bridge()?;
        let stdin = child
            .stdin
            .take()
            .context("Authentication bridge has no input")?;
        let stdout = child
            .stdout
            .take()
            .context("Authentication bridge has no output")?;
        let mut bridge = Self {
            child,
            stdin,
            stdout: BufReader::new(stdout),
        };
        bridge.send(&BridgeCredentials { email, password })?;
        let response = bridge.receive()?;
        Ok((bridge, response))
    }

    fn send<T: Serialize>(&mut self, value: &T) -> Result<()> {
        serde_json::to_writer(&mut self.stdin, value)
            .context("Could not send authentication bridge input")?;
        self.stdin
            .write_all(b"\n")
            .and_then(|_| self.stdin.flush())
            .context("Could not flush authentication bridge input")
    }

    fn receive(&mut self) -> Result<BridgeResponse> {
        let mut line = String::new();
        self.stdout
            .read_line(&mut line)
            .context("Could not read authentication bridge response")?;
        if line.is_empty() {
            bail!(
                "Browser-compatible Garmin sign-in was unavailable. Run: python -m pip install \".[auth]\""
            );
        }
        let response =
            serde_json::from_str(&line).context("Authentication bridge returned invalid data");
        line.zeroize();
        response
    }
}

impl Drop for AuthBridge {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

impl GarminClient {
    pub fn from_saved_tokens() -> Result<Option<Self>> {
        let Some(tokens) = load_saved_tokens()? else {
            return Ok(None);
        };
        let mut client = Self {
            http: http_client()?,
            tokens,
        };
        match client.profile_name() {
            Ok(_) => Ok(Some(client)),
            Err(error) if is_auth_error(&error) => {
                clear_saved_tokens()?;
                Ok(None)
            }
            Err(error) => Err(error),
        }
    }

    pub fn login<F>(email: &str, password: &str, report: F) -> Result<LoginResult>
    where
        F: Fn(&str),
    {
        if email.trim().is_empty() || password.is_empty() {
            bail!("Email and password are required");
        }
        if DIRECT_SSO_BLOCKED.load(Ordering::Relaxed) {
            report("Using browser-compatible sign-in (usually 10-30 seconds)...");
            return Self::bridge_login(email.trim(), password);
        }
        let mobile = http_client()?;
        match Self::login_flow(mobile, SsoFlow::Mobile, email.trim(), password) {
            Ok(result) => Ok(result),
            Err(error) if is_incorrect_credentials(&error) => Err(error),
            Err(mobile_error) => {
                DIRECT_SSO_BLOCKED.store(true, Ordering::Relaxed);
                report("Using browser-compatible sign-in (usually 10-30 seconds)...");
                Self::bridge_login(email.trim(), password)
                    .with_context(|| format!("Direct sign-in failed first: {mobile_error:#}"))
            }
        }
    }

    fn bridge_login(email: &str, password: &str) -> Result<LoginResult> {
        let (bridge, response) = AuthBridge::start(email, password)?;
        Self::bridge_login_result(bridge, response)
    }

    fn bridge_login_result(bridge: AuthBridge, response: BridgeResponse) -> Result<LoginResult> {
        match response.status.as_str() {
            "mfa_required" => Ok(LoginResult::MfaRequired(PendingMfa {
                session: MfaSession::Bridge(bridge),
            })),
            "authenticated" => {
                let tokens = response
                    .tokens
                    .context("Authentication bridge omitted Garmin tokens")?;
                let mut client = Self {
                    http: http_client()?,
                    tokens,
                };
                client.profile_name()?;
                client.save_tokens()?;
                Ok(LoginResult::Authenticated(client))
            }
            "error" => Err(bridge_error(response)),
            other => bail!("Authentication bridge returned unknown status: {other}"),
        }
    }

    fn login_flow(http: Client, flow: SsoFlow, email: &str, password: &str) -> Result<LoginResult> {
        let response = sso_request(&http, flow, "login")
            .json(&json!({
                "username": email,
                "password": password,
                "rememberMe": true,
                "captchaToken": ""
            }))
            .send()
            .context("Could not reach Garmin sign-in")?;
        let status = response.status();
        if status == StatusCode::TOO_MANY_REQUESTS {
            return Err(anyhow!(SsoRateLimited(rate_limit_message(
                retry_after(response.headers()),
                match flow {
                    SsoFlow::Mobile => "mobile sign-in",
                    SsoFlow::Portal => "portal sign-in",
                }
            ))));
        }
        if status == StatusCode::FORBIDDEN {
            bail!("Garmin blocked {} sign-in with HTTP 403", flow.path());
        }
        let body: LoginResponse = response
            .json()
            .with_context(|| format!("Garmin sign-in returned HTTP {status} with invalid data"))?;
        if login_body_rate_limited(&body) {
            return Err(anyhow!(SsoRateLimited(format!(
                "Garmin returned a hidden 429 for {} sign-in.",
                flow.path()
            ))));
        }
        match body
            .response_status
            .as_ref()
            .map(|value| value.kind.as_str())
        {
            Some("SUCCESSFUL") => {
                let ticket = body
                    .service_ticket_id
                    .context("Garmin sign-in omitted the service ticket")?;
                let client = Self::from_ticket(http_client()?, &ticket, flow.service_url())?;
                client.save_tokens()?;
                Ok(LoginResult::Authenticated(client))
            }
            Some("MFA_REQUIRED") => Ok(LoginResult::MfaRequired(PendingMfa {
                session: MfaSession::Direct(DirectMfa {
                    http,
                    method: body
                        .customer_mfa_info
                        .and_then(|value| value.mfa_last_method_used)
                        .unwrap_or_else(|| "email".to_owned()),
                    flow,
                }),
            })),
            Some("INVALID_USERNAME_PASSWORD") => Err(anyhow!(IncorrectCredentials(
                "Incorrect Garmin email or password".to_owned()
            ))),
            Some("CAPTCHA_REQUIRED") => {
                bail!("Garmin requires a CAPTCHA. Sign in on garmin.com, then retry.")
            }
            other => bail!("Garmin sign-in failed: {other:?} {:?}", body.error),
        }
    }

    pub fn complete_mfa(pending: PendingMfa, code: &str) -> Result<Self> {
        if code.trim().is_empty() {
            bail!("MFA code is required");
        }
        match pending.session {
            MfaSession::Direct(direct) => Self::complete_direct_mfa(direct, code),
            MfaSession::Bridge(mut bridge) => {
                bridge.send(&BridgeMfa {
                    mfa_code: code.trim(),
                })?;
                let response = bridge.receive()?;
                match Self::bridge_login_result(bridge, response)? {
                    LoginResult::Authenticated(client) => Ok(client),
                    LoginResult::MfaRequired(_) => bail!("Garmin requested MFA twice"),
                }
            }
        }
    }

    fn complete_direct_mfa(pending: DirectMfa, code: &str) -> Result<Self> {
        let alternate = match pending.flow {
            SsoFlow::Mobile => SsoFlow::Portal,
            SsoFlow::Portal => SsoFlow::Mobile,
        };
        let mut failures = Vec::new();
        for flow in [pending.flow, alternate] {
            let response = sso_request(&pending.http, flow, "mfa/verifyCode")
                .json(&json!({
                    "mfaMethod": pending.method,
                    "mfaVerificationCode": code.trim(),
                    "rememberMyBrowser": true,
                    "reconsentList": [],
                    "mfaSetup": false
                }))
                .send()
                .context("Could not submit Garmin MFA code")?;
            let status = response.status();
            if status == StatusCode::TOO_MANY_REQUESTS {
                failures.push(rate_limit_message(retry_after(response.headers()), "MFA"));
                continue;
            }
            let body: LoginResponse = response
                .json()
                .with_context(|| format!("Garmin MFA returned HTTP {status} with invalid data"))?;
            if body
                .response_status
                .as_ref()
                .map(|value| value.kind.as_str())
                == Some("SUCCESSFUL")
            {
                let ticket = body
                    .service_ticket_id
                    .context("Garmin MFA omitted the service ticket")?;
                let client =
                    Self::from_ticket(http_client()?, &ticket, pending.flow.service_url())?;
                client.save_tokens()?;
                return Ok(client);
            }
            failures.push(format!("{} MFA returned {:?}", flow.path(), body.error));
        }
        bail!("Garmin rejected the MFA code or blocked verification: {failures:?}")
    }

    fn from_ticket(http: Client, ticket: &str, service_url: &str) -> Result<Self> {
        let mut last_error = None;
        for client_id in DI_CLIENT_IDS {
            let response = http
                .post(DI_TOKEN_URL)
                .headers(native_headers())
                .basic_auth(client_id, Some(""))
                .form(&[
                    ("client_id", client_id),
                    ("service_ticket", ticket),
                    ("grant_type", DI_GRANT_TYPE),
                    ("service_url", service_url),
                ])
                .send()
                .context("Could not exchange Garmin service ticket")?;
            if response.status() == StatusCode::TOO_MANY_REQUESTS {
                bail!("Garmin token exchange is rate limited");
            }
            if !response.status().is_success() {
                last_error = Some(format!("HTTP {}", response.status()));
                continue;
            }
            let mut token: TokenResponse = match response.json() {
                Ok(value) => value,
                Err(error) => {
                    last_error = Some(error.to_string());
                    continue;
                }
            };
            let access_token = std::mem::take(&mut token.access_token);
            let refresh_token = std::mem::take(&mut token.refresh_token)
                .context("Garmin did not return a refresh token")?;
            let tokens = Tokens {
                client_id: jwt_client_id(&access_token).unwrap_or_else(|| client_id.to_owned()),
                refresh_token,
                access_token,
            };
            let mut client = Self {
                http: http.clone(),
                tokens,
            };
            match client.profile_name() {
                Ok(_) => return Ok(client),
                Err(error) => last_error = Some(error.to_string()),
            }
        }
        bail!("Garmin token exchange failed: {last_error:?}");
    }

    pub fn profile_name(&mut self) -> Result<String> {
        let response = self.send_with_auth_retry("verify Garmin login", |client| {
            Ok(client.api_request(reqwest::Method::GET, "/userprofile-service/socialProfile"))
        })?;
        let value: Value = checked_json(response)?;
        Ok(value
            .get("displayName")
            .and_then(Value::as_str)
            .unwrap_or("Garmin Connect")
            .to_owned())
    }

    pub fn upload_if_missing(&mut self, path: &Path) -> Result<UploadOutcome> {
        let fit = load(path)?;
        if let Some(activity) = self.find_duplicate(&fit.fingerprint)? {
            let name = activity
                .activity_name
                .unwrap_or_else(|| "existing Garmin activity".to_owned());
            let suffix = activity
                .activity_id
                .map(|value| format!(" ({value})"))
                .unwrap_or_default();
            return Ok(UploadOutcome::AlreadyUploaded(format!("{name}{suffix}")));
        }

        let file_name = path
            .file_name()
            .and_then(|value| value.to_str())
            .context("FIT path has no valid file name")?
            .to_owned();
        let response = self.send_with_auth_retry("upload FIT to Garmin Connect", |client| {
            let part = multipart::Part::bytes(fit.bytes.clone())
                .file_name(file_name.clone())
                .mime_str("application/octet-stream")?;
            Ok(client
                .api_request(reqwest::Method::POST, "/upload-service/upload")
                .multipart(multipart::Form::new().part("file", part)))
        })?;
        let status = response.status();
        let text = response
            .text()
            .context("Could not read Garmin upload response")?;
        if status == StatusCode::CONFLICT || text.to_lowercase().contains("duplicate") {
            return Ok(UploadOutcome::AlreadyUploaded(
                "Garmin Connect duplicate".to_owned(),
            ));
        }
        if !status.is_success() {
            bail!("Garmin upload failed: HTTP {status} {}", short(&text));
        }
        Ok(UploadOutcome::Uploaded)
    }

    fn find_duplicate(&mut self, fingerprint: &ActivityFingerprint) -> Result<Option<Activity>> {
        let start_date = (fingerprint.start_utc - ChronoDuration::days(1))
            .date_naive()
            .to_string();
        let end_date = (fingerprint.start_utc + ChronoDuration::days(1))
            .date_naive()
            .to_string();
        for page in 0..MAX_ACTIVITY_PAGES {
            let start = (page * 20).to_string();
            let response =
                self.send_with_auth_retry("check existing Garmin activities", |client| {
                    Ok(client
                        .api_request(
                            reqwest::Method::GET,
                            "/activitylist-service/activities/search/activities",
                        )
                        .query(&[
                            ("startDate", start_date.as_str()),
                            ("endDate", end_date.as_str()),
                            ("start", start.as_str()),
                            ("limit", "20"),
                        ]))
                })?;
            let activities: Vec<Activity> = checked_json(response)?;
            if activities.is_empty() {
                return Ok(None);
            }
            if let Some(activity) = activities
                .into_iter()
                .find(|activity| activity_matches(activity, fingerprint))
            {
                return Ok(Some(activity));
            }
        }
        bail!("Garmin activity search exceeded the safety page limit");
    }

    fn refresh(&mut self) -> Result<()> {
        let response = self
            .http
            .post(DI_TOKEN_URL)
            .headers(native_headers())
            .basic_auth(&self.tokens.client_id, Some(""))
            .form(&[
                ("grant_type", "refresh_token"),
                ("client_id", self.tokens.client_id.as_str()),
                ("refresh_token", self.tokens.refresh_token.as_str()),
            ])
            .send()
            .context("Could not refresh Garmin login")?;
        if !response.status().is_success() {
            let status = response.status();
            if matches!(
                status,
                StatusCode::BAD_REQUEST | StatusCode::UNAUTHORIZED | StatusCode::FORBIDDEN
            ) {
                bail!("Garmin authentication expired: HTTP {status}");
            }
            if status == StatusCode::TOO_MANY_REQUESTS {
                bail!("Garmin login refresh is rate limited");
            }
            bail!("Garmin login refresh failed: HTTP {status}");
        }
        let mut token: TokenResponse =
            response.json().context("Invalid Garmin refresh response")?;
        self.tokens.access_token = std::mem::take(&mut token.access_token);
        if let Some(refresh) = std::mem::take(&mut token.refresh_token) {
            self.tokens.refresh_token = refresh;
        }
        if let Some(client_id) = jwt_client_id(&self.tokens.access_token) {
            self.tokens.client_id = client_id;
        }
        self.save_tokens()
    }

    fn save_tokens(&self) -> Result<()> {
        let mut serialized = serde_json::to_vec(&self.tokens)?;
        let result = save_token_secret(&serialized);
        serialized.zeroize();
        result
    }

    fn ensure_fresh(&mut self) -> Result<()> {
        if self.token_expires_soon() {
            self.refresh().context("Saved Garmin login expired")?;
        }
        Ok(())
    }

    fn send_with_auth_retry<F>(
        &mut self,
        operation: &str,
        build: F,
    ) -> Result<reqwest::blocking::Response>
    where
        F: Fn(&Self) -> Result<RequestBuilder>,
    {
        self.ensure_fresh()?;
        let mut response = build(self)?
            .send()
            .with_context(|| format!("Could not {operation}"))?;
        if response.status() == StatusCode::UNAUTHORIZED {
            self.refresh().context("Garmin login refresh failed")?;
            response = build(self)?
                .send()
                .with_context(|| format!("Could not {operation} after refreshing login"))?;
        }
        Ok(response)
    }

    fn api_request(&self, method: reqwest::Method, path: &str) -> RequestBuilder {
        self.http
            .request(
                method,
                format!("{CONNECT_API}/{}", path.trim_start_matches('/')),
            )
            .headers(native_headers())
            .bearer_auth(&self.tokens.access_token)
    }

    fn token_expires_soon(&self) -> bool {
        jwt_expiry(&self.tokens.access_token)
            .is_some_and(|expiry| expiry <= Utc::now() + ChronoDuration::minutes(15))
    }
}

fn token_entry() -> Result<Entry> {
    Entry::new(SERVICE, ACCOUNT).context("Could not open Windows Credential Manager")
}

fn token_chunk_entry(manifest: &TokenManifest, index: usize) -> Result<Entry> {
    Entry::new(
        SERVICE,
        &format!(
            "{ACCOUNT}-v{}-{}-{index:02}",
            manifest.format, manifest.checksum
        ),
    )
    .context("Could not open Windows Credential Manager token chunk")
}

fn token_checksum(secret: &[u8]) -> String {
    general_purpose::URL_SAFE_NO_PAD.encode(Sha256::digest(secret))
}

fn load_saved_tokens() -> Result<Option<Tokens>> {
    let entry = token_entry()?;
    let mut main_secret = match entry.get_secret() {
        Ok(value) => value,
        Err(keyring::Error::NoEntry) => return Ok(None),
        Err(error) => return Err(error).context("Could not read Windows Credential Manager"),
    };
    let manifest = serde_json::from_slice::<TokenManifest>(&main_secret);
    main_secret.zeroize();

    match manifest {
        Ok(manifest) => load_chunked_tokens(&entry, &manifest),
        Err(_) => load_legacy_tokens(&entry),
    }
}

fn load_chunked_tokens(entry: &Entry, manifest: &TokenManifest) -> Result<Option<Tokens>> {
    if manifest.validate().is_err() {
        delete_entry(entry)?;
        return Ok(None);
    }

    let mut serialized = Vec::with_capacity(manifest.byte_len);
    for index in 0..manifest.chunk_count {
        let mut chunk = match token_chunk_entry(manifest, index)?.get_secret() {
            Ok(value) => value,
            Err(keyring::Error::NoEntry) => {
                serialized.zeroize();
                clear_manifest_tokens(entry, manifest)?;
                return Ok(None);
            }
            Err(error) => {
                serialized.zeroize();
                return Err(error).context("Could not read Windows Credential Manager token chunk");
            }
        };
        let expected_len = if index + 1 == manifest.chunk_count {
            manifest.byte_len - (index * TOKEN_CHUNK_BYTES)
        } else {
            TOKEN_CHUNK_BYTES
        };
        if chunk.len() != expected_len {
            chunk.zeroize();
            serialized.zeroize();
            clear_manifest_tokens(entry, manifest)?;
            return Ok(None);
        }
        serialized.extend_from_slice(&chunk);
        chunk.zeroize();
    }

    if token_checksum(&serialized) != manifest.checksum {
        serialized.zeroize();
        clear_manifest_tokens(entry, manifest)?;
        return Ok(None);
    }
    let parsed = serde_json::from_slice(&serialized);
    serialized.zeroize();
    match parsed {
        Ok(tokens) => Ok(Some(tokens)),
        Err(_) => {
            clear_manifest_tokens(entry, manifest)?;
            Ok(None)
        }
    }
}

fn load_legacy_tokens(entry: &Entry) -> Result<Option<Tokens>> {
    let mut serialized = match entry.get_password() {
        Ok(value) => value,
        Err(keyring::Error::BadEncoding(mut bytes)) => {
            bytes.zeroize();
            delete_entry(entry)?;
            return Ok(None);
        }
        Err(keyring::Error::NoEntry) => return Ok(None),
        Err(error) => return Err(error).context("Could not read Windows Credential Manager"),
    };
    let parsed = serde_json::from_str(&serialized);
    serialized.zeroize();
    match parsed {
        Ok(tokens) => Ok(Some(tokens)),
        Err(_) => {
            delete_entry(entry)?;
            Ok(None)
        }
    }
}

fn save_token_secret(secret: &[u8]) -> Result<()> {
    let entry = token_entry()?;
    let previous = read_valid_manifest(&entry)?;
    let manifest = TokenManifest::for_secret(secret)?;

    for (index, chunk) in secret.chunks(TOKEN_CHUNK_BYTES).enumerate() {
        if let Err(error) = token_chunk_entry(&manifest, index)?.set_secret(chunk) {
            if previous
                .as_ref()
                .is_none_or(|value| value.checksum != manifest.checksum)
            {
                let _ = delete_manifest_chunks(&manifest);
            }
            return Err(error)
                .context("Could not save Garmin token chunk in Windows Credential Manager");
        }
    }

    let manifest_secret = serde_json::to_vec(&manifest)?;
    if let Err(error) = entry.set_secret(&manifest_secret) {
        if previous
            .as_ref()
            .is_none_or(|value| value.checksum != manifest.checksum)
        {
            let _ = delete_manifest_chunks(&manifest);
        }
        return Err(error)
            .context("Could not save Garmin token manifest in Windows Credential Manager");
    }

    if let Some(previous) = previous.filter(|value| value.checksum != manifest.checksum) {
        let _ = delete_manifest_chunks(&previous);
    }
    Ok(())
}

fn read_valid_manifest(entry: &Entry) -> Result<Option<TokenManifest>> {
    let mut secret = match entry.get_secret() {
        Ok(value) => value,
        Err(keyring::Error::NoEntry) => return Ok(None),
        Err(error) => return Err(error).context("Could not read Windows Credential Manager"),
    };
    let manifest = serde_json::from_slice::<TokenManifest>(&secret).ok();
    secret.zeroize();
    Ok(manifest.filter(|value| value.validate().is_ok()))
}

fn spawn_auth_bridge() -> Result<Child> {
    let script = auth_bridge_path()?;
    let mut failures = Vec::new();
    for candidate in crate::python::candidates() {
        let mut command = Command::new(&candidate.program);
        if let Some(argument) = candidate.prefix {
            command.arg(argument);
        }
        command
            .arg(&script)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null());
        #[cfg(windows)]
        command.creation_flags(CREATE_NO_WINDOW);
        match command.spawn() {
            Ok(child) => return Ok(child),
            Err(error) => failures.push(format!("{}: {error}", candidate.program.display())),
        }
    }
    bail!(
        "Could not start browser-compatible Garmin sign-in. Install Python and run `python -m pip install \".[auth]\"`. Attempts: {failures:?}"
    )
}

fn auth_bridge_path() -> Result<PathBuf> {
    let mut candidates = vec![std::env::current_dir()?.join("garmin_auth_bridge.py")];
    if let Some(directory) = std::env::current_exe()?.parent() {
        candidates.push(directory.join("garmin_auth_bridge.py"));
        if let Some(parent) = directory.parent() {
            candidates.push(parent.join("garmin_auth_bridge.py"));
        }
    }
    candidates
        .into_iter()
        .find(|path| path.is_file())
        .map(|path| path.canonicalize())
        .transpose()?
        .context("garmin_auth_bridge.py was not found beside the project or executable")
}

fn clear_saved_tokens() -> Result<()> {
    let entry = token_entry()?;
    if let Some(manifest) = read_valid_manifest(&entry)? {
        clear_manifest_tokens(&entry, &manifest)
    } else {
        delete_entry(&entry)
    }
}

fn clear_manifest_tokens(entry: &Entry, manifest: &TokenManifest) -> Result<()> {
    delete_entry(entry)?;
    let _ = delete_manifest_chunks(manifest);
    Ok(())
}

fn delete_manifest_chunks(manifest: &TokenManifest) -> Result<()> {
    manifest.validate()?;
    let mut first_error = None;
    for index in 0..manifest.chunk_count {
        if let Err(error) =
            token_chunk_entry(manifest, index).and_then(|entry| delete_entry(&entry))
            && first_error.is_none()
        {
            first_error = Some(error);
        }
    }
    if let Some(error) = first_error {
        Err(error)
    } else {
        Ok(())
    }
}

fn delete_entry(entry: &Entry) -> Result<()> {
    match entry.delete_credential() {
        Ok(()) | Err(keyring::Error::NoEntry) => Ok(()),
        Err(error) => Err(error).context("Could not clear invalid Garmin login"),
    }
}

fn http_client() -> Result<Client> {
    Client::builder()
        .cookie_store(true)
        .connect_timeout(Duration::from_secs(15))
        .timeout(Duration::from_secs(35))
        .https_only(true)
        .build()
        .context("Could not initialize secure HTTP client")
}

fn sso_request(http: &Client, flow: SsoFlow, operation: &str) -> RequestBuilder {
    let request = http
        .post(format!("{SSO}/{}/api/{operation}", flow.path()))
        .query(&[
            ("clientId", flow.client_id()),
            ("locale", "en-US"),
            ("service", flow.service_url()),
        ])
        .header("Accept", "application/json, text/plain, */*")
        .header("Accept-Language", "en-US,en;q=0.9")
        .header("Origin", SSO);
    match flow {
        SsoFlow::Mobile => request.header("User-Agent", IOS_LOGIN_UA),
        SsoFlow::Portal => request
            .header("User-Agent", PORTAL_LOGIN_UA)
            .header(
                "Referer",
                format!(
                    "{SSO}/portal/sso/en-US/sign-in?clientId={PORTAL_CLIENT_ID}&service={PORTAL_SERVICE_URL}"
                ),
            ),
    }
}

fn native_headers() -> reqwest::header::HeaderMap {
    use reqwest::header::{ACCEPT, ACCEPT_LANGUAGE, HeaderMap, HeaderValue, USER_AGENT};
    let mut headers = HeaderMap::new();
    headers.insert(USER_AGENT, HeaderValue::from_static(NATIVE_API_UA));
    headers.insert(
        "X-Garmin-User-Agent",
        HeaderValue::from_static(NATIVE_GARMIN_UA),
    );
    headers.insert(
        "X-Garmin-Paired-App-Version",
        HeaderValue::from_static("10861"),
    );
    headers.insert(
        "X-Garmin-Client-Platform",
        HeaderValue::from_static("Android"),
    );
    headers.insert("X-App-Ver", HeaderValue::from_static("10861"));
    headers.insert("X-Lang", HeaderValue::from_static("en"));
    headers.insert("X-GCExperience", HeaderValue::from_static("GC5"));
    headers.insert(ACCEPT_LANGUAGE, HeaderValue::from_static("en-US,en;q=0.9"));
    headers.insert(ACCEPT, HeaderValue::from_static("application/json"));
    headers
}

fn retry_after(headers: &reqwest::header::HeaderMap) -> Option<u64> {
    headers
        .get(reqwest::header::RETRY_AFTER)
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.parse::<u64>().ok())
}

fn rate_limit_message(retry_after: Option<u64>, operation: &str) -> String {
    match retry_after {
        Some(1) => format!("Garmin rate-limited {operation}. Retry in 1 second."),
        Some(seconds) => format!("Garmin rate-limited {operation}. Retry in {seconds} seconds."),
        None => format!(
            "Garmin returned HTTP 429 for {operation} without a cooldown. This can persist until the IP or network changes."
        ),
    }
}

fn bridge_error(response: BridgeResponse) -> anyhow::Error {
    let message = response
        .message
        .unwrap_or_else(|| "Browser-compatible Garmin sign-in failed".to_owned());
    if response
        .kind
        .as_deref()
        .is_some_and(|kind| kind.contains("Authentication"))
    {
        anyhow!(IncorrectCredentials(message))
    } else {
        anyhow!(message)
    }
}

fn login_body_rate_limited(body: &LoginResponse) -> bool {
    body.error
        .as_ref()
        .and_then(|error| error.get("status-code"))
        .is_some_and(|status| status.as_str() == Some("429") || status.as_u64() == Some(429))
}

pub fn is_incorrect_credentials(error: &anyhow::Error) -> bool {
    error.downcast_ref::<IncorrectCredentials>().is_some()
}

fn checked_json<T: serde::de::DeserializeOwned>(
    response: reqwest::blocking::Response,
) -> Result<T> {
    let status = response.status();
    if !status.is_success() {
        let text = response.text().unwrap_or_default();
        bail!("Garmin API error {status}: {}", short(&text));
    }
    response.json().context("Garmin API returned invalid JSON")
}

fn short(value: &str) -> String {
    value.chars().take(240).collect()
}

fn jwt_payload(token: &str) -> Option<Value> {
    let payload = token.split('.').nth(1)?;
    let bytes = general_purpose::URL_SAFE_NO_PAD
        .decode(payload)
        .or_else(|_| general_purpose::URL_SAFE.decode(payload))
        .ok()?;
    serde_json::from_slice(&bytes).ok()
}

fn jwt_client_id(token: &str) -> Option<String> {
    jwt_payload(token)?
        .get("client_id")?
        .as_str()
        .map(str::to_owned)
}

fn jwt_expiry(token: &str) -> Option<DateTime<Utc>> {
    let seconds = jwt_payload(token)?.get("exp")?.as_i64()?;
    DateTime::from_timestamp(seconds, 0)
}

fn is_auth_error(error: &anyhow::Error) -> bool {
    error.chain().any(|cause| {
        let message = cause.to_string().to_lowercase();
        message.contains("401") || message.contains("authentication expired")
    })
}

fn remote_start(activity: &Activity) -> Option<DateTime<Utc>> {
    let value = activity.start_time_gmt.as_deref()?;
    DateTime::parse_from_rfc3339(value)
        .map(|value| value.with_timezone(&Utc))
        .or_else(|_| {
            NaiveDateTime::parse_from_str(value, "%Y-%m-%d %H:%M:%S%.f")
                .map(|value| value.and_utc())
        })
        .ok()
}

pub fn activity_matches(activity: &Activity, fingerprint: &ActivityFingerprint) -> bool {
    let Some(start) = remote_start(activity) else {
        return false;
    };
    if (start - fingerprint.start_utc).num_seconds().abs() > START_TOLERANCE_SECONDS {
        return false;
    }
    let duration_matches = fingerprint.duration_seconds.is_some_and(|target| {
        [
            activity.duration,
            activity.elapsed_duration,
            activity.moving_duration,
        ]
        .into_iter()
        .flatten()
        .any(|value| (value - target).abs() <= DURATION_TOLERANCE_SECONDS)
    });
    let distance_matches = fingerprint.distance_meters.is_some_and(|target| {
        let tolerance = 20.0_f64.max(target * 0.002);
        activity
            .distance
            .is_some_and(|value| (value - target).abs() <= tolerance)
    });
    if fingerprint.duration_seconds.is_none() && fingerprint.distance_meters.is_none() {
        true
    } else {
        duration_matches || distance_matches
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::TimeZone;

    fn fingerprint_value() -> ActivityFingerprint {
        ActivityFingerprint {
            start_utc: Utc.with_ymd_and_hms(2026, 6, 5, 13, 14, 20).unwrap(),
            duration_seconds: Some(4859.0),
            distance_meters: Some(40138.58),
        }
    }

    fn activity() -> Activity {
        Activity {
            activity_id: Some(123),
            activity_name: Some("Indoor Cycling".to_owned()),
            start_time_gmt: Some("2026-06-05 13:14:20".to_owned()),
            duration: Some(4859.0),
            elapsed_duration: None,
            moving_duration: None,
            distance: Some(40138.58),
        }
    }

    #[test]
    fn matches_same_ride() {
        assert!(activity_matches(&activity(), &fingerprint_value()));
    }

    #[test]
    fn rejects_different_start() {
        let mut value = activity();
        value.start_time_gmt = Some("2026-06-05 13:15:20".to_owned());
        assert!(!activity_matches(&value, &fingerprint_value()));
    }

    #[test]
    fn rejects_different_totals() {
        let mut value = activity();
        value.duration = Some(3000.0);
        value.distance = Some(30000.0);
        assert!(!activity_matches(&value, &fingerprint_value()));
    }

    #[test]
    fn parses_rfc3339_start() {
        let mut value = activity();
        value.start_time_gmt = Some("2026-06-05T13:14:20Z".to_owned());
        assert!(activity_matches(&value, &fingerprint_value()));
    }

    #[test]
    fn parses_fractional_garmin_start() {
        let mut value = activity();
        value.start_time_gmt = Some("2026-06-05 13:14:20.000".to_owned());
        assert!(activity_matches(&value, &fingerprint_value()));
    }

    #[test]
    fn reports_exact_rate_limit_delay_when_available() {
        assert_eq!(
            rate_limit_message(Some(42), "sign-in"),
            "Garmin rate-limited sign-in. Retry in 42 seconds."
        );
    }

    #[test]
    fn rate_limit_message_does_not_invent_a_delay() {
        assert_eq!(
            rate_limit_message(None, "sign-in"),
            "Garmin returned HTTP 429 for sign-in without a cooldown. This can persist until the IP or network changes."
        );
    }

    #[test]
    fn detects_rate_limit_buried_in_login_json() {
        let body: LoginResponse = serde_json::from_value(json!({
            "error": {"status-code": "429"}
        }))
        .unwrap();
        assert!(login_body_rate_limited(&body));
    }

    #[test]
    fn recognizes_wrapped_incorrect_credentials() {
        let error = Err::<(), _>(anyhow!(IncorrectCredentials("wrong password".to_owned())))
            .context("direct sign-in failed")
            .unwrap_err();
        assert!(is_incorrect_credentials(&error));
    }

    #[test]
    fn bridge_authentication_error_is_incorrect_credentials() {
        let error = bridge_error(BridgeResponse {
            status: "error".to_owned(),
            kind: Some("GarminConnectAuthenticationError".to_owned()),
            message: Some("401 Unauthorized".to_owned()),
            tokens: None,
        });
        assert!(is_incorrect_credentials(&error));
    }

    #[test]
    fn oversized_token_secret_uses_bounded_chunks() {
        let secret = vec![b'x'; (TOKEN_CHUNK_BYTES * 2) + 17];
        let manifest = TokenManifest::for_secret(&secret).unwrap();

        assert_eq!(manifest.byte_len, secret.len());
        assert_eq!(manifest.chunk_count, 3);
        assert_eq!(manifest.checksum.len(), 43);
        manifest.validate().unwrap();
    }

    #[test]
    fn token_manifest_rejects_unsafe_checksum() {
        let manifest = TokenManifest {
            format: TOKEN_STORE_FORMAT,
            checksum: "../not-a-valid-checksum".to_owned(),
            byte_len: 10,
            chunk_count: 1,
        };

        assert!(manifest.validate().is_err());
    }

    #[test]
    fn token_manifest_rejects_inconsistent_chunk_count() {
        let secret = vec![b'x'; TOKEN_CHUNK_BYTES + 1];
        let mut manifest = TokenManifest::for_secret(&secret).unwrap();
        manifest.chunk_count = 1;

        assert!(manifest.validate().is_err());
    }
}
