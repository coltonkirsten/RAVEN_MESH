use std::collections::{HashMap, HashSet, VecDeque};
use std::path::PathBuf;
use std::sync::Arc;

use serde_json::Value;
use tokio::sync::{mpsc, oneshot, Mutex, RwLock};

use crate::audit::AuditLog;
use crate::manifest::{self, LoadedManifest, NodeDecl};
use crate::supervisor::Supervisor;

pub const ENVELOPE_TAIL_MAX: usize = 200;

#[derive(Clone)]
pub struct DeliverEvent {
    pub kind: String,
    pub data: Value,
}

pub struct Connection {
    pub session_id: String,
    pub queue: mpsc::Sender<DeliverEvent>,
    pub connected_at: String,
}

pub struct Pending {
    pub responder: oneshot::Sender<Value>,
    pub target_node: String,
}

pub struct CoreInner {
    pub manifest_path: PathBuf,
    pub nodes: HashMap<String, NodeDecl>,
    pub edges: HashSet<(String, String)>,
    pub connections: HashMap<String, Connection>,
    pub sessions: HashMap<String, String>,
    pub pending: HashMap<String, Pending>,
    pub envelope_tail: VecDeque<Value>,
    pub admin_streams: Vec<mpsc::Sender<Value>>,
    pub node_status: HashMap<String, Value>,
}

#[derive(Clone)]
pub struct Core {
    pub inner: Arc<RwLock<CoreInner>>,
    pub audit: Arc<AuditLog>,
    pub supervisor: Arc<Mutex<Option<Supervisor>>>,
    pub admin_token: String,
}

impl Core {
    pub fn new(manifest_path: PathBuf, audit_path: PathBuf, admin_token: String) -> Self {
        Self {
            inner: Arc::new(RwLock::new(CoreInner {
                manifest_path,
                nodes: HashMap::new(),
                edges: HashSet::new(),
                connections: HashMap::new(),
                sessions: HashMap::new(),
                pending: HashMap::new(),
                envelope_tail: VecDeque::with_capacity(ENVELOPE_TAIL_MAX),
                admin_streams: Vec::new(),
                node_status: HashMap::new(),
            })),
            audit: Arc::new(AuditLog::new(audit_path)),
            supervisor: Arc::new(Mutex::new(None)),
            admin_token,
        }
    }

    pub async fn load_manifest(&self) -> Result<(), String> {
        let path = { self.inner.read().await.manifest_path.clone() };
        let LoadedManifest { nodes, edges } = manifest::load_from_path(&path)?;
        let mut g = self.inner.write().await;
        g.nodes = nodes;
        g.edges = edges.into_iter().collect();
        Ok(())
    }

    pub async fn relationships_for(&self, node_id: &str) -> Vec<(String, String)> {
        let g = self.inner.read().await;
        let mut out: Vec<(String, String)> = g
            .edges
            .iter()
            .filter(|(f, t)| {
                f == node_id || t.split_once('.').map(|(n, _)| n) == Some(node_id)
            })
            .cloned()
            .collect();
        out.sort();
        out
    }

    pub async fn push_tail(&self, evt: Value) {
        let mut g = self.inner.write().await;
        if g.envelope_tail.len() >= ENVELOPE_TAIL_MAX {
            g.envelope_tail.pop_front();
        }
        g.envelope_tail.push_back(evt.clone());
        // best-effort fanout to admin SSE subscribers
        let mut alive = Vec::with_capacity(g.admin_streams.len());
        for s in g.admin_streams.drain(..) {
            if s.try_send(evt.clone()).is_ok() {
                alive.push(s);
            }
        }
        g.admin_streams = alive;
    }
}
