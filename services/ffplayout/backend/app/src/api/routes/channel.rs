use axum::{
    Json,
    extract::{Path, State},
};
use protect_axum::authorities::AuthDetails;

use crate::{
    api::{
        routes::{AuthUser, ensure_any_authority},
        state::AppState,
    },
    db::{
        handles,
        models::{Channel, Role},
    },
    utils::{
        channels::{create_channel, delete_channel},
        config::get_config,
        errors::ServiceError,
    },
};

const INTERNAL_PREVIEW_PREFIXES: [&str; 4] = [
    "http://127.0.0.1:8787",
    "http://localhost:8787",
    "http://0.0.0.0:8787",
    "http://[::1]:8787",
];

/// The seeded channel points at ffplayout's standalone development listener.
/// When the UI is served through VideoRoll's playout virtual host that address
/// is not reachable from the browser because port 8787 is internal-only.
/// Return a same-origin path for that built-in value while preserving any URL
/// explicitly configured by an operator (including CDN URLs).
fn normalize_preview_url(channel: &mut Channel) {
    for prefix in INTERNAL_PREVIEW_PREFIXES {
        if let Some(path) = channel.preview_url.strip_prefix(prefix)
            && path.starts_with("/public/")
        {
            channel.preview_url = path.to_string();
            return;
        }
    }
}

/// #### Settings
///
/// **Get Settings from Channel**
///
/// ```BASH
/// curl -X GET http://127.0.0.1:8787/api/channel/1 -H "Authorization: Bearer <TOKEN>"
/// ```
///
/// **Response:**
///
/// ```JSON
/// {
///     "id": 1,
///     "name": "Channel 1",
///     "preview_url": "http://localhost/live/preview.m3u8",
///     "extra_extensions": "jpg,jpeg,png",
///     "utc_offset": "+120"
/// }
/// ```
pub async fn get_channel(
    State(state): State<AppState>,
    Path(id): Path<i32>,
    user: AuthUser,
    details: AuthDetails<Role>,
) -> Result<Json<Channel>, ServiceError> {
    ensure_any_authority(
        &details,
        &[&Role::GlobalAdmin, &Role::ChannelAdmin, &Role::User],
    )?;
    user.ensure_channel_or_admin(id)?;

    if let Ok(mut channel) = handles::select_channel(&state.pool, &id).await {
        normalize_preview_url(&mut channel);
        return Ok(Json(channel));
    }

    Err(ServiceError::InternalServerError)
}

/// **Get settings from all Channels**
///
/// ```BASH
/// curl -X GET http://127.0.0.1:8787/api/channels -H "Authorization: Bearer <TOKEN>"
/// ```
pub async fn get_all_channels(
    State(state): State<AppState>,
    user: AuthUser,
    details: AuthDetails<Role>,
) -> Result<Json<Vec<Channel>>, ServiceError> {
    ensure_any_authority(
        &details,
        &[&Role::GlobalAdmin, &Role::ChannelAdmin, &Role::User],
    )?;

    if let Ok(mut channels) = handles::select_related_channels(&state.pool, Some(user.id)).await {
        for channel in &mut channels {
            normalize_preview_url(channel);
        }
        return Ok(Json(channels));
    }

    Err(ServiceError::InternalServerError)
}

/// **Update Channel**
///
/// ```BASH
/// curl -X PATCH http://127.0.0.1:8787/api/channel/1 -H "Content-Type: application/json" \
/// -d '{ "id": 1, "name": "Channel 1", "preview_url": "http://localhost/live/stream.m3u8", "extra_extensions": "jpg,jpeg,png"}' \
/// -H "Authorization: Bearer <TOKEN>"
/// ```
pub async fn patch_channel(
    State(state): State<AppState>,
    Path(id): Path<i32>,
    user: AuthUser,
    details: AuthDetails<Role>,
    Json(mut data): Json<Channel>,
) -> Result<&'static str, ServiceError> {
    ensure_any_authority(&details, &[&Role::GlobalAdmin, &Role::ChannelAdmin])?;
    user.ensure_channel_or_admin(id)?;

    let manager = {
        let guard = state.controller.read().await;
        guard.get(id)
    }
    .ok_or_else(|| ServiceError::BadRequest(format!("Channel {id} not found!")))?;

    if !user.is_global_admin() {
        let channel = handles::select_channel(&state.pool, &id).await?;

        data.public = channel.public;
        data.playlists = channel.playlists;
        data.storage = channel.storage;
    }

    handles::update_channel(&state.pool, id, data.clone()).await?;
    let new_config = get_config(&state.pool, id).await?;

    manager.update_config(new_config).await;
    manager.update_channel(&data).await;

    Ok("Update Success")
}

/// **Create new Channel**
///
/// ```BASH
/// curl -X POST http://127.0.0.1:8787/api/channel -H "Content-Type: application/json" \
/// -d '{ "name": "Channel 2", "preview_url": "http://localhost/live/channel2.m3u8", "extra_extensions": "jpg,jpeg,png" }' \
/// -H "Authorization: Bearer <TOKEN>"
/// ```
pub async fn add_channel(
    State(state): State<AppState>,
    _user: AuthUser,
    details: AuthDetails<Role>,
    Json(data): Json<Channel>,
) -> Result<Json<Channel>, ServiceError> {
    ensure_any_authority(&details, &[&Role::GlobalAdmin])?;

    match create_channel(
        &state.pool,
        state.controller,
        state.mail_queues,
        state.shutdown,
        state.system,
        data,
    )
    .await
    {
        Ok(mut channel) => {
            normalize_preview_url(&mut channel);
            Ok(Json(channel))
        }
        Err(e) => Err(e),
    }
}

/// **Delete Channel**
///
/// ```BASH
/// curl -X DELETE http://127.0.0.1:8787/api/channel/2 -H "Authorization: Bearer <TOKEN>"
/// ```
pub async fn remove_channel(
    State(state): State<AppState>,
    Path(id): Path<i32>,
    _user: AuthUser,
    details: AuthDetails<Role>,
) -> Result<Json<&'static str>, ServiceError> {
    ensure_any_authority(&details, &[&Role::GlobalAdmin])?;

    delete_channel(&state.pool, id, state.controller, state.mail_queues).await?;

    Ok(Json("Delete Channel Success"))
}

#[cfg(test)]
mod tests {
    use super::normalize_preview_url;
    use crate::db::models::Channel;

    fn channel(preview_url: &str) -> Channel {
        Channel {
            preview_url: preview_url.to_string(),
            ..Channel::default()
        }
    }

    #[test]
    fn internal_preview_urls_are_same_origin_paths() {
        let mut value = channel("http://127.0.0.1:8787/public/1/live/stream.m3u8");

        normalize_preview_url(&mut value);

        assert_eq!(value.preview_url, "/public/1/live/stream.m3u8");
    }

    #[test]
    fn operator_configured_preview_urls_are_preserved() {
        let mut value = channel("https://cdn.example.test/channel/live.m3u8");

        normalize_preview_url(&mut value);

        assert_eq!(value.preview_url, "https://cdn.example.test/channel/live.m3u8");
    }
}
