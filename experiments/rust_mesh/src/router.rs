use std::collections::HashMap;
use std::convert::Infallible;
use std::time::Duration;

use axum::extract::{Query, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::sse::{Event, KeepAlive, Sse};
use axum::response::IntoResponse;
use axum::routing::{get, post};
use axum::{Json, Router};
use chrono::Utc;
use futures::stream::Stream;
use serde_json::{json, Value};
use tokio::sync::{mpsc, oneshot};
use tower_http::cors::{Any, CorsLayer};

use crate::canonical::{attach_signature, verify};
use crate::state::{Connection, Core, DeliverEvent, Pending};

pub fn build_router(core: Core) -> Router {
    let cors = CorsLayer::new()
        .allow_origin(Any)
        .allow_methods(Any)
        .allow_headers(Any);

    Router::new()
        .route("/v0/healthz", get(healthz))
        .route("/v0/register", post(register))
        .route("/v0/invoke", post(invoke))
        .route("/v0/respond", post(respond))
        .route("/v0/stream", get(stream))
        .route("/v0/introspect", get(introspect))
        .route("/v0/admin/state", get(admin_state))
        .route("/v0/admin/stream", get(admin_stream))
        .route("/v0/admin/reload", post(admin_reload))
        .route("/v0/admin/invoke", post(admin_invoke))
        .route("/v0/admin/processes", get(admin_processes))
        .with_state(core)
        .layer(cors)
}

async fn healthz(State(core): State<Core>) -> Json<Value> {
    let g = core.inner.read().await;
    Json(json!({
        "ok": true,
        "nodes_declared": g.nodes.len(),
        "nodes_connected": g.connections.len(),
        "edges": g.edges.len(),
        "pending": g.pending.len(),
    }))
}

async fn register(
    State(core): State<Core>,
    Json(body): Json<Value>,
) -> (StatusCode, Json<Value>) {
    let node_id = match body.get("node_id").and_then(|v| v.as_str()) {
        Some(s) => s.to_string(),
        None => return (StatusCode::BAD_REQUEST, Json(json!({"error": "missing_node_id"}))),
    };
    let secret_opt = {
        let g = core.inner.read().await;
        g.nodes.get(&node_id).map(|d| d.secret.clone())
    };
    let secret = match secret_opt {
        Some(s) => s,
        None => return (StatusCode::NOT_FOUND, Json(json!({"error": "unknown_node"}))),
    };
    if !verify(&body, &secret) {
        return (StatusCode::UNAUTHORIZED, Json(json!({"error": "bad_signature"})));
    }
    let session_id = uuid::Uuid::new_v4().to_string();
    let (tx, _rx) = mpsc::channel::<DeliverEvent>(64);
    let conn = Connection {
        session_id: session_id.clone(),
        queue: tx,
        connected_at: Utc::now().to_rfc3339(),
    };
    let surfaces_view: Vec<Value>;
    let relationships: Vec<(String, String)>;
    {
        let mut g = core.inner.write().await;
        if let Some(old) = g.connections.remove(&node_id) {
            g.sessions.remove(&old.session_id);
        }
        g.connections.insert(node_id.clone(), conn);
        g.sessions.insert(session_id.clone(), node_id.clone());
        let decl = g.nodes.get(&node_id).expect("node exists");
        surfaces_view = decl
            .surfaces
            .values()
            .map(|s| {
                json!({
                    "name": s.name,
                    "type": s.surface_type,
                    "invocation_mode": s.invocation_mode,
                })
            })
            .collect();
        relationships = g
            .edges
            .iter()
            .filter(|(f, t)| f == &node_id || t.split_once('.').map(|(n, _)| n) == Some(&node_id))
            .cloned()
            .collect();
    }
    (
        StatusCode::OK,
        Json(json!({
            "session_id": session_id,
            "node_id": node_id,
            "surfaces": surfaces_view,
            "relationships": relationships
                .into_iter()
                .map(|(f, t)| json!({"from": f, "to": t}))
                .collect::<Vec<_>>(),
        })),
    )
}

pub async fn route_invocation(
    core: &Core,
    mut env: Value,
    signature_pre_verified: bool,
) -> (StatusCode, Value) {
    if !env.is_object() {
        return (StatusCode::BAD_REQUEST, json!({"error": "bad_envelope"}));
    }
    {
        let m = env.as_object_mut().unwrap();
        m.entry("id")
            .or_insert_with(|| Value::String(uuid::Uuid::new_v4().to_string()));
        let id = m["id"].clone();
        m.entry("correlation_id").or_insert(id);
    }
    let kind = env
        .get("kind")
        .and_then(|v| v.as_str())
        .unwrap_or("invocation");
    if kind != "invocation" {
        return (
            StatusCode::BAD_REQUEST,
            json!({"error": "bad_kind", "expected": "invocation"}),
        );
    }
    let from_node = match env.get("from").and_then(|v| v.as_str()) {
        Some(s) => s.to_string(),
        None => {
            return (StatusCode::BAD_REQUEST, json!({"error": "missing_from"}));
        }
    };
    let to = match env.get("to").and_then(|v| v.as_str()) {
        Some(s) => s.to_string(),
        None => return (StatusCode::BAD_REQUEST, json!({"error": "missing_to"})),
    };

    let (target_node, surface_name) = match to.split_once('.') {
        Some((a, b)) => (a.to_string(), b.to_string()),
        None => return (StatusCode::BAD_REQUEST, json!({"error": "bad_surface_id"})),
    };

    let (
        secret,
        edge_ok,
        target_decl_clone,
        invocation_mode,
        target_queue,
    );
    {
        let g = core.inner.read().await;
        let Some(decl) = g.nodes.get(&from_node) else {
            return (StatusCode::NOT_FOUND, json!({"error": "unknown_node"}));
        };
        secret = decl.secret.clone();
        edge_ok = g.edges.contains(&(from_node.clone(), to.clone()));
        let Some(target) = g.nodes.get(&target_node) else {
            return (StatusCode::NOT_FOUND, json!({"error": "unknown_surface"}));
        };
        let Some(surface) = target.surfaces.get(&surface_name) else {
            return (StatusCode::NOT_FOUND, json!({"error": "unknown_surface"}));
        };
        target_decl_clone = surface.schema.clone();
        invocation_mode = surface.invocation_mode.clone();
        target_queue = g.connections.get(&target_node).map(|c| c.queue.clone());
    }

    if !signature_pre_verified && !verify(&env, &secret) {
        let _ = log_envelope(core, &env, "in", false, "denied_signature_invalid").await;
        return (StatusCode::UNAUTHORIZED, json!({"error": "bad_signature"}));
    }
    if !edge_ok {
        let _ = log_envelope(core, &env, "in", true, "denied_no_relationship").await;
        return (
            StatusCode::FORBIDDEN,
            json!({"error": "denied_no_relationship", "from": from_node, "to": to}),
        );
    }
    // Schema validation. We compile the schema on demand; for a hotter path
    // we'd cache compiled validators on NodeDecl.
    let payload = env.get("payload").cloned().unwrap_or(json!({}));
    if let Ok(compiled) = jsonschema::JSONSchema::compile(&target_decl_clone) {
        if let Err(errors) = compiled.validate(&payload) {
            let detail: Vec<String> = errors.map(|e| e.to_string()).collect();
            let _ = log_envelope(core, &env, "in", true, "denied_schema_invalid").await;
            return (
                StatusCode::BAD_REQUEST,
                json!({"error": "denied_schema_invalid", "details": detail}),
            );
        }
    }
    let Some(queue) = target_queue else {
        let _ = log_envelope(core, &env, "in", true, "denied_node_unreachable").await;
        return (
            StatusCode::SERVICE_UNAVAILABLE,
            json!({"error": "denied_node_unreachable", "node": target_node}),
        );
    };

    let msg_id = env
        .get("id")
        .and_then(|v| v.as_str())
        .unwrap_or_default()
        .to_string();
    let _ = log_envelope(core, &env, "in", true, "routed").await;
    let deliver = DeliverEvent {
        kind: "deliver".to_string(),
        data: env.clone(),
    };

    if invocation_mode == "fire_and_forget" {
        if queue.send(deliver).await.is_err() {
            return (
                StatusCode::SERVICE_UNAVAILABLE,
                json!({"error": "denied_node_unreachable"}),
            );
        }
        return (
            StatusCode::ACCEPTED,
            json!({"id": msg_id, "status": "accepted"}),
        );
    }

    let (tx, rx) = oneshot::channel::<Value>();
    {
        let mut g = core.inner.write().await;
        g.pending.insert(
            msg_id.clone(),
            Pending {
                responder: tx,
                target_node: target_node.clone(),
            },
        );
    }
    if queue.send(deliver).await.is_err() {
        let mut g = core.inner.write().await;
        g.pending.remove(&msg_id);
        return (
            StatusCode::SERVICE_UNAVAILABLE,
            json!({"error": "denied_node_unreachable"}),
        );
    }

    let timeout = Duration::from_secs(
        std::env::var("MESH_INVOKE_TIMEOUT")
            .ok()
            .and_then(|s| s.parse().ok())
            .unwrap_or(30),
    );
    match tokio::time::timeout(timeout, rx).await {
        Ok(Ok(v)) => (StatusCode::OK, v),
        Ok(Err(_)) | Err(_) => {
            let mut g = core.inner.write().await;
            g.pending.remove(&msg_id);
            (
                StatusCode::GATEWAY_TIMEOUT,
                json!({"error": "timeout", "id": msg_id}),
            )
        }
    }
}

async fn invoke(State(core): State<Core>, Json(body): Json<Value>) -> impl IntoResponse {
    let (status, data) = route_invocation(&core, body, false).await;
    (status, Json(data))
}

async fn respond(State(core): State<Core>, Json(env): Json<Value>) -> (StatusCode, Json<Value>) {
    let from_node = match env.get("from").and_then(|v| v.as_str()) {
        Some(s) => s.to_string(),
        None => return (StatusCode::BAD_REQUEST, Json(json!({"error": "missing_from"}))),
    };
    let secret = {
        let g = core.inner.read().await;
        match g.nodes.get(&from_node) {
            Some(d) => d.secret.clone(),
            None => {
                return (StatusCode::NOT_FOUND, Json(json!({"error": "unknown_node"})));
            }
        }
    };
    if !verify(&env, &secret) {
        return (StatusCode::UNAUTHORIZED, Json(json!({"error": "bad_signature"})));
    }
    let kind = env.get("kind").and_then(|v| v.as_str()).unwrap_or("");
    if kind != "response" && kind != "error" {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({"error": "bad_kind"})),
        );
    }
    let cid = match env.get("correlation_id").and_then(|v| v.as_str()) {
        Some(s) => s.to_string(),
        None => return (StatusCode::BAD_REQUEST, Json(json!({"error": "missing_cid"}))),
    };
    let mut g = core.inner.write().await;
    let entry = match g.pending.remove(&cid) {
        Some(e) => e,
        None => {
            return (
                StatusCode::NOT_FOUND,
                Json(json!({"error": "no_pending_request", "correlation_id": cid})),
            );
        }
    };
    if entry.target_node != from_node {
        return (
            StatusCode::FORBIDDEN,
            Json(json!({"error": "responder_not_target", "expected": entry.target_node})),
        );
    }
    let _ = entry.responder.send(env.clone());
    drop(g);
    let _ = log_envelope(&core, &env, "out", true, "routed").await;
    (StatusCode::OK, Json(json!({"status": "accepted"})))
}

