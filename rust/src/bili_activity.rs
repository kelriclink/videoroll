//! Read-only Bilibili creator-center topic/mission ID verification.
//! Public activity catalogue IDs are a DIFFERENT namespace; they must never
//! be submitted as mission_id. A matched mission is not a reward entitlement.
//! Unknown/incomplete creator-center data fails closed before any UPOS upload.
use crate::{AppError, AppResult, bilibili::ArchiveMeta};
use reqwest::Client;
use serde_json::Value;
use std::time::Duration;

fn positive_id(value: &Value) -> Option<i64> {
    value.as_i64().filter(|v| *v > 0).or_else(|| {
        value
            .as_str()
            .and_then(|s| s.parse::<i64>().ok())
            .filter(|v| *v > 0)
    })
}
fn creator_topic(value: &Value) -> Option<(i64, Option<i64>, String, String)> {
    let topic_id = positive_id(value.get("topic_id").or_else(|| value.get("id"))?)?;
    let mission_id = value.get("mission_id").and_then(positive_id);
    let name = value
        .get("topic_name")
        .or_else(|| value.get("name"))
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .to_owned();
    let rules = value
        .get("act_protocol")
        .or_else(|| value.get("activity_description"))
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .to_owned();
    let desc = value
        .get("description")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim();
    Some((
        topic_id,
        mission_id,
        name,
        format!("{rules} {desc}").trim().to_owned(),
    ))
}
fn blocked_activity_rules(rules: &str) -> bool {
    [
        "仅限原创",
        "必须原创",
        "不允许转载",
        "禁止转载",
        "不可转载",
        "不得转载",
        "严禁转载",
        "仅限首发",
        "独家发布",
        "禁止搬运",
        "不得搬运",
        "需要报名",
        "需先报名",
        "报名审核",
        "报名成功",
        "需申请",
        "白名单",
        "仅限受邀",
        "限定UP主",
        "指定UP主",
        "签约UP主",
        "需要实名认证",
        "需先参与",
        "需要@",
        "必须@",
        "必须首发",
        "首发视频",
        "需审核",
    ]
    .iter()
    .any(|rule| rules.contains(rule))
}
pub fn verify_creator_result(meta: &ArchiveMeta, rows: &Value) -> AppResult<()> {
    let Some(requested_topic) = meta.topic_id else {
        if meta.mission_id.is_some() {
            return Err(AppError::Invalid(
                "mission_id cannot be used without topic_id".into(),
            ));
        }
        return Ok(());
    };
    let records = rows.as_array().ok_or_else(|| {
        AppError::Unavailable("Bilibili creator-center topic search returned invalid data".into())
    })?;
    if records.len() > 100 {
        return Err(AppError::Unavailable(
            "Bilibili creator-center returned too many records".into(),
        ));
    }
    let matches = records
        .iter()
        .filter_map(creator_topic)
        .filter(|(topic, _, _, _)| *topic == requested_topic)
        .collect::<Vec<_>>();
    if matches.len() != 1 || matches[0].2.is_empty() {
        return Err(AppError::Conflict(
            "Bilibili topic ID not uniquely confirmed by creator center".into(),
        ));
    }
    let (_, creator_mission, name, rules) = &matches[0];
    if let Some(requested_mission) = meta.mission_id {
        if *creator_mission != Some(requested_mission) {
            return Err(AppError::Conflict(
                "creator-center mission_id does not match topic_id".into(),
            ));
        }
        if rules.trim().is_empty() || rules.trim() == name || blocked_activity_rules(rules) {
            return Err(AppError::Conflict(
                "Bilibili activity participation rules cannot be verified safely".into(),
            ));
        }
        if meta.copyright == 2
            && ![
                "允许转载",
                "转载可参加",
                "转载可参与",
                "转载作品可参与",
                "转载作品可以参与",
            ]
            .iter()
            .any(|phrase| rules.contains(phrase))
        {
            return Err(AppError::Conflict(
                "reposted video is not explicitly allowed by activity rules".into(),
            ));
        }
        // Creator-center indicates ID pairing, NOT account enrollment/rewards.
    }
    Ok(())
}
fn keyword(meta: &ArchiveMeta) -> AppResult<String> {
    let named = meta
        .extra
        .get("activity_name")
        .and_then(Value::as_str)
        .filter(|v| !v.trim().is_empty());
    let term = named
        .or_else(|| {
            meta.tags
                .iter()
                .map(String::as_str)
                .find(|s| !matches!(*s, "视频分享" | "videoroll"))
        })
        .unwrap_or(meta.title.as_str())
        .trim();
    if term.is_empty() || term.chars().count() > 120 {
        return Err(AppError::Invalid(
            "Bilibili activity search requires a bounded name or tag".into(),
        ));
    }
    Ok(term.chars().take(80).collect())
}
pub async fn verify_remote(client: &Client, meta: &ArchiveMeta) -> AppResult<()> {
    if meta.topic_id.is_none() && meta.mission_id.is_none() {
        return Ok(());
    }
    let search = keyword(meta)?;
    // Only fixed Bilibili HTTPS endpoints, never a submitted URL.
    let response = client
        .get("https://member.bilibili.com/x/vupre/web/topic/search")
        .query(&[
            ("keywords", search.as_str()),
            ("page_size", "30"),
            ("offset", "0"),
        ])
        .timeout(Duration::from_secs(10))
        .send()
        .await
        .map_err(|_| AppError::Unavailable("Bilibili creator-center topic lookup failed".into()))?;
    if !response.status().is_success() {
        return Err(AppError::Unavailable(
            "Bilibili creator-center topic lookup returned non-success".into(),
        ));
    }
    if response
        .content_length()
        .is_some_and(|len| len > 256 * 1024)
    {
        return Err(AppError::Unavailable(
            "Bilibili creator-center topic response oversized".into(),
        ));
    }
    let bytes = response.bytes().await?;
    if bytes.len() > 256 * 1024 {
        return Err(AppError::Unavailable(
            "Bilibili creator-center topic response oversized".into(),
        ));
    }
    let value: Value = serde_json::from_slice(&bytes).map_err(|_| {
        AppError::Unavailable("Bilibili creator-center topic response invalid".into())
    })?;
    if value.get("code").and_then(Value::as_i64) != Some(0) {
        return Err(AppError::Unavailable(
            "Bilibili creator-center rejected topic lookup".into(),
        ));
    }
    let rows = value
        .pointer("/data/result/topics")
        .ok_or_else(|| AppError::Unavailable("creator-center topic list missing".into()))?;
    verify_creator_result(meta, rows)
}
#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    fn metadata() -> ArchiveMeta {
        serde_json::from_value(json!({
            "title":"视频投稿","typeid":21,"tags":["魔兽世界"],
            "copyright":2,"source":"https://youtu.be/0123456789A",
            "topic_id":301,"mission_id":901,"activity_name":"魔兽活动"
        }))
        .unwrap()
    }
    #[test]
    fn real_topic_and_mission_must_be_an_exact_creator_center_pair() {
        let meta = metadata();
        let found = json!([{"topic_id":301,"mission_id":901,"topic_name":"魔兽活动",
                          "act_protocol":"所有符合规则的作品均允许转载，可参与活动投稿。"}]);
        verify_creator_result(&meta, &found).unwrap();
        assert!(
            verify_creator_result(
                &meta,
                &json!([{"topic_id":301,"mission_id":902,
            "topic_name":"魔兽活动","act_protocol":"允许转载"}])
            )
            .is_err()
        );
        assert!(
            verify_creator_result(
                &meta,
                &json!([{"topic_id":901,"mission_id":901,
            "topic_name":"魔兽活动","act_protocol":"允许转载"}])
            )
            .is_err()
        );
        assert!(
            verify_creator_result(
                &meta,
                &json!([{"topic_id":301,"mission_id":901,
            "topic_name":"魔兽活动","act_protocol":"仅限原创作品"}])
            )
            .is_err()
        );
        assert!(
            verify_creator_result(
                &meta,
                &json!([{"topic_id":301,"mission_id":901,
            "topic_name":"魔兽活动","act_protocol":"需要报名审核，允许转载"}])
            )
            .is_err()
        );
    }
    #[test]
    fn regular_topics_do_not_claim_activity_enrollment() {
        let mut meta = metadata();
        meta.mission_id = None;
        verify_creator_result(&meta, &json!([{"topic_id":301,"topic_name":"普通话题"}])).unwrap();
        assert!(verify_creator_result(&meta, &json!([])).is_err());
        assert!(
            verify_creator_result(
                &meta,
                &json!([{"topic_id":301,"topic_name":"A"},
            {"topic_id":301,"topic_name":"B"}])
            )
            .is_err()
        );
        meta.topic_id = None;
        assert!(verify_creator_result(&meta, &json!(null)).is_ok());
    }
}
