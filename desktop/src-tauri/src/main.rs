//! Cacophony as a desktop application (design document section 41).
//!
//! > Cacophony should eventually feel like a desktop application while
//! > retaining web architecture. Tauri is preferable if practical because the
//! > application primarily needs to host the web UI while the Python backend
//! > performs generation. However, web deployment should remain possible.
//!
//! That last sentence is why this file is short. There is no desktop *mode* in
//! the backend: this spawns the same server `cacophony serve` runs, waits for it
//! to say where it landed, and opens a window there. Everything the application
//! does, it does over the same API a browser would use.
//!
//! Three things this has to get right, and each is a way a desktop application
//! goes wrong that a served one does not.
//!
//! **Wait for the handshake, do not guess.** The backend binds a port the
//! operating system chose and prints one line of JSON once it is listening. A
//! shell that opened a window on a guessed URL would show an error page on slow
//! machines and work on fast ones, which is the worst kind of bug.
//!
//! **Kill the backend when the window closes.** A generator left running after
//! its window is gone is invisible, still writing files, still occupying a model
//! server. The child is killed on exit, and the backend independently watches
//! its own stdin - so it also dies if *this* process is killed outright and
//! never gets to run its exit handler.
//!
//! **Say something useful when the backend is missing.** The single most likely
//! failure in a hand-built checkout is that `cacophony` is not on PATH. That
//! gets a window with a sentence, not a silent exit.

use std::io::{BufRead, BufReader};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;

use tauri::{Manager, WindowEvent};

/// What the backend prints when it is ready. Must match `HANDSHAKE_PREFIX` in
/// `cacophony/desktop/sidecar.py`.
const HANDSHAKE_PREFIX: &str = "CACOPHONY_HANDSHAKE ";

/// The handshake version this shell understands. A backend that speaks a newer
/// one is refused rather than misread.
const HANDSHAKE_VERSION: u64 = 1;

/// How long to wait for the backend to announce itself. Generous: a cold start
/// on a slow disk imports a good deal of Python.
const STARTUP_TIMEOUT_SECS: u64 = 60;

struct Backend(Mutex<Option<Child>>);

impl Drop for Backend {
    fn drop(&mut self) {
        if let Ok(mut guard) = self.0.lock() {
            if let Some(mut child) = guard.take() {
                let _ = child.kill();
                let _ = child.wait();
            }
        }
    }
}

struct Ready {
    url: String,
    token: String,
}

/// Drop the library paths of a snap we are not part of.
///
/// Launching this from the terminal inside a snap-confined editor - VS Code
/// being the common one - inherits that snap's environment: `GTK_PATH`,
/// `LOCPATH`, `GIO_MODULE_DIR` and friends all point into `/snap/...`. WebKit's
/// helper processes then load the snap's libraries in preference to the
/// system's, and die on the mismatch:
///
/// ```text
/// WebKitNetworkProcess: symbol lookup error:
///   /snap/core20/current/lib/x86_64-linux-gnu/libpthread.so.0:
///   undefined symbol: __libc_pthread_init, version GLIBC_PRIVATE
/// ERROR: WebKit encountered an internal error. This is a WebKit bug.
/// ```
///
/// It is not a WebKit bug. It is a program being told to use another
/// application's C library.
///
/// Only when the environment is lying: `SNAP` is set and this executable is not
/// inside `/snap`, which means the variables describe somebody else's
/// confinement rather than ours. A genuine snap build of this application is
/// left alone.
#[cfg(target_os = "linux")]
fn escape_somebody_elses_snap() {
    let confined = std::env::var_os("SNAP").is_some();
    let ours = std::env::current_exe()
        .map(|path| path.starts_with("/snap/"))
        .unwrap_or(false);
    if !confined || ours {
        return;
    }

    for name in [
        "GTK_PATH",
        "GIO_MODULE_DIR",
        "GSETTINGS_SCHEMA_DIR",
        "GDK_PIXBUF_MODULEDIR",
        "GDK_PIXBUF_MODULE_FILE",
        "LOCPATH",
        "GTK_IM_MODULE_FILE",
    ] {
        std::env::remove_var(name);
    }

    // These are lists, and only the snap's entries are the problem: dropping
    // the rest would take the system's own directories with them.
    for name in ["LD_LIBRARY_PATH", "XDG_DATA_DIRS"] {
        if let Ok(value) = std::env::var(name) {
            let kept: Vec<&str> = value
                .split(':')
                .filter(|entry| !entry.is_empty() && !entry.starts_with("/snap/"))
                .collect();
            if kept.is_empty() {
                std::env::remove_var(name);
            } else {
                std::env::set_var(name, kept.join(":"));
            }
        }
    }
}

