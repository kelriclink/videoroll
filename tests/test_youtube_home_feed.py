from __future__ import annotations

import json
import sys
import types
import unittest

try:
    import httpx as _httpx  # type: ignore
except ModuleNotFoundError:
    fake_httpx = types.ModuleType("httpx")

    class Client:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

    fake_httpx.Client = Client
    sys.modules["httpx"] = fake_httpx

from videoroll.apps.orchestrator_api.youtube_home_feed import (
    extract_home_feed_continuations,
    extract_home_feed_videos,
    parse_sw_session_data,
)


class YouTubeHomeFeedTests(unittest.TestCase):
    def test_parse_sw_session_data_extracts_context(self) -> None:
        device_info: list[object] = [None] * 108
        device_info[11] = "Google"
        device_info[12] = "Pixel"
        device_info[13] = "visitor-data-123"
        device_info[16] = "2.20260301.00.00"
        device_info[17] = "Windows"
        device_info[18] = "10.0"
        device_info[61] = ["ignored", "app-install-data"]
        device_info[79] = "Asia/Shanghai"
        device_info[86] = "Chrome"
        device_info[87] = "134.0.0.0"
        device_info[103] = "device-exp"
        device_info[107] = "rollout-token"

        text = ")]}'" + json.dumps([[0, 0, [[device_info], "api-key-test"]]])
        session = parse_sw_session_data(text, user_agent="UA/1.0", timezone_name="UTC", visitor_cookie="fallback-cookie")

        self.assertEqual(session["api_key"], "api-key-test")
        client = session["context"]["client"]
        self.assertEqual(client["visitorData"], "visitor-data-123")
        self.assertEqual(client["clientVersion"], "2.20260301.00.00")
        self.assertEqual(client["browserName"], "Chrome")
        self.assertEqual(client["browserVersion"], "134.0.0.0")
        self.assertEqual(client["deviceMake"], "Google")
        self.assertEqual(client["deviceModel"], "Pixel")
        self.assertEqual(client["timeZone"], "Asia/Shanghai")
        self.assertEqual(client["rolloutToken"], "rollout-token")
        self.assertEqual(client["deviceExperimentId"], "device-exp")
        self.assertEqual(client["configInfo"]["appInstallData"], "app-install-data")

    def test_extract_home_feed_videos_and_continuations(self) -> None:
        payload = {
            "contents": {
                "twoColumnBrowseResultsRenderer": {
                    "tabs": [
                        {
                            "tabRenderer": {
                                "content": {
                                    "richGridRenderer": {
                                        "contents": [
                                            {
                                                "richItemRenderer": {
                                                    "content": {
                                                        "videoRenderer": {
                                                            "videoId": "abc12345",
                                                            "title": {"runs": [{"text": "First video"}]},
                                                        }
                                                    }
                                                }
                                            },
                                            {
                                                "richItemRenderer": {
                                                    "content": {
                                                        "videoRenderer": {
                                                            "videoId": "def67890",
                                                            "title": {"simpleText": "Second video"},
                                                            "thumbnail": {"thumbnails": []},
                                                        }
                                                    }
                                                }
                                            },
                                            {
                                                "navigationEndpoint": {
                                                    "watchEndpoint": {
                                                        "videoId": "abc12345",
                                                    }
                                                }
                                            },
                                            {
                                                "continuationItemRenderer": {
                                                    "continuationEndpoint": {
                                                        "continuationCommand": {
                                                            "token": "CONT-1",
                                                        }
                                                    }
                                                }
                                            },
                                        ]
                                    }
                                }
                            }
                        }
                    ]
                }
            },
            "onResponseReceivedActions": [
                {
                    "appendContinuationItemsAction": {
                        "continuationItems": [
                            {
                                "continuationItemRenderer": {
                                    "continuationEndpoint": {
                                        "continuationCommand": {
                                            "token": "CONT-2",
                                        }
                                    }
                                }
                            }
                        ]
                    }
                }
            ],
        }

        videos = extract_home_feed_videos(payload)
        continuations = extract_home_feed_continuations(payload)

        self.assertEqual([video.video_id for video in videos], ["abc12345", "def67890"])
        self.assertEqual([video.title for video in videos], ["First video", "Second video"])
        self.assertEqual(videos[0].url, "https://www.youtube.com/watch?v=abc12345")
        self.assertEqual(continuations, ["CONT-1", "CONT-2"])

    def test_extract_home_feed_videos_long_only_filters_shorts_and_short_duration(self) -> None:
        payload = {
            "contents": {
                "richGridRenderer": {
                    "contents": [
                        {
                            "richItemRenderer": {
                                "content": {
                                    "reelItemRenderer": {
                                        "videoId": "shorts001",
                                        "headline": {"simpleText": "Shorts item"},
                                    }
                                }
                            }
                        },
                        {
                            "richItemRenderer": {
                                "content": {
                                    "videoRenderer": {
                                        "videoId": "short0012",
                                        "title": {"simpleText": "Too short"},
                                        "lengthText": {"simpleText": "1:20"},
                                    }
                                }
                            }
                        },
                        {
                            "richItemRenderer": {
                                "content": {
                                    "videoRenderer": {
                                        "videoId": "long0001",
                                        "title": {"simpleText": "Long enough"},
                                        "lengthText": {"simpleText": "12:34"},
                                    }
                                }
                            }
                        },
                    ]
                }
            }
        }

        videos = extract_home_feed_videos(payload, long_videos_only=True)

        self.assertEqual([video.video_id for video in videos], ["long0001"])
        self.assertEqual(videos[0].duration_seconds, 12 * 60 + 34)
        self.assertFalse(videos[0].is_short)

    def test_extract_home_feed_videos_reads_duration_from_accessibility_label(self) -> None:
        payload = {
            "contents": {
                "richGridRenderer": {
                    "contents": [
                        {
                            "richItemRenderer": {
                                "content": {
                                    "videoRenderer": {
                                        "videoId": "label0001",
                                        "title": {"simpleText": "Accessibility duration"},
                                        "accessibility": {
                                            "accessibilityData": {
                                                "label": "Accessibility duration by Creator 12 minutes, 34 seconds, play video"
                                            }
                                        },
                                    }
                                }
                            }
                        }
                    ]
                }
            }
        }

        videos = extract_home_feed_videos(payload, long_videos_only=True)

        self.assertEqual([video.video_id for video in videos], ["label0001"])
        self.assertEqual(videos[0].duration_seconds, 12 * 60 + 34)
        self.assertEqual(videos[0].duration_source, "label")

    def test_extract_home_feed_videos_long_only_keeps_unknown_duration_if_not_short(self) -> None:
        payload = {
            "contents": {
                "richGridRenderer": {
                    "contents": [
                        {
                            "richItemRenderer": {
                                "content": {
                                    "videoRenderer": {
                                        "videoId": "unknown01",
                                        "title": {"simpleText": "Unknown duration"},
                                    }
                                }
                            }
                        }
                    ]
                }
            }
        }

        videos = extract_home_feed_videos(payload, long_videos_only=True)

        self.assertEqual([video.video_id for video in videos], ["unknown01"])
        self.assertIsNone(videos[0].duration_seconds)
        self.assertFalse(videos[0].is_short)

    def test_extract_home_feed_videos_supports_lockup_view_model_cards(self) -> None:
        payload = {
            "contents": {
                "richGridRenderer": {
                    "contents": [
                        {
                            "richItemRenderer": {
                                "content": {
                                    "lockupViewModel": {
                                        "contentId": "lockup001",
                                        "contentType": "LOCKUP_CONTENT_TYPE_VIDEO",
                                        "metadata": {
                                            "lockupMetadataViewModel": {
                                                "title": {
                                                    "content": "Lockup long video",
                                                }
                                            }
                                        },
                                        "contentImage": {
                                            "collectionThumbnailViewModel": {
                                                "primaryThumbnail": {
                                                    "thumbnailViewModel": {
                                                        "overlays": [
                                                            {
                                                                "thumbnailOverlayBadgeViewModel": {
                                                                    "thumbnailBadges": [
                                                                        {
                                                                            "thumbnailBadgeViewModel": {
                                                                                "text": "17:33",
                                                                            }
                                                                        }
                                                                    ]
                                                                }
                                                            }
                                                        ]
                                                    }
                                                }
                                            }
                                        },
                                        "rendererContext": {
                                            "commandContext": {
                                                "onTap": {
                                                    "innertubeCommand": {
                                                        "watchEndpoint": {
                                                            "videoId": "lockup001",
                                                        },
                                                        "commandMetadata": {
                                                            "webCommandMetadata": {
                                                                "url": "/watch?v=lockup001",
                                                            }
                                                        },
                                                    }
                                                }
                                            }
                                        },
                                    }
                                }
                            }
                        },
                        {
                            "richItemRenderer": {
                                "content": {
                                    "shortsLockupViewModel": {
                                        "entityId": "shorts-shelf-item",
                                        "overlayMetadata": {
                                            "primaryText": {
                                                "content": "Lockup shorts item",
                                            }
                                        },
                                        "onTap": {
                                            "innertubeCommand": {
                                                "reelWatchEndpoint": {
                                                    "videoId": "shorts002",
                                                },
                                                "commandMetadata": {
                                                    "webCommandMetadata": {
                                                        "url": "/shorts/shorts002",
                                                    }
                                                },
                                            }
                                        },
                                    }
                                }
                            }
                        },
                    ]
                }
            }
        }

        videos = extract_home_feed_videos(payload, long_videos_only=True)

        self.assertEqual([video.video_id for video in videos], ["lockup001"])
        self.assertEqual(videos[0].title, "Lockup long video")
        self.assertEqual(videos[0].duration_seconds, 17 * 60 + 33)
        self.assertEqual(videos[0].duration_source, "thumbnailOverlayBadgeViewModel.text")
        self.assertFalse(videos[0].is_short)

    def test_extract_home_feed_videos_does_not_treat_age_text_as_duration(self) -> None:
        payload = {
            "contents": {
                "richGridRenderer": {
                    "contents": [
                        {
                            "richItemRenderer": {
                                "content": {
                                    "videoRenderer": {
                                        "videoId": "recent001",
                                        "title": {"simpleText": "Recently uploaded"},
                                        "accessibility": {
                                            "accessibilityData": {
                                                "label": "Recently uploaded by Creator 12 minutes, 34 seconds ago 123 views"
                                            }
                                        },
                                    }
                                }
                            }
                        }
                    ]
                }
            }
        }

        videos = extract_home_feed_videos(payload, long_videos_only=True)

        self.assertEqual([video.video_id for video in videos], ["recent001"])
        self.assertIsNone(videos[0].duration_seconds)


if __name__ == "__main__":
    unittest.main()
