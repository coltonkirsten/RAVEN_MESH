use std::path::PathBuf;
use std::time::Duration;

use rust_mesh::canonical::{attach_signature, sign, verify};
use rust_mesh::manifest;
use rust_mesh::router::build_router;
use rust_mesh::state::Core;
use rust_mesh::supervisor::{ChildSpec, RestartPolicy, Supervisor};
use serde_json::{json, Value};
use tokio::net::TcpListener;

fn manifest_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("manifests")
}

#[test]
fn signature_round_trip_matches_python_canonical() {
    // Same canonical form Python uses: keys sorted, no spaces, signature excluded.
    let mut env = json!({
        "id": "abc",
        "from": "alice",
        "to": "bob.ping",
        "kind": "invocation",
        "payload": {"x": 1, "a": [1, 2]},
    });
    let secret = "shared-secret";
    attach_signature(&mut env, secret);
    assert!(verify(&env, secret));

    // Tampering invalidates.
    env["payload"]["x"] = json!(2);
    assert!(!verify(&env, secret));

    // Wrong secret rejects.
    let mut env2 = json!({"a": "b", "payload": {"k": "v"}});
    let s = sign(&env2, secret);
    env2["signature"] = Value::String(s);
    assert!(!verify(&env2, "different-secret"));
}

#[tokio::test]
async fn manifest_load_parses_nodes_and_edges() {
    let path = manifest_dir().join("test.yaml");
    let m = manifest::load_from_path(&path).expect("load manifest");
    assert_eq!(m.nodes.len(), 2);
    assert!(m.nodes.contains_key("alice"));
    assert!(m.nodes.contains_key("bob"));
    assert_eq!(m.edges.len(), 1);
    assert!(m.edges.contains(&("alice".into(), "bob.ping".into())));
    let bob = &m.nodes["bob"];
    assert!(bob.surfaces.contains_key("ping"));
}

async fn spawn_test_core() -> (Core, String) {
    let manifest_path = manifest_dir().join("test.yaml");
    let tmp = tempfile::NamedTempFile::new().unwrap();
    let core = Core::new(
        manifest_path,
        tmp.path().to_path_buf(),
        "test-token".into(),
    );
    core.load_manifest().await.expect("load manifest");
    let app = build_router(core.clone());
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move {
        axum::serve(listener, app).await.ok();
    });
    // Give the server a tick to be ready.
    tokio::time::sleep(Duration::from_millis(50)).await;
    (core, format!("http://{}", addr))
}

#[tokio::test]
async fn envelope_routing_request_response_via_http() {
    let (_core, base) = spawn_test_core().await;
    let client = reqwest::Client::new();

    // Register bob (the responder)
    let mut bob_reg = json!({"node_id": "bob"});
    attach_signature(&mut bob_reg, "bob-secret");
    let r: Value = client
        .post(format!("{base}/v0/register"))
        .json(&bob_reg)
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    let session = r["session_id"].as_str().unwrap().to_string();

    // Open SSE stream for bob in a background task that responds to the first deliver
    let stream_url = format!("{base}/v0/stream?session={}", session);
    let base_for_bob = base.clone();
    tokio::spawn(async move {
        use futures::StreamExt;
        let resp = reqwest::Client::new()
            .get(&stream_url)
            .send()
            .await
            .unwrap();
        let mut s = resp.bytes_stream();
        let mut buf = String::new();
        while let Some(chunk) = s.next().await {
            buf.push_str(&String::from_utf8_lossy(&chunk.unwrap()));
            if let Some(pos) = buf.find("\n\n") {
                let frame = buf[..pos].to_string();
                buf = buf[pos + 2..].to_string();
                let mut event = String::new();
                let mut data = String::new();
                for line in frame.lines() {
                    if let Some(s) = line.strip_prefix("event: ") {
                        event = s.into();
                    } else if let Some(s) = line.strip_prefix("data: ") {
                        data.push_str(s);
                    }
                }
                if event != "deliver" {
                    continue;
                }
                let env: Value = serde_json::from_str(&data).unwrap();
                let cid = env.get("correlation_id").cloned().unwrap();
                let mut resp_env = json!({
                    "id": "r1",
                    "correlation_id": cid,
                    "from": "bob",
                    "to": env.get("from"),
                    "kind": "response",
                    "payload": {"pong": true, "got": env.get("payload")},
                });
                attach_signature(&mut resp_env, "bob-secret");
                let _ = reqwest::Client::new()
                    .post(format!("{base_for_bob}/v0/respond"))
                    .json(&resp_env)
                    .send()
                    .await;
                break;
            }
        }
    });

    // Wait briefly for SSE to attach.
    tokio::time::sleep(Duration::from_millis(150)).await;

    // Alice invokes bob.ping.
    let mut env = json!({
        "id": "m1",
        "correlation_id": "m1",
        "from": "alice",
        "to": "bob.ping",
        "kind": "invocation",
        "payload": {"hello": "world"},
    });
    attach_signature(&mut env, "alice-secret");
    let resp = client
        .post(format!("{base}/v0/invoke"))
        .json(&env)
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), 200);
    let body: Value = resp.json().await.unwrap();
    assert_eq!(body["payload"]["pong"], json!(true));
    assert_eq!(body["payload"]["got"]["hello"], json!("world"));
}

