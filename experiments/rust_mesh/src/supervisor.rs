//! Tokio-based process supervisor analogous to core/supervisor.py.
//!
//! Each child gets one long-running monitor task. The task spawns the
//! process, awaits exit, applies the restart policy + sliding-window
//! backoff, and respawns from the same task — no recursion through the
//! supervisor handle. A watch channel signals deliberate stops.

use std::collections::HashMap;
use std::path::PathBuf;
use std::process::Stdio;
use std::sync::Arc;
use std::time::{Duration, Instant};

use serde::Serialize;
use tokio::process::Command;
use tokio::sync::Mutex;
use tokio::time::sleep;

#[derive(Clone, Debug)]
pub enum RestartPolicy {
    Permanent,
    Transient,
    Temporary,
}

#[derive(Clone, Debug)]
pub struct ChildSpec {
    pub node_id: String,
    pub cmd: Vec<String>,
    pub env: HashMap<String, String>,
    pub cwd: PathBuf,
    pub log_path: PathBuf,
    pub restart: RestartPolicy,
    pub max_restarts: u32,
    pub restart_window: Duration,
}

#[derive(Debug, Serialize, Clone)]
pub struct ChildSnapshot {
    pub node_id: String,
    pub pid: Option<u32>,
    pub status: String,
    pub uptime_seconds: f64,
    pub restart_count: u32,
    pub log_path: String,
    pub cmd: Vec<String>,
    pub last_exit_code: Option<i32>,
}

struct ChildState {
    spec: ChildSpec,
    pid: Option<u32>,
    started_at: Option<Instant>,
    last_exit_code: Option<i32>,
    restart_count: u32,
    status: String,
    stop_tx: Option<tokio::sync::watch::Sender<bool>>,
}

impl ChildState {
    fn snapshot(&self) -> ChildSnapshot {
        let uptime = match (self.status.as_str(), self.started_at) {
            ("running", Some(t)) => t.elapsed().as_secs_f64(),
            _ => 0.0,
        };
        ChildSnapshot {
            node_id: self.spec.node_id.clone(),
            pid: self.pid,
            status: self.status.clone(),
            uptime_seconds: (uptime * 10.0).round() / 10.0,
            restart_count: self.restart_count,
            log_path: self.spec.log_path.display().to_string(),
            cmd: self.spec.cmd.clone(),
            last_exit_code: self.last_exit_code,
        }
    }
}

#[derive(Clone)]
pub struct Supervisor {
    inner: Arc<Mutex<HashMap<String, ChildState>>>,
    pub stopping: Arc<std::sync::atomic::AtomicBool>,
}

impl Default for Supervisor {
    fn default() -> Self {
        Self::new()
    }
}

impl Supervisor {
    pub fn new() -> Self {
        Self {
            inner: Arc::new(Mutex::new(HashMap::new())),
            stopping: Arc::new(std::sync::atomic::AtomicBool::new(false)),
        }
    }

    /// Idempotent spawn: starts the child if not already running and
    /// installs a per-child monitor task that handles restart.
    pub async fn spawn(&self, spec: ChildSpec) -> Result<ChildSnapshot, String> {
        {
            let g = self.inner.lock().await;
            if let Some(existing) = g.get(&spec.node_id) {
                if existing.status == "running" || existing.status == "starting" {
                    return Ok(existing.snapshot());
                }
            }
        }

        let (stop_tx, stop_rx) = tokio::sync::watch::channel(false);
        {
            let mut g = self.inner.lock().await;
            g.insert(
                spec.node_id.clone(),
                ChildState {
                    spec: spec.clone(),
                    pid: None,
                    started_at: None,
                    last_exit_code: None,
                    restart_count: 0,
                    status: "starting".into(),
                    stop_tx: Some(stop_tx),
                },
            );
        }

        let sup = self.clone();
        let spec_for_loop = spec.clone();
        tokio::spawn(async move {
            sup.monitor_loop(spec_for_loop, stop_rx).await;
        });

        // Allow the loop one tick to actually spawn the process so the
        // first snapshot reflects the running pid.
        for _ in 0..25 {
            sleep(Duration::from_millis(10)).await;
            let g = self.inner.lock().await;
            if let Some(c) = g.get(&spec.node_id) {
                if c.pid.is_some() && c.status == "running" {
                    return Ok(c.snapshot());
                }
            }
        }
        let g = self.inner.lock().await;
        g.get(&spec.node_id)
            .map(|c| c.snapshot())
            .ok_or_else(|| "spawn_lost".into())
    }

    fn build_command(spec: &ChildSpec) -> Result<Command, String> {
        if let Some(parent) = spec.log_path.parent() {
            let _ = std::fs::create_dir_all(parent);
        }
        let log_file = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&spec.log_path)
            .map_err(|e| format!("open log: {e}"))?;
        let log_file2 = log_file.try_clone().map_err(|e| format!("clone log: {e}"))?;

