# PublishService 统一投稿编排层 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 创建独立的 PublishService 模块，统一自动模式和手动模式的投稿编排，支持多平台并行投稿与错误隔离。

**Architecture:** 在 `src/videoroll/apps/publish_service.py` 新建共享模块，提供 `publish()`（多平台）和 `publish_one()`（单平台）两个入口。自动模式和手动模式的调用方改为委托给它，不再直接 httpx 调后端。

**Tech Stack:** Python 3.12, FastAPI, httpx, SQLAlchemy, Celery

## Global Constraints

- 遵循项目现有 4 空格缩进、snake_case 命名风格
- 不改动 bilibili_publisher/ 和 social_publisher/ 微服务
- 不改动数据库模型（无新增表或字段）
- 复用现有 publish_platform_settings_store 和 publish_gateway
- 每个平台的 publish 调用独立 try/except，互不阻塞

---

### Task 1: 新建 PublishAllResult 数据类

**Files:**
- Create: `src/videoroll/apps/publish_service.py`
- Test: `tests/test_publish_service.py`

**Interfaces:**
- Produces: `PublishAllResult` 类，供后续所有 task 使用

- [ ] **Step 1: 创建 publish_service.py 骨架**

```python
# src/videoroll/apps/publish_service.py
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class PublishAllResult:
    """多平台投稿的结果汇总。"""
    results: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def all_ok(self) -> bool:
        return bool(self.results) and all(
            r.get("status") == "ok" for r in self.results.values()
        )

    @property
    def has_any_ok(self) -> bool:
        return any(r.get("status") == "ok" for r in self.results.values())

    @property
    def errors(self) -> dict[str, str]:
        return {
            platform: str(r.get("detail") or r.get("error") or "unknown")
            for platform, r in self.results.items()
            if r.get("status") != "ok"
        }

    @property
    def platform_count(self) -> int:
        return len(self.results)

    @property
    def ok_count(self) -> int:
        return sum(1 for r in self.results.values() if r.get("status") == "ok")

    @property
    def error_count(self) -> int:
        return self.platform_count - self.ok_count
```

- [ ] **Step 2: 编写 PublishAllResult 测试**

```python
# tests/test_publish_service.py
from videoroll.apps.publish_service import PublishAllResult


def test_all_ok_empty():
    r = PublishAllResult()
    assert r.all_ok is False  # 空结果不算 all_ok
    assert r.has_any_ok is False
    assert r.platform_count == 0


def test_all_ok_true():
    r = PublishAllResult(results={
        "bilibili": {"status": "ok", "job_id": "abc"},
        "douyin": {"status": "ok", "job_id": "def"},
    })
    assert r.all_ok is True
    assert r.has_any_ok is True
    assert r.platform_count == 2
    assert r.ok_count == 2
    assert r.error_count == 0
    assert r.errors == {}


def test_partial_failure():
    r = PublishAllResult(results={
        "bilibili": {"status": "ok"},
        "douyin": {"status": "error", "detail": "no account"},
    })
    assert r.all_ok is False
    assert r.has_any_ok is True
    assert r.error_count == 1
    assert r.errors == {"douyin": "no account"}


def test_all_failed():
    r = PublishAllResult(results={
        "bilibili": {"status": "error", "detail": "timeout"},
        "douyin": {"status": "error", "detail": "no account"},
    })
    assert r.all_ok is False
    assert r.has_any_ok is False
    assert r.error_count == 2
```

- [ ] **Step 3: 运行测试确认通过**

```bash
cd /mnt/d/kelric_soft/videoroll && python -m pytest tests/test_publish_service.py -v
```

- [ ] **Step 4: 提交**

```bash
git add src/videoroll/apps/publish_service.py tests/test_publish_service.py
git commit -m "feat: add PublishAllResult data class for multi-platform publish orchestration"
```

---

### Task 2: 实现 PublishService.publish() 多平台入口

**Files:**
- Modify: `src/videoroll/apps/publish_service.py`
- Modify: `tests/test_publish_service.py`

