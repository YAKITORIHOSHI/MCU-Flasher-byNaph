//! Behavior-preserving port of `main/core/serial_stream.py::SerialTextDecoder`.
//! This decoder performs no device I/O and retains at most three UTF-8 bytes
//! plus one CR, regardless of how long a device runs without a newline.

#[derive(Default)]
pub struct SerialTextDecoder {
    pending: Vec<u8>,
    pending_cr: bool,
    invalid_bytes: u64,
}

impl SerialTextDecoder {
    pub fn new() -> Self {
        Self::default()
    }
    pub fn invalid_bytes(&self) -> u64 {
        self.invalid_bytes
    }
    pub fn buffered_bytes(&self) -> usize {
        self.pending.len()
    }

    pub fn feed(&mut self, data: &[u8], final_chunk: bool) -> String {
        let mut bytes = std::mem::take(&mut self.pending);
        bytes.extend_from_slice(data);
        let mut output = String::with_capacity(bytes.len());
        let mut cursor = 0;
        while cursor < bytes.len() {
            match std::str::from_utf8(&bytes[cursor..]) {
                Ok(text) => {
                    for character in text.chars() {
                        self.append_character(character, &mut output);
                    }
                    break;
                }
                Err(error) => {
                    let valid_end = cursor + error.valid_up_to();
                    let valid = std::str::from_utf8(&bytes[cursor..valid_end])
                        .expect("validated UTF-8 prefix");
                    for character in valid.chars() {
                        self.append_character(character, &mut output);
                    }
                    cursor = valid_end;
                    match error.error_len() {
                        Some(count) => {
                            for byte in &bytes[cursor..cursor + count] {
                                self.append_invalid(*byte, &mut output);
                            }
                            cursor += count;
                        }
                        None if final_chunk => {
                            for byte in &bytes[cursor..] {
                                self.append_invalid(*byte, &mut output);
                            }
                            break;
                        }
                        None => {
                            self.pending.extend_from_slice(&bytes[cursor..]);
                            break;
                        }
                    }
                }
            }
        }
        if final_chunk && self.pending_cr {
            output.push_str("\\x0d");
            self.pending_cr = false;
        }
        debug_assert!(self.pending.len() <= 3);
        output
    }

    fn flush_cr(&mut self, next: Option<char>, output: &mut String) -> bool {
        if !self.pending_cr {
            return false;
        }
        self.pending_cr = false;
        if next == Some('\n') {
            output.push('\n');
            true
        } else {
            output.push_str("\\x0d");
            false
        }
    }

    fn append_invalid(&mut self, byte: u8, output: &mut String) {
        use std::fmt::Write;
        self.flush_cr(None, output);
        self.invalid_bytes = self.invalid_bytes.saturating_add(1);
        write!(output, "\\x{byte:02x}").expect("String formatting is infallible");
    }

    fn append_character(&mut self, character: char, output: &mut String) {
        use std::fmt::Write;
        if self.flush_cr(Some(character), output) {
            return;
        }
        let code = character as u32;
        if character == '\r' {
            self.pending_cr = true;
        } else if code < 32 && !matches!(character, '\n' | '\t' | '\x1b' | '\x07') || code == 127 {
            write!(output, "\\x{code:02x}").expect("String formatting is infallible");
        } else if (0x80..=0x9f).contains(&code) || matches!(code, 0x2028 | 0x2029) {
            write!(output, "\\u{code:04x}").expect("String formatting is infallible");
        } else {
            output.push(character);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn unicode_and_crlf_survive_all_byte_boundaries() {
        let bytes = "café 日本語 Ω\r\nline\rnext\r".as_bytes();
        for split in 0..=bytes.len() {
            let mut decoder = SerialTextDecoder::new();
            let first = decoder.feed(&bytes[..split], false);
            assert!(decoder.buffered_bytes() <= 3);
            let result = first + &decoder.feed(&bytes[split..], true);
            assert_eq!(result, "café 日本語 Ω\nline\\x0dnext\\x0d");
            assert_eq!(decoder.invalid_bytes(), 0);
        }
    }
    #[test]
    fn invalid_bytes_and_unicode_controls_are_distinct() {
        let mut decoder = SerialTextDecoder::new();
        assert_eq!(decoder.feed(&[0xff, 0x80, 0xc2], false), "\\xff\\x80");
        assert_eq!(
            decoder.feed(&[0x85, 0xe2, 0x80, 0xa8, 0x00, 0x7f], true),
            "\\u0085\\u2028\\x00\\x7f"
        );
        assert_eq!(decoder.invalid_bytes(), 2);
        assert_eq!(decoder.feed(&[0xf0, 0x9f], true), "\\xf0\\x9f");
        assert_eq!(decoder.invalid_bytes(), 4);
    }
    #[test]
    fn long_unterminated_stream_retains_no_history() {
        let mut decoder = SerialTextDecoder::new();
        for _ in 0..10_000 {
            assert_eq!(
                decoder.feed(b"hello\x1b[31m\x07\t", false),
                "hello\x1b[31m\x07\t"
            );
            assert_eq!(decoder.buffered_bytes(), 0);
        }
    }
}