async fn stream(
    State(core): State<Core>,
    Query(params): Query<HashMap<String, String>>,
) -> Result<Sse<impl Stream<Item = Result<Event, Infallible>>>, (StatusCode, Json<Value>)> {
    let Some(session) = params.get("session").cloned() else {
        return Err((StatusCode::UNAUTHORIZED, Json(json!({"error": "missing_session"}))));
    };
    let (node_id, rx_holder) = {
        let mut g = core.inner.write().await;
        let Some(nid) = g.sessions.get(&session).cloned() else {
            return Err((StatusCode::UNAUTHORIZED, Json(json!({"error": "unknown_session"}))));
        };
        // Replace the connection's send half with a fresh channel owned here.
        let (tx, rx) = mpsc::channel::<DeliverEvent>(64);
        if let Some(conn) = g.connections.get_mut(&nid) {
            conn.queue = tx;
        }
        (nid, rx)
    };

    let stream = async_stream::stream! {
        let hello = serde_json::to_string(&json!({"node_id": node_id})).unwrap_or_default();
        yield Ok::<_, Infallible>(Event::default().event("hello").data(hello));
        let mut rx = rx_holder;
        while let Some(evt) = rx.recv().await {
            let data = serde_json::to_string(&evt.data).unwrap_or_default();
            yield Ok(Event::default().event(evt.kind).data(data));
        }
    };
    Ok(Sse::new(stream).keep_alive(KeepAlive::new().interval(Duration::from_secs(15))))
}