        let mut command = Command::new(&spec.cmd[0]);
        command
            .args(&spec.cmd[1..])
            .current_dir(&spec.cwd)
            .stdout(Stdio::from(log_file))
            .stderr(Stdio::from(log_file2))
            .kill_on_drop(false);
        for (k, v) in &spec.env {
            command.env(k, v);
        }
        Ok(command)
    }

    async fn monitor_loop(
        self,
        spec: ChildSpec,
        mut stop_rx: tokio::sync::watch::Receiver<bool>,
    ) {
        let node_id = spec.node_id.clone();
        let mut restart_window_start = Instant::now();
        let mut restart_count: u32 = 0;

        loop {
            if self.stopping.load(std::sync::atomic::Ordering::Relaxed) {
                self.set_status(&node_id, "stopped").await;
                return;
            }

            let mut command = match Self::build_command(&spec) {
                Ok(c) => c,
                Err(_) => {
                    self.set_status(&node_id, "failed").await;
                    return;
                }
            };
            let mut child = match command.spawn() {
                Ok(c) => c,
                Err(_) => {
                    self.set_status(&node_id, "failed").await;
                    return;
                }
            };
            let pid = child.id();
            {
                let mut g = self.inner.lock().await;
                if let Some(s) = g.get_mut(&node_id) {
                    s.pid = pid;
                    s.started_at = Some(Instant::now());
                    s.status = "running".into();
                }
            }

            let exit_rc: i32 = tokio::select! {
                exit = child.wait() => match exit {
                    Ok(s) => s.code().unwrap_or(-1),
                    Err(_) => -1,
                },
                _ = stop_rx.changed() => {
                    let _ = child.kill().await;
                    let _ = child.wait().await;
                    let mut g = self.inner.lock().await;
                    if let Some(s) = g.get_mut(&node_id) {
                        s.status = "stopped".into();
                        s.pid = None;
                    }
                    return;
                }
            };

            {
                let mut g = self.inner.lock().await;
                if let Some(s) = g.get_mut(&node_id) {
                    s.last_exit_code = Some(exit_rc);
                    s.pid = None;
                }
            }

            if self.stopping.load(std::sync::atomic::Ordering::Relaxed) {
                self.set_status(&node_id, "stopped").await;
                return;
            }

            let normal = exit_rc == 0;
            let restart = match spec.restart {
                RestartPolicy::Temporary => false,
                RestartPolicy::Transient => !normal,
                RestartPolicy::Permanent => true,
            };
            if !restart {
                self.set_status(&node_id, if normal { "stopped" } else { "crashed" })
                    .await;
                return;
            }

            if restart_window_start.elapsed() > spec.restart_window {
                restart_window_start = Instant::now();
                restart_count = 0;
            }
            restart_count += 1;
            if restart_count > spec.max_restarts {
                self.set_status(&node_id, "failed").await;
                return;
            }

            self.bump_restart_count(&node_id, restart_count).await;
            let backoff = Duration::from_millis(((restart_count as u64) * 100).min(2_000));
            tokio::select! {
                _ = sleep(backoff) => {},
                _ = stop_rx.changed() => {
                    self.set_status(&node_id, "stopped").await;
                    return;
                }
            }
        }
    }

    async fn set_status(&self, node_id: &str, status: &str) {
        let mut g = self.inner.lock().await;
        if let Some(s) = g.get_mut(node_id) {
            s.status = status.into();
        }
    }

    async fn bump_restart_count(&self, node_id: &str, count: u32) {
        let mut g = self.inner.lock().await;
        if let Some(s) = g.get_mut(node_id) {
            s.restart_count = count;
            s.status = "crashed".into();
        }
    }

    pub async fn stop(&self, node_id: &str) -> Result<ChildSnapshot, String> {
        let stop_tx = {
            let g = self.inner.lock().await;
            let Some(child) = g.get(node_id) else {
                return Err("unknown_node".into());
            };
            child.stop_tx.clone()
        };
        if let Some(tx) = stop_tx {
            let _ = tx.send(true);
        }
        for _ in 0..50 {
            sleep(Duration::from_millis(20)).await;
            let g = self.inner.lock().await;
            if let Some(child) = g.get(node_id) {
                if child.status != "running" && child.status != "starting" {
                    return Ok(child.snapshot());
                }
            }
        }
        let mut g = self.inner.lock().await;
        if let Some(child) = g.get_mut(node_id) {
            child.status = "stopped".into();
            return Ok(child.snapshot());
        }
        Err("unknown_node".into())
    }

    pub async fn list(&self) -> Vec<ChildSnapshot> {
        let g = self.inner.lock().await;
        g.values().map(|c| c.snapshot()).collect()
    }

    pub async fn shutdown_all(&self) {
        self.stopping
            .store(true, std::sync::atomic::Ordering::Relaxed);
        let ids: Vec<String> = {
            let g = self.inner.lock().await;
            g.keys().cloned().collect()
        };
        for id in ids {
            let _ = self.stop(&id).await;
        }
    }
}
