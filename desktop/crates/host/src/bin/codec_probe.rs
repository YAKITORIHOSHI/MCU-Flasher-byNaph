//! Hardware-free differential harness. A bounded JSON request names only byte
//! chunks; no path, process command, device or settings operation is accepted.
use mcu_core::serial_text::SerialTextDecoder;
use std::io::Read;

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut raw = Vec::new();
    std::io::stdin().take(2_000_001).read_to_end(&mut raw)?;
    if raw.len() > 2_000_000 {
        return Err("Codec fixture input exceeds limit".into());
    }
    let cases: Vec<Vec<Vec<u8>>> = serde_json::from_slice(&raw)?;
    if cases.len() > 2048 {
        return Err("Too many codec cases".into());
    }
    let mut results = Vec::new();
    for chunks in cases {
        let mut decoder = SerialTextDecoder::new();
        let mut output = String::new();
        for chunk in chunks {
            output.push_str(&decoder.feed(&chunk, false));
            if decoder.buffered_bytes() > 3 {
                return Err("Decoder retained excess state".into());
            }
        }
        output.push_str(&decoder.feed(&[], true));
        results.push(serde_json::json!({"output":output,"invalidBytes":decoder.invalid_bytes(),"bufferedBytes":decoder.buffered_bytes()}));
    }
    println!("{}", serde_json::to_string(&results)?);
    Ok(())
}
