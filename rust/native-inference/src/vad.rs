//! Silero V6 batch model and timestamp rules compatible with faster-whisper.
use anyhow::{Result, ensure};
use ort::{session::Session, value::Tensor};
const FRAME: usize = 512;
const CONTEXT: usize = 64;
const RATE: usize = 16000;
pub struct Vad {
    session: Session,
}
impl Vad {
    pub fn new(model: &str, library: &str) -> Result<Self> {
        let env = ort::init_from(library)?.build()?;
        let session = Session::builder(&env)?
            .with_intra_threads(1)
            .map_err(|e| anyhow::anyhow!(e.to_string()))?
            .with_inter_threads(1)
            .map_err(|e| anyhow::anyhow!(e.to_string()))?
            .commit_from_file(model)?;
        Ok(Self { session })
    }
    pub fn probabilities(&mut self, samples: &[f32]) -> Result<Vec<f32>> {
        ensure!(
            !samples.is_empty()
                && samples.len() <= 120 * RATE
                && samples.iter().all(|x| x.is_finite()),
            "invalid VAD audio"
        );
        let frames = samples.len() / FRAME + 1;
        let mut data = vec![0.0f32; frames * (FRAME + CONTEXT)];
        for n in 0..frames {
            let start = n * FRAME;
            let end = (start + FRAME).min(samples.len());
            let pos = n * (FRAME + CONTEXT);
            if n > 0 {
                data[pos..pos + CONTEXT].copy_from_slice(&samples[start - CONTEXT..start]);
            }
            if end > start {
                data[pos + CONTEXT..pos + CONTEXT + end - start]
                    .copy_from_slice(&samples[start..end]);
            }
        }
        let outputs = self.session.run(ort::inputs!["input" => Tensor::from_array(([frames, FRAME+CONTEXT], data.into_boxed_slice()))?, "h" => Tensor::from_array(([1usize,1,128],vec![0.0f32;128].into_boxed_slice()))?, "c" => Tensor::from_array(([1usize,1,128],vec![0.0f32;128].into_boxed_slice()))?])?;
        let (_, p) = outputs["speech_probs"].try_extract_tensor::<f32>()?;
        ensure!(
            p.len() == frames && p.iter().all(|x| x.is_finite() && *x >= 0.0 && *x <= 1.0),
            "invalid VAD output"
        );
        Ok(p.to_vec())
    }
}
// The threshold/hysteresis/silence algorithm follows the existing faster-whisper VAD behavior.
pub fn spans(p: &[f32], len: usize, threshold: f32, refine: bool) -> Vec<(usize, usize)> {
    let threshold = threshold.clamp(0.01, 0.99);
    let negative = (threshold - 0.15).max(0.01);
    let (min_speech, min_silence, pad, max_seconds) = if refine {
        (1920, 2560, 1280, 60)
    } else {
        (4000, 8000, 2880, 30)
    };
    let maximum = RATE * max_seconds - FRAME - 2 * pad;
    let mut result = Vec::new();
    let mut start: Option<usize> = None;
    let (mut temp, mut prev, mut next) = (0usize, 0usize, 0usize);
    for (i, &prob) in p.iter().enumerate() {
        let at = i * FRAME;
        if prob >= threshold && temp != 0 {
            temp = 0;
            if next < prev {
                next = at;
            }
        }
        if prob >= threshold && start.is_none() {
            start = Some(at);
            continue;
        }
        if let Some(a) = start {
            if at.saturating_sub(a) > maximum {
                if prev != 0 {
                    result.push((a, prev));
                    start = if next < prev { None } else { Some(next) };
                    prev = 0;
                    next = 0;
                    temp = 0;
                } else {
                    result.push((a, at.min(len)));
                    start = None;
                    prev = 0;
                    next = 0;
                    temp = 0;
                    continue;
                }
            }
        }
        if let Some(a) = start {
            if prob < negative {
                if temp == 0 {
                    temp = at;
                }
                if at - temp > 1568 {
                    prev = temp;
                }
                if at - temp < min_silence {
                    continue;
                }
                if temp.saturating_sub(a) > min_speech {
                    result.push((a, temp.min(len)));
                }
                start = None;
                prev = 0;
                next = 0;
                temp = 0;
            }
        }
    }
    if let Some(a) = start {
        if len.saturating_sub(a) > min_speech {
            result.push((a, len));
        }
    }
    if !result.is_empty() {
        result[0].0 = result[0].0.saturating_sub(pad);
    }
    for i in 0..result.len() {
        if i + 1 < result.len() {
            let gap = result[i + 1].0.saturating_sub(result[i].1);
            if gap < 2 * pad {
                result[i].1 += gap / 2;
                result[i + 1].0 = result[i + 1].0.saturating_sub(gap / 2);
            } else {
                result[i].1 = (result[i].1 + pad).min(len);
                result[i + 1].0 = result[i + 1].0.saturating_sub(pad);
            }
        } else {
            result[i].1 = (result[i].1 + pad).min(len);
        }
    }
    result.retain(|&(a, b)| a < b && b <= len);
    result
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn silence() {
        assert!(spans(&[0.; 100], 51200, 0.5, false).is_empty());
    }
    #[test]
    fn speech() {
        let mut p = vec![0.; 100];
        p[10..40].fill(0.9);
        assert_eq!(spans(&p, 51200, 0.5, false), vec![(2240, 23360)]);
    }
}
