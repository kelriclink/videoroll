//! Original /system/resources envelope, sourced from the actual Rust host.
//! Live host metrics are not confused with worker GPU measurements.
use crate::{AppResult, api::AppState};
use axum::{Json, Router, extract::State, routing::get};
use serde_json::{Value, json};
use std::collections::HashMap;
use std::{
    path::Path,
    time::{Duration, Instant},
};
use tokio::{fs, io::AsyncReadExt, sync::Mutex};

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
fn cgroup_view(max: &str, current: &str, v1: bool) -> Option<Value> {
    let total = max.trim().parse::<u64>().ok()?;
    let used = current.trim().parse::<u64>().ok()?.min(total);
    if total == 0 || (v1 && total >= 1 << 60) {
        return None;
    }
    Some(mem_view(total, used, total - used))
}
async fn cgroup_memory() -> Option<Value> {
    for (max, current, v1) in [
        (
            "/sys/fs/cgroup/memory.max",
            "/sys/fs/cgroup/memory.current",
            false,
        ),
        (
            "/sys/fs/cgroup/memory/memory.limit_in_bytes",
            "/sys/fs/cgroup/memory/memory.usage_in_bytes",
            true,
        ),
    ] {
        if let (Ok(max), Ok(current)) =
            tokio::join!(fs::read_to_string(max), fs::read_to_string(current))
        {
            if let Some(view) = cgroup_view(&max, &current, v1) {
                return Some(view);
            }
        }
    }
    None
}
fn cpu_snapshot(raw: &str) -> Option<(u64, u64)> {
    let mut fields = raw.lines().next()?.split_whitespace();
    if fields.next()? != "cpu" {
        return None;
    }
    let v = fields
        .take(10)
        .map(str::parse::<u64>)
        .collect::<Result<Vec<_>, _>>()
        .ok()?;
    if v.len() < 4 {
        return None;
    }
    // Linux guest counters are already included in user/nice.
    let total = v
        .iter()
        .take(8)
        .try_fold(0u64, |acc, &n| acc.checked_add(n))?;
    Some((total, v[3].checked_add(*v.get(4).unwrap_or(&0))?))
}
fn cpu_percent(a: (u64, u64), b: (u64, u64)) -> Option<f64> {
    let total = b.0.checked_sub(a.0)?;
    let idle = b.1.checked_sub(a.1)?;
    if total == 0 || idle > total {
        return None;
    }
    Some((total - idle) as f64 * 100. / total as f64)
}
async fn cpu_sample() -> Option<f64> {
    let a = cpu_snapshot(&fs::read_to_string("/proc/stat").await.ok()?)?;
    tokio::time::sleep(Duration::from_millis(50)).await;
    let b = cpu_snapshot(&fs::read_to_string("/proc/stat").await.ok()?)?;
    cpu_percent(a, b)
}
async fn cpu_cores() -> usize {
    let cores = std::thread::available_parallelism()
        .map(|n| n.get())
        .unwrap_or(0);
    let quota = if let Ok(v) = fs::read_to_string("/sys/fs/cgroup/cpu.max").await {
        let mut fields = v.split_whitespace();
        fields
            .next()
            .and_then(|q| q.parse::<u64>().ok())
            .zip(fields.next().and_then(|p| p.parse::<u64>().ok()))
    } else {
        None
    };
    let quota = if quota.is_some() {
        quota
    } else {
        let (q, p) = tokio::join!(
            fs::read_to_string("/sys/fs/cgroup/cpu/cpu.cfs_quota_us"),
            fs::read_to_string("/sys/fs/cgroup/cpu/cpu.cfs_period_us")
        );
        q.ok()
            .and_then(|q| q.trim().parse::<u64>().ok())
            .zip(p.ok().and_then(|p| p.trim().parse::<u64>().ok()))
    };
    match quota {
        Some((q, p)) if q > 0 && p > 0 => (cores.max(1)).min((q / p).max(1) as usize),
        _ => cores,
    }
}
async fn read_trim(path: &Path) -> Option<String> {
    Some(fs::read_to_string(path).await.ok()?.trim().to_owned())
}
fn pmu_engines(sample: &Value) -> Option<Vec<Value>> {
    let mut engines = Vec::new();
    for (name, value) in sample["engines"].as_object()? {
        if value["unit"] != "%" {
            continue;
        }
        if let Some(busy) = value["busy"]
            .as_f64()
            .filter(|v| v.is_finite() && (0.0..=100.0).contains(v))
        {
            engines.push(json!({"name":name,"percent":busy}));
        }
    }
    (!engines.is_empty()).then_some(engines)
}
/// The first intel_gpu_top report may cover only initialization. Decode
/// the second complete object from its streaming JSON array, within a hard
/// byte/time limit. Do not expose per-client names, PIDs or memory details.
async fn pmu_sample(device: &str) -> Option<Vec<Value>> {
    let mut child = tokio::process::Command::new("/usr/bin/intel_gpu_top")
        .args(["-J", "-s", "200", "-d", &format!("drm:{device}"), "-o", "-"])
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::null())
        .kill_on_drop(true)
        .spawn()
        .ok()?;
    let mut stdout = child.stdout.take()?;
    let result = tokio::time::timeout(Duration::from_secs(2), async {
        let mut raw = Vec::new();
        let mut chunk = [0u8; 8192];
        let mut offset = 0;
        let mut seen = 0;
        loop {
            let n = stdout.read(&mut chunk).await.ok()?;
            if n == 0 || raw.len() + n > 512 * 1024 {
                return None;
            }
            raw.extend_from_slice(&chunk[..n]);
            loop {
                while offset < raw.len()
                    && (raw[offset].is_ascii_whitespace() || matches!(raw[offset], b'[' | b','))
                {
                    offset += 1;
                }
                if offset == raw.len() {
                    break;
                }
                let mut parser =
                    serde_json::Deserializer::from_slice(&raw[offset..]).into_iter::<Value>();
                match parser.next() {
                    Some(Ok(sample)) => {
                        offset += parser.byte_offset();
                        seen += 1;
                        if seen >= 2 {
                            return pmu_engines(&sample);
                        }
                    }
                    Some(Err(e)) if e.is_eof() => break,
                    _ => return None,
                }
            }
        }
    })
    .await
    .ok()
    .flatten();
    let _ = child.kill().await;
    let _ = child.wait().await;
    result
}
async fn gpu_sample(device: &str) -> Value {
    let mut out = json!({"enabled":true,"checked":true,"available":false,"render_device":device,
        "model_name":null,"driver":null,"pci_slot":null,"pci_id":null,"usage_supported":false,"usage_percent":null,"engines":[],"detail":""});
    let Some(name) = device
        .strip_prefix("/dev/dri/renderD")
        .filter(|n| !n.is_empty() && n.bytes().all(|b| b.is_ascii_digit()))
    else {
        out["detail"] = json!("渲染设备路径无效");
        return out;
    };
    if fs::metadata(device).await.is_err() {
        out["detail"] = json!("未找到渲染设备");
        return out;
    }
    let sys = std::path::PathBuf::from(format!("/sys/class/drm/renderD{name}/device"));
    let vendor = read_trim(&sys.join("vendor")).await;
    if vendor.as_deref() != Some("0x8086") {
        out["detail"] = json!("不是可识别的 Intel GPU");
        return out;
    }
    let pci = read_trim(&sys.join("device")).await;
    let slot = fs::canonicalize(&sys)
        .await
        .ok()
        .and_then(|p| p.file_name().map(|n| n.to_string_lossy().into_owned()));
    let driver = fs::read_link(sys.join("driver"))
        .await
        .ok()
        .and_then(|p| p.file_name().map(|n| n.to_string_lossy().into_owned()));
    out["available"] = json!(true);
    out["driver"] = json!(driver);
    out["pci_slot"] = json!(slot);
    out["pci_id"] = json!(
        pci.as_ref()
            .map(|s| format!("8086:{}", s.trim_start_matches("0x")))
    );
    out["model_name"] = json!(pci.as_ref().map(|p| format!("Intel GPU ({p})")));
    // PMU is preferred over assuming unspecified sysfs counter units.
    if let Some(engines) = pmu_sample(device).await {
        out["usage_supported"] = json!(true);
        out["usage_percent"] = json!(
            engines
                .iter()
                .filter_map(|v| v["percent"].as_f64())
                .fold(0., f64::max)
        );
        out["engines"] = json!(engines);
        out["detail"] = json!("已检测到 Intel 硬件；PMU 实际引擎采样");
    } else {
        out["detail"] = json!("已检测到 Intel 硬件；PMU 不可用或采样超时");
    }
    out
}
async fn sample_uncached(device: Option<&str>) -> AppResult<Value> {
    let (cpu, gpu) = tokio::join!(cpu_sample(), async {
        match device {
            Some(d) => gpu_sample(d).await,
            None => Value::Null,
        }
    });
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
    let cores = cpu_cores().await;
    Ok(json!({
        "sampled_at":chrono::Utc::now().to_rfc3339(),
        "cpu":{"percent":cpu,"cores":cores,"load_average":load},
        "memory":mem,
        "cgroup_memory":cgroup_memory().await,
        "intel_gpu":gpu,
    }))
}
// Shared by HTTP and WebSocket. One bounded sample per process every 3s
// even with many dashboard connections. Settings are read from original DB.
static CACHE: Mutex<Option<(Instant, Option<String>, Value)>> = Mutex::const_new(None);
pub async fn sample(state: &AppState) -> AppResult<Value> {
    let profile = crate::settings::effective(&state.db, "subtitle.auto_profile").await?;
    let device = profile["use_intel_gpu"].as_bool().filter(|v| *v).map(|_| {
        std::env::var("INTEL_GPU_RENDER_DEVICE")
            .ok()
            .filter(|s| !s.trim().is_empty())
            .unwrap_or_else(|| "/dev/dri/renderD128".into())
    });
    let mut cache = CACHE.lock().await;
    if let Some((time, key, value)) = cache.as_ref() {
        if time.elapsed() < Duration::from_secs(3) && *key == device {
            return Ok(value.clone());
        }
    }
    let value = sample_uncached(device.as_deref()).await?;
    *cache = Some((Instant::now(), device, value.clone()));
    Ok(value)
}
async fn system_resources(State(state): State<AppState>) -> AppResult<Json<Value>> {
    Ok(Json(sample(&state).await?))
}