#[cfg(not(target_os = "linux"))]
fn escape_somebody_elses_snap() {}

/// Turn off the WebKit renderer that crashes, unless somebody asked for it.
///
/// WebKitGTK's DMA-BUF renderer segfaults the web process on a good number of
/// Linux graphics stacks - drivers, compositors and virtual displays alike -
/// and what a user sees is a window containing "WebKit encountered an internal
/// error", which says nothing about any of this. It is the single most common
/// complaint about Tauri applications on Linux.
///
/// Measured rather than assumed, on the machine this was written on: an AMD
/// card with Mesa under Wayland, and a virtual X display with no DRI3, both
/// segfault with the renderer on and both run with it off. Forcing software
/// GL instead (`LIBGL_ALWAYS_SOFTWARE`) does *not* help, which is what says the
/// fault is in that renderer rather than in the driver underneath it.
///
/// The cost is some rendering performance for a window that mostly displays
/// forms and tables. Set the variable yourself - to `0`, or anything - and this
/// leaves it alone, because a machine where the fast path works should be
/// allowed to use it.
#[cfg(target_os = "linux")]
fn survive_webkit() {
    if std::env::var_os("WEBKIT_DISABLE_DMABUF_RENDERER").is_none() {
        std::env::set_var("WEBKIT_DISABLE_DMABUF_RENDERER", "1");
    }
}

#[cfg(not(target_os = "linux"))]
fn survive_webkit() {}

fn main() {
    survive_webkit();
    escape_somebody_elses_snap();
    match start_backend() {
        Ok((child, ready)) => run_window(child, ready),
        Err(message) => {
            // On stderr first, and unconditionally. The window below is a
            // courtesy for somebody who double-clicked an icon; a terminal gets
            // the reason whatever the webview then makes of it. Without this,
            // a backend that could not be found produced no output at all and
            // every symptom was whatever WebKit happened to say - which on a
            // machine where the failure page will not render is "WebKit
            // encountered an internal error", a sentence about nothing.
            eprintln!("cacophony-desktop: {message}");
            run_failure_window(&message);
        }
    }
}

/// Spawn the backend and read its handshake.
fn start_backend() -> Result<(Child, Ready), String> {
    let program = backend_command();
    let mut child = Command::new(&program)
        .arg("desktop")
        // stdin is held open deliberately: the backend watches it and stops
        // when it closes, which is the one shutdown path that survives this
        // process being killed outright.
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit())
        .spawn()
        .map_err(|error| {
            format!(
                "Could not start the Cacophony backend ({program}): {error}.\n\n\
                 Install it with `pip install cacophony[api]`, or set \
                 CACOPHONY_BACKEND to its path."
            )
        })?;

    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| "The backend produced no output.".to_string())?;

    let (sender, receiver) = std::sync::mpsc::channel::<Result<Ready, String>>();
    std::thread::spawn(move || {
        for line in BufReader::new(stdout).lines() {
            let Ok(line) = line else { break };
            if let Some(rest) = line.strip_prefix(HANDSHAKE_PREFIX) {
                let _ = sender.send(parse_handshake(rest));
                return;
            }
            // Anything else is the backend's own logging; pass it through so a
            // developer running the shell from a terminal still sees it.
            eprintln!("{line}");
        }
        let _ = sender.send(Err(
            "The backend stopped before it was ready. Run `cacophony desktop` \
             in a terminal to see why."
                .to_string(),
        ));
    });

    match receiver.recv_timeout(std::time::Duration::from_secs(STARTUP_TIMEOUT_SECS)) {
        Ok(Ok(ready)) => Ok((child, ready)),
        Ok(Err(message)) => {
            let _ = child.kill();
            Err(message)
        }
        Err(_) => {
            let _ = child.kill();
            Err(format!(
                "The backend did not start within {STARTUP_TIMEOUT_SECS} seconds."
            ))
        }
    }
}

