# Social Browser Runtime Home Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent Patchright Chromium from crashing under the non-root social-publisher runtime user because its HOME configuration is owned by root.

**Architecture:** Give the runtime user a dedicated writable HOME and execute the browser smoke test after switching to that user. Extend the smoke test through page creation so a Crashpad startup failure cannot pass image construction.

**Tech Stack:** Docker, Python 3.12, Patchright Chromium, pytest

## Global Constraints

- Do not modify or restart the production deployment.
- Do not add `fromprod/`, secrets, cookies, or local SAU working trees to Git.
- Keep the change limited to the social-publisher image and its container regression test.

---

### Task 1: Lock the runtime browser contract

**Files:**
- Modify: `tests/test_social_publisher_container.py`

**Interfaces:**
- Consumes: `docker/social-publisher.Dockerfile` and `docker/verify-social-browser.py` as text fixtures.
- Produces: Regression assertions for a private runtime HOME, non-root smoke-test ordering, and real page creation.

- [x] **Step 1: Write the failing test**

Add assertions that the Dockerfile sets `HOME=/tmp/videoroll-home`, creates it for `videoroll`, runs `USER videoroll` before the browser verifier, and that the verifier creates a page and navigates to `about:blank`.

- [x] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_social_publisher_container.py -q`

Expected: FAIL because the current image uses `HOME=/tmp`, runs verification as root, and does not create a page.

### Task 2: Fix the image runtime identity boundary

**Files:**
- Modify: `docker/social-publisher.Dockerfile`
- Modify: `docker/verify-social-browser.py`

**Interfaces:**
- Consumes: Patchright's installed Chrome compatibility channel.
- Produces: A build that proves Chrome can create a page as UID 10001 using the same HOME used at runtime.

- [x] **Step 1: Implement the minimal Dockerfile fix**

Create `/tmp/videoroll-home` with owner `videoroll`, set it as HOME only after root-only installation steps, switch to `USER videoroll`, then run the verifier.

- [x] **Step 2: Strengthen the verifier**

Create a page, navigate to `about:blank`, and close the browser normally.

- [x] **Step 3: Run the regression test**

Run: `.venv/bin/python -m pytest tests/test_social_publisher_container.py -q`

Expected: PASS.

### Task 3: Verify and commit

**Files:**
- Verify: `docker/social-publisher.Dockerfile`
- Verify: `docker/verify-social-browser.py`
- Verify: `tests/test_social_publisher_container.py`

**Interfaces:**
- Consumes: Docker build context and the targeted pytest suite.
- Produces: A locally verified `videoroll-social-publisher:prod` image and focused Git commit.

- [x] **Step 1: Build the social-publisher image**

Run: `docker build -f docker/social-publisher.Dockerfile -t videoroll-social-publisher:prod .`

Expected: The non-root browser verifier prints its compatibility message and the build succeeds.

- [x] **Step 2: Review the diff and repository status**

Run: `git diff --check && git status --short`

Expected: Only the Dockerfile, verifier, regression test, and this plan are changed; unrelated local directories remain untracked.

- [ ] **Step 3: Commit**

Run: `git add docker/social-publisher.Dockerfile docker/verify-social-browser.py tests/test_social_publisher_container.py docs/superpowers/plans/2026-07-13-social-browser-runtime-home.md && git commit -m "fix: run social browser with writable home"`
