//! Original /system/resources envelope, sourced from the actual Rust host.
//! Live host metrics are not confused with worker GPU measurements.
use crate::{AppResult, api::AppState};
use axum::{Json, Router, routing::get};
use serde_json::{Value, json};
use std::collections::HashMap;
use tokio::fs;

fn parse_meminfo(input: &str) -> Option<(u64, u64, u64)> {
    let fields = input
        .lines()
        .filter_map(|line| {
            let (key, raw) = line.split_once(':')?;
            let kb = raw.split_whitespace().next()?.parse::<u64>().ok()?;
            Some((key.to_string(), kb.saturating_mul(1024)))
        })
        .collect::<HashMap<_, _>>();
    let total = *fields.get("MemTotal")?;
    let available = *fields.get("MemAvailable")?;
    if total == 0 || available > total {
        return None;
    }
    Some((total, total - available, available))
}
fn mem_view(total: u64, used: u64, available: u64) -> Value {
    json!({"total_bytes":total,"used_bytes":used,"available_bytes":available,
        "percent":if total==0{None}else{Some(used as f64*100.0/total as f64)}})
}
async fn cgroup_memory() -> Option<Value> {
    let (Ok(max), Ok(current)) = (
        fs::read_to_string("/sys/fs/cgroup/memory.max").await,
        fs::read_to_string("/sys/fs/cgroup/memory.current").await,
    ) else {
        return None;
    };
    let total = max.trim().parse::<u64>().ok()?;
    let used = current.trim().parse::<u64>().ok()?.min(total);
    if total == 0 {
        return None;
    }
    Some(mem_view(total, used, total - used))
}
pub async fn sample() -> AppResult<Value> {
    let stats = fs::read_to_string("/proc/meminfo")
        .await
        .ok()
        .and_then(|content| parse_meminfo(&content));
    let mem = stats
        .map(|(t, u, a)| mem_view(t, u, a))
        .unwrap_or(mem_view(0, 0, 0));
    let load = fs::read_to_string("/proc/loadavg")
        .await
        .ok()
        .and_then(|content| {
            let values = content
                .split_whitespace()
                .take(3)
                .map(str::parse::<f64>)
                .collect::<Result<Vec<_>, _>>()
                .ok()?;
            if values.len() == 3 {
                Some(values)
            } else {
                None
            }
        });
    let cores = std::thread::available_parallelism()
        .map(|v| v.get())
        .unwrap_or(0);
    Ok(json!({
        "sampled_at":chrono::Utc::now().to_rfc3339(),
        "cpu":{"percent":null,"cores":cores,"load_average":load},
        "memory":mem,
        "cgroup_memory":cgroup_memory().await,
        "intel_gpu":null,
    }))
}
async fn system_resources() -> AppResult<Json<Value>> {
    Ok(Json(sample().await?))
}

pub fn router() -> Router<AppState> {
    Router::new().route("/system/resources", get(system_resources))
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn linux_meminfo_preserves_units_and_bounds() {
        assert_eq!(
            parse_meminfo("MemTotal: 8192 kB\nMemAvailable: 2048 kB\n"),
            Some((8_388_608, 6_291_456, 2_097_152))
        );
        assert_eq!(parse_meminfo("MemTotal: 12 kB\nMemAvailable: 14 kB"), None);
        assert_eq!(parse_meminfo(""), None);
    }
}
