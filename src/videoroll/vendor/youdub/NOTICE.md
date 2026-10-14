# YouDub third-party audio engine

The files in this directory are derived from [YouDub WebUI](https://github.com/liuzhao1225/YouDub-webui), by Zhao Liu and other contributors.

- Upstream revision: `0f6c75935e7a208c4b8ea56140e31c5316953fec`
- License: Apache License 2.0; the complete upstream license is in `LICENSE`.
- Copied without modifications from upstream:
  - `backend/app/adapters/audio.py` → `adapters/audio.py`
  - `backend/app/audio_mode.py` → `audio_mode.py`
  - `backend/app/adapters/voxcpm.py` → `reference_sources/voxcpm.py`
- The VoxCPM source is archived as a reference for potential separate model support, and **is not an active or importable production backend**. VideoRoll currently supplies WAVs using its installed CosyVoice3/Qwen3-TTS model drivers.
- Integration adapter: `videoroll.apps.dubbing.youdub_adapter` converts VideoRoll time ranges and per-fragment WAV caches to YouDub's `translation.json` and numbered WAV format, and calls the original `merge_tts_audio` implementation.
- VideoRoll publishing adapter pads the mix to the source duration and extends the final video frame when YouDub's audio exceeds the source video's duration. This packaging step is separate from the upstream audio alignment algorithm.

Preserve upstream copyright/license notices when redistributing, and describe any modifications to upstream files. No upstream source files listed above have been modified.