async fn introspect(State(core): State<Core>) -> Json<Value> {
    let g = core.inner.read().await;
    let nodes: Vec<Value> = g
        .nodes
        .values()
        .map(|d| {
            json!({
                "id": d.id,
                "kind": d.kind,
                "runtime": d.runtime,
                "metadata": d.metadata,
                "connected": g.connections.contains_key(&d.id),
                "surfaces": d.surfaces.values().map(|s| json!({
                    "name": s.name,
                    "type": s.surface_type,
                    "invocation_mode": s.invocation_mode,
                })).collect::<Vec<_>>(),
            })
        })
        .collect();
    let mut edges: Vec<(String, String)> = g.edges.iter().cloned().collect();
    edges.sort();
    Json(json!({
        "nodes": nodes,
        "relationships": edges.into_iter().map(|(f,t)| json!({"from":f,"to":t})).collect::<Vec<_>>(),
    }))
}

fn admin_authed(headers: &HeaderMap, query: &HashMap<String, String>, expected: &str) -> bool {
    if let Some(h) = headers.get("X-Admin-Token").and_then(|v| v.to_str().ok()) {
        if h == expected {
            return true;
        }
    }
    matches!(query.get("admin_token"), Some(t) if t == expected)
}

async fn admin_state(
    State(core): State<Core>,
    headers: HeaderMap,
    Query(q): Query<HashMap<String, String>>,
) -> (StatusCode, Json<Value>) {
    if !admin_authed(&headers, &q, &core.admin_token) {
        return (StatusCode::UNAUTHORIZED, Json(json!({"error": "unauthorized"})));
    }
    let g = core.inner.read().await;
    let nodes: Vec<Value> = g
        .nodes
        .values()
        .map(|d| {
            json!({
                "id": d.id,
                "kind": d.kind,
                "connected": g.connections.contains_key(&d.id),
                "surfaces": d.surfaces.values().map(|s| json!({
                    "name": s.name,
                    "type": s.surface_type,
                    "invocation_mode": s.invocation_mode,
                    "schema": s.schema,
                })).collect::<Vec<_>>(),
            })
        })
        .collect();
    let mut edges: Vec<(String, String)> = g.edges.iter().cloned().collect();
    edges.sort();
    (
        StatusCode::OK,
        Json(json!({
            "manifest_path": g.manifest_path.display().to_string(),
            "nodes": nodes,
            "relationships": edges.into_iter().map(|(f,t)| json!({"from":f,"to":t})).collect::<Vec<_>>(),
            "envelope_tail": g.envelope_tail.iter().cloned().collect::<Vec<_>>(),
            "node_status": g.node_status,
        })),
    )
}

