//! Worker-budget policy ported from `main/core/build_resources.py`.
//!
//! Metrics are supplied by the host. This calculation never probes storage,
//! reads settings, mutates environment variables, or treats logical threads as
//! proof that the machine meets the separate physical-core admission policy.

#[derive(Clone, Copy, Debug)]
pub struct WorkerBudget<'a> {
    pub mode: &'a str,
    pub logical_cpus: u32,
    pub physical_cpus: Option<u32>,
    pub available_gib: Option<f64>,
    pub storage_limit: Option<u32>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct InvalidMemoryMetric;

/// Equivalent to `_resource_safe_worker_count` once discovery has supplied its
/// inputs. Unknown RAM/storage stays unknown; no synchronous fallback is used.
pub fn safe_worker_count(input: WorkerBudget<'_>) -> Result<u32, InvalidMemoryMetric> {
    if input.available_gib.is_some_and(|value| !value.is_finite()) {
        return Err(InvalidMemoryMetric);
    }
    let cpus = input.logical_cpus.max(1);
    let reserved = if cpus >= 4 { 2 } else { u32::from(cpus > 1) };
    let mut budget = cpus.saturating_sub(reserved).max(1);
    if let Some(physical) = input.physical_cpus.filter(|&count| count > 0) {
        let physical_budget = if physical <= 6 {
            physical.saturating_sub(2)
        } else {
            physical
        };
        budget = budget.min(physical_budget.max(1));
    }
    if let Some(memory) = input.available_gib {
        let memory_budget = if memory < 1.0 {
            1
        } else if memory < 2.0 {
            (((memory - 0.25) / 0.45) as u32).clamp(1, 2)
        } else if memory < 4.0 {
            (((memory - 0.5) / 0.35) as u32).max(1)
        } else {
            (((memory - 0.5) / 0.28) as u32).max(1)
        };
        budget = budget.min(memory_budget);
    }
    if let Some(storage) = input.storage_limit {
        budget = budget.min(storage);
    }
    let mode = input.mode.to_uppercase();
    let limit = match mode.as_str() {
        "LOW" => (cpus / 2).max(1),
        "MEDIUM" => (cpus / 2 + cpus % 2).max(2),
        "ULTRA" | "MAX" | "MAXIMUM" => 32,
        _ if input.available_gib.is_some_and(|memory| memory >= 4.0) => 24,
        _ => 12,
    };
    Ok(budget.min(cpus).min(limit).max(1))
}

/// Equivalent to `get_optimal_compiler_jobs` after the host has loaded the
/// selected mode and collected current metrics off the GUI thread.
pub fn optimal_compiler_jobs(mut input: WorkerBudget<'_>) -> Result<u32, InvalidMemoryMetric> {
    let mode = input.mode.to_uppercase();
    input.mode = match mode.as_str() {
        "LOW" => "LOW",
        "MEDIUM" => "MEDIUM",
        _ if input.available_gib.is_some_and(|memory| memory >= 4.0) => "MAX",
        _ => "HIGH",
    };
    safe_worker_count(input)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn small_smt_machines_keep_physical_and_storage_headroom() {
        let mut input = WorkerBudget {
            mode: "MAX",
            logical_cpus: 12,
            physical_cpus: Some(6),
            available_gib: Some(16.0),
            storage_limit: None,
        };
        assert_eq!(safe_worker_count(input), Ok(4));
        input.logical_cpus = 8;
        input.physical_cpus = Some(4);
        assert_eq!(safe_worker_count(input), Ok(2));
        input.storage_limit = Some(1);
        assert_eq!(safe_worker_count(input), Ok(1));
    }

    #[test]
    fn invalid_metrics_cannot_remove_resource_caps() {
        for memory in [f64::NAN, f64::INFINITY, f64::NEG_INFINITY] {
            let input = WorkerBudget {
                mode: "MAX",
                logical_cpus: 32,
                physical_cpus: None,
                available_gib: Some(memory),
                storage_limit: None,
            };
            assert_eq!(safe_worker_count(input), Err(InvalidMemoryMetric));
            assert_eq!(optimal_compiler_jobs(input), Err(InvalidMemoryMetric));
        }
    }
}
