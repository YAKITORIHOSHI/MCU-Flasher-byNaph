//! Pure transport-selection parity with four target_profile.py helpers.
//!
//! This does not validate target identity, installed toolchain preparation,
//! Arduino CLI proof or INI safety. Production must retain those separate gates.

use serde_json::{Map, Value};
use std::fmt;

/// Existing application policy, main/core/constants.py::MAX_BAUD_RATE.
pub const MAX_BAUD_RATE: u64 = 921_600;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct TargetTransportError {
    pub field: &'static str,
    pub message: &'static str,
}

impl fmt::Display for TargetTransportError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "{}: {}", self.field, self.message)
    }
}

impl std::error::Error for TargetTransportError {}

pub fn requires_upload_port(info: &Value) -> Result<bool, TargetTransportError> {
    let info = object(info)?;
    if let Some(Value::Bool(declared)) = info.get("require_upload_port") {
        return Ok(*declared);
    }
    // Non-string JSON values cannot stringify to one of these protocol names.
    // Other/custom programmer names remain supported and conservatively need a
    // port unless their manifest declares an explicit boolean override.
    let protocol = info
        .get("upload_protocol")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_lowercase();
    Ok(!matches!(
        protocol.as_str(),
        "stlink"
            | "jlink"
            | "cmsis-dap"
            | "dfu"
            | "picotool"
            | "mbed"
            | "uf2"
            | "teensy-cli"
            | "teensy-gui"
            | "micronucleus"
            | "wch-link"
            | "nrfjprog"
            | "atmel-ice"
            | "usbasp"
            | "usbtiny"
    ))
}

pub fn serial_upload_speed(
    info: &Value,
    requested_speed: &Value,
) -> Result<String, TargetTransportError> {
    let fields = object(info)?;
    if fields.get("backend").and_then(Value::as_str) == Some("arduino-cli")
        || !requires_upload_port(info)?
    {
        return Ok(String::new());
    }
    let esp = fields
        .get("platform")
        .and_then(Value::as_str)
        .is_some_and(|platform| {
            matches!(
                platform.to_lowercase().as_str(),
                "espressif32" | "espressif8266"
            )
        });
    let value = if esp {
        requested_speed
    } else {
        fields.get("upload_speed").unwrap_or(&Value::Null)
    };
    Ok(match baud_value(value) {
        Some(speed) => (if esp { speed.min(MAX_BAUD_RATE) } else { speed }).to_string(),
        None => String::new(),
    })
}

pub fn upload_configuration(
    info: &Value,
    requested_speed: &Value,
) -> Result<String, TargetTransportError> {
    let fields = object(info)?;
    let protocol = match fields.get("upload_protocol") {
        Some(Value::String(value)) => python_strip(value),
        Some(value) if python_truthy(value) => {
            // Deliberate structural refusal: never format Python object/float
            // representations into generated INI. Valid string manifests retain
            // their exact existing behavior; target_problem remains unmigrated.
            return Err(TargetTransportError {
                field: "upload_protocol",
                message: "Expected a string protocol or an empty value",
            });
        }
        _ => "",
    };
    let mut output = String::new();
    if !protocol.is_empty() {
        output.push_str("upload_protocol = ");
        output.push_str(protocol);
        output.push('\n');
    }
    // Preserve the original case-sensitive configuration condition, even though
    // serial_upload_speed recognizes ESP platform names case-insensitively.
    if matches!(
        fields.get("platform").and_then(Value::as_str),
        Some("espressif32" | "espressif8266")
    ) && requires_upload_port(info)?
    {
        let speed = serial_upload_speed(info, requested_speed)?;
        if !speed.is_empty() {
            output.push_str("upload_speed = ");
            output.push_str(&speed);
            output.push('\n');
        }
    }
    Ok(output)
}

pub fn upload_target_ready(info: &Value, port: &Value) -> Result<bool, TargetTransportError> {
    let fields = object(info)?;
    Ok(python_truthy(port)
        || (fields.get("board").is_some_and(python_truthy)
            && fields.get("pio_resolved") != Some(&Value::Bool(false))
            && !requires_upload_port(info)?))
}

fn object(info: &Value) -> Result<&Map<String, Value>, TargetTransportError> {
    info.as_object().ok_or(TargetTransportError {
        field: "info",
        message: "Expected a JSON target-definition object",
    })
}

fn python_truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(value) => *value,
        Value::Number(value) => value.as_f64().is_some_and(|number| number != 0.0),
        Value::String(value) => !value.is_empty(),
        Value::Array(value) => !value.is_empty(),
        Value::Object(value) => !value.is_empty(),
    }
}

fn python_strip(text: &str) -> &str {
    // Python additionally strips the four ASCII information separators.
    text.trim_matches(|character: char| {
        character.is_whitespace() || matches!(character, '\u{1c}'..='\u{1f}')
    })
}

