//! Explicit opt-in tests of the actual Rust process lifecycle.
use serde_json::{Value, json};
use std::{path::PathBuf, time::Duration};
use tokio::time::{Instant, sleep};
use videoroll_v2::local_inference::{asr_settings, invoke};
const LONG: Duration = Duration::from_secs(150);
fn pid(v: &Value) -> i64 {
    v["pid"].as_i64().unwrap()
}
fn children() -> Vec<u32> {
    let mut pids = Vec::new();
    for thread in std::fs::read_dir("/proc/self/task").unwrap().flatten() {
        if let Ok(data) = std::fs::read_to_string(thread.path().join("children")) {
            pids.extend(data.split_whitespace().map(|v| v.parse::<u32>().unwrap()));
        }
    }
    pids.sort_unstable();
    pids.dedup();
    pids
}
fn lock_available(path: &std::path::Path) -> bool {
    use std::os::fd::AsRawFd;
    let file = std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open(path)
        .unwrap();
    let ok = unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } == 0;
    if !ok {
        assert_eq!(
            std::io::Error::last_os_error().raw_os_error(),
            Some(libc::EWOULDBLOCK)
        );
    }
    ok
}
async fn gone(p: i64) {
    for _ in 0..100 {
        if !PathBuf::from(format!("/proc/{p}")).exists() {
            return;
        }
        sleep(Duration::from_millis(30)).await;
    }
    panic!("inference child {p} was not reaped");
}
#[tokio::test]
async fn lifecycle() {
    let Ok(mode) = std::env::var("VIDEOROLL_TEST_MODEL_RUNTIME_MODE") else {
        return;
    };
    let root = PathBuf::from(std::env::var_os("VIDEOROLL_V2_STORAGE_ROOT").unwrap());
    assert!(
        root.file_name()
            .unwrap()
            .to_str()
            .unwrap()
            .starts_with("v2-model-runtime.")
    );
    if mode == "mock" {
        let model = root.join("model");
        std::fs::create_dir_all(&model).unwrap();
        let mut req = json!({"kind":"embedding","model_path":model,"device":"cpu","value":1});
        let first = invoke(&req, LONG).await.unwrap();
        req["value"] = json!(2);
        let second = invoke(&req, LONG).await.unwrap();
        assert_eq!(pid(&first), pid(&second));
        assert_eq!(second["calls"], 2);
        assert_eq!(second["value"], 2);
        // Simultaneous requests are serialized, and receive their own result.
        let (a, b) = tokio::join!(invoke(&req, LONG), invoke(&req, LONG));
        let mut counts = [
            a.unwrap()["calls"].as_i64().unwrap(),
            b.unwrap()["calls"].as_i64().unwrap(),
        ];
        counts.sort();
        assert_eq!(counts, [3, 4]);
        // Directory replacement invalidates residency even with the same path.
        std::fs::rename(&model, root.join("old")).unwrap();
        std::fs::create_dir(&model).unwrap();
        let fresh = invoke(&req, LONG).await.unwrap();
        assert_ne!(pid(&fresh), pid(&first));
        gone(pid(&first)).await;
        assert_eq!(fresh["calls"], 1);
        // Every failed exchange discards the whole session.
        let mut last = fresh;
        for op in ["hang", "oversize", "badjson", "error", "crash"] {
            req["op"] = json!(op);
            assert!(invoke(&req, Duration::from_secs(1)).await.is_err(), "{op}");
            gone(pid(&last)).await;
            req["op"] = Value::Null;
            let new = invoke(&req, LONG).await.unwrap();
            assert_eq!(new["calls"], 1);
            assert_ne!(pid(&new), pid(&last));
            last = new;
        }
        // Cancellation by caller, not merely the internal deadline.
        let mut hanging = req.clone();
        hanging["op"] = json!("hang");
        let task = tokio::spawn(async move { invoke(&hanging, LONG).await });
        sleep(Duration::from_millis(100)).await;
        task.abort();
        let _ = task.await;
        gone(pid(&last)).await;
        let new = invoke(&req, LONG).await.unwrap();
        assert_eq!(new["calls"], 1);
        gone(pid(&new)).await; // test env sets idle deadline=1s
        assert!(children().is_empty());
        println!(
            "PASS resident process reuse, concurrent serialization, directory replacement, timeout, malformed frames, crash, cancellation and idle reaping"
        );
        return;
    }
    let audio = PathBuf::from(std::env::var_os("VIDEOROLL_TEST_ASR_AUDIO").unwrap());
    let asr = |engine: &str, model: &str, device: &str| {
        let engine = engine.to_owned();
        let model = model.to_owned();
        let device = device.to_owned();
        let audio = audio.clone();
        async move {
            let mut req = asr_settings(&engine, &model, &json!({"openvino_device":device}))
                .await
                .unwrap();
            req["audio"] = json!(audio);
            req["language"] = json!("en");
            req
        }
    };
    let gpu = asr("openvino", "whisper-large-v3-fp16-ov", "GPU").await;
    if mode == "contender" {
        assert!(
            invoke(&gpu, Duration::from_secs(1)).await.is_err(),
            "second process must wait while GPU model resident"
        );
        println!("PASS contender admission timed out");
        return;
    }
    assert_eq!(mode, "real");
    let start = Instant::now();
    let result = invoke(&gpu, LONG).await.unwrap();
    let cold = start.elapsed();
    assert!(result["segments"].as_array().unwrap().iter().any(|s| {
        s["text"]
            .as_str()
            .unwrap_or("")
            .to_lowercase()
            .contains("circuit")
    }));
    let first = children();
    assert_eq!(first.len(), 1);
    let start = Instant::now();
    let result2 = invoke(&gpu, LONG).await.unwrap();
    let warm = start.elapsed();
    assert_eq!(result, result2);
    assert_eq!(children(), first);
    println!(
        "PASS real OpenVINO GPU ASR same child and transcript; cold {:?}, warm {:?}",
        cold, warm
    );
    // A second Rust process uses the same lock; it must not load another GPU model.
    let out = tokio::process::Command::new(std::env::current_exe().unwrap())
        .args(["--exact", "lifecycle", "--nocapture"])
        .env("VIDEOROLL_TEST_MODEL_RUNTIME_MODE", "contender")
        .output()
        .await
        .unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stdout)
    );
    assert!(String::from_utf8_lossy(&out.stdout).contains("PASS contender admission timed out"));
    assert_eq!(children(), first);
    let lock = root.join(".local-inference-gpu.lock");
    assert!(
        !lock_available(&lock),
        "GPU lock must remain held between requests"
    );
    // Switching to CPU frees GPU residency and the process itself.
    let model = PathBuf::from(std::env::var_os("SUBTITLE_RAG_EMBEDDING_MODEL_DIR").unwrap())
        .join("BAAI--bge-small-zh-v1.5");
    let req = json!({"kind":"embedding","model_path":model,"device":"openvino:CPU","text":"电子电路和电容"});
    let start = Instant::now();
    let e = invoke(&req, LONG).await.unwrap();
    let cold = start.elapsed();
    gone(first[0] as i64).await;
    assert!(
        lock_available(&lock),
        "GPU lock must be released after model switch"
    );
    let cpu = children();
    assert_eq!(cpu.len(), 1);
    assert_ne!(cpu, first);
    let start = Instant::now();
    let e2 = invoke(&req, LONG).await.unwrap();
    let warm = start.elapsed();
    assert_eq!(e["embedding"].as_array().unwrap().len(), 512);
    assert!(
        e["embedding"]
            .as_array()
            .unwrap()
            .iter()
            .zip(e2["embedding"].as_array().unwrap())
            .all(|(a, b)| (a.as_f64().unwrap() - b.as_f64().unwrap()).abs() < 1e-5)
    );
    assert_eq!(children(), cpu);
    println!(
        "PASS real BGE OpenVINO CPU same child and 512-dim embedding; cold {:?}, warm {:?}",
        cold, warm
    );
    let fw = asr("faster-whisper", "large-v3", "cpu").await;
    let start = Instant::now();
    let f = invoke(&fw, LONG).await.unwrap();
    let cold = start.elapsed();
    assert!(f["segments"].as_array().unwrap().iter().any(|s| {
        s["text"]
            .as_str()
            .unwrap_or("")
            .to_lowercase()
            .contains("circuit")
    }));
    let fw_child = children();
    assert_eq!(fw_child.len(), 1);
    assert_ne!(fw_child, cpu);
    let start = Instant::now();
    let f2 = invoke(&fw, LONG).await.unwrap();
    let warm = start.elapsed();
    assert_eq!(f, f2);
    assert_eq!(children(), fw_child);
    println!(
        "PASS real faster-whisper CPU same child and transcript; cold {:?}, warm {:?}",
        cold, warm
    );
}