async fn admin_stream(
    State(core): State<Core>,
    headers: HeaderMap,
    Query(q): Query<HashMap<String, String>>,
) -> Result<Sse<impl Stream<Item = Result<Event, Infallible>>>, (StatusCode, Json<Value>)> {
    if !admin_authed(&headers, &q, &core.admin_token) {
        return Err((StatusCode::UNAUTHORIZED, Json(json!({"error": "unauthorized"}))));
    }
    let (tx, rx) = mpsc::channel::<Value>(256);
    let backlog: Vec<Value> = {
        let mut g = core.inner.write().await;
        g.admin_streams.push(tx);
        g.envelope_tail.iter().cloned().collect()
    };
    let stream = async_stream::stream! {
        yield Ok::<_, Infallible>(Event::default().event("hello").data("{}"));
        for evt in backlog {
            let s = serde_json::to_string(&evt).unwrap_or_default();
            yield Ok(Event::default().event("envelope").data(s));
        }
        let mut rx = rx;
        while let Some(evt) = rx.recv().await {
            let s = serde_json::to_string(&evt).unwrap_or_default();
            yield Ok(Event::default().event("envelope").data(s));
        }
    };
    Ok(Sse::new(stream).keep_alive(KeepAlive::new().interval(Duration::from_secs(10))))
}

