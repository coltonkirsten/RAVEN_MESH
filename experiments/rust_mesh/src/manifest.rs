use std::collections::HashMap;
use std::path::{Path, PathBuf};

use serde::Deserialize;
use serde_json::Value;
use sha2::{Digest, Sha256};

#[derive(Debug, Clone, Deserialize, serde::Serialize)]
pub struct RawSurface {
    pub name: String,
    #[serde(rename = "type")]
    pub surface_type: String,
    #[serde(default = "default_invocation_mode")]
    pub invocation_mode: String,
    pub schema: String,
}

fn default_invocation_mode() -> String {
    "request_response".into()
}

#[derive(Debug, Clone, Deserialize)]
pub struct RawNode {
    pub id: String,
    pub kind: String,
    #[serde(default = "default_runtime")]
    pub runtime: String,
    #[serde(default)]
    pub identity_secret: String,
    #[serde(default)]
    pub metadata: serde_json::Value,
    #[serde(default)]
    pub surfaces: Vec<RawSurface>,
}

fn default_runtime() -> String {
    "local-process".into()
}

#[derive(Debug, Clone, Deserialize)]
pub struct RawRelationship {
    pub from: String,
    pub to: String,
}

#[derive(Debug, Deserialize)]
pub struct RawManifest {
    #[serde(default)]
    pub nodes: Vec<RawNode>,
    #[serde(default)]
    pub relationships: Vec<RawRelationship>,
}

#[derive(Debug, Clone)]
pub struct Surface {
    pub name: String,
    pub surface_type: String,
    pub invocation_mode: String,
    pub schema: Value,
}

#[derive(Debug, Clone)]
pub struct NodeDecl {
    pub id: String,
    pub kind: String,
    pub runtime: String,
    pub metadata: Value,
    pub secret: String,
    pub surfaces: HashMap<String, Surface>,
    pub raw: Value,
}

#[derive(Debug, Clone)]
pub struct LoadedManifest {
    pub nodes: HashMap<String, NodeDecl>,
    pub edges: Vec<(String, String)>,
}

pub fn load_from_path(path: &Path) -> Result<LoadedManifest, String> {
    let text = std::fs::read_to_string(path).map_err(|e| format!("read manifest: {e}"))?;
    let raw: RawManifest = serde_yaml::from_str(&text).map_err(|e| format!("parse yaml: {e}"))?;
    parse(raw, path.parent().unwrap_or(Path::new(".")))
}

pub fn parse(raw: RawManifest, base_dir: &Path) -> Result<LoadedManifest, String> {
    let mut nodes = HashMap::new();
    for node in raw.nodes {
        let secret = resolve_secret(&node.id, &node.identity_secret);
        let mut surfaces = HashMap::new();
        for s in &node.surfaces {
            let schema_path: PathBuf = if Path::new(&s.schema).is_absolute() {
                PathBuf::from(&s.schema)
            } else {
                base_dir.join(&s.schema)
            };
            let schema_text = std::fs::read_to_string(&schema_path)
                .map_err(|e| format!("read schema {}: {}", schema_path.display(), e))?;
            let schema: Value = serde_json::from_str(&schema_text)
                .map_err(|e| format!("parse schema {}: {}", schema_path.display(), e))?;
            surfaces.insert(
                s.name.clone(),
                Surface {
                    name: s.name.clone(),
                    surface_type: s.surface_type.clone(),
                    invocation_mode: s.invocation_mode.clone(),
                    schema,
                },
            );
        }
        let raw_value = serde_json::to_value(&NodeRawWire {
            id: &node.id,
            kind: &node.kind,
            runtime: &node.runtime,
            metadata: &node.metadata,
            surfaces: &node.surfaces,
        })
        .unwrap_or(Value::Null);
        nodes.insert(
            node.id.clone(),
            NodeDecl {
                id: node.id.clone(),
                kind: node.kind.clone(),
                runtime: node.runtime.clone(),
                metadata: node.metadata.clone(),
                secret,
                surfaces,
                raw: raw_value,
            },
        );
    }
    let edges = raw
        .relationships
        .into_iter()
        .map(|r| (r.from, r.to))
        .collect();
    Ok(LoadedManifest { nodes, edges })
}

#[derive(serde::Serialize)]
struct NodeRawWire<'a> {
    id: &'a str,
    kind: &'a str,
    runtime: &'a str,
    metadata: &'a Value,
    surfaces: &'a [RawSurface],
}

fn resolve_secret(node_id: &str, spec: &str) -> String {
    if let Some(var) = spec.strip_prefix("env:") {
        if let Ok(val) = std::env::var(var) {
            if !val.is_empty() {
                return val;
            }
        }
        let auto = autogen(node_id);
        std::env::set_var(var, &auto);
        return auto;
    }
    if spec.is_empty() {
        autogen(node_id)
    } else {
        spec.to_string()
    }
}

fn autogen(node_id: &str) -> String {
    let mut hasher = Sha256::new();
    hasher.update(format!("mesh:{}:autogen", node_id).as_bytes());
    hex::encode(hasher.finalize())
}
