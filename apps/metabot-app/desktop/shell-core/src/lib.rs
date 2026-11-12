//! What the desktop window does before it shows a page, with nothing but the standard
//! library: find the app server's state folder the same way the server does
//! (`METACHAT_STATE`, else the config's `state_dir`, else `~/.local/state/metachat`), read
//! `server.json` and believe it only while its pid is alive, read the desktop token, and
//! build the page URL the Android shell builds too (`http://127.0.0.1:<port>/?t=<token>`).
//! The shell is thin on purpose: it starts or attaches a server and shows its page; every
//! rule lives in the server.

use std::path::{Path, PathBuf};

/// `STATE/server.json`: written by the server once its loopback listener is bound.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Record {
    pub pid: u32,
    pub port: u16,
    pub build: String,
}

pub const DEFAULT_CONFIG: &str = ".config/metachat/config.json";
pub const DEFAULT_STATE: &str = ".local/state/metachat";
pub const SERVER_EXE: &str = "metabot-app";

fn after_key<'a>(text: &'a str, key: &str) -> Option<&'a str> {
    let pat = format!("\"{key}\"");
    let i = text.find(&pat)? + pat.len();
    text[i..].trim_start().strip_prefix(':').map(str::trim_start)
}

fn json_number(text: &str, key: &str) -> Option<u64> {
    let rest = after_key(text, key)?;
    let digits: String = rest.chars().take_while(|c| c.is_ascii_digit()).collect();
    if digits.is_empty() {
        None
    } else {
        digits.parse().ok()
    }
}

fn json_string(text: &str, key: &str) -> Option<String> {
    let rest = after_key(text, key)?.strip_prefix('"')?;
    Some(rest.split('"').next().unwrap_or("").to_string())
}

/// The record a `server.json` holds; None when it does not hold a pid and a port.
pub fn parse_record(text: &str) -> Option<Record> {
    let pid = json_number(text, "pid")?;
    let port = json_number(text, "port")?;
    if pid == 0 || pid > u32::MAX as u64 || port == 0 || port > u16::MAX as u64 {
        return None;
    }
    Some(Record { pid: pid as u32, port: port as u16, build: json_string(text, "build").unwrap_or_default() })
}

/// Whether `pid` names a running process: `/proc/<pid>/stat` exists and its state is not
/// exited or dead. Where there is no `/proc`, a pid is taken as alive (the server is asked
/// next, and a dead one does not answer).
pub fn pid_alive(pid: u32) -> bool {
    let proc_dir = Path::new("/proc");
    if !proc_dir.is_dir() {
        return true;
    }
    match std::fs::read_to_string(proc_dir.join(pid.to_string()).join("stat")) {
        Ok(stat) => {
            let state = stat.rsplit(')').next().unwrap_or("").split_whitespace().next().unwrap_or("");
            !matches!(state, "Z" | "X" | "x")
        }
        Err(_) => false,
    }
}

/// `~/x` -> `<home>/x`; anything else unchanged.
pub fn expand_home(path: &str, home: &str) -> PathBuf {
    match path.strip_prefix("~/") {
        Some(rest) => Path::new(home).join(rest),
        None if path == "~" => PathBuf::from(home),
        None => PathBuf::from(path),
    }
}

/// The config file: `METACHAT_CONFIG`, else `~/.config/metachat/config.json`.
pub fn config_path(env_config: Option<&str>, home: &str) -> PathBuf {
    match env_config.filter(|s| !s.is_empty()) {
        Some(p) => expand_home(p, home),
        None => Path::new(home).join(DEFAULT_CONFIG),
    }
}

/// The state folder: `METACHAT_STATE`, else the config's `state_dir`, else
/// `~/.local/state/metachat`; the same order the server uses.
pub fn state_dir(env_state: Option<&str>, config_text: Option<&str>, home: &str) -> PathBuf {
    if let Some(p) = env_state.filter(|s| !s.is_empty()) {
        return expand_home(p, home);
    }
    if let Some(p) = config_text.and_then(|t| json_string(t, "state_dir")).filter(|s| !s.is_empty()) {
        return expand_home(&p, home);
    }
    Path::new(home).join(DEFAULT_STATE)
}

/// The record of a server that is running now: `server.json` parsed and its pid alive.
pub fn live_record(state: &Path) -> Option<Record> {
    let text = std::fs::read_to_string(state.join("server.json")).ok()?;
    let rec = parse_record(&text)?;
    if pid_alive(rec.pid) {
        Some(rec)
    } else {
        None
    }
}

/// The desktop token (`STATE/token`), trimmed; None when absent or empty.
pub fn read_token(state: &Path) -> Option<String> {
    let t = std::fs::read_to_string(state.join("token")).ok()?;
    let t = t.trim().to_string();
    if t.is_empty() {
        None
    } else {
        Some(t)
    }
}

fn percent_encode(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for b in s.bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => out.push(b as char),
            _ => out.push_str(&format!("%{b:02X}")),
        }
    }
    out
}

/// The page with its desktop token, on loopback only.
pub fn page_url(port: u16, token: &str) -> String {
    format!("http://127.0.0.1:{port}/?t={}", percent_encode(token))
}

/// The server's command-line entry point on `PATH`, if installed.
pub fn find_server_exe(path_env: &str) -> Option<PathBuf> {
    path_env
        .split(':')
        .filter(|d| !d.is_empty())
        .map(|d| Path::new(d).join(SERVER_EXE))
        .find(|p| p.is_file())
}

/// What the shell does next, decided from the state folder and `PATH`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Plan {
    /// A server runs: open its page.
    Attach { port: u16 },
    /// No server runs and the entry point exists: start it, then open the page.
    Start { exe: PathBuf },
    /// No server and nothing to start: show the install page.
    Missing,
}