#[tokio::test]
async fn envelope_routing_rejects_no_relationship() {
    let (_core, base) = spawn_test_core().await;
    let client = reqwest::Client::new();
    // bob -> alice.inbox is NOT in the manifest's edges
    let mut env = json!({
        "id": "x",
        "correlation_id": "x",
        "from": "bob",
        "to": "alice.inbox",
        "kind": "invocation",
        "payload": {},
    });
    attach_signature(&mut env, "bob-secret");
    let resp = client
        .post(format!("{base}/v0/invoke"))
        .json(&env)
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), 403);
    let body: Value = resp.json().await.unwrap();
    assert_eq!(body["error"], json!("denied_no_relationship"));
}

#[tokio::test]
async fn supervisor_restarts_crashed_child() {
    let tmp = tempfile::tempdir().unwrap();
    let counter = tmp.path().join("counter");
    let log = tmp.path().join("child.log");

    // Shell script that bumps a counter file and exits 1 (crash).
    let script = format!(
        "echo x >> {counter} ; exit 1",
        counter = counter.display()
    );
    let spec = ChildSpec {
        node_id: "crashy".into(),
        cmd: vec!["/bin/sh".into(), "-c".into(), script],
        env: Default::default(),
        cwd: tmp.path().to_path_buf(),
        log_path: log,
        restart: RestartPolicy::Permanent,
        max_restarts: 5,
        restart_window: Duration::from_secs(60),
    };
    let sup = Supervisor::new();
    sup.spawn(spec).await.unwrap();

    // Wait for several restarts to land. Backoff is 100ms * count, so 4 restarts take roughly 1s.
    tokio::time::sleep(Duration::from_millis(1500)).await;
    sup.shutdown_all().await;

    let lines = std::fs::read_to_string(&counter).unwrap_or_default();
    let count = lines.lines().count();
    assert!(
        count >= 2,
        "expected supervisor to restart child at least once, got {count} bumps"
    );
}

#[tokio::test]
async fn admin_sse_broadcasts_envelope_tail() {
    use futures::StreamExt;
    let (_core, base) = spawn_test_core().await;
    let client = reqwest::Client::new();

    // Open admin SSE first
    let admin_url = format!("{base}/v0/admin/stream?admin_token=test-token");
    let resp = client.get(&admin_url).send().await.unwrap();
    assert_eq!(resp.status(), 200);
    let mut stream = resp.bytes_stream();

    // Read the "hello" frame first.
    let mut buf = String::new();
    let deadline = tokio::time::Instant::now() + Duration::from_secs(2);
    let mut got_hello = false;
    while tokio::time::Instant::now() < deadline && !got_hello {
        if let Some(chunk) = tokio::time::timeout(Duration::from_millis(500), stream.next())
            .await
            .ok()
            .flatten()
        {
            buf.push_str(&String::from_utf8_lossy(&chunk.unwrap()));
            if buf.contains("event: hello") {
                got_hello = true;
            }
        }
    }
    assert!(got_hello, "did not receive admin hello frame");

    // Trigger a denied envelope (no relationship) to push something into the tail.
    let mut env = json!({
        "id": "tap1",
        "correlation_id": "tap1",
        "from": "bob",
        "to": "alice.inbox",
        "kind": "invocation",
        "payload": {},
    });
    attach_signature(&mut env, "bob-secret");
    let _ = client
        .post(format!("{base}/v0/invoke"))
        .json(&env)
        .send()
        .await
        .unwrap();

    // Now read until we see an envelope event referencing tap1.
    let deadline = tokio::time::Instant::now() + Duration::from_secs(3);
    let mut got_envelope = false;
    while tokio::time::Instant::now() < deadline && !got_envelope {
        if let Some(chunk) = tokio::time::timeout(Duration::from_millis(500), stream.next())
            .await
            .ok()
            .flatten()
        {
            let s = String::from_utf8_lossy(&chunk.unwrap()).to_string();
            buf.push_str(&s);
            if buf.contains("\"msg_id\":\"tap1\"") || buf.contains("\"tap1\"") {
                got_envelope = true;
            }
        }
    }
    assert!(got_envelope, "did not see envelope tap1 in admin SSE: {buf}");
}
