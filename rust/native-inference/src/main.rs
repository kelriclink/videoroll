//! Isolated model adapter candidate. Workflow, leases and subtitle artifacts stay in the Rust parent.
mod vad;
use anyhow::{Context, Result, anyhow, ensure};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    fs::{File, OpenOptions},
    io::{Read, Write},
    os::unix::{
        fs::{MetadataExt, OpenOptionsExt},
        io::{AsRawFd, FromRawFd},
    },
    path::{Path, PathBuf},
    process::{Command, Stdio},
    time::{Duration, Instant},
};
use unicode_general_category::{GeneralCategory as Cat, get_general_category};
const MAX_REQUEST: usize = 256 * 1024;
const MAX_RESPONSE: usize = 12 * 1024 * 1024;
const MAX_SAMPLES: usize = 120 * 16000;
fn ov_root() -> PathBuf {
    std::env::var_os("VIDEOROLL_OPENVINO_RUNTIME")
        .map(PathBuf::from)
        .unwrap_or_else(|| "/opt/videoroll/runtime/openvino-2026.4.1".into())
}
fn vad_root() -> PathBuf {
    std::env::var_os("VIDEOROLL_SILERO_RUNTIME")
        .map(PathBuf::from)
        .unwrap_or_else(|| "/opt/videoroll/runtime/silero-v6-ort-1.30.0".into())
}
fn audio(path: &str) -> Result<Vec<f32>> {
    let path = Path::new(path);
    ensure!(path.is_absolute() && path.is_file(), "invalid audio path");
    if let Ok(mut r) = hound::WavReader::open(path) {
        let s = r.spec();
        if s.channels == 1
            && s.sample_rate == 16000
            && s.bits_per_sample == 16
            && s.sample_format == hound::SampleFormat::Int
        {
            ensure!(r.duration() as usize <= MAX_SAMPLES, "audio too long");
            let v = r
                .samples::<i16>()
                .map(|x| x.map(|s| s as f32 / 32768.0))
                .collect::<std::result::Result<Vec<_>, _>>()?;
            ensure!(!v.is_empty(), "empty audio");
            return Ok(v);
        }
    }
    let mut child = Command::new("/usr/bin/ffmpeg")
        .args([
            "-nostdin",
            "-v",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-i",
        ])
        .arg(path)
        .args(["-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "pipe:1"])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()?;
    let mut bytes = Vec::new();
    let result = child
        .stdout
        .take()
        .ok_or_else(|| anyhow!("missing decoder pipe"))?
        .take((MAX_SAMPLES * 2 + 1) as u64)
        .read_to_end(&mut bytes);
    if result.is_err() || bytes.len() > MAX_SAMPLES * 2 {
        let _ = child.kill();
        let _ = child.wait();
        return Err(anyhow!("audio decoder limit"));
    }
    result?;
    ensure!(
        child.wait()?.success() && bytes.len() % 2 == 0 && !bytes.is_empty(),
        "audio decode failed"
    );
    Ok(bytes
        .chunks_exact(2)
        .map(|x| i16::from_le_bytes([x[0], x[1]]) as f32 / 32768.)
        .collect())
}
fn silent(v: &[f32]) -> bool {
    v.is_empty()
        || (v.iter().all(|x| x.abs() <= 0.005)
            && (v.iter().map(|x| (*x as f64).powi(2)).sum::<f64>() / v.len() as f64).sqrt()
                <= 0.0008)
}
fn garbage(s: &str) -> bool {
    let chars: Vec<char> = s.chars().filter(|c| !c.is_whitespace()).collect();
    let letters = chars
        .iter()
        .filter(|&&c| {
            matches!(
                get_general_category(c),
                Cat::UppercaseLetter
                    | Cat::LowercaseLetter
                    | Cat::TitlecaseLetter
                    | Cat::ModifierLetter
                    | Cat::OtherLetter
            )
        })
        .count();
    let digits = chars
        .iter()
        .filter(|&&c| matches!(get_general_category(c), Cat::DecimalNumber))
        .count();
    let punct = chars
        .iter()
        .filter(|&&c| {
            matches!(
                get_general_category(c),
                Cat::ConnectorPunctuation
                    | Cat::DashPunctuation
                    | Cat::OpenPunctuation
                    | Cat::ClosePunctuation
                    | Cat::InitialPunctuation
                    | Cat::FinalPunctuation
                    | Cat::OtherPunctuation
                    | Cat::MathSymbol
                    | Cat::CurrencySymbol
                    | Cat::ModifierSymbol
                    | Cat::OtherSymbol
            )
        })
        .count();
    chars.is_empty()
        || (letters == 0 && digits == 0)
        || (digits >= 2
            && chars
                .iter()
                .all(|c| c.is_numeric() || ".,:;!?-".contains(*c)))
        || (letters == 0 && digits > 0 && punct >= 2)
        || (chars.len() >= 4
            && chars
                .iter()
                .all(|c| c.to_lowercase().eq(chars[0].to_lowercase())))
        || (letters == 0 && digits <= 1 && punct >= 3usize.max(chars.len().saturating_sub(1)))
}
fn string<'a>(r: &'a Value, key: &str) -> Result<&'a str> {
    r[key].as_str().ok_or_else(|| anyhow!("missing {key}"))
}
fn ov_load() -> Result<()> {
    openvino_genai::load_from(ov_root().join("runtime/lib/intel64/libopenvino_genai_c.so"))
        .map_err(|e| anyhow!(e))?;
    Ok(())
}
struct Embedding {
    tokenizer: tokenizers::Tokenizer,
    compiled: openvino::CompiledModel,
}
impl Embedding {
    fn new(req: &Value) -> Result<Self> {
        let model = Path::new(string(req, "model_path")?);
        let mut tokenizer = tokenizers::Tokenizer::from_file(model.join("tokenizer.json"))
            .map_err(|e| anyhow!(e.to_string()))?;
        let max_len = std::fs::read_to_string(model.join("sentence_bert_config.json"))
            .ok()
            .and_then(|s| serde_json::from_str::<Value>(&s).ok())
            .and_then(|v| v["max_seq_length"].as_u64())
            .unwrap_or(512)
            .clamp(1, 512) as usize;
        tokenizer
            .with_truncation(Some(tokenizers::TruncationParams {
                max_length: max_len,
                ..Default::default()
            }))
            .map_err(|e| anyhow!(e.to_string()))?;
        let mut core = openvino::Core::new()?;
        let ir = if model.join("openvino_model.xml").is_file() {
            model.join("openvino_model.xml")
        } else {
            model.join("openvino/openvino_model.xml")
        };
        let loaded = core.read_model_from_file(
            ir.to_str().context("model path")?,
            ir.with_extension("bin").to_str().context("weights path")?,
        )?;
        let device = string(req, "device")?;
        let device = device
            .split_once(':')
            .map(|(_, d)| d)
            .filter(|d| !d.is_empty())
            .unwrap_or("CPU");
        let compiled = core.compile_model(&loaded, openvino::DeviceType::from(device))?;
        Ok(Self {
            tokenizer,
            compiled,
        })
    }
    fn encode(&mut self, text: &str) -> Result<Value> {
        let encoded = self
            .tokenizer
            .encode(text, true)
            .map_err(|e| anyhow!(e.to_string()))?;
        let len = encoded.len();
        ensure!(len > 0, "empty tokens");
        let compiled = &mut self.compiled;
        let mut request = compiled.create_infer_request()?;
        let mut tensors = vec![];
        for i in 0..compiled.get_input_size()? {
            let name = compiled.get_input_by_index(i)?.get_name()?;
            let values = match name.as_str() {
                "input_ids" => encoded.get_ids(),
                "attention_mask" => encoded.get_attention_mask(),
                "token_type_ids" => encoded.get_type_ids(),
                _ => return Err(anyhow!("unsupported model input")),
            };
            let mut tensor = openvino::Tensor::new(
                openvino::ElementType::I64,
                &openvino::Shape::new(&[1, len as i64])?,
            )?;
            for (d, s) in tensor.get_data_mut::<i64>()?.iter_mut().zip(values) {
                *d = *s as i64;
            }
            request.set_tensor(&name, &tensor)?;
            tensors.push(tensor);
        }
        request.infer()?;
        let mut selected = None;
        for i in 0..compiled.get_output_size()? {
            let out = request.get_output_tensor_by_index(i)?;
            let shape = out.get_shape()?;
            let dims = shape.get_dimensions();
            if dims.len() == 3 && dims[0] == 1 && dims[1] == len as i64 && dims[2] > 0 {
                ensure!(selected.is_none(), "ambiguous embedding output");
                selected = Some((out, dims[2] as usize));
            }
        }
        let (out, hidden) = selected.ok_or_else(|| anyhow!("embedding hidden output missing"))?;
        let data = out.get_data::<f32>()?;
        ensure!(data.len() == len * hidden, "embedding size mismatch");
        let mut vector = vec![0f32; hidden];
        let mut count = 0f32;
        for (row, &mask) in data.chunks_exact(hidden).zip(encoded.get_attention_mask()) {
            if mask != 0 {
                for (a, b) in vector.iter_mut().zip(row) {
                    *a += *b;
                }
                count += 1.;
            }
        }
        ensure!(count > 0., "empty attention mask");
        for x in &mut vector {
            *x /= count;
        }
        let norm = vector.iter().map(|x| x * x).sum::<f32>().sqrt();
        ensure!(norm.is_finite() && norm > 0., "invalid embedding norm");
        for x in &mut vector {
            *x /= norm;
        }
        Ok(json!({"embedding":vector}))
    }
}
#[derive(Default)]
struct Adapter {
    vad: Option<vad::Vad>,
    whisper: Option<openvino_genai::WhisperPipeline>,
    embedding: Option<Embedding>,
}
impl Adapter {
    fn spans(&mut self, v: &[f32], threshold: f32, refine: bool) -> Result<Vec<(usize, usize)>> {
        if silent(v) {
            return Ok(vec![]);
        }
        if self.vad.is_none() {
            let root = vad_root();
            self.vad = Some(vad::Vad::new(
                root.join("silero_vad_v6.onnx")
                    .to_str()
                    .context("vad path")?,
                root.join("libonnxruntime.so")
                    .to_str()
                    .context("ort path")?,
            )?);
        }
        let p = self.vad.as_mut().unwrap().probabilities(v)?;
        Ok(vad::spans(&p, v.len(), threshold, refine))
    }
    fn whisper(&mut self, v: &[f32], req: &Value) -> Result<Value> {
        let spans = if req["vad_enabled"].as_bool().unwrap_or(true) {
            self.spans(
                v,
                req["vad_threshold"].as_f64().unwrap_or(0.5) as f32,
                false,
            )?
        } else if silent(v) {
            vec![]
        } else {
            vec![(0, v.len())]
        };
        if spans.is_empty() {
            return Ok(json!({"segments":[]}));
        }
        let model = string(req, "model_path")?;
        if self.whisper.is_none() {
            ov_load()?;
            let timer = Instant::now();
            self.whisper = Some(openvino_genai::WhisperPipeline::new(
                model,
                string(req, "device")?,
            )?);
            eprintln!("model_load_seconds={:.3}", timer.elapsed().as_secs_f64());
        }
        let pipe = self.whisper.as_mut().unwrap();
        let beams = req["num_beams"].as_u64().unwrap_or(1).clamp(1, 32);
        let max_tokens = req["max_new_tokens"].as_u64().unwrap_or(448).clamp(1, 2048);
        // Build the exact model generation config, rather than assume the C API's returned base config is mutable in place.
        let mut raw: Value = serde_json::from_slice(&std::fs::read(
            Path::new(model).join("generation_config.json"),
        )?)?;
        ensure!(raw.is_object(), "invalid generation config");
        raw["num_beams"] = json!(beams);
        raw["max_new_tokens"] = json!(max_tokens);
        raw["return_timestamps"] = json!(true);
        let mut file = tempfile::NamedTempFile::new()?;
        serde_json::to_writer(&mut file, &raw)?;
        file.flush()?;
        let mut cfg = openvino_genai::WhisperGenerationConfig::from_json(
            file.path().to_str().context("config path")?,
        )?;
        cfg.set_return_timestamps(true)?;
        cfg.set_task("transcribe")?;
        if let Some(lang) = req["language"]
            .as_str()
            .filter(|v| *v != "auto" && !v.is_empty())
        {
            cfg.set_language(&if lang.starts_with("<|") {
                lang.to_owned()
            } else {
                format!("<|{}|>", lang.to_lowercase())
            })?;
        }
        ensure!(
            cfg.get_generation_config()?.get_max_new_tokens()? == max_tokens as usize,
            "generation config not applied"
        );
        let mut rows = vec![];
        let timer = Instant::now();
        for (a, b) in spans {
            let dur = (b - a) as f64 / 16000.;
            let offset = a as f64 / 16000.;
            let result = pipe.generate(&v[a..b], Some(&cfg))?;
            let mut current = vec![];
            if result.has_chunks()? {
                for i in 0..result.get_chunks_count()? {
                    let x = result.get_chunk_at(i)?;
                    let start = (x.get_start_ts()? as f64).clamp(0., dur);
                    let end = (x.get_end_ts()? as f64).clamp(start, dur);
                    let text = x
                        .get_text()?
                        .split_whitespace()
                        .collect::<Vec<_>>()
                        .join(" ");
                    if end > start && !text.is_empty() {
                        current.push(json!({"start":start+offset,"end":end+offset,"text":text}));
                    }
                }
            }
            if current.is_empty() {
                let text = result
                    .get_string()?
                    .split_whitespace()
                    .collect::<Vec<_>>()
                    .join(" ");
                if !text.is_empty() {
                    current.push(json!({"start":offset,"end":offset+dur,"text":text}));
                }
            }
            rows.extend(
                current
                    .into_iter()
                    .filter(|x| !garbage(x["text"].as_str().unwrap_or(""))),
            );
        }
        eprintln!("inference_seconds={:.3}", timer.elapsed().as_secs_f64());
        Ok(json!({"segments":rows}))
    }
    fn dispatch(&mut self, req: &Value) -> Result<Value> {
        match req["kind"].as_str() {
            Some("vad") => {
                let result = (|| {
                    let p = Path::new(string(req, "audio")?);
                    ensure!(
                        p.metadata()?.len() <= 24 * 1024 * 1024,
                        "VAD input too large"
                    );
                    let v = audio(p.to_str().context("audio path")?)?;
                    self.spans(
                        &v,
                        req["threshold"].as_f64().unwrap_or(0.5) as f32,
                        req["purpose"] == "timing",
                    )
                })();
                match result {
                    Ok(spans) => Ok(
                        json!({"available":true,"spans":spans.iter().map(|&(a,b)|json!({"start_ms":(a as f64/16.).round_ties_even() as u64,"end_ms":(b as f64/16.).round_ties_even() as u64})).collect::<Vec<_>>()}),
                    ),
                    Err(_) => Ok(json!({"available":false})),
                }
            }
            Some("asr") => {
                ensure!(req["engine"] == "openvino", "unsupported native ASR engine");
                self.whisper(&audio(string(req, "audio")?)?, req)
            }
            Some("embedding") => {
                ensure!(
                    string(req, "device")?
                        .to_ascii_lowercase()
                        .starts_with("openvino"),
                    "unsupported native embedding device"
                );
                if self.embedding.is_none() {
                    self.embedding = Some(Embedding::new(req)?);
                }
                self.embedding
                    .as_mut()
                    .unwrap()
                    .encode(string(req, "text")?)
            }
            Some("devices") => {
                let c = openvino::Core::new()?;
                Ok(
                    json!({"devices":c.available_devices()?.iter().map(ToString::to_string).collect::<Vec<_>>()}),
                )
            }
            _ => Err(anyhow!("unsupported operation")),
        }
    }
}
fn identity(req: &Value) -> Value {
    json!([
        req["kind"],
        req["engine"],
        req["model_path"],
        req["device"],
        req["compute_type"],
        req["cpu_threads"],
        req["num_workers"]
    ])
}
fn lock_gpu(req: &Value) -> Result<Option<File>> {
    let mut device = req["device"].as_str().unwrap_or("").trim().to_uppercase();
    let mut engine = req["engine"].as_str().unwrap_or("").to_lowercase();
    if device == "OPENVINO" || device == "OPENVINO:" {
        device = "CPU".into();
    } else if let Some(d) = device.strip_prefix("OPENVINO:") {
        device = d.to_owned();
        engine = "openvino".into();
    }
    if req["kind"] == "devices" || device == "CPU" {
        return Ok(None);
    }
    if engine == "openvino" && matches!(device.as_str(), "GPU" | "GPU.0" | "AUTO") {
        device = "GPU.0".into();
    }
    if device.is_empty() {
        device = "GPU".into();
    }
    let base = std::env::var("VIDEOROLL_LOCAL_GPU_LOCK")?;
    ensure!(Path::new(&base).is_absolute(), "invalid GPU lock root");
    let digest = format!("{:x}", Sha256::digest(device.as_bytes()));
    let count = std::env::var("VIDEOROLL_LOCAL_GPU_SLOTS")
        .ok()
        .and_then(|s| s.parse::<u8>().ok())
        .unwrap_or(1)
        .clamp(1, 8);
    let deadline = Instant::now() + Duration::from_secs(3600);
    while Instant::now() < deadline {
        for slot in 0..count {
            let file = OpenOptions::new()
                .read(true)
                .write(true)
                .create(true)
                .mode(0o600)
                .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
                .open(format!("{base}.{}.slot-{slot}.lock", &digest[..16]))?;
            let meta = file.metadata()?;
            ensure!(
                meta.is_file()
                    && meta.uid() == unsafe { libc::geteuid() }
                    && meta.nlink() == 1
                    && meta.mode() & 0o077 == 0,
                "unsafe GPU lock"
            );
            let rc = unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) };
            if rc == 0 {
                return Ok(Some(file));
            }
            let e = std::io::Error::last_os_error();
            if e.kind() != std::io::ErrorKind::WouldBlock {
                return Err(e.into());
            }
        }
        std::thread::sleep(Duration::from_millis(250));
    }
    Err(anyhow!("GPU admission timeout"))
}
fn serve() -> Result<()> {
    let fd = unsafe { libc::dup(libc::STDOUT_FILENO) };
    ensure!(fd >= 0, "protocol pipe");
    let mut output = unsafe { File::from_raw_fd(fd) };
    ensure!(
        unsafe { libc::dup2(libc::STDERR_FILENO, libc::STDOUT_FILENO) } >= 0,
        "redirect stdout"
    );
    let mut stdin = std::io::stdin().lock();
    let mut current = None;
    let mut gpu_lock = None;
    let mut adapter = Adapter::default();
    loop {
        let mut head = [0u8; 4];
        let n = stdin.read(&mut head[..1])?;
        if n == 0 {
            break;
        }
        stdin.read_exact(&mut head[1..])?;
        let len = u32::from_be_bytes(head) as usize;
        ensure!(len > 0 && len <= MAX_REQUEST, "invalid request frame");
        let mut raw = vec![0; len];
        stdin.read_exact(&mut raw)?;
        let result = (|| {
            let req: Value = serde_json::from_slice(&raw)?;
            let key = identity(&req);
            if let Some(previous) = &current {
                ensure!(*previous == key, "model identity changed");
            } else {
                gpu_lock = lock_gpu(&req)?;
                current = Some(key);
            }
            adapter.dispatch(&req)
        })();
        let ok = result.is_ok();
        let envelope = match result {
            Ok(result) => json!({"ok":true,"result":result}),
            Err(e) => {
                eprintln!("native inference error: {e:#}");
                json!({"ok":false,"error_type":"NativeInferenceError"})
            }
        };
        let bytes = serde_json::to_vec(&envelope)?;
        ensure!(bytes.len() <= MAX_RESPONSE, "response too large");
        output.write_all(&(bytes.len() as u32).to_be_bytes())?;
        output.write_all(&bytes)?;
        output.flush()?;
        if !ok {
            break;
        }
    }
    drop(adapter);
    drop(gpu_lock);
    Ok(())
}
fn main() -> Result<()> {
    let args = std::env::args().skip(1).collect::<Vec<_>>();
    if args == ["--serve"] {
        return serve();
    }
    ensure!(args.is_empty(), "unsupported arguments");
    let req: Value = serde_json::from_reader(std::io::stdin().lock().take(MAX_REQUEST as u64 + 1))?;
    println!("{}", Adapter::default().dispatch(&req)?);
    Ok(())
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn cpu_devices_never_take_gpu_lock() {
        assert!(lock_gpu(&json!({"kind":"asr","engine":"openvino","device":"CPU"})).unwrap().is_none());
        assert!(lock_gpu(&json!({"kind":"embedding","device":"openvino:CPU"})).unwrap().is_none());
        assert!(lock_gpu(&json!({"kind":"devices"})).unwrap().is_none());
    }
    #[test]
    fn garbage_filter() {
        assert!(garbage("!!!"));
        assert!(garbage("1,2,3"));
        assert!(garbage("哈哈哈哈"));
        assert!(!garbage("Hello world"));
        assert!(!garbage("你好，世界"));
    }
}