pub fn plan(state: &Path, path_env: &str) -> Plan {
    if let Some(rec) = live_record(state) {
        return Plan::Attach { port: rec.port };
    }
    match find_server_exe(path_env) {
        Some(exe) => Plan::Start { exe },
        None => Plan::Missing,
    }
}

/// Poll `state` until a live server record and its token exist, or `deadline` passes:
/// the port and token to open, or None. `tick` is the pause between looks.
pub fn wait_for_server(state: &Path, deadline: std::time::Duration, tick: std::time::Duration) -> Option<(u16, String)> {
    let start = std::time::Instant::now();
    loop {
        if let (Some(rec), Some(token)) = (live_record(state), read_token(state)) {
            return Some((rec.port, token));
        }
        if start.elapsed() >= deadline {
            return None;
        }
        std::thread::sleep(tick);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    fn tmp(name: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("metabot-shell-core-{}-{}", std::process::id(), name));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    #[test]
    fn a_record_needs_a_pid_and_a_port() {
        let rec = parse_record("{\"pid\": 4242, \"port\": 8460, \"build\": \"0.1.0.dev0\"}").unwrap();
        assert_eq!(rec, Record { pid: 4242, port: 8460, build: "0.1.0.dev0".into() });
        assert_eq!(parse_record("{\"pid\": 4242}"), None);
        assert_eq!(parse_record("{\"pid\": 0, \"port\": 8460}"), None);
        assert_eq!(parse_record("{\"pid\": 1, \"port\": 70000}"), None);
        assert_eq!(parse_record("not json"), None);
        assert_eq!(parse_record("{\"port\":1,\"pid\":2}").unwrap().build, "");
    }

    #[test]
    fn the_state_folder_is_found_the_way_the_server_finds_it() {
        let home = "/srv/someone";
        assert_eq!(state_dir(Some("/tmp/st"), None, home), PathBuf::from("/tmp/st"));
        assert_eq!(state_dir(Some("~/st"), None, home), PathBuf::from("/srv/someone/st"));
        assert_eq!(state_dir(None, Some("{\"state_dir\": \"~/.local/state/other\"}"), home),
                   PathBuf::from("/srv/someone/.local/state/other"));
        assert_eq!(state_dir(None, Some("{\"port\": 1}"), home), PathBuf::from("/srv/someone/.local/state/metachat"));
        assert_eq!(state_dir(Some(""), None, home), PathBuf::from("/srv/someone/.local/state/metachat"));
        assert_eq!(config_path(None, home), PathBuf::from("/srv/someone/.config/metachat/config.json"));
        assert_eq!(config_path(Some("/etc/mb.json"), home), PathBuf::from("/etc/mb.json"));
    }

    #[test]
    fn page_url_carries_the_token_on_loopback_only() {
        assert_eq!(page_url(8460, "abc-123"), "http://127.0.0.1:8460/?t=abc-123");
        assert_eq!(page_url(1, "a b&c"), "http://127.0.0.1:1/?t=a%20b%26c");
    }

    #[test]
    fn a_record_left_by_a_dead_process_is_not_believed() {
        let d = tmp("dead");
        std::fs::write(d.join("server.json"), "{\"pid\": 4194304, \"port\": 8460}").unwrap(); // past PID_MAX: never alive
        assert_eq!(live_record(&d), None);
        let me = std::process::id();
        std::fs::write(d.join("server.json"), format!("{{\"pid\": {me}, \"port\": 8460}}")).unwrap();
        assert_eq!(live_record(&d).map(|r| r.port), Some(8460));
        assert_eq!(read_token(&d), None);
        std::fs::write(d.join("token"), "  tok-1\n").unwrap();
        assert_eq!(read_token(&d).as_deref(), Some("tok-1"));
    }

    #[test]
    fn the_plan_attaches_then_starts_then_says_missing() {
        let d = tmp("plan");
        let bin = tmp("plan-bin");
        assert_eq!(plan(&d, bin.to_str().unwrap()), Plan::Missing);
        let exe = bin.join(SERVER_EXE);
        std::fs::write(&exe, "#!/bin/sh\n").unwrap();
        std::fs::set_permissions(&exe, std::fs::Permissions::from_mode(0o755)).unwrap();
        assert_eq!(plan(&d, &format!("/nowhere:{}", bin.display())), Plan::Start { exe: exe.clone() });
        let me = std::process::id();
        std::fs::write(d.join("server.json"), format!("{{\"pid\": {me}, \"port\": 8461}}")).unwrap();
        assert_eq!(plan(&d, bin.to_str().unwrap()), Plan::Attach { port: 8461 });
    }

    #[test]
    fn waiting_for_a_server_returns_its_port_and_token_once_both_exist_and_gives_up_on_time() {
        let d = tmp("wait");
        let t0 = std::time::Instant::now();
        assert_eq!(wait_for_server(&d, std::time::Duration::from_millis(120), std::time::Duration::from_millis(20)), None);
        assert!(t0.elapsed() >= std::time::Duration::from_millis(120));
        let d2 = d.clone();
        let writer = std::thread::spawn(move || {
            std::thread::sleep(std::time::Duration::from_millis(60));
            std::fs::write(d2.join("server.json"), format!("{{\"pid\":{},\"port\":8123,\"build\":\"t\"}}", std::process::id())).unwrap();
            std::fs::write(d2.join("token"), "tok-1\n").unwrap();
        });
        let got = wait_for_server(&d, std::time::Duration::from_secs(5), std::time::Duration::from_millis(10));
        writer.join().unwrap();
        assert_eq!(got, Some((8123, "tok-1".to_string())));
    }
}
