use crate::protocol::{
    AssetRead, CancelState, ClaimRequest, ClaimResponse, EnrollRequest, EnrollResponse,
    WorkerHeartbeat,
};
use anyhow::{Context, Result};
use reqwest::blocking::{multipart, Client};
use reqwest::StatusCode;
use serde::Serialize;
use serde_json::Value;
use std::fs::File;
use std::io;
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;
use uuid::Uuid;

#[derive(Clone)]
pub struct ApiClient {
    base: String,
    client: Client,
}

impl ApiClient {
    pub fn new(base: String) -> Result<Self> {
        let client = Client::builder()
            .connect_timeout(Duration::from_secs(10))
            .timeout(Duration::from_secs(30))
            .user_agent("VideoRollRenderWorkerNative/0.1.0")
            .build()
            .context("cannot initialize HTTP client")?;
        Ok(Self { base, client })
    }

    pub fn enroll(&self, payload: &EnrollRequest) -> Result<EnrollResponse> {
        self.post_json(&format!("{}/enroll", self.base), None, payload)
    }

    pub fn worker_heartbeat(
        &self,
        worker_id: Uuid,
        credential: &str,
        payload: &WorkerHeartbeat,
    ) -> Result<Value> {
        self.post_json(
            &format!("{}/{worker_id}/heartbeat", self.base),
            Some(credential),
            payload,
        )
    }

    pub fn claim(
        &self,
        worker_id: Uuid,
        credential: &str,
        payload: &ClaimRequest,
    ) -> Result<ClaimResponse> {
        self.post_json(
            &format!("{}/{worker_id}/claim", self.base),
            Some(credential),
            payload,
        )
    }

    pub fn execution_post<T: Serialize>(
        &self,
        execution_id: Uuid,
        suffix: &str,
        credential: &str,
        payload: &T,
    ) -> Result<Value> {
        self.post_json(
            &format!("{}/executions/{execution_id}/{suffix}", self.base),
            Some(credential),
            payload,
        )
    }

    pub fn cancel_state(&self, execution_id: Uuid, credential: &str) -> Result<CancelState> {
        let response = self
            .client
            .get(format!("{}/executions/{execution_id}/cancel", self.base))
            .bearer_auth(credential)
            .send()
            .context("cancel poll request failed")?;
        let response = response
            .error_for_status()
            .context("cancel poll was rejected")?;
        response.json().context("invalid cancel response")
    }

    pub fn download_artifact(
        &self,
        execution_id: Uuid,
        role: &str,
        credential: &str,
        target: &Path,
    ) -> Result<()> {
        let mut response = self
            .client
            .get(format!(
                "{}/executions/{execution_id}/artifacts/{role}",
                self.base
            ))
            .bearer_auth(credential)
            .timeout(Duration::from_secs(60 * 60))
            .send()
            .with_context(|| format!("artifact download failed: {role}"))?;
        response = response
            .error_for_status()
            .with_context(|| format!("artifact download was rejected: {role}"))?;
        let mut output =
            File::create(target).with_context(|| format!("cannot create {}", target.display()))?;
        io::copy(&mut response, &mut output)
            .with_context(|| format!("cannot write {}", target.display()))?;
        Ok(())
    }

    fn verify_output(asset: AssetRead, size: u64, sha256: &str) -> Result<Uuid> {
        anyhow::ensure!(
            asset.size_bytes == Some(size)
                && asset
                    .sha256
                    .as_deref()
                    .is_some_and(|v| v.eq_ignore_ascii_case(sha256)),
            "registered output does not match local size and SHA256"
        );
        Ok(asset.id)
    }

    fn probe_output(
        &self,
        execution_id: Uuid,
        fence_token: &str,
        credential: &str,
        size: u64,
        sha256: &str,
    ) -> Result<Option<Uuid>> {
        let response = self
            .client
            .get(format!("{}/executions/{execution_id}/output", self.base))
            .query(&[("fence_token", fence_token)])
            .bearer_auth(credential)
            .timeout(Duration::from_secs(12))
            .send()
            .context("output receipt probe failed")?;
        if response.status() == StatusCode::NOT_FOUND {
            return Ok(None);
        }
        let asset: AssetRead = response
            .error_for_status()
            .context("output receipt probe rejected")?
            .json()
            .context("invalid output receipt")?;
        Self::verify_output(asset, size, sha256).map(Some)
    }

