# Rust 2.0 Python 路由逐项验收矩阵

本清单只由原版 Python 路由装饰器和 Rust V1 适配器登记表生成；标记 native_adapter_partial 不代表业务完整。
缺少 V1 对应的接口可能在 V2 中部分实现，但不能直接供旧版前端调用。
全部完成状态只允许结合 API 契约、权限、并发、重启、外部副作用与设备测试证据填写。

- 原版声明：217
- 原生 V1 部分适配：48
- 无原生 V1 入口：169
- 已完成全面验收：0
- 前端引用文本：193

| Python 子系统 | 原始声明 | Rust V1 部分适配 | 未映射 V1 |
|---|---:|---:|---:|
| bilibili_publisher | 10 | 0 | 10 |
| egress_gateway | 2 | 0 | 2 |
| orchestrator_api | 132 | 47 | 85 |
| social_publisher | 9 | 0 | 9 |
| subtitle_service | 55 | 0 | 55 |
| youtube_ingest | 9 | 1 | 8 |

## 完整逐项清单

| 子系统 | 方法 | 路径 | 原生 V1 状态 | Python 原方法 |
|---|---|---|---|---|
| bilibili_publisher | POST | /bilibili/archive/type/recommend | not_native_v1 | recommend_archive_type |
| bilibili_publisher | GET | /bilibili/archive/types | not_native_v1 | get_archive_types |
| bilibili_publisher | GET | /bilibili/auth/me | not_native_v1 | get_auth_me |
| bilibili_publisher | GET | /bilibili/auth/settings | not_native_v1 | get_auth_settings |
| bilibili_publisher | PUT | /bilibili/auth/settings | not_native_v1 | put_auth_settings |
| bilibili_publisher | POST | /bilibili/publish | not_native_v1 | publish |
| bilibili_publisher | GET | /bilibili/publish/jobs/{job_id} | not_native_v1 | get_job |
| bilibili_publisher | GET | /bilibili/publish/settings | not_native_v1 | get_publish_settings |
| bilibili_publisher | PUT | /bilibili/publish/settings | not_native_v1 | put_publish_settings |
| bilibili_publisher | GET | /health | not_native_v1 | health |
| egress_gateway | POST | /fetch | not_native_v1 | fetch |
| egress_gateway | GET | /health | not_native_v1 | health |
| orchestrator_api | POST | /auth/login | native_adapter_partial | auth_login |
| orchestrator_api | POST | /auth/logout | native_adapter_partial | auth_logout |
| orchestrator_api | POST | /auth/setup | native_adapter_partial | auth_setup |
| orchestrator_api | GET | /auth/status | native_adapter_partial | auth_status |
| orchestrator_api | POST | /auto/youtube | native_adapter_partial | auto_youtube |
| orchestrator_api | GET | /bilibili/discovery | not_native_v1 | bilibili_discovery |
| orchestrator_api | GET | /bilibili/{service_path:path} | not_native_v1 | proxy_bilibili_browser_operation |
| orchestrator_api | POST | /bilibili/{service_path:path} | not_native_v1 | proxy_bilibili_browser_operation |
| orchestrator_api | PUT | /bilibili/{service_path:path} | not_native_v1 | proxy_bilibili_browser_operation |
| orchestrator_api | GET | /desktop/authorize | not_native_v1 | authorize_grant |
| orchestrator_api | POST | /desktop/grants | not_native_v1 | create_grant |
| orchestrator_api | GET | /health | native_adapter_partial | health |
| orchestrator_api | POST | /maintenance/storage/cleanup-terminal | not_native_v1 | cleanup_terminal_storage_resources |
| orchestrator_api | GET | /maintenance/workdir | not_native_v1 | get_workdir_maintenance |
| orchestrator_api | POST | /maintenance/workdir/cleanup | not_native_v1 | cleanup_workdir_maintenance |
| orchestrator_api | GET | /operations/ai-usage | not_native_v1 | get_ai_usage |
| orchestrator_api | GET | /operations/ai-usage/pricing | not_native_v1 | get_ai_usage_pricing |
| orchestrator_api | PUT | /operations/ai-usage/pricing | not_native_v1 | put_ai_usage_pricing |
| orchestrator_api | GET | /operations/alerts | not_native_v1 | get_alerts |
| orchestrator_api | POST | /operations/alerts/report | not_native_v1 | post_alert_report |
| orchestrator_api | POST | /operations/alerts/scan | not_native_v1 | post_alert_scan |
| orchestrator_api | POST | /operations/alerts/{alert_id}/ack | not_native_v1 | acknowledge_alert |
| orchestrator_api | POST | /operations/alerts/{alert_id}/resolve | not_native_v1 | resolve_alert |
| orchestrator_api | GET | /render-management/connection | not_native_v1 | connection |
| orchestrator_api | PUT | /render-management/connection | not_native_v1 | update_connection |
| orchestrator_api | GET | /render-management/enrollments | not_native_v1 | enrollments |
| orchestrator_api | POST | /render-management/enrollments | not_native_v1 | create_enrollment |
| orchestrator_api | DELETE | /render-management/enrollments/{enrollment_id} | not_native_v1 | revoke_enrollment |
| orchestrator_api | GET | /render-management/executions | not_native_v1 | executions |
| orchestrator_api | GET | /render-management/executions/{execution_id} | not_native_v1 | execution |
| orchestrator_api | POST | /render-management/executions/{execution_id}/cancel | not_native_v1 | cancel |
| orchestrator_api | POST | /render-management/executions/{execution_id}/requeue | not_native_v1 | requeue |
| orchestrator_api | GET | /render-management/workers | not_native_v1 | workers |
| orchestrator_api | PATCH | /render-management/workers/{worker_id} | not_native_v1 | control_worker |
| orchestrator_api | PATCH | /render-management/workers/{worker_id}/devices/{device_id:path} | not_native_v1 | control_device |
| orchestrator_api | POST | /render-management/workers/{worker_id}/revoke-credential | not_native_v1 | revoke_worker |
| orchestrator_api | POST | /render-workers/v1/enroll | not_native_v1 | enroll |
| orchestrator_api | GET | /render-workers/v1/executions/{execution_id}/artifacts/{role} | not_native_v1 | download_artifact |
| orchestrator_api | GET | /render-workers/v1/executions/{execution_id}/cancel | not_native_v1 | execution_cancel |
| orchestrator_api | POST | /render-workers/v1/executions/{execution_id}/complete | not_native_v1 | complete |
| orchestrator_api | POST | /render-workers/v1/executions/{execution_id}/fail | not_native_v1 | fail |
| orchestrator_api | POST | /render-workers/v1/executions/{execution_id}/heartbeat | not_native_v1 | execution_heartbeat |
| orchestrator_api | POST | /render-workers/v1/executions/{execution_id}/logs | not_native_v1 | execution_logs |
| orchestrator_api | GET | /render-workers/v1/executions/{execution_id}/output | not_native_v1 | registered_output |
| orchestrator_api | POST | /render-workers/v1/executions/{execution_id}/output | not_native_v1 | upload_output |
| orchestrator_api | POST | /render-workers/v1/executions/{execution_id}/progress | not_native_v1 | execution_progress |
| orchestrator_api | POST | /render-workers/v1/local-enroll | not_native_v1 | local_enroll |
| orchestrator_api | GET | /render-workers/v1/protocol | not_native_v1 | protocol |
| orchestrator_api | POST | /render-workers/v1/{worker_id}/claim | not_native_v1 | claim |
| orchestrator_api | POST | /render-workers/v1/{worker_id}/heartbeat | not_native_v1 | heartbeat |
| orchestrator_api | GET | /settings/api | not_native_v1 | get_remote_api_settings_view |
| orchestrator_api | PUT | /settings/api | not_native_v1 | put_remote_api_settings_view |
| orchestrator_api | GET | /settings/dubbing | not_native_v1 | settings |
| orchestrator_api | GET | /settings/dubbing/jobs/summary | not_native_v1 | recent_native_jobs |
| orchestrator_api | GET | /settings/dubbing/native | not_native_v1 | engine_state |
| orchestrator_api | PUT | /settings/dubbing/native | not_native_v1 | engine_control |
| orchestrator_api | GET | /settings/publish/platforms | native_adapter_partial | read_publish_platform_settings |
| orchestrator_api | PUT | /settings/publish/platforms/{platform} | native_adapter_partial | put_publish_platform_setting |
| orchestrator_api | GET | /settings/publish/social/accounts | not_native_v1 | list_social_publish_accounts |
| orchestrator_api | DELETE | /settings/publish/social/accounts/{account_id} | not_native_v1 | delete_social_publish_account |
| orchestrator_api | POST | /settings/publish/social/accounts/{account_id}/check | not_native_v1 | check_social_publish_account |
| orchestrator_api | POST | /settings/publish/social/accounts/{platform} | not_native_v1 | import_social_publish_account |
| orchestrator_api | POST | /settings/publish/social/login-sessions/{platform} | not_native_v1 | start_social_login_session |
| orchestrator_api | DELETE | /settings/publish/social/login-sessions/{session_id} | not_native_v1 | cancel_social_login_session |
| orchestrator_api | GET | /settings/publish/social/login-sessions/{session_id} | not_native_v1 | get_social_login_session |
| orchestrator_api | GET | /settings/review | native_adapter_partial | get_publish_review_settings_view |
| orchestrator_api | PUT | /settings/review | native_adapter_partial | put_publish_review_settings_view |
| orchestrator_api | GET | /settings/storage | not_native_v1 | get_storage_settings |
| orchestrator_api | PUT | /settings/storage | not_native_v1 | put_storage_settings |
| orchestrator_api | GET | /settings/youtube | native_adapter_partial | get_youtube_settings_view |
| orchestrator_api | PUT | /settings/youtube | native_adapter_partial | put_youtube_settings_view |
| orchestrator_api | POST | /settings/youtube/home_scan/run | not_native_v1 | run_youtube_home_scan_now |
| orchestrator_api | POST | /settings/youtube/test | native_adapter_partial | test_youtube_proxy |
| orchestrator_api | DELETE | /subtitle/{service_path:path} | not_native_v1 | proxy_subtitle_browser_operation |
| orchestrator_api | GET | /subtitle/{service_path:path} | not_native_v1 | proxy_subtitle_browser_operation |
| orchestrator_api | POST | /subtitle/{service_path:path} | not_native_v1 | proxy_subtitle_browser_operation |
| orchestrator_api | PUT | /subtitle/{service_path:path} | not_native_v1 | proxy_subtitle_browser_operation |
| orchestrator_api | GET | /system/resources | native_adapter_partial | system_resources |
| orchestrator_api | GET | /tasks | native_adapter_partial | list_tasks |
| orchestrator_api | POST | /tasks | native_adapter_partial | create_task |
| orchestrator_api | POST | /tasks/actions/resume_failed_recent | native_adapter_partial | resume_recent_failed_tasks |
| orchestrator_api | POST | /tasks/actions/resume_stopped | native_adapter_partial | resume_all_stopped_tasks |
| orchestrator_api | POST | /tasks/actions/stop_all | native_adapter_partial | stop_all_tasks |
| orchestrator_api | GET | /tasks/{task_id} | native_adapter_partial | get_task |
| orchestrator_api | POST | /tasks/{task_id}/actions/auto_dubbing | not_native_v1 | auto_dubbing |
| orchestrator_api | POST | /tasks/{task_id}/actions/auto_publish | not_native_v1 | auto_publish_task |
| orchestrator_api | POST | /tasks/{task_id}/actions/auto_subtitle_handoff | not_native_v1 | auto_subtitle_handoff |
| orchestrator_api | POST | /tasks/{task_id}/actions/auto_youtube_start | native_adapter_partial | start_auto_youtube_for_existing_task |
| orchestrator_api | POST | /tasks/{task_id}/actions/dubbing | native_adapter_partial | create |
| orchestrator_api | POST | /tasks/{task_id}/actions/publish | native_adapter_partial | enqueue_publish_job |
| orchestrator_api | POST | /tasks/{task_id}/actions/publish_all | not_native_v1 | publish_all_platforms |
| orchestrator_api | POST | /tasks/{task_id}/actions/publish_review | native_adapter_partial | run_task_publish_review |
| orchestrator_api | POST | /tasks/{task_id}/actions/resume | native_adapter_partial | resume_stopped_task |
| orchestrator_api | POST | /tasks/{task_id}/actions/stop | native_adapter_partial | stop_task |
| orchestrator_api | POST | /tasks/{task_id}/actions/subtitle | native_adapter_partial | enqueue_subtitle_job |
| orchestrator_api | POST | /tasks/{task_id}/actions/subtitle_resume | native_adapter_partial | resume_subtitle_job |
| orchestrator_api | POST | /tasks/{task_id}/actions/subtitle_retranslate | not_native_v1 | retranslate_subtitle_indices |
| orchestrator_api | POST | /tasks/{task_id}/actions/youtube_download | not_native_v1 | download_youtube |
| orchestrator_api | POST | /tasks/{task_id}/actions/youtube_download_enqueue | not_native_v1 | enqueue_youtube_download |
| orchestrator_api | POST | /tasks/{task_id}/actions/youtube_meta | native_adapter_partial | fetch_youtube_meta |
| orchestrator_api | GET | /tasks/{task_id}/assets | native_adapter_partial | list_task_assets |
| orchestrator_api | DELETE | /tasks/{task_id}/assets/{asset_id} | not_native_v1 | delete_task_asset |
| orchestrator_api | GET | /tasks/{task_id}/assets/{asset_id}/download | native_adapter_partial | download_task_asset |
| orchestrator_api | POST | /tasks/{task_id}/assets/{asset_id}/playout | not_native_v1 | add_task_asset_to_playout |
| orchestrator_api | GET | /tasks/{task_id}/assets/{asset_id}/stream | native_adapter_partial | stream_task_asset |
| orchestrator_api | GET | /tasks/{task_id}/dubbing_jobs | native_adapter_partial | dubbing_history |
| orchestrator_api | POST | /tasks/{task_id}/dubbing_jobs/{job_id}/confirm | not_native_v1 | retired_legacy_workflow |
| orchestrator_api | POST | /tasks/{task_id}/dubbing_jobs/{job_id}/control | native_adapter_partial | control |
| orchestrator_api | POST | /tasks/{task_id}/dubbing_jobs/{job_id}/rerun | not_native_v1 | retired_legacy_workflow |
| orchestrator_api | POST | /tasks/{task_id}/dubbing_jobs/{job_id}/retry | native_adapter_partial | retry |
| orchestrator_api | GET | /tasks/{task_id}/playout-assets | native_adapter_partial | list_task_playout_assets |
| orchestrator_api | GET | /tasks/{task_id}/publish_batches | not_native_v1 | list_task_publish_batches |
| orchestrator_api | GET | /tasks/{task_id}/publish_jobs | native_adapter_partial | list_task_publish_jobs |
| orchestrator_api | POST | /tasks/{task_id}/publish_jobs/{publish_job_id}/append_p2 | not_native_v1 | append_bilibili_p2 |
| orchestrator_api | GET | /tasks/{task_id}/publish_meta | native_adapter_partial | get_task_publish_meta |
| orchestrator_api | PUT | /tasks/{task_id}/publish_meta | native_adapter_partial | put_task_publish_meta |
| orchestrator_api | GET | /tasks/{task_id}/publish_meta/draft | native_adapter_partial | get_task_publish_meta_draft |
| orchestrator_api | POST | /tasks/{task_id}/publish_meta/draft | native_adapter_partial | generate_task_publish_meta_draft |
| orchestrator_api | GET | /tasks/{task_id}/publish_review | native_adapter_partial | get_task_publish_review |
| orchestrator_api | GET | /tasks/{task_id}/subtitle_jobs | native_adapter_partial | list_task_subtitle_jobs |
| orchestrator_api | POST | /tasks/{task_id}/upload/cover | not_native_v1 | upload_task_cover |
| orchestrator_api | POST | /tasks/{task_id}/upload/video | native_adapter_partial | upload_task_video |
| orchestrator_api | GET | /tasks/{task_id}/youtube_download_progress | native_adapter_partial | get_youtube_download_progress |
| orchestrator_api | GET | /tasks/{task_id}/youtube_meta | native_adapter_partial | get_cached_youtube_meta |
| orchestrator_api | GET | /videos/converted | native_adapter_partial | list_converted_videos |
| orchestrator_api | WEBSOCKET | /ws/events | not_native_v1 | realtime_events |
| orchestrator_api | DELETE | /youtube/{service_path:path} | not_native_v1 | proxy_youtube_browser_operation |
| orchestrator_api | GET | /youtube/{service_path:path} | not_native_v1 | proxy_youtube_browser_operation |
| orchestrator_api | PATCH | /youtube/{service_path:path} | not_native_v1 | proxy_youtube_browser_operation |
| orchestrator_api | POST | /youtube/{service_path:path} | not_native_v1 | proxy_youtube_browser_operation |
| orchestrator_api | GET | None | not_native_v1 | remote_auto_youtube_legacy |
| orchestrator_api | POST | None | not_native_v1 | remote_auto_youtube |
| social_publisher | GET | /accounts | not_native_v1 | list_accounts |
| social_publisher | DELETE | /accounts/{account_id} | not_native_v1 | delete_account |
| social_publisher | POST | /accounts/{account_id}/check | not_native_v1 | recheck_account |
| social_publisher | POST | /accounts/{platform} | not_native_v1 | import_account |
| social_publisher | GET | /health | not_native_v1 | health |
| social_publisher | POST | /login-sessions/{platform} | not_native_v1 | start_login_session |
| social_publisher | DELETE | /login-sessions/{session_id} | not_native_v1 | cancel_login_session |
| social_publisher | GET | /login-sessions/{session_id} | not_native_v1 | get_login_session |
| social_publisher | POST | /sau/{platform}/publish | not_native_v1 | publish |
| subtitle_service | GET | /health | not_native_v1 | health |
| subtitle_service | GET | /subtitle/agent/skills | not_native_v1 | list_agent_skills_view |
| subtitle_service | GET | /subtitle/agents/runs | not_native_v1 | list_agent_runs_view |
| subtitle_service | GET | /subtitle/agents/runs/{run_id} | not_native_v1 | get_agent_run_view |
| subtitle_service | POST | /subtitle/asr/cloudflare/test | not_native_v1 | test_cloudflare_workers_ai |
| subtitle_service | POST | /subtitle/asr/external/test | not_native_v1 | test_external_whisper |
| subtitle_service | POST | /subtitle/asr/groq/test | not_native_v1 | test_groq_whisper |
| subtitle_service | GET | /subtitle/asr/settings | not_native_v1 | get_asr_settings_view |
| subtitle_service | PUT | /subtitle/asr/settings | not_native_v1 | put_asr_settings_view |
| subtitle_service | GET | /subtitle/auto/profile | not_native_v1 | get_subtitle_auto_profile |
| subtitle_service | PUT | /subtitle/auto/profile | not_native_v1 | put_subtitle_auto_profile |
| subtitle_service | GET | /subtitle/dictionaries/entries | not_native_v1 | list_dictionary_entries_view |
| subtitle_service | DELETE | /subtitle/dictionaries/entries/{entry_id} | not_native_v1 | delete_dictionary_entry_view |
| subtitle_service | PUT | /subtitle/dictionaries/entries/{entry_id} | not_native_v1 | update_dictionary_entry_view |
| subtitle_service | POST | /subtitle/dictionaries/import | not_native_v1 | import_dictionary_view |
| subtitle_service | GET | /subtitle/dictionaries/import-presets | not_native_v1 | list_dictionary_import_presets_view |
| subtitle_service | POST | /subtitle/dictionaries/lookup | not_native_v1 | lookup_dictionary_view |
| subtitle_service | POST | /subtitle/dictionaries/promote | not_native_v1 | promote_dictionary_entry_view |
| subtitle_service | GET | /subtitle/dictionaries/sources | not_native_v1 | list_dictionary_sources_view |
| subtitle_service | DELETE | /subtitle/dictionaries/sources/{source_id} | not_native_v1 | delete_dictionary_source_view |
| subtitle_service | PUT | /subtitle/dictionaries/sources/{source_id} | not_native_v1 | update_dictionary_source_view |
| subtitle_service | GET | /subtitle/embedding/models | not_native_v1 | list_embedding_models |
| subtitle_service | POST | /subtitle/embedding/models/download | not_native_v1 | download_embedding_model |
| subtitle_service | POST | /subtitle/embedding/models/list | not_native_v1 | list_embedding_models_for_dir |
| subtitle_service | DELETE | /subtitle/embedding/models/{name} | not_native_v1 | delete_embedding_model |
| subtitle_service | GET | /subtitle/embedding/runtime | not_native_v1 | get_embedding_runtime_status |
| subtitle_service | POST | /subtitle/embedding/test | not_native_v1 | test_embedding |
| subtitle_service | GET | /subtitle/hardware/intel | not_native_v1 | get_intel_hardware_view |
| subtitle_service | POST | /subtitle/jobs | not_native_v1 | create_job |
| subtitle_service | GET | /subtitle/jobs/{job_id} | not_native_v1 | get_job |
| subtitle_service | POST | /subtitle/knowledge/import | not_native_v1 | import_knowledge_items_view |
| subtitle_service | GET | /subtitle/knowledge/items | not_native_v1 | list_knowledge_items_view |
| subtitle_service | POST | /subtitle/knowledge/items | not_native_v1 | upsert_knowledge_item_view |
| subtitle_service | DELETE | /subtitle/knowledge/items/{item_id} | not_native_v1 | delete_knowledge_item_view |
| subtitle_service | POST | /subtitle/knowledge/rebuild-embeddings | not_native_v1 | rebuild_knowledge_embeddings_view |
| subtitle_service | GET | /subtitle/models | not_native_v1 | list_whisper_models |
| subtitle_service | POST | /subtitle/models/download | not_native_v1 | download_whisper_model |
| subtitle_service | POST | /subtitle/models/proxy/test | not_native_v1 | test_model_download_proxy |
| subtitle_service | POST | /subtitle/models/upload | not_native_v1 | upload_whisper_model |
| subtitle_service | DELETE | /subtitle/models/{name} | not_native_v1 | delete_whisper_model |
| subtitle_service | GET | /subtitle/settings | not_native_v1 | get_subtitle_settings_view |
| subtitle_service | GET | /subtitle/task_queue | not_native_v1 | get_task_queue |
| subtitle_service | GET | /subtitle/task_queue/broker | not_native_v1 | get_task_queue_broker |
| subtitle_service | PUT | /subtitle/task_queue/settings | not_native_v1 | update_task_queue_settings |
| subtitle_service | GET | /subtitle/tasks/{task_id}/quality | not_native_v1 | get_task_subtitle_quality |
| subtitle_service | GET | /subtitle/tasks/{task_id}/translation-context | not_native_v1 | get_task_translation_context |
| subtitle_service | PUT | /subtitle/tasks/{task_id}/translation-context | not_native_v1 | put_task_translation_context |
| subtitle_service | GET | /subtitle/translate/model-providers | not_native_v1 | get_model_providers |
| subtitle_service | POST | /subtitle/translate/model-providers | not_native_v1 | create_model_provider |
| subtitle_service | DELETE | /subtitle/translate/model-providers/{provider_id} | not_native_v1 | remove_model_provider |
| subtitle_service | PUT | /subtitle/translate/model-providers/{provider_id} | not_native_v1 | update_model_provider |
| subtitle_service | GET | /subtitle/translate/model-providers/{provider_id}/remote-models | not_native_v1 | remote_model_provider_models |
| subtitle_service | GET | /subtitle/translate/settings | not_native_v1 | get_translate_settings_view |
| subtitle_service | PUT | /subtitle/translate/settings | not_native_v1 | put_translate_settings_view |
| subtitle_service | POST | /subtitle/translate/test | not_native_v1 | translate_test |
| youtube_ingest | GET | /health | not_native_v1 | health |
| youtube_ingest | POST | /youtube/ingest | native_adapter_partial | ingest_single |
| youtube_ingest | POST | /youtube/scan | not_native_v1 | scan_source |
| youtube_ingest | GET | /youtube/sources | not_native_v1 | list_sources |
| youtube_ingest | POST | /youtube/sources | not_native_v1 | create_source |
| youtube_ingest | DELETE | /youtube/sources/{source_pk} | not_native_v1 | delete_source |
| youtube_ingest | PATCH | /youtube/sources/{source_pk} | not_native_v1 | patch_source |
| youtube_ingest | POST | /youtube/sources/{source_pk}/scan | not_native_v1 | scan_source_by_row_id |
| youtube_ingest | POST | /youtube/sources/{source_pk}/scan/start | not_native_v1 | start_source_scan |

此矩阵是待核验清单，不是实现完成率。