**Interfaces:**
- Consumes: `publish_platform_settings_store.get_publish_platform_settings()`, `publish_gateway.publish_backend_url()`
- Produces: `PublishService.publish(task_id, publish_payload=None) -> PublishAllResult`

- [ ] **Step 1: 实现 PublishService 核心类**

在 `publish_service.py` 中添加：

```python
import httpx
from sqlalchemy.orm import Session

from videoroll.apps.publish_gateway import (
    SUPPORTED_PUBLISH_PLATFORMS,
    normalize_publish_platform,
    publish_backend_url,
)
from videoroll.apps.publish_platform_settings_store import get_publish_platform_settings
from videoroll.apps.orchestrator_api.infrastructure.internal_http import internal_http_headers
from videoroll.config import OrchestratorSettings
from videoroll.db.models import Asset, AssetKind, PublishJob, PublishState, Task, TaskStatus
from videoroll.storage.s3 import S3Store


class PublishService:
    """
    统一投稿编排层。
    - publish(): 读取已启用平台，逐个投稿（自动模式用）
    - publish_one(): 只投指定平台（手动模式用）
    """

    def __init__(self, db: Session, settings: OrchestratorSettings, s3: S3Store):
        self._db = db
        self._settings = settings
        self._s3 = s3

    def publish(
        self,
        task_id: uuid.UUID,
        *,
        publish_payload: dict[str, Any] | None = None,
    ) -> PublishAllResult:
        """读取投稿设置里所有已启用平台，逐个投稿。"""
        enabled = self._get_enabled_platforms()
        if not enabled:
            return PublishAllResult(results={})
        return self._publish_to_platforms(task_id, enabled, publish_payload)

    def _get_enabled_platforms(self) -> list[str]:
        settings = get_publish_platform_settings(self._db)
        return [p for p, enabled in settings.items() if enabled]

    def _publish_to_platforms(
        self,
        task_id: uuid.UUID,
        platforms: list[str],
        base_payload: dict[str, Any] | None,
    ) -> PublishAllResult:
        results: dict[str, dict[str, Any]] = {}
        for platform in platforms:
            try:
                result = self._publish_single(task_id, platform, base_payload)
                results[platform] = {"status": "ok", **result}
            except Exception as exc:
                results[platform] = {"status": "error", "detail": str(exc)}
        return PublishAllResult(results=results)

    def _publish_single(
        self,
        task_id: uuid.UUID,
        platform: str,
        base_payload: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """调用单个平台的 publish API。"""
        task = self._db.get(Task, task_id)
        if not task:
            raise ValueError("task not found")
        if task.status == TaskStatus.published:
            return {"status": "skipped", "detail": "already published"}

        url = publish_backend_url(self._settings, platform)
        payload = dict(base_payload or {})
        payload.setdefault("platform", platform)

        with httpx.Client(timeout=60.0, headers=internal_http_headers(self._settings)) as client:
            resp = client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json() if resp.content else {}
        if isinstance(data, dict):
            data.setdefault("platform", platform)
        return data
```

- [ ] **Step 2: 编写 publish() 测试（mock httpx）**

```python
from unittest.mock import MagicMock, patch
import uuid

from videoroll.apps.publish_service import PublishService


@patch("videoroll.apps.publish_service.get_publish_platform_settings")
@patch("videoroll.apps.publish_service.httpx.Client")
def test_publish_calls_all_enabled_platforms(mock_client_cls, mock_get_settings):
    mock_get_settings.return_value = {"bilibili": True, "douyin": True, "xiaohongshu": False, "kuaishou": False}
    mock_resp = MagicMock()
    mock_resp.content = b'{"bvid": "test"}'
    mock_resp.json.return_value = {"bvid": "test"}
    mock_resp.raise_for_status = MagicMock()
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.post.return_value = mock_resp
    mock_client_cls.return_value = mock_client

    db = MagicMock()
    task = MagicMock()
    task.status = TaskStatus.rendered
    db.get.return_value = task
    settings = MagicMock()
    s3 = MagicMock()

    svc = PublishService(db, settings, s3)
    result = svc.publish(uuid.uuid4())

    assert result.platform_count == 2
    assert "bilibili" in result.results
    assert "douyin" in result.results
    assert mock_client.post.call_count == 2


@patch("videoroll.apps.publish_service.get_publish_platform_settings")
def test_publish_no_enabled_platforms(mock_get_settings):
    mock_get_settings.return_value = {"bilibili": False, "douyin": False, "xiaohongshu": False, "kuaishou": False}
    db = MagicMock()
    svc = PublishService(db, MagicMock(), MagicMock())
    result = svc.publish(uuid.uuid4())
    assert result.platform_count == 0
    assert result.has_any_ok is False
```