    fn upload_once(
        &self,
        execution_id: Uuid,
        fence_token: &str,
        credential: &str,
        output: &Path,
        size: u64,
        sha256: &str,
    ) -> Result<Uuid> {
        let form = multipart::Form::new()
            .text("fence_token", fence_token.to_owned())
            .file("file", output)
            .with_context(|| format!("cannot open upload {}", output.display()))?;
        let response = self
            .client
            .post(format!("{}/executions/{execution_id}/output", self.base))
            .bearer_auth(credential)
            .timeout(Duration::from_secs(60 * 60))
            .multipart(form)
            .send()
            .context("output upload request failed")?;
        let asset: AssetRead = response
            .error_for_status()
            .context("output upload was rejected")?
            .json()
            .context("invalid output upload response")?;
        Self::verify_output(asset, size, sha256)
    }

    fn retryable_transfer(error: &anyhow::Error) -> bool {
        error
            .chain()
            .find_map(|cause| cause.downcast_ref::<reqwest::Error>())
            .is_some_and(|e| {
                if let Some(status) = e.status() {
                    matches!(status.as_u16(), 408 | 500 | 502 | 503 | 504)
                } else {
                    e.is_timeout()
                        || e.is_connect()
                        || e.is_request()
                        || e.is_body()
                        || e.is_decode()
                }
            })
    }

    pub fn upload_output(
        &self,
        execution_id: Uuid,
        fence_token: &str,
        credential: &str,
        output: &Path,
        size: u64,
        sha256: &str,
        cancel: &AtomicBool,
    ) -> Result<Uuid> {
        for attempt in 0..4 {
            anyhow::ensure!(!cancel.load(Ordering::Relaxed), "render execution canceled");
            // Never upload again if the preceding request may already have
            // committed. Require a successful absence probe before each PUT.
            match self.probe_output(execution_id, fence_token, credential, size, sha256) {
                Ok(Some(id)) => return Ok(id),
                Ok(None) => {
                    match self.upload_once(
                        execution_id,
                        fence_token,
                        credential,
                        output,
                        size,
                        sha256,
                    ) {
                        Ok(id) => return Ok(id),
                        Err(error) => {
                            // A concurrent response can report already
                            // registered (409). Recover only a matching receipt.
                            if let Ok(Some(id)) = self.probe_output(
                                execution_id,
                                fence_token,
                                credential,
                                size,
                                sha256,
                            ) {
                                return Ok(id);
                            }
                            if !Self::retryable_transfer(&error) || attempt == 3 {
                                return Err(error);
                            }
                        }
                    }
                }
                Err(error) => {
                    if !Self::retryable_transfer(&error) || attempt == 3 {
                        return Err(error);
                    }
                }
            }
            for _ in 0..((2_u64 << attempt) * 10).min(120) {
                anyhow::ensure!(!cancel.load(Ordering::Relaxed), "render execution canceled");
                std::thread::sleep(Duration::from_millis(100));
            }
        }
        anyhow::bail!("render output transfer retry budget exhausted")
    }

    pub fn is_fence_status(error: &anyhow::Error) -> bool {
        error
            .chain()
            .find_map(|cause| cause.downcast_ref::<reqwest::Error>())
            .and_then(|error| error.status())
            .is_some_and(|status| status == StatusCode::CONFLICT || status == StatusCode::GONE)
    }

