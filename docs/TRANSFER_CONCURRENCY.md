# YouTube / Bilibili transfer concurrency

## YouTube downloader

YouTube URL extraction stays in yt-dlp (signature handling, cookies, format
selection, video+audio mux, retries). Once a direct signed HTTP(S) media URL is
resolved, the aria2c downloader tries bounded Range downloads for an estimated
size >=24 MiB, or if the extractor does not know the size in advance. aria2c
checks Range support; if it fails, the workflow falls back to yt-dlp with
independent partial files and retained checkpoints. An explicit configured
connection ceiling of 1–16 is shared with yt-dlp's DASH/HLS native concurrent
fragment downloader, which cannot use aria2c's direct-file Range mechanism.

Actual Range connections may be fewer than the requested number because of
media size, server throttling or supported splits. Reports retain the last
measured connection count alongside aggregate speed, bytes, progress and ETA.

Real isolated download trial (link, 2026-10-17): NASA `mrqXTHiphn0`, 103.4MB,
finished in **48.63s** via aria2c, max 8 observed Range connections; previous
same-video auto task took 184.7s on yt-dlp native. Not a controlled WAN
benchmark; results vary with server and network.

## Bilibili UPOS uploader

Bilibili already has independent multipart PUT chunks, and its protocol
requires all partNumbers and ETags to be acknowledged before completion.
Upload **one video at a time** inside its existing publication job. Use
configurable concurrent PUT workers (1–8, default 4), shared by regular P1/P2
publish and automatic P2 append. Settings are in **投稿与发布 > B站分片上传并发**.
UPOS parallel chunk upload is not parallel archive submission and will not
create duplicated Bilibili posts.

Retry only one failed PUT on transient `httpx` network/timeouts and HTTP 408 or
5xx (bounded three additional attempts and exponential backoff). Never retry
HTTP 429 or other 4xx automatically. If any chunk fails, do **not** perform
multipart-finalize POST. Upload progress counts only acknowledged completed
bytes. Finalize sorts ETags by partNumber, independent of completion ordering.

A crash mid-upload still follows VideoRoll's existing unknown/reconciliation
rules; we do **not** claim durable cross-process upload resumes or automatically
re-submit potentially published Bilibili archives. The `upload_workers` and
`chunk_retries` diagnostics are returned without UPOS credentials.

## Testing

- Real NASA HTTP Range transfer benchmark above, in independent `work/benchmark`.
- `tests/test_youtube_parallel_downloader.py`: live local Range server, DASH
  selection, connection limits, and isolated yt-dlp fallback.
- `tests/test_bilibili_upload_parallel.py`: concurrent PUT barrier, 8 workers,
  sparse transient retry, correct ETag ordering, no completion on failed parts,
  429 non-retry, settings persistence and bounds.
- The complete Python test suite plus the `src/web` TypeScript/Vite build.
- Do not run a second real Bilibili post just to benchmark upload speed; the
  prior production published archive provides the baseline (~437 MB/418.5s).
