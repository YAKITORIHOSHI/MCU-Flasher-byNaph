//! Pure exact-board recipe policy. Inputs come from a future trusted reader;
//! this module grants no filesystem, package, target-selection or tool authority.
use serde_json::Value;
use std::collections::BTreeMap;

pub struct RecipeInput<'a> {
    pub platform_name: &'a str,
    pub board_id: &'a str,
    pub framework: &'a str,
    pub board: &'a Value,
    pub platform: &'a Value,
    pub framework_package: &'a Value,
    pub sdk_package: &'a Value,
    pub board_sha256: &'a str,
}

#[derive(Debug, Default, PartialEq, Eq)]
pub struct RecipeDecision {
    pub rule: Option<&'static str>,
    pub options: BTreeMap<&'static str, &'static str>,
}

const ARDUINO_PACKAGE: &str = "framework-arduinoespressif32";
const ONEHORSE_SHA256: &str = "483741724c41624f1be320ef09ec3396fc0f34d7d0bc633b092e257cd621990b";
const MINIMAIN_SHA256: &str = "e9a066074564a920d0e9df6761ad366b5ae32755a67beb32e1ad5f307ceafe9c";
const MINIMAIN_FLAGS: [&str; 6] = [
    "-DARDUINO_DEPARTMENT_OF_ALCHEMY_MINIMAIN_ESP32S2",
    "-DARDUINO_RUNNING_CORE=1",
    "-DARDUINO_EVENT_RUNNING_CORE=1",
    "-DBOARD_HAS_PSRAM",
    "-DARDUINO_USB_MODE=1",
    "-DARDUINO_USB_CDC_ON_BOOT=1",
];

/// Return only the exact reviewed overrides and a rule for nonempty results.
/// Unknown/new recipes retain their native behavior. Arduino Q reader/hook
/// selection is deliberately outside this pure policy's scope.
pub fn recipe_options(input: &RecipeInput<'_>) -> RecipeDecision {
    let (sdk_name, sha256, rule) = match (input.platform_name, input.board_id, input.framework) {
        ("espressif32", "onehorse32dev", "arduino") => (
            "framework-arduinoespressif32-esp32-libs",
            ONEHORSE_SHA256,
            "onehorse32dev-espressif32-8-arduino-3.3.12-dout",
        ),
        ("espressif32", "minimain_esp32s2", "arduino") => (
            "framework-arduinoespressif32-esp32s2-libs",
            MINIMAIN_SHA256,
            "minimain-esp32s2-espressif32-8-arduino-3.3.12-usb",
        ),
        _ => return RecipeDecision::default(),
    };
    if ![
        input.board,
        input.platform,
        input.framework_package,
        input.sdk_package,
    ]
    .iter()
    .all(|value| value.is_object())
        || input.board_sha256 != sha256
        || !field_is(input.platform, "name", "espressif32")
        || !field_is(input.platform, "version", "8.0.0")
        || !field_is(input.framework_package, "name", ARDUINO_PACKAGE)
        || !field_is(input.framework_package, "version", "4.30312.261016")
        || !field_is(input.sdk_package, "name", sdk_name)
        || !field_is(input.sdk_package, "version", "3.3.12")
    {
        return RecipeDecision::default();
    }
    let dispatch = &input.platform["frameworks"]["arduino"];
    let packages = &input.platform["packages"];
    if !field_is(dispatch, "package", ARDUINO_PACKAGE)
        || !field_is(dispatch, "script", "builder/frameworks/arduino.py")
        || ![ARDUINO_PACKAGE, sdk_name]
            .iter()
            .all(|name| field_is(&packages[*name], "owner", "platformio"))
    {
        return RecipeDecision::default();
    }
    let build = &input.board["build"];
    let options = if input.board_id == "onehorse32dev" {
        let Some(build_fields) = build.as_object() else {
            return RecipeDecision::default();
        };
        let arduino_unspecified = match build_fields.get("arduino") {
            None => true,
            Some(Value::Object(fields)) => !fields.contains_key("memory_type"),
            _ => false,
        };
        if !field_is(build, "mcu", "esp32")
            || !field_is(build, "core", "esp32")
            || !field_is(build, "variant", "onehorse32dev")
            || !field_is(build, "flash_mode", "dout")
            || build_fields.contains_key("boot")
            || build_fields.contains_key("memory_type")
            || !arduino_unspecified
        {
            return RecipeDecision::default();
        }
        BTreeMap::from([
            ("board_build.boot", "dio"),
            ("board_build.arduino.memory_type", "dio_qspi"),
        ])
    } else {
        let flags_match = build
            .get("extra_flags")
            .and_then(Value::as_array)
            .is_some_and(|flags| {
                flags.len() == MINIMAIN_FLAGS.len()
                    && flags
                        .iter()
                        .zip(MINIMAIN_FLAGS)
                        .all(|(actual, expected)| actual.as_str() == Some(expected))
            });
        if !field_is(build, "mcu", "esp32s2")
            || !field_is(build, "variant", "department_of_alchemy_minimain_esp32s2")
            || !flags_match
        {
            return RecipeDecision::default();
        }
        BTreeMap::from([("board_build.extra_flags", "-DARDUINO_DEPARTMENT_OF_ALCHEMY_MINIMAIN_ESP32S2 -DARDUINO_RUNNING_CORE=1 -DARDUINO_EVENT_RUNNING_CORE=1 -DBOARD_HAS_PSRAM -DARDUINO_USB_MODE=0 -DARDUINO_USB_CDC_ON_BOOT=1")])
    };
    RecipeDecision {
        rule: Some(rule),
        options,
    }
}