- [ ] **Step 3: 运行测试确认通过**

```bash
cd /mnt/d/kelric_soft/videoroll && python -m pytest tests/test_publish_service.py -v
```

- [ ] **Step 4: 提交**

```bash
git add src/videoroll/apps/publish_service.py tests/test_publish_service.py
git commit -m "feat: implement PublishService.publish() for multi-platform orchestration"
```

---

### Task 3: 实现 PublishService.publish_one() 单平台入口

**Files:**
- Modify: `src/videoroll/apps/publish_service.py`
- Modify: `tests/test_publish_service.py`

**Interfaces:**
- Consumes: 现有 `publishing_service.enqueue_publish_job` 的核心逻辑
- Produces: `PublishService.publish_one(task_id, platform, payload) -> RemotePublishResponse`

- [ ] **Step 1: 实现 publish_one()**

在 `PublishService` 类中添加：

```python
from videoroll.apps.orchestrator_api.schemas import PublishActionRequest, RemotePublishResponse

def publish_one(
    self,
    task_id: uuid.UUID,
    *,
    payload: PublishActionRequest,
) -> RemotePublishResponse:
    """手动模式：只投指定平台。"""
    from videoroll.apps.orchestrator_api.services import publishing_service

    # 复用现有的单平台发布逻辑
    return publishing_service.enqueue_publish_job(
        task_id, payload, self._settings, self._db, self._s3,
    )
```

- [ ] **Step 2: 编写 publish_one() 测试**

```python
@patch("videoroll.apps.publish_service.get_publish_platform_settings")
@patch("videoroll.apps.publish_service.httpx.Client")
def test_publish_single_platform_success(mock_client_cls, mock_get_settings):
    mock_get_settings.return_value = {"bilibili": True, "douyin": False}
    mock_resp = MagicMock()
    mock_resp.content = b'{"bvid": "BV_test"}'
    mock_resp.json.return_value = {"bvid": "BV_test", "platform": "bilibili"}
    mock_resp.raise_for_status = MagicMock()
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.post.return_value = mock_resp
    mock_client_cls.return_value = mock_client

    db = MagicMock()
    task = MagicMock()
    task.status = TaskStatus.rendered
    db.get.return_value = task
    settings = MagicMock()
    s3 = MagicMock()

    svc = PublishService(db, settings, s3)
    result = svc.publish(uuid.uuid4())
    assert result.all_ok is True
    assert "bilibili" in result.results
```

- [ ] **Step 3: 运行测试确认通过**

```bash
cd /mnt/d/kelric_soft/videoroll && python -m pytest tests/test_publish_service.py -v
```

- [ ] **Step 4: 提交**

```bash
git add src/videoroll/apps/publish_service.py tests/test_publish_service.py
git commit -m "feat: implement PublishService.publish_one() for single-platform publish"
```

---

### Task 4: 集成到 subtitle_service worker 的 after_render_publish

**Files:**
- Modify: `src/videoroll/apps/subtitle_service/worker.py` (around line 2313)

**Interfaces:**
- Consumes: `PublishService.publish(task_id, publish_payload=...)`
- Produces: after_render_publish 从调单平台 API 改为调 PublishService.publish()

- [ ] **Step 1: 修改 after_render_publish 函数**

找到 `after_render_publish` 函数（约 line 2313），将：