/// Which backend to run: an explicit one, a bundled sidecar, or PATH.
fn backend_command() -> String {
    if let Ok(explicit) = std::env::var("CACOPHONY_BACKEND") {
        if !explicit.is_empty() {
            return explicit;
        }
    }
    // A packaged build ships the backend beside the executable as a Tauri
    // sidecar; a developer build finds it on PATH.
    if let Ok(exe) = std::env::current_exe() {
        if let Some(directory) = exe.parent() {
            let bundled = directory.join(if cfg!(windows) {
                "cacophony-backend.exe"
            } else {
                "cacophony-backend"
            });
            if bundled.is_file() {
                return bundled.to_string_lossy().into_owned();
            }
        }
    }
    "cacophony".to_string()
}

fn parse_handshake(payload: &str) -> Result<Ready, String> {
    let value: serde_json::Value =
        serde_json::from_str(payload).map_err(|error| format!("Unreadable handshake: {error}"))?;

    let version = value.get("version").and_then(|item| item.as_u64()).unwrap_or(0);
    if version > HANDSHAKE_VERSION {
        return Err(format!(
            "This backend speaks handshake version {version}; this application understands \
             {HANDSHAKE_VERSION}. Update the desktop application."
        ));
    }

    let url = value
        .get("url")
        .and_then(|item| item.as_str())
        .ok_or_else(|| "The handshake carried no URL.".to_string())?;
    let token = value
        .get("token")
        .and_then(|item| item.as_str())
        .unwrap_or_default();

    Ok(Ready {
        url: url.to_string(),
        token: token.to_string(),
    })
}

fn run_window(child: Child, ready: Ready) {
    // The token travels in the query string; the page reads it once and strips
    // it from the address bar, so it does not end up in a screenshot.
    let target = if ready.token.is_empty() {
        ready.url.clone()
    } else {
        format!("{}/?token={}", ready.url, urlencode(&ready.token))
    };

    tauri::Builder::default()
        .manage(Backend(Mutex::new(Some(child))))
        .setup(move |app| {
            let url = tauri::WebviewUrl::External(target.parse()?);
            tauri::WebviewWindowBuilder::new(app, "main", url)
                .title("Cacophony")
                .inner_size(1440.0, 900.0)
                .min_inner_size(960.0, 600.0)
                .build()?;
            Ok(())
        })
        .on_window_event(|window, event| {
            if let WindowEvent::Destroyed = event {
                // Dropping the managed Backend kills the child. The backend
                // would also notice its stdin closing, but doing both means the
                // process is gone before the window animation finishes.
                let _ = window.app_handle().state::<Backend>();
            }
        })
        .run(tauri::generate_context!())
        .expect("could not start the Cacophony window");
}

/// A window that explains why there is nothing to show.
///
/// The alternative - exiting silently - is what a user experiences as
/// double-clicking an icon and nothing happening.
fn run_failure_window(message: &str) {
    let html = format!(
        "<!doctype html><meta charset=\"utf-8\">\
         <style>\
           body{{background:#12121a;color:#e8e8f0;font:15px/1.6 system-ui,sans-serif;\
                 margin:0;display:grid;place-items:center;height:100vh}}\
           main{{max-width:38rem;padding:2rem}}\
           h1{{font-size:1.1rem;letter-spacing:.14em;text-transform:uppercase;color:#b388ff}}\
           pre{{white-space:pre-wrap;color:#c9c9d6}}\
         </style>\
         <main><h1>Cacophony</h1><pre>{}</pre></main>",
        html_escape(message)
    );
    let data = format!("data:text/html;charset=utf-8,{}", urlencode(&html));

    tauri::Builder::default()
        .setup(move |app| {
            let url = tauri::WebviewUrl::External(data.parse()?);
            tauri::WebviewWindowBuilder::new(app, "main", url)
                .title("Cacophony")
                .inner_size(720.0, 420.0)
                .build()?;
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("could not show the Cacophony error window; the reason is on stderr above");
}

fn html_escape(text: &str) -> String {
    text.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
}

/// Percent-encode everything that is not unreserved, so a token containing
/// `-` or `_` survives and anything else cannot break out of the query string.
fn urlencode(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for byte in text.as_bytes() {
        match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                out.push(*byte as char)
            }
            _ => out.push_str(&format!("%{byte:02X}")),
        }
    }
    out
}