fn field_is(value: &Value, key: &str, expected: &str) -> bool {
    value.get(key).and_then(Value::as_str) == Some(expected)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn fixture(s2: bool) -> [Value; 4] {
        let sdk = if s2 {
            "framework-arduinoespressif32-esp32s2-libs"
        } else {
            "framework-arduinoespressif32-esp32-libs"
        };
        [
            if s2 {
                json!({"build":{"mcu":"esp32s2","variant":"department_of_alchemy_minimain_esp32s2","extra_flags":["-DARDUINO_DEPARTMENT_OF_ALCHEMY_MINIMAIN_ESP32S2","-DARDUINO_RUNNING_CORE=1","-DARDUINO_EVENT_RUNNING_CORE=1","-DBOARD_HAS_PSRAM","-DARDUINO_USB_MODE=1","-DARDUINO_USB_CDC_ON_BOOT=1"]}})
            } else {
                json!({"build":{"mcu":"esp32","core":"esp32","variant":"onehorse32dev","flash_mode":"dout"}})
            },
            json!({"name":"espressif32","version":"8.0.0","frameworks":{"arduino":{"package":"framework-arduinoespressif32","script":"builder/frameworks/arduino.py"}},"packages":{"framework-arduinoespressif32":{"owner":"platformio"},sdk:{"owner":"platformio"}}}),
            json!({"name":"framework-arduinoespressif32","version":"4.30312.261016"}),
            json!({"name":sdk,"version":"3.3.12"}),
        ]
    }

    fn input(parts: &[Value; 4], s2: bool) -> RecipeInput<'_> {
        RecipeInput {
            platform_name: "espressif32",
            board_id: if s2 {
                "minimain_esp32s2"
            } else {
                "onehorse32dev"
            },
            framework: "arduino",
            board: &parts[0],
            platform: &parts[1],
            framework_package: &parts[2],
            sdk_package: &parts[3],
            board_sha256: if s2 {
                "e9a066074564a920d0e9df6761ad366b5ae32755a67beb32e1ad5f307ceafe9c"
            } else {
                "483741724c41624f1be320ef09ec3396fc0f34d7d0bc633b092e257cd621990b"
            },
        }
    }

    #[test]
    fn exact_onehorse_adds_only_reviewed_boot_and_sdk_options() {
        let parts = fixture(false);
        let actual = recipe_options(&input(&parts, false));
        assert_eq!(
            actual.rule,
            Some("onehorse32dev-espressif32-8-arduino-3.3.12-dout")
        );
        assert_eq!(
            actual.options,
            BTreeMap::from([
                ("board_build.boot", "dio"),
                ("board_build.arduino.memory_type", "dio_qspi")
            ])
        );
    }

    #[test]
    fn exact_minimain_preserves_flag_order_and_changes_only_usb_mode() {
        let parts = fixture(true);
        let actual = recipe_options(&input(&parts, true));
        assert_eq!(
            actual.rule,
            Some("minimain-esp32s2-espressif32-8-arduino-3.3.12-usb")
        );
        assert_eq!(actual.options, BTreeMap::from([("board_build.extra_flags", "-DARDUINO_DEPARTMENT_OF_ALCHEMY_MINIMAIN_ESP32S2 -DARDUINO_RUNNING_CORE=1 -DARDUINO_EVENT_RUNNING_CORE=1 -DBOARD_HAS_PSRAM -DARDUINO_USB_MODE=0 -DARDUINO_USB_CDC_ON_BOOT=1")]));
    }

    #[test]
    fn onehorse_explicit_overrides_including_null_are_preserved() {
        for (pointer, key) in [
            ("/build", "boot"),
            ("/build", "memory_type"),
            ("/build/arduino", "memory_type"),
        ] {
            for value in [Value::Null, json!("dio"), json!(false)] {
                let mut parts = fixture(false);
                parts[0]["build"]["arduino"] = json!({});
                parts[0].pointer_mut(pointer).unwrap()[key] = value;
                assert_eq!(
                    recipe_options(&input(&parts, false)),
                    RecipeDecision::default()
                );
            }
        }
    }

    #[test]
    fn onehorse_missing_arduino_is_allowed_but_null_is_refused() {
        let mut parts = fixture(false);
        assert!(!recipe_options(&input(&parts, false)).options.is_empty());
        parts[0]["build"]["arduino"] = Value::Null;
        assert_eq!(
            recipe_options(&input(&parts, false)),
            RecipeDecision::default()
        );
    }

    #[test]
    fn changed_version_hash_and_dispatch_leave_native_recipe_untouched() {
        for s2 in [false, true] {
            let parts = fixture(s2);
            let mut selected = input(&parts, s2);
            selected.board_sha256 = "future-board-definition";
            assert_eq!(recipe_options(&selected), RecipeDecision::default());
            for (part, pointer, value) in [
                (1, "/version", json!("8.0.1")),
                (2, "/version", json!("4.30313.0")),
                (3, "/version", json!("3.3.13")),
                (1, "/frameworks/arduino/script", json!("builder/future.py")),
                (
                    1,
                    "/packages/framework-arduinoespressif32/owner",
                    json!("other"),
                ),
            ] {
                let mut changed = fixture(s2);
                *changed[part].pointer_mut(pointer).unwrap() = value;
                assert_eq!(
                    recipe_options(&input(&changed, s2)),
                    RecipeDecision::default()
                );
            }
        }
    }

    #[test]
    fn malformed_metadata_and_wrong_selection_refuse_all_options() {
        for s2 in [false, true] {
            for part in 0..4 {
                for value in [
                    Value::Null,
                    json!([]),
                    json!(true),
                    json!(42),
                    json!("metadata"),
                ] {
                    let mut parts = fixture(s2);
                    parts[part] = value;
                    assert_eq!(
                        recipe_options(&input(&parts, s2)),
                        RecipeDecision::default()
                    );
                }
            }
            let parts = fixture(s2);
            let mut selected = input(&parts, s2);
            selected.framework = "espidf";
            assert_eq!(recipe_options(&selected), RecipeDecision::default());
            selected.framework = "arduino";
            selected.platform_name = "arduinoq";
            assert_eq!(recipe_options(&selected), RecipeDecision::default());
            selected.platform_name = "espressif32";
            selected.board_id = "future-board";
            assert_eq!(recipe_options(&selected), RecipeDecision::default());
        }
    }

    #[test]
    fn minimain_new_native_flags_and_reordering_are_preserved() {
        for change in 0..3 {
            let mut parts = fixture(true);
            let flags = parts[0]["build"]["extra_flags"].as_array_mut().unwrap();
            match change {
                0 => flags.swap(0, 1),
                1 => flags[4] = json!("-DARDUINO_USB_MODE=0"),
                _ => flags.push(json!("-DNEW_NATIVE_FLAG=1")),
            }
            assert_eq!(
                recipe_options(&input(&parts, true)),
                RecipeDecision::default()
            );
        }
    }
}
