from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator


class BilibiliSubtitle(BaseModel):
    # NOTE: B 站 Web 投稿接口里 open=0 表示启用字幕投稿，open=1 表示不启用。
    open: int = 0
    lan: str = ""


class BilibiliPublishMeta(BaseModel):
    """
    A "safe" meta model for Bilibili publish.

    This project is compliance-first and currently uses a mock publisher by default.
    We still validate and normalize common fields so the UI + orchestration can be stable,
    and future real publishing can map to Bilibili's required payload shape.
    """

    model_config = ConfigDict(extra="allow")

    title: str = Field(..., description="视频标题（建议 ≤ 80 字）")
    desc: str = Field("", description="视频简介（建议 ≤ 2000 字，需包含来源/授权说明）")

    # Common aliases from various clients/libraries.
    typeid: int = Field(..., validation_alias=AliasChoices("typeid", "tid"), description="分区 ID（tid/typeid）")
    tags: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("tags", "tag", "keywords"),
        description="标签数组（最多 10 个）；也可用逗号分隔字符串 tag/keywords",
    )

    copyright: Literal[1, 2] = Field(1, description="1: 自制 2: 转载")
    source: str = Field("", description="转载来源（copyright=2 时必填）")

    # Optional: scheduled publish time. Keep as unix seconds for portability.
    dtime: Optional[int] = Field(None, description="定时发布 UNIX 秒时间戳（可选）")

    # Extended fields commonly required by B 站 Web 投稿（add/v3）接口；这里提供默认值，便于后续真实接入。
    dynamic: str = Field("", description="粉丝动态文案（可为空字符串）")
    desc_format_id: int = Field(9999, description="简介格式 ID（9999=纯文本）")
    desc_v2: Optional[Any] = Field(None, description="简介附加结构（@ 用户等，可选）")

    recreate: int = Field(-1, description="是否允许二创：-1 允许（默认）/ 1 不允许")
    interactive: int = Field(0, description="互动视频：0 否")
    act_reserve_create: int = Field(0, description="活动预约：0 否")
    no_disturbance: int = Field(0, description="是否推送到动态：0 不推送 / 1 推送")
    no_reprint: int = Field(1, description="是否允许转载：1 允许 / 0 不允许")

    subtitle: BilibiliSubtitle = Field(default_factory=BilibiliSubtitle, description="字幕投稿设置")

    dolby: int = Field(0, description="杜比音效：0 否 / 1 是")
    lossless_music: int = Field(0, description="无损音乐：0 否 / 1 是")

    up_selection_reply: bool = Field(False, description="精选评论")
    up_close_reply: bool = Field(False, description="关闭评论")
    up_close_danmu: bool = Field(False, validation_alias=AliasChoices("up_close_danmu", "up_close_danmaku"), description="关闭弹幕")

    web_os: int = Field(3, description="平台类型（B 站 Web 常见为 3）")

    # Less commonly used flags; keep optional.
    is_only_self: Optional[int] = Field(None, description="可见性：0 公开 / 1 仅自己可见（可选）")
    topic_id: Optional[int] = None
    mission_id: Optional[int] = None
    is_360: Optional[int] = Field(None, description="全景：-1 非全景 / 1 全景（可选）")
    neutral_mark: Optional[str] = None
    human_type2: Optional[int] = None

    @field_validator("title", mode="before")
    @classmethod
    def _title_strip(cls, v: Any) -> str:
        return str(v or "").strip()

    @field_validator("desc", "source", "dynamic", mode="before")
    @classmethod
    def _str_strip(cls, v: Any) -> str:
        return str(v or "").strip()

    @field_validator("tags", mode="before")
    @classmethod
    def _tags_normalize(cls, v: Any) -> list[str]:
        if v is None:
            return []
        if isinstance(v, str):
            parts = [p.strip() for p in v.replace("，", ",").split(",")]
            return [p for p in parts if p]
        if isinstance(v, (list, tuple, set)):
            out: list[str] = []
            for item in v:
                s = str(item or "").strip()
                if s:
                    out.append(s)
            return out
        return [str(v).strip()] if str(v).strip() else []

    @model_validator(mode="after")
    def _validate_meta(self) -> "BilibiliPublishMeta":
        if not self.title:
            raise ValueError("meta.title is required")
        if len(self.title) > 80:
            raise ValueError("meta.title too long (max 80)")
        if len(self.desc) > 2000:
            raise ValueError("meta.desc too long (max 2000)")
        if not isinstance(self.typeid, int) or self.typeid <= 0:
            raise ValueError("meta.typeid/tid must be a positive integer")

        # B 站限制：最多 10 个标签。这里做强校验，避免后续真实投稿失败。
        tags = [t for t in self.tags if t]
        if len(tags) == 0:
            raise ValueError("meta.tags is required (at least 1)")
        if len(tags) > 10:
            raise ValueError("meta.tags too long (max 10)")
        # Dedupe while keeping order.
        seen: set[str] = set()
        deduped: list[str] = []
        for t in tags:
            if t in seen:
                continue
            seen.add(t)
            deduped.append(t)
        self.tags = deduped

        if self.copyright == 2 and not self.source:
            raise ValueError("meta.source is required when meta.copyright=2")
        if self.recreate not in (-1, 1):
            raise ValueError("meta.recreate must be -1 (allow) or 1 (disallow)")
        if self.no_reprint not in (0, 1):
            raise ValueError("meta.no_reprint must be 0 or 1")
        if self.no_disturbance not in (0, 1):
            raise ValueError("meta.no_disturbance must be 0 or 1")
        return self


class BilibiliPublishSettingsRead(BaseModel):
    default_meta: BilibiliPublishMeta


class BilibiliPublishSettingsUpdate(BaseModel):
    # Replace the default meta template (stored in DB).
    default_meta: Optional[dict[str, Any]] = None


class BilibiliAuthSettingsRead(BaseModel):
    cookie_set: bool
    sessdata_set: bool
    bili_jct_set: bool


class BilibiliAuthSettingsUpdate(BaseModel):
    # Preferred: paste the full Cookie string copied from browser DevTools.
    # If set to "" it clears stored cookies.
    cookie: Optional[str] = None

    # Advanced: set specific fields (also encrypted at rest).
    # If set to "" it clears the stored field.
    sessdata: Optional[str] = None
    bili_jct: Optional[str] = None


class BilibiliMeRead(BaseModel):
    mid: int
    uname: str
    userid: Optional[str] = None
    sign: Optional[str] = None
    rank: Optional[str] = None


class InputRef(BaseModel):
    type: Literal["s3"]
    key: str


class PublishRequest(BaseModel):
    task_id: uuid.UUID
    account_id: Optional[str] = None
    video: InputRef
    cover: Optional[InputRef] = None
    meta: BilibiliPublishMeta


class PublishResponse(BaseModel):
    state: str
    aid: Optional[str] = None
    bvid: Optional[str] = None
    response: Optional[dict[str, Any]] = None


class PublishJobRead(BaseModel):
    id: uuid.UUID
    task_id: uuid.UUID
    state: str
    aid: Optional[str]
    bvid: Optional[str]
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True
