use std::path::PathBuf;

use chrono::Utc;
use serde_json::{json, Value};
use tokio::io::AsyncWriteExt;
use tokio::sync::Mutex;

pub struct AuditLog {
    path: PathBuf,
    lock: Mutex<()>,
}

impl AuditLog {
    pub fn new(path: PathBuf) -> Self {
        Self {
            path,
            lock: Mutex::new(()),
        }
    }

    pub async fn write(&self, mut fields: serde_json::Map<String, Value>) {
        let id = uuid::Uuid::new_v4().to_string();
        let ts = Utc::now().to_rfc3339();
        fields.insert("id".to_string(), json!(id));
        fields.insert("timestamp".to_string(), json!(ts));
        let line = serde_json::to_string(&Value::Object(fields)).unwrap_or_default();
        let _g = self.lock.lock().await;
        if let Some(parent) = self.path.parent() {
            let _ = tokio::fs::create_dir_all(parent).await;
        }
        if let Ok(mut f) = tokio::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.path)
            .await
        {
            let _ = f.write_all(line.as_bytes()).await;
            let _ = f.write_all(b"\n").await;
        }
    }
}
