//! The MetaBot desktop shell (D2). A thin window: it finds the app server this
//! person already runs (the state folder's `server.json` and `token`), or starts
//! `metabot-app serve` from PATH and waits for it, then opens the page with the
//! desktop token. Nothing else: the page is the product, the server owns the
//! API, and this binary holds no logic the CLI does not have.
//!
//! `METABOT_SHELL_SMOKE=1` prints the plan and the port it would open (never the
//! token) and exits without a window, so CI can prove the binary under xvfb.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use metabot_shell_core::{config_path, page_url, plan, state_dir, wait_for_server, Plan};
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::Duration;

const START_WAIT: Duration = Duration::from_secs(20);
const TICK: Duration = Duration::from_millis(100);

fn env(key: &str) -> Option<String> {
    std::env::var(key).ok().filter(|v| !v.is_empty())
}

/// What the window opens: `Some(url)` for a live or freshly started server, `None` for the
/// install page. Starting detaches the server (its own session) so closing the window keeps it.
fn resolve(state: &Path, path_env: &str) -> (String, Option<String>) {
    match plan(state, path_env) {
        Plan::Attach { port } => match wait_for_server(state, Duration::from_secs(2), TICK) {
            Some((_, token)) => ("attach".into(), Some(page_url(port, &token))),
            None => ("attach-no-token".into(), None),
        },
        Plan::Start { exe } => {
            if start_server(&exe).is_err() {
                return ("start-failed".into(), None);
            }
            match wait_for_server(state, START_WAIT, TICK) {
                Some((port, token)) => ("start".into(), Some(page_url(port, &token))),
                None => ("start-timeout".into(), None),
            }
        }
        Plan::Missing => ("missing".into(), None),
    }
}

fn start_server(exe: &PathBuf) -> std::io::Result<()> {
    Command::new(exe)
        .arg("serve")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .process_group(0)
        .spawn()
        .map(|_| ())
}

fn main() {
    let home = env("HOME").unwrap_or_default();
    let cfg = std::fs::read_to_string(config_path(env("METACHAT_CONFIG").as_deref(), &home)).ok();
    let state = state_dir(env("METACHAT_STATE").as_deref(), cfg.as_deref(), &home);
    let path_env = env("PATH").unwrap_or_default();
    let (how, url) = resolve(&state, &path_env);

    if env("METABOT_SHELL_SMOKE").is_some() {
        // The URL's port alone: the token stays off every terminal and log.
        let port = url
            .as_deref()
            .and_then(|u| u.strip_prefix("http://127.0.0.1:"))
            .and_then(|rest| rest.split('/').next())
            .unwrap_or("-");
        println!("{how} port={port}");
        std::process::exit(if url.is_some() { 0 } else { 3 });
    }

    tauri::Builder::default()
        .setup(move |app| {
            let target = match &url {
                Some(u) => tauri::WebviewUrl::External(u.parse()?),
                None => tauri::WebviewUrl::App("missing.html".into()),
            };
            tauri::WebviewWindowBuilder::new(app, "main", target)
                .title("MetaBot")
                .inner_size(1200.0, 800.0)
                .min_inner_size(360.0, 480.0)
                .build()?;
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("the MetaBot window could not be opened");
}