```python
with httpx.Client(timeout=30.0, headers=_ORCH_INTERNAL_HEADERS) as client:
    resp = client.post(f"{orch_base}/tasks/{task.id}/actions/publish", json=publish_payload)
    resp.raise_for_status()
```

改为：

```python
from videoroll.apps.publish_service import PublishService

store = S3Store(settings)
store.ensure_bucket()
svc = PublishService(db, settings, store)
result = svc.publish(task.id, publish_payload=publish_payload)
if not result.has_any_ok:
    error_details = "; ".join(f"{p}: {msg}" for p, msg in result.errors.items())
    raise RuntimeError(f"all platforms failed: {error_details}")
```

- [ ] **Step 2: 手动验证修改后的函数结构正确**

```bash
cd /mnt/d/kelric_soft/videoroll && python -c "from videoroll.apps.subtitle_service.worker import after_render_publish; print('import ok')"
```

- [ ] **Step 3: 提交**

```bash
git add src/videoroll/apps/subtitle_service/worker.py
git commit -m "refactor: after_render_publish uses PublishService for multi-platform publish"
```

---

### Task 5: 集成到 auto_youtube_pipeline 的尾部投稿

**Files:**
- Modify: `src/videoroll/apps/subtitle_service/worker.py` (around line 2750-2770)

**Interfaces:**
- Consumes: `PublishService.publish(task_id, publish_payload=...)`
- Produces: auto_youtube_pipeline 尾部从调单平台改为调 PublishService.publish()

- [ ] **Step 1: 修改 auto_youtube_pipeline 尾部**

找到 `auto_youtube_pipeline` 中发布部分（约 line 2750-2770），将：

```python
with httpx.Client(timeout=30.0, headers=_ORCH_INTERNAL_HEADERS) as client:
    resp = client.post(f"{orch_base}/tasks/{tid}/actions/publish", json=publish_payload)
    resp.raise_for_status()
```

改为：

```python
svc = PublishService(db, settings, store)
result = svc.publish(tid, publish_payload=publish_payload)
if not result.has_any_ok:
    error_details = "; ".join(f"{p}: {msg}" for p, msg in result.errors.items())
    raise RuntimeError(f"all platforms failed: {error_details}")
```

- [ ] **Step 2: 验证 import 正确**

```bash
cd /mnt/d/kelric_soft/videoroll && python -c "from videoroll.apps.subtitle_service.worker import auto_youtube_pipeline; print('import ok')"
```

- [ ] **Step 3: 提交**

```bash
git add src/videoroll/apps/subtitle_service/worker.py
git commit -m "refactor: auto_youtube_pipeline uses PublishService for multi-platform publish"
```

---

### Task 6: 添加 publish_all API 端点

**Files:**
- Modify: `src/videoroll/apps/orchestrator_api/routers/publishing.py`
- Modify: `src/videoroll/apps/orchestrator_api/services/publishing_service.py`
- Modify: `src/videoroll/apps/orchestrator_api/schemas.py`

**Interfaces:**
- Produces: `POST /tasks/{id}/actions/publish_all` → `PublishAllResult`

- [ ] **Step 1: 在 schemas.py 中添加 PublishAllResult schema**

```python
class PublishAllResultResponse(BaseModel):
    results: dict[str, dict[str, Any]] = Field(default_factory=dict)
    all_ok: bool = False
    has_any_ok: bool = False
    platform_count: int = 0
    ok_count: int = 0
    error_count: int = 0
    errors: dict[str, str] = Field(default_factory=dict)
```

- [ ] **Step 2: 在 publishing_service.py 中添加 publish_all 函数**

```python
def publish_all(
    task_id: uuid.UUID,
    publish_payload: dict[str, Any] | None,
    settings: OrchestratorSettings,
    db: Session,
    s3: S3Store,
) -> dict[str, Any]:
    from videoroll.apps.publish_service import PublishService
    svc = PublishService(db, settings, s3)
    result = svc.publish(task_id, publish_payload=publish_payload)
    return {
        "results": result.results,
        "all_ok": result.all_ok,
        "has_any_ok": result.has_any_ok,
        "platform_count": result.platform_count,
        "ok_count": result.ok_count,
        "error_count": result.error_count,
        "errors": result.errors,
    }
```

