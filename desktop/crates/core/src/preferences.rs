//! Snapshot merge ported from `main/core/config_store.py::merge_changes`.
//! This module has no persistence or credential access. Native interprocess
//! locking and settings import remain separate release gates.
use mcu_contracts::{ErrorCode, ServiceError, ServiceResult};
use serde_json::{Map, Number, Value};

pub fn merge_changes(current: &Value, original: &Value, edited: &Value) -> ServiceResult<Value> {
    let (Some(current), Some(original), Some(edited)) = (
        current.as_object(),
        original.as_object(),
        edited.as_object(),
    ) else {
        return Err(ServiceError {
            code: ErrorCode::InvalidPath,
            message: "Preference groups changed shape. Read a fresh snapshot before saving.".into(),
            retryable: true,
        });
    };
    let mut result = current.clone();
    for key in original.keys().filter(|key| !edited.contains_key(*key)) {
        result.remove(key);
    }
    for (key, value) in edited {
        if original
            .get(key)
            .is_some_and(|old| reference_equal(old, value))
        {
            continue;
        }
        let old_group = original.get(key).and_then(Value::as_object);
        let current_group = result.get(key).and_then(Value::as_object);
        if value.is_object()
            && (old_group.is_some() || !original.contains_key(key) && current_group.is_some())
        {
            let empty = Value::Object(Map::new());
            let merged = merge_changes(
                result.get(key).unwrap_or(&empty),
                original.get(key).unwrap_or(&empty),
                value,
            )?;
            result.insert(key.clone(), merged);
        } else {
            result.insert(key.clone(), value.clone());
        }
    }
    Ok(Value::Object(result))
}

fn integer(number: &Number) -> Option<i128> {
    number
        .as_i64()
        .map(i128::from)
        .or_else(|| number.as_u64().map(i128::from))
}

// Python's snapshot equality considers JSON `true`, `1` and `1.0` equal.
// Retaining that behavior avoids treating an unchanged legacy preference as a
// new edit and overwriting another window's concurrent value.
fn numeric_equal(first: &Number, second: &Number) -> bool {
    match (integer(first), integer(second)) {
        (Some(a), Some(b)) => a == b,
        (Some(a), None) => second.as_f64().is_some_and(|b| {
            b.fract() == 0.0 && b >= i128::MIN as f64 && b < i128::MAX as f64 && b as i128 == a
        }),
        (None, Some(_)) => numeric_equal(second, first),
        (None, None) => first.as_f64() == second.as_f64(),
    }
}

fn reference_equal(first: &Value, second: &Value) -> bool {
    match (first, second) {
        (Value::Bool(a), Value::Number(b)) => numeric_equal(&Number::from(u8::from(*a)), b),
        (Value::Number(_), Value::Bool(_)) => reference_equal(second, first),
        (Value::Number(a), Value::Number(b)) => numeric_equal(a, b),
        (Value::Array(a), Value::Array(b)) => {
            a.len() == b.len() && a.iter().zip(b).all(|(a, b)| reference_equal(a, b))
        }
        (Value::Object(a), Value::Object(b)) => {
            a.len() == b.len()
                && a.iter()
                    .all(|(key, value)| b.get(key).is_some_and(|v| reference_equal(value, v)))
        }
        _ => first == second,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    #[test]
    fn concurrent_groups_and_unedited_preferences_survive() {
        let original = json!({"shared":{"theme":"dark","font":14},"recent":["old"]});
        let edited = json!({"shared":{"theme":"light","font":14},"recent":["old"]});
        let current = json!({"shared":{"theme":"dark","font":18},"recent":["new"],"other":true});
        assert_eq!(
            merge_changes(&current, &original, &edited).unwrap(),
            json!({"shared":{"theme":"light","font":18},"recent":["new"],"other":true})
        );
        assert_eq!(current["shared"]["font"], 18);
    }
    #[test]
    fn deletions_and_new_groups_match_snapshot_intent() {
        assert_eq!(
            merge_changes(
                &json!({"remove":3,"new":{"other":1}}),
                &json!({"remove":1}),
                &json!({"new":{"added":2}})
            )
            .unwrap(),
            json!({"new":{"other":1,"added":2}})
        );
    }
    #[test]
    fn legacy_numeric_equality_does_not_replace_a_concurrent_value() {
        assert_eq!(
            merge_changes(
                &json!({"enabled":false}),
                &json!({"enabled":true}),
                &json!({"enabled":1})
            )
            .unwrap(),
            json!({"enabled":false})
        );
        assert!(!numeric_equal(
            &Number::from(9_007_199_254_740_993u64),
            &Number::from_f64(9_007_199_254_740_992.0).unwrap()
        ));
    }
    #[test]
    fn malformed_group_conflict_is_reported_without_mutation() {
        let current = json!({"shared":"external-shape-change"});
        assert!(merge_changes(
            &current,
            &json!({"shared":{"font":12}}),
            &json!({"shared":{"font":14}})
        )
        .is_err());
        assert_eq!(current["shared"], "external-shape-change");
    }
}
