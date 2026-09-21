use crate::protocol::{
    AssetRead, CancelState, ClaimRequest, ClaimResponse, EnrollRequest, EnrollResponse, WorkerHeartbeat,
};
use anyhow::{Context, Result};
use reqwest::blocking::{multipart, Client};
use reqwest::StatusCode;
use serde::Serialize;
use serde_json::Value;
use std::fs::File;
use std::io;
use std::path::Path;
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
        let mut output = File::create(target)
            .with_context(|| format!("cannot create {}", target.display()))?;
        io::copy(&mut response, &mut output)
            .with_context(|| format!("cannot write {}", target.display()))?;
        Ok(())
    }

    pub fn upload_output(
        &self,
        execution_id: Uuid,
        fence_token: &str,
        credential: &str,
        output: &Path,
    ) -> Result<Uuid> {
        let file_name = output
            .file_name()
            .and_then(|name| name.to_str())
            .unwrap_or("render-output.bin")
            .to_string();
        let form = multipart::Form::new()
            .text("fence_token", fence_token.to_string())
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
        let response = response.error_for_status().context("output upload was rejected")?;
        let asset: AssetRead = response.json().context("invalid output upload response")?;
        let _ = file_name;
        Ok(asset.id)
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
        let response = response.error_for_status().context("HTTP POST was rejected")?;
        response.json().context("invalid JSON response")
    }
}