- [ ] **Step 3: 在 routers/publishing.py 中添加端点**

```python
@router.post("/tasks/{task_id}/actions/publish_all", response_model=PublishAllResultResponse)
def publish_all_platforms(
    task_id: uuid.UUID,
    publish_payload: dict[str, Any] | None = None,
    settings: OrchestratorSettings = Depends(get_settings),
    db: Session = Depends(get_db),
    s3: S3Store = Depends(get_s3),
) -> PublishAllResultResponse:
    return PublishAllResultResponse(**publishing_service.publish_all(task_id, publish_payload, settings, db, s3))
```

- [ ] **Step 4: 验证 API 可访问**

```bash
cd /mnt/d/kelric_soft/videoroll && python -c "from videoroll.apps.orchestrator_api.routers.publishing import router; print('router ok')"
```

- [ ] **Step 5: 提交**

```bash
git add src/videoroll/apps/orchestrator_api/
git commit -m "feat: add POST /tasks/{id}/actions/publish_all API endpoint"
```

---

### Task 7: 重构手动模式的 enqueue_publish_job 委托给 PublishService

**Files:**
- Modify: `src/videoroll/apps/orchestrator_api/services/publishing_service.py` (line ~470)
- Modify: `src/videoroll/apps/orchestrator_api/routers/publishing.py`

**Interfaces:**
- Consumes: `PublishService.publish_one()`
- Produces: 手动模式 API 端点行为不变，但内部走 PublishService

- [ ] **Step 1: 重构 enqueue_publish_job 内部实现**

将 `publishing_service.py` 中的 `enqueue_publish_job()` 函数改为内部委托：

```python
def enqueue_publish_job(
    task_id: uuid.UUID,
    payload: PublishActionRequest,
    settings: OrchestratorSettings,
    db: Session,
    s3: S3Store,
) -> RemotePublishResponse:
    # 保留前置检查（task 存在性、license、平台启用状态）
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    if task.source_license.value == "unknown":
        raise HTTPException(status_code=400, detail="source_license=unknown; add proof before publishing")
    requested_platform = normalize_publish_platform(payload.platform)
    if not is_publish_platform_enabled(db, requested_platform):
        raise HTTPException(status_code=409, detail=f"publish platform is disabled: {requested_platform}")

    # 委托给 PublishService
    from videoroll.apps.publish_service import PublishService
    svc = PublishService(db, settings, s3)
    return svc.publish_one(task_id, payload=payload)
```

- [ ] **Step 2: 运行现有测试确认不破坏手动模式**

```bash
cd /mnt/d/kelric_soft/videoroll && python -m pytest tests/ -v -k publish 2>&1 | tail -20
```

- [ ] **Step 3: 提交**

```bash
git add src/videoroll/apps/orchestrator_api/
git commit -m "refactor: enqueue_publish_job delegates to PublishService.publish_one()"
```

---

### Task 8: 前端展示自动模式的已启用平台列表

**Files:**
- Modify: `src/web/src/pages/SettingsAutoPage.tsx` (around line 463)

**Interfaces:**
- Consumes: `GET /settings/publish/platforms` (已有 API)
- Produces: 自动模式投稿区域展示已启用平台

- [ ] **Step 1: 在 SettingsAutoPage.tsx 中添加平台列表展示**

在「投稿（当前默认：哔哩哔哩）」区域，添加一个从 `/settings/publish/platforms` 读取已启用平台的展示组件。用现有的 `enabledPublishPlatforms` helper。

- [ ] **Step 2: 前端构建确认**

```bash
cd /mnt/d/kelric_soft/videoroll/src/web && npm run build
```

- [ ] **Step 3: 提交**

```bash
git add src/web/src/pages/SettingsAutoPage.tsx
git commit -m "feat: show enabled publish platforms in auto mode settings"
```
