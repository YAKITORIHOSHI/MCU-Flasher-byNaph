//! Development-only framed fixture observer. No tool, build, package or hardware
//! operation is exposed. Root authority is a native command-line argument.
use mcu_core::board_compatibility::{NativeHost, NativeRecipeSelection};
use mcu_host::recipe_snapshot::{FixtureRecipeAuthority, RecipeGeneration};
use serde::Deserialize;
use serde_json::{json, Value};
use std::io::{self, BufRead, Read, Write};
use std::path::{Path, PathBuf};
use std::sync::mpsc;
use std::time::Duration;

#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
enum Target {
    Onehorse,
    Minimain,
    Q,
}
#[derive(Debug, Deserialize)]
#[serde(tag = "action", rename_all = "snake_case", deny_unknown_fields)]
enum Request {
    Capture { target: Target },
    Revalidate { generation: u64, target: Target },
    Clear {},
    Exit {},
}

const FRAME_LIMIT: usize = 8192;
const COMMAND_LIMIT: usize = 64;
const INPUT_TIMEOUT: Duration = Duration::from_secs(10);

fn parse_request(raw: &[u8]) -> Result<Request, ()> {
    serde_json::from_slice(raw).map_err(|_| ())
}
fn parameters(target: Target) -> (&'static str, &'static str) {
    match target {
        Target::Onehorse => ("espressif32", "onehorse32dev"),
        Target::Minimain => ("espressif32", "minimain_esp32s2"),
        Target::Q => ("arduinoq", "uno_q"),
    }
}
fn manifest(root: &Path, target: Target) -> PathBuf {
    let (platform, board) = parameters(target);
    root.join("core/platforms")
        .join(platform)
        .join("boards")
        .join(format!("{board}.json"))
}
fn refused(reason: &str) -> Value {
    json!({"status":"refused", "refusal":reason})
}
fn observed(status: &str, receipt: &RecipeGeneration) -> Value {
    json!({"status": status, "generation": receipt.generation(),
        "rule": receipt.decision().rule, "options": receipt.decision().options,
        "files": receipt.files(), "directories": receipt.directories(), "bytesRead": receipt.bytes_read()})
}
struct Session {
    root: PathBuf,
    authority: FixtureRecipeAuthority,
    receipt: Option<(Target, RecipeGeneration)>,
}
impl Session {
    fn clear(&mut self) {
        self.receipt = None;
        self.authority.clear();
    }
    fn request(&mut self, request: Request) -> Value {
        let target = match request {
            Request::Capture { target } | Request::Revalidate { target, .. } => target,
            Request::Clear {} => {
                self.clear();
                return json!({"status":"cleared"});
            }
            Request::Exit {} => {
                self.clear();
                return json!({"status":"bye"});
            }
        };
        let path = manifest(&self.root, target);
        let (platform, board) = parameters(target);
        let selection = NativeRecipeSelection {
            backend: None,
            platform,
            board,
            framework: "arduino",
            manifest: &path,
        };
        let host = if cfg!(target_os = "linux") {
            NativeHost::Linux
        } else if cfg!(windows) {
            NativeHost::Windows
        } else {
            NativeHost::Unsupported
        };
        match request {
            Request::Capture { .. } => {
                self.receipt = None;
                match self.authority.capture(&selection, host) {
                    Ok(receipt) => {
                        let response = observed("captured", &receipt);
                        self.receipt = Some((target, receipt));
                        response
                    }
                    Err(refusal) => refused(refusal.name()),
                }
            }
            Request::Revalidate { generation, .. } => {
                let result = match &self.receipt {
                    None => refused("StaleGeneration"),
                    Some((_, receipt)) if generation != receipt.generation() => {
                        refused("StaleGeneration")
                    }
                    Some((selected, _)) if target != *selected => refused("SelectionChanged"),
                    Some((_, receipt)) => match self.authority.revalidate(
                        receipt,
                        &selection,
                        host,
                        receipt.decision(),
                    ) {
                        Ok(()) => observed("matches", receipt),
                        Err(refusal) => refused(refusal.name()),
                    },
                };
                if result["status"] == "refused" {
                    self.clear();
                }
                result
            }
            Request::Clear {} | Request::Exit {} => unreachable!(),
        }
    }
}
fn send(output: &mut impl Write, response: &Value) -> io::Result<()> {
    let raw = serde_json::to_vec(response)?;
    if raw.len() >= FRAME_LIMIT {
        return Err(io::Error::other("Fixture response exceeds bound"));
    }
    output.write_all(&raw)?;
    output.write_all(b"\n")?;
    output.flush()
}
fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<_> = std::env::args_os().collect();
    if args.len() != 3 || args[1] != "--fixture-root" {
        return Err("Use --fixture-root with an existing owned audit scratch directory".into());
    }
    let checkout = Path::new(env!("CARGO_MANIFEST_DIR"))
        .ancestors()
        .nth(3)
        .ok_or("Missing checkout")?;
    let root = PathBuf::from(&args[2]);
    let authority = FixtureRecipeAuthority::new(checkout, &root)
        .map_err(|r| format!("Invalid native fixture: {}", r.name()))?;
    let mut session = Session {
        root,
        authority,
        receipt: None,
    };
    // A bounded channel prevents an eager stdin producer accumulating frames.
    // An idle input worker is abandoned only as this fixed process exits; no
    // detached native worker survives the probe. Blocking stdout consumers and
    // filesystem-driver calls still need an external process deadline.
    let (sender, receiver) = mpsc::sync_channel(1);
    std::thread::spawn(move || {
        let mut input = io::BufReader::new(io::stdin());
        for _ in 0..=COMMAND_LIMIT {
            let mut raw = Vec::new();
            let read = (&mut input)
                .take(FRAME_LIMIT as u64 + 1)
                .read_until(b'\n', &mut raw);
            let frame = match read {
                Ok(0) => return,
                Ok(_) if raw.len() <= FRAME_LIMIT => Ok(raw),
                _ => Err(()),
            };
            let stop = frame.is_err();
            if sender.send(frame).is_err() || stop {
                return;
            }
        }
    });
    let mut output = io::stdout().lock();
    for index in 0..=COMMAND_LIMIT {
        let raw = match receiver.recv_timeout(INPUT_TIMEOUT) {
            Ok(Ok(raw)) => raw,
            Ok(Err(())) => {
                session.clear();
                send(&mut output, &refused("InvalidRequest"))?;
                return Ok(());
            }
            Err(mpsc::RecvTimeoutError::Disconnected) => return Ok(()),
            Err(mpsc::RecvTimeoutError::Timeout) => {
                session.clear();
                send(&mut output, &refused("Timeout"))?;
                return Ok(());
            }
        };
        if index == COMMAND_LIMIT {
            session.clear();
            send(&mut output, &refused("TooLarge"))?;
            return Ok(());
        }
        let request = match parse_request(&raw) {
            Ok(request) => request,
            Err(()) => {
                session.clear();
                send(&mut output, &refused("InvalidRequest"))?;
                continue;
            }
        };
        let exit = matches!(request, Request::Exit {});
        send(&mut output, &session.request(request))?;
        if exit {
            return Ok(());
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn protocol_requires_known_target_and_exact_unsigned_generation() {
        assert!(matches!(
            parse_request(br#"{"action":"capture","target":"onehorse"}"#),
            Ok(Request::Capture {
                target: Target::Onehorse
            })
        ));
        assert!(matches!(
            parse_request(br#"{"action":"revalidate","generation":1,"target":"q"}"#),
            Ok(Request::Revalidate {
                generation: 1,
                target: Target::Q
            })
        ));
        for raw in [
            br#"{"action":"revalidate","generation":true,"target":"q"}"#.as_slice(),
            br#"{"action":"revalidate","generation":"1","target":"q"}"#,
            br#"{"action":"revalidate","generation":-1,"target":"q"}"#,
            br#"{"action":"capture","target":"../../outside"}"#,
            br#"{"action":"capture","target":"onehorse","options":{}}"#,
            br#"{"action":"clear","root":"outside"}"#,
            b"\xff",
            b"{",
        ] {
            assert!(parse_request(raw).is_err(), "{raw:?}");
        }
    }
    #[test]
    fn fixed_native_targets_select_only_known_fixture_leafs() {
        let root = Path::new("fixture");
        assert_eq!(
            manifest(root, Target::Onehorse),
            root.join("core/platforms/espressif32/boards/onehorse32dev.json")
        );
        assert_eq!(
            manifest(root, Target::Minimain),
            root.join("core/platforms/espressif32/boards/minimain_esp32s2.json")
        );
        assert_eq!(
            manifest(root, Target::Q),
            root.join("core/platforms/arduinoq/boards/uno_q.json")
        );
    }
}