    fn post_json<T: serde::de::DeserializeOwned, B: Serialize>(
        &self,
        url: &str,
        credential: Option<&str>,
        payload: &B,
    ) -> Result<T> {
        let mut request = self.client.post(url).json(payload);
        if let Some(credential) = credential {
            request = request.bearer_auth(credential);
        }
        let response = request.send().context("HTTP POST failed")?;
        let response = response
            .error_for_status()
            .context("HTTP POST was rejected")?;
        response.json().context("invalid JSON response")
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{BufRead, BufReader, Read, Write};
    use std::net::TcpListener;
    use std::thread;
    use std::time::Instant;

    // Loopback-only HTTP fixture, no real worker or coordinator credentials.
    fn fixture(replies: Vec<(&'static str, u16, String)>) -> (ApiClient, thread::JoinHandle<()>) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let base = format!("http://{}", listener.local_addr().unwrap());
        let handle = thread::spawn(move || {
            for (method, status, body) in replies {
                let deadline = Instant::now() + Duration::from_secs(12);
                let mut stream = loop {
                    match listener.accept() {
                        Ok((stream, _)) => break stream,
                        Err(e)
                            if e.kind() == io::ErrorKind::WouldBlock
                                && Instant::now() < deadline =>
                        {
                            thread::sleep(Duration::from_millis(10))
                        }
                        Err(e) => panic!("fixture accept: {e}"),
                    }
                };
                stream
                    .set_read_timeout(Some(Duration::from_secs(5)))
                    .unwrap();
                let mut reader = BufReader::new(&mut stream);
                let mut line = String::new();
                reader.read_line(&mut line).unwrap();
                assert!(
                    line.starts_with(&format!("{method} /executions/")),
                    "{line}"
                );
                let mut length = 0usize;
                loop {
                    line.clear();
                    reader.read_line(&mut line).unwrap();
                    if line == "\r\n" || line.is_empty() {
                        break;
                    }
                    if let Some(value) = line.to_ascii_lowercase().strip_prefix("content-length:") {
                        length = value.trim().parse().unwrap();
                    }
                }
                assert!(length < 1024 * 1024);
                if length > 0 {
                    reader.read_exact(&mut vec![0u8; length]).unwrap();
                }
                drop(reader);
                write!(stream,"HTTP/1.1 {status} Fixture\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",body.len()).unwrap();
                stream.flush().unwrap();
            }
        });
        (
            ApiClient {
                base,
                client: Client::builder()
                    .no_proxy()
                    .timeout(Duration::from_secs(5))
                    .build()
                    .unwrap(),
            },
            handle,
        )
    }
    #[test]
    fn upload_response_lost_recovers_verified_receipt_without_resubmit() {
        let dir = tempfile::tempdir().unwrap();
        let output = dir.path().join("video.mp4");
        std::fs::write(&output, b"fixture").unwrap();
        let id = Uuid::new_v4();
        let sha = "a".repeat(64);
        let receipt = serde_json::json!({"id":id,"size_bytes":7,"sha256":sha}).to_string();
        let (api, server) = fixture(vec![
            ("GET", 404, "{}".into()),
            ("POST", 503, "{}".into()),
            ("GET", 200, receipt),
        ]);
        let result = api
            .upload_output(
                Uuid::new_v4(),
                "test-fence",
                "test-credential",
                &output,
                7,
                &sha,
                &AtomicBool::new(false),
            )
            .unwrap();
        assert_eq!(result, id);
        server.join().unwrap();
    }
    #[test]
    fn output_fence_and_digest_fail_closed() {
        let dir = tempfile::tempdir().unwrap();
        let output = dir.path().join("video.mp4");
        std::fs::write(&output, b"fixture").unwrap();
        let sha = "a".repeat(64);
        for (status, body) in [
            (409, "{}".into()),
            (
                200,
                serde_json::json!({"id":Uuid::new_v4(),"size_bytes":8,"sha256":sha}).to_string(),
            ),
        ] {
            let (api, server) = fixture(vec![("GET", status, body)]);
            let result = api.upload_output(
                Uuid::new_v4(),
                "test-fence",
                "test-credential",
                &output,
                7,
                &sha,
                &AtomicBool::new(false),
            );
            assert!(result.is_err());
            server.join().unwrap();
        }
    }
}
