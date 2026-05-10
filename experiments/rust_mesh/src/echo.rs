//! Tiny echo node — registers with Core, opens an SSE stream, and replies
//! to every invocation by echoing the payload back. Demonstrates the wire
//! protocol end-to-end inside the same Rust binary as the Core.

use std::time::Duration;

use chrono::Utc;
use futures::StreamExt;
use serde_json::{json, Value};

use crate::canonical::{attach_signature, sign};

pub async fn run(core_url: String, node_id: String, secret: String) -> Result<(), String> {
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(60))
        .build()
        .map_err(|e| e.to_string())?;

    // Register
    let mut reg = json!({
        "node_id": node_id,
        "timestamp": Utc::now().to_rfc3339(),
    });
    attach_signature(&mut reg, &secret);

    let res: Value = client
        .post(format!("{}/v0/register", core_url))
        .json(&reg)
        .send()
        .await
        .map_err(|e| e.to_string())?
        .json()
        .await
        .map_err(|e| e.to_string())?;
    let session = res["session_id"].as_str().ok_or("missing session")?.to_string();
    eprintln!("[echo:{node_id}] registered session={session}");

    // Open SSE stream
    let url = format!("{}/v0/stream?session={}", core_url, session);
    let mut response = client
        .get(&url)
        .send()
        .await
        .map_err(|e| e.to_string())?
        .bytes_stream();

    let mut buffer = String::new();
    while let Some(chunk) = response.next().await {
        let bytes = chunk.map_err(|e| e.to_string())?;
        buffer.push_str(&String::from_utf8_lossy(&bytes));
        while let Some(pos) = buffer.find("\n\n") {
            let frame = buffer[..pos].to_string();
            buffer = buffer[pos + 2..].to_string();
            handle_frame(&client, &core_url, &node_id, &secret, &frame).await;
        }
    }
    Ok(())
}

async fn handle_frame(
    client: &reqwest::Client,
    core_url: &str,
    node_id: &str,
    secret: &str,
    frame: &str,
) {
    let mut event = String::new();
    let mut data = String::new();
    for line in frame.lines() {
        if let Some(s) = line.strip_prefix("event: ") {
            event = s.to_string();
        } else if let Some(s) = line.strip_prefix("data: ") {
            data.push_str(s);
        }
    }
    if event != "deliver" {
        return;
    }
    let env: Value = match serde_json::from_str(&data) {
        Ok(v) => v,
        Err(_) => return,
    };
    let cid = env.get("correlation_id").cloned().unwrap_or(Value::Null);
    let from = env.get("from").cloned().unwrap_or(Value::Null);
    let to = env.get("to").cloned().unwrap_or(Value::Null);
    let payload = env.get("payload").cloned().unwrap_or(json!({}));

    let mut response_env = json!({
        "id": uuid::Uuid::new_v4().to_string(),
        "correlation_id": cid,
        "from": node_id,
        "to": to,
        "kind": "response",
        "payload": {"echoed": payload, "from": from},
        "timestamp": Utc::now().to_rfc3339(),
    });
    let sig = sign(&response_env, secret);
    response_env["signature"] = Value::String(sig);

    let _ = client
        .post(format!("{}/v0/respond", core_url))
        .json(&response_env)
        .send()
        .await;
}
