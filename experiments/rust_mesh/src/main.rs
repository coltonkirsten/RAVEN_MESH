use std::path::PathBuf;

use clap::{Parser, Subcommand};
use rust_mesh::{router, state::Core};
use tracing_subscriber::EnvFilter;

#[derive(Parser, Debug)]
#[command(name = "rust_mesh", about = "RAVEN_MESH Rust prototype core")]
struct Cli {
    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand, Debug)]
enum Cmd {
    /// Run the mesh Core (HTTP server + supervisor).
    Core {
        #[arg(long, default_value = "127.0.0.1")]
        host: String,
        #[arg(long, default_value_t = 8000)]
        port: u16,
        #[arg(long, default_value = "manifests/demo.yaml")]
        manifest: PathBuf,
        #[arg(long, default_value = "audit.log")]
        audit_log: PathBuf,
        #[arg(long, default_value = "admin-dev-token")]
        admin_token: String,
    },
    /// Run the built-in echo node.
    Echo {
        #[arg(long, default_value = "http://127.0.0.1:8000")]
        core_url: String,
        #[arg(long)]
        node_id: String,
        #[arg(long)]
        secret: String,
    },
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let _ = tracing_subscriber::fmt()
        .with_env_filter(EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info")))
        .try_init();
    let cli = Cli::parse();
    match cli.cmd {
        Cmd::Core {
            host,
            port,
            manifest,
            audit_log,
            admin_token,
        } => {
            let core = Core::new(manifest, audit_log, admin_token);
            core.load_manifest().await.map_err(|e| -> Box<dyn std::error::Error> { e.into() })?;
            let app = router::build_router(core.clone());
            let addr: std::net::SocketAddr = format!("{host}:{port}").parse()?;
            let listener = tokio::net::TcpListener::bind(addr).await?;
            tracing::info!("rust_mesh core listening on {}", addr);
            println!("[core] listening on http://{addr}");
            axum::serve(listener, app).await?;
        }
        Cmd::Echo {
            core_url,
            node_id,
            secret,
        } => {
            rust_mesh::echo::run(core_url, node_id, secret).await?;
        }
    }
    Ok(())
}