fn baud_value(value: &Value) -> Option<u64> {
    let speed = match value {
        Value::String(value) => {
            let value = python_strip(value);
            if value.is_empty()
                || value.len() > 10
                || !value.bytes().all(|byte| byte.is_ascii_digit())
            {
                return None;
            }
            value.parse().ok()?
        }
        // A JSON float remains a Python float ("115200.0"), hence is not a
        // digit-only baud. Do not truncate floats or reinterpret booleans.
        Value::Number(value) => value.as_u64()?,
        _ => return None,
    };
    (speed > 0 && speed <= 9_999_999_999).then_some(speed)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn programmer_transports_and_dynamic_overrides_are_preserved() {
        for protocol in [
            "stlink",
            "jlink",
            "cmsis-dap",
            "dfu",
            "picotool",
            "mbed",
            "uf2",
            "teensy-cli",
            "teensy-gui",
            "micronucleus",
            "wch-link",
            "nrfjprog",
            "atmel-ice",
            "usbasp",
            "usbtiny",
        ] {
            let info = json!({"board":"custom.board_42","upload_protocol":protocol});
            assert!(!requires_upload_port(&info).unwrap());
            assert!(upload_target_ready(&info, &json!("")).unwrap());
        }
        assert!(requires_upload_port(&json!({"upload_protocol":"future-programmer"})).unwrap());
        assert!(!requires_upload_port(
            &json!({"upload_protocol":"future-programmer","require_upload_port":false})
        )
        .unwrap());
        assert!(
            requires_upload_port(&json!({"upload_protocol":"dfu","require_upload_port":true}))
                .unwrap()
        );
        assert!(requires_upload_port(&json!({"upload_protocol":" dfu "})).unwrap());
    }

    #[test]
    fn baud_policy_preserves_board_defaults_and_caps_only_esp_requests() {
        let avr = json!({"platform":"atmelavr","upload_speed":57600,"upload_protocol":"arduino"});
        assert_eq!(
            serial_upload_speed(&avr, &json!(3_000_000)).unwrap(),
            "57600"
        );
        assert_eq!(
            upload_configuration(&avr, &json!(3_000_000)).unwrap(),
            "upload_protocol = arduino\n"
        );
        let esp =
            json!({"platform":"espressif32","upload_speed":115200,"upload_protocol":"esptool"});
        assert_eq!(
            serial_upload_speed(&esp, &json!(3_000_000)).unwrap(),
            "921600"
        );
        assert_eq!(
            upload_configuration(&esp, &json!(3_000_000)).unwrap(),
            "upload_protocol = esptool\nupload_speed = 921600\n"
        );
        assert_eq!(serial_upload_speed(&esp, &json!("")).unwrap(), "");
        let cli = json!({"platform":"espressif32","backend":"arduino-cli"});
        assert_eq!(serial_upload_speed(&cli, &json!(115200)).unwrap(), "");
        let usb = json!({"platform":"espressif32","upload_protocol":"dfu"});
        assert_eq!(serial_upload_speed(&usb, &json!(115200)).unwrap(), "");
    }

    #[test]
    fn python_numeric_text_and_boolean_identity_boundaries_are_retained() {
        let esp = json!({"platform":"ESPRESSIF32","upload_protocol":"esptool"});
        assert_eq!(
            serial_upload_speed(&esp, &json!("\u{1c}0921601\u{1f}")).unwrap(),
            "921600"
        );
        assert_eq!(
            upload_configuration(&esp, &json!(115200)).unwrap(),
            "upload_protocol = esptool\n"
        );
        for speed in [
            json!(115200.0),
            json!(true),
            json!([115200]),
            json!("١١٥٢٠٠"),
            json!("10000000000"),
        ] {
            assert_eq!(serial_upload_speed(&esp, &speed).unwrap(), "");
        }
        assert!(upload_target_ready(
            &json!({"board":"future","pio_resolved":0,"upload_protocol":"dfu"}),
            &json!("")
        )
        .unwrap());
        assert!(!upload_target_ready(
            &json!({"board":"future","pio_resolved":false,"upload_protocol":"dfu"}),
            &json!("")
        )
        .unwrap());
        assert!(upload_target_ready(&json!({}), &json!("COM9")).unwrap());
    }

    #[test]
    fn invalid_structure_is_reported_without_stringifying_container_ini_values() {
        assert_eq!(requires_upload_port(&json!([])).unwrap_err().field, "info");
        assert_eq!(
            upload_configuration(&json!({"upload_protocol":["dfu"]}), &json!(""))
                .unwrap_err()
                .field,
            "upload_protocol"
        );
        assert!(requires_upload_port(&json!({"upload_protocol":["dfu"]})).unwrap());
        assert_eq!(
            upload_configuration(&json!({"upload_protocol":[]}), &json!("")).unwrap(),
            ""
        );
    }
}