async fn admin_reload(
    State(core): State<Core>,
    headers: HeaderMap,
    Query(q): Query<HashMap<String, String>>,
) -> (StatusCode, Json<Value>) {
    if !admin_authed(&headers, &q, &core.admin_token) {
        return (StatusCode::UNAUTHORIZED, Json(json!({"error": "unauthorized"})));
    }
    match core.load_manifest().await {
        Ok(_) => {
            let g = core.inner.read().await;
            (
                StatusCode::OK,
                Json(json!({
                    "ok": true,
                    "nodes_declared": g.nodes.len(),
                    "edges": g.edges.len(),
                })),
            )
        }
        Err(e) => (
            StatusCode::BAD_REQUEST,
            Json(json!({"error": "load_failed", "details": e})),
        ),
    }
}

async fn admin_invoke(
    State(core): State<Core>,
    headers: HeaderMap,
    Query(q): Query<HashMap<String, String>>,
    Json(body): Json<Value>,
) -> (StatusCode, Json<Value>) {
    if !admin_authed(&headers, &q, &core.admin_token) {
        return (StatusCode::UNAUTHORIZED, Json(json!({"error": "unauthorized"})));
    }
    let from_node = match body.get("from_node").and_then(|v| v.as_str()) {
        Some(s) => s.to_string(),
        None => return (StatusCode::BAD_REQUEST, Json(json!({"error": "missing_from_node"}))),
    };
    let target = match body.get("target").and_then(|v| v.as_str()) {
        Some(s) => s.to_string(),
        None => return (StatusCode::BAD_REQUEST, Json(json!({"error": "missing_target"}))),
    };
    let payload = body.get("payload").cloned().unwrap_or(json!({}));
    let secret = {
        let g = core.inner.read().await;
        match g.nodes.get(&from_node) {
            Some(d) => d.secret.clone(),
            None => return (StatusCode::NOT_FOUND, Json(json!({"error": "unknown_node"}))),
        }
    };
    let id = uuid::Uuid::new_v4().to_string();
    let mut env = json!({
        "id": id,
        "correlation_id": id,
        "from": from_node,
        "to": target,
        "kind": "invocation",
        "payload": payload,
        "timestamp": Utc::now().to_rfc3339(),
    });
    attach_signature(&mut env, &secret);
    let (status, data) = route_invocation(&core, env, true).await;
    (status, Json(data))
}

async fn admin_processes(
    State(core): State<Core>,
    headers: HeaderMap,
    Query(q): Query<HashMap<String, String>>,
) -> (StatusCode, Json<Value>) {
    if !admin_authed(&headers, &q, &core.admin_token) {
        return (StatusCode::UNAUTHORIZED, Json(json!({"error": "unauthorized"})));
    }
    let sup = core.supervisor.lock().await;
    match sup.as_ref() {
        Some(s) => {
            let list = s.list().await;
            (
                StatusCode::OK,
                Json(json!({"supervisor_enabled": true, "processes": list})),
            )
        }
        None => (
            StatusCode::OK,
            Json(json!({"supervisor_enabled": false, "processes": []})),
        ),
    }
}

async fn log_envelope(
    core: &Core,
    env: &Value,
    direction: &str,
    sig_valid: bool,
    route_status: &str,
) {
    let evt = json!({
        "ts": Utc::now().to_rfc3339(),
        "direction": direction,
        "from_node": env.get("from"),
        "to_surface": env.get("to"),
        "msg_id": env.get("id"),
        "correlation_id": env.get("correlation_id"),
        "kind": env.get("kind"),
        "payload": env.get("payload").cloned().unwrap_or(json!({})),
        "signature_valid": sig_valid,
        "route_status": route_status,
    });
    core.push_tail(evt.clone()).await;

    let mut fields = serde_json::Map::new();
    fields.insert("type".into(), json!(direction));
    fields.insert("from_node".into(), evt["from_node"].clone());
    fields.insert("to_surface".into(), evt["to_surface"].clone());
    fields.insert("decision".into(), json!(route_status));
    fields.insert("correlation_id".into(), evt["correlation_id"].clone());
    fields.insert("details".into(), json!({"kind": evt["kind"]}));
    core.audit.write(fields).await;
}