pub fn router() -> Router<AppState> {
    Router::new().route("/system/resources", get(system_resources))
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn cpu_and_cgroup_samples_match_kernel_counters() {
        let a = cpu_snapshot("cpu 10 1 3 80 6 0 0 0 2 0").unwrap();
        let b = cpu_snapshot("cpu 30 1 3 100 6 0 0 0 12 0").unwrap();
        assert_eq!(cpu_percent(a, b), Some(50.));
        assert_eq!(cpu_percent(b, a), None);
        assert_eq!(cgroup_view("1000", "250", false).unwrap()["percent"], 25.0);
        assert!(cgroup_view("max", "0", false).is_none());
        assert!(cgroup_view("1152921504606846976", "0", true).is_none());
        assert_eq!(pmu_engines(&json!({"engines":{"Video":{"busy":25.5,"unit":"%"},"bad":{"busy":101.,"unit":"%"}}})).unwrap(),vec![json!({"name":"Video","percent":25.5})]);
    }
    #[tokio::test]
    async fn actual_host_resource_sample() {
        if std::env::var("VIDEOROLL_TEST_HOST_RESOURCES").is_err() {
            return;
        }
        let value = sample_uncached(Some("/dev/dri/renderD128")).await.unwrap();
        assert!(
            value["cpu"]["percent"]
                .as_f64()
                .is_some_and(|v| (0.0..=100.0).contains(&v))
        );
        assert_eq!(value["intel_gpu"]["available"], true);
        assert_eq!(value["intel_gpu"]["usage_supported"], true);
        assert!(!value["intel_gpu"]["engines"].as_array().unwrap().is_empty());
        println!(
            "PASS real host CPU and Intel PMU: {}",
            json!({"cpu":value["cpu"],"intel_gpu":value["intel_gpu"]})
        );
    }
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
