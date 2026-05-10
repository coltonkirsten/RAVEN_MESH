use hmac::{Hmac, Mac};
use serde_json::{Map, Value};
use sha2::Sha256;

type HmacSha256 = Hmac<Sha256>;

/// Build canonical JSON over an envelope, excluding the `signature` field.
/// Keys are sorted recursively so the bytes are deterministic and match
/// Python's `json.dumps(..., sort_keys=True, separators=(",", ":"))`.
pub fn canonical(env: &Value) -> String {
    let stripped = strip_signature(env);
    let mut buf = String::new();
    write_canonical(&stripped, &mut buf);
    buf
}

fn strip_signature(env: &Value) -> Value {
    match env {
        Value::Object(m) => {
            let mut out = Map::new();
            for (k, v) in m.iter() {
                if k == "signature" {
                    continue;
                }
                out.insert(k.clone(), v.clone());
            }
            Value::Object(out)
        }
        other => other.clone(),
    }
}

fn write_canonical(v: &Value, out: &mut String) {
    match v {
        Value::Null => out.push_str("null"),
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::Number(n) => out.push_str(&n.to_string()),
        Value::String(s) => {
            out.push_str(&serde_json::to_string(s).expect("string serialize"));
        }
        Value::Array(arr) => {
            out.push('[');
            for (i, item) in arr.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_canonical(item, out);
            }
            out.push(']');
        }
        Value::Object(obj) => {
            let mut keys: Vec<&String> = obj.keys().collect();
            keys.sort();
            out.push('{');
            for (i, k) in keys.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                out.push_str(&serde_json::to_string(k).expect("string serialize"));
                out.push(':');
                write_canonical(&obj[*k], out);
            }
            out.push('}');
        }
    }
}

pub fn sign(env: &Value, secret: &str) -> String {
    let body = canonical(env);
    let mut mac = HmacSha256::new_from_slice(secret.as_bytes()).expect("hmac key");
    mac.update(body.as_bytes());
    hex::encode(mac.finalize().into_bytes())
}

pub fn verify(env: &Value, secret: &str) -> bool {
    let Some(sig) = env.get("signature").and_then(|v| v.as_str()) else {
        return false;
    };
    let expected = sign(env, secret);
    constant_time_eq(sig.as_bytes(), expected.as_bytes())
}

fn constant_time_eq(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    let mut diff: u8 = 0;
    for (x, y) in a.iter().zip(b.iter()) {
        diff |= x ^ y;
    }
    diff == 0
}

pub fn attach_signature(env: &mut Value, secret: &str) {
    let sig = sign(env, secret);
    if let Value::Object(m) = env {
        m.insert("signature".to_string(), Value::String(sig));
    }
}
