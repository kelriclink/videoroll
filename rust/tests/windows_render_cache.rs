//! Compile and test the actual native worker media modules without its GUI.
//! Hardware D3D11/QSV validation still requires a Windows render node.
#![allow(dead_code)]
#[path = "../../native/windows-render-worker/src/ffmpeg.rs"]
mod ffmpeg;
#[rustfmt::skip]
#[path = "../../native/windows-render-worker/src/hardware.rs"]
mod hardware;
#[path = "../../native/windows-render-worker/src/logging.rs"]
mod logging;
#[rustfmt::skip]
#[path = "../../native/windows-render-worker/src/paths.rs"]
mod paths;

#[rustfmt::skip]
#[path = "../../native/windows-render-worker/src/protocol.rs"]
mod protocol;
#[rustfmt::skip]
#[path = "../../native/windows-render-worker/src/render_cache.rs"]
mod render_cache;
