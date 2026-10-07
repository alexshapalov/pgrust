//! `pgrust_pin_database(text)` / `pgrust_unpin_database(text)` /
//! `pgrust_seal_template(text)` — pgrust-native internal builtins on the
//! reserved-oid EXTRA_BUILTINS path (the `pgrust_lane_coverage` precedent:
//! execmain/src/lanev2/coverage.rs documents the 9000..=9099 range; 9000 is
//! taken by the coverage SRF, this table claims 9001, 9002 and 9005 —
//! 9003/9004 were pgrust_janitor_unpause and pgrust_set_template_grace,
//! deleted 2026-08-05 and permanently retired, never reassigned).
//!
//! These three are TRUE builtins: bootstrap.rs backfills their pg_proc rows
//! (same oids, pg_catalog namespace) into every database a session reaches,
//! so no install script exists and clones inherit nothing they didn't
//! already have — the total janitor SQL surface is these three functions.
//!
//! Privileges (spec "Security posture"): pin/unpin = database owner or
//! superuser (`object_ownercheck`, which passes superusers); seal = owner
//! of the target database or superuser.

use datum::Datum;
use elog::ereport;
use types_core::catalog::DATABASE_RELATION_ID;
use types_core::Oid;
use types_error::{
    PgError, PgResult, ERRCODE_INSUFFICIENT_PRIVILEGE, ERRCODE_UNDEFINED_DATABASE, ERROR,
};
use types_fmgr::{FmgrBuiltin, FmgrInfo, FunctionCallInfoBaseData as Fcinfo};

use crate::registry;

/// Reserved pg_proc-style oids (see PGRUST_FOID_RANGE, 9000..=9099; the
/// range's reservation rationale lives on the coverage builtin). 9003 and
/// 9004 are RETIRED (module doc) — do not reassign them.
pub const PGRUST_PIN_DATABASE_FOID: Oid = 9001;
pub const PGRUST_UNPIN_DATABASE_FOID: Oid = 9002;
pub const PGRUST_SEAL_TEMPLATE_FOID: Oid = 9005;
pub const PGRUST_RUNTIME_STATUS_FOID: Oid = 9006;

/// Decode the text arg of a STRICT single-arg builtin into an owned name.
fn text_arg0(fcinfo: &mut Fcinfo) -> PgResult<String> {
    // SAFETY: null-checked by strictness; arg 0 is a text datum.
    let v = unsafe { fcinfo.arg_varlena_packed(0)? };
    let bytes = v.data().to_vec();
    String::from_utf8(bytes).map_err(|_| {
        Box::new(
            PgError::error("database name is not valid UTF-8".to_string())
                .with_sqlstate(ERRCODE_UNDEFINED_DATABASE),
        )
    })
}

/// Owner-or-superuser check against a live database (the alterdb.rs
/// precedent). Returns the database's CATALOG name (datname), which is what
/// pin state must key on: the name lookup's scan key truncates to
/// NAMEDATALEN-1 bytes (matching CREATE DATABASE's own truncation), so an
/// over-long argument can RESOLVE a database whose datname it does not
/// byte-equal — pinning the raw argument would return true yet never match
/// the datname the reap loop compares. ERRORs undefined_database on a miss,
/// insufficient_privilege on a failed check.
fn owner_or_superuser_check(fcinfo: &Fcinfo, name: &str, func: &'static str) -> PgResult<String> {
    let mcx = fcinfo.result_mcx();
    let Some(db) = pg_database::get_database_tuple_by_name(mcx, name)? else {
        return Err(ereport(ERROR)
            .errcode(ERRCODE_UNDEFINED_DATABASE)
            .errmsg(format!("database \"{name}\" does not exist"))
            .into_error()
            .into());
    };
    if !aclchk::object_ownercheck(DATABASE_RELATION_ID, db.oid, miscinit::GetUserId())? {
        return Err(ereport(ERROR)
            .errcode(ERRCODE_INSUFFICIENT_PRIVILEGE)
            .errmsg(format!(
                "must be owner of database {name} or superuser to call {func}"
            ))
            .into_error()
            .into());
    }
    Ok(db.datname.as_str().to_owned())
}

/// pgrust_pin_database(text) -> bool: exempt the named database from
/// reaping for the rest of this postmaster lifetime (restart-lossy BY
/// DESIGN — rename out of the prefix for durable protection). Returns true
/// if newly pinned, false if it was already pinned.
pub fn fc_pgrust_pin_database(
    _flinfo: Option<&mut FmgrInfo>,
    fcinfo: &mut Fcinfo,
) -> PgResult<Datum> {
    let name = text_arg0(fcinfo)?;
    let datname = owner_or_superuser_check(fcinfo, &name, "pgrust_pin_database")?;
    Ok(Datum::from_bool(registry::pin(&datname)?))
}

/// pgrust_unpin_database(text) -> bool: drop the pin; the database becomes
/// reapable again after a fresh full grace period of idleness. Returns true
/// if a pin was removed. A pin whose database no longer exists (manual DROP
/// while pinned) has no owner to check: superusers may clear such stale
/// entries — otherwise a same-named future database would be born pinned.
pub fn fc_pgrust_unpin_database(
    _flinfo: Option<&mut FmgrInfo>,
    fcinfo: &mut Fcinfo,
) -> PgResult<Datum> {
    let name = text_arg0(fcinfo)?;
    let key = {
        let mcx = fcinfo.result_mcx();
        match pg_database::get_database_tuple_by_name(mcx, &name)? {
            Some(db) => {
                if !aclchk::object_ownercheck(DATABASE_RELATION_ID, db.oid, miscinit::GetUserId())?
                {
                    return Err(ereport(ERROR)
                        .errcode(ERRCODE_INSUFFICIENT_PRIVILEGE)
                        .errmsg(format!(
                            "must be owner of database {name} or superuser to call pgrust_unpin_database"
                        ))
                        .into_error()
                        .into());
                }
                // Unpin by the resolved catalog datname, mirroring pin
                // (owner_or_superuser_check's rationale).
                db.datname.as_str().to_owned()
            }
            None => {
                if !superuser_seams::superuser::call()? {
                    return Err(ereport(ERROR)
                        .errcode(ERRCODE_UNDEFINED_DATABASE)
                        .errmsg(format!("database \"{name}\" does not exist"))
                        .into_error()
                        .into());
                }
                // Superuser clearing a stale pin (dropped-while-pinned):
                // pins are stored as catalog datnames, so the raw argument
                // compares exactly.
                name
            }
        }
    };
    Ok(Datum::from_bool(registry::unpin(&key)))
}

/// pgrust_seal_template(text) -> void: janitor-executed one-call sealing —
/// VACUUM (FREEZE, ANALYZE) inside the target through an internal session,
/// then IS_TEMPLATE true ALLOW_CONNECTIONS false, in the manual recipe's
/// exact order (seal.rs owns the choreography and the why-the-janitor
/// rationale). Callable from ANY database. Privilege: owner of the TARGET
/// database or superuser (the pgrust_set_template_grace style). The
/// backend-side already-a-template check gives callers the cheap ERROR;
/// the janitor re-validates under its own serialization (mutations of the
/// target's lifecycle all serialize in its loop).
pub fn fc_pgrust_seal_template(
    _flinfo: Option<&mut FmgrInfo>,
    fcinfo: &mut Fcinfo,
) -> PgResult<Datum> {
    let name = text_arg0(fcinfo)?;
    // Capture the CHECK-TIME identity: the role we owner-check, and the oid
    // the name resolves to right now. Both travel to the janitor, which
    // re-validates them before its superuser-privileged flip — without that,
    // the flip re-resolves the name to whatever it points at LATER, so a
    // caller could rename their owned database out of the name and a victim
    // in, and the janitor would seal a database the caller never owned
    // (the seal-path TOCTOU).
    let caller_role = miscinit::GetUserId();
    let (datname, expected_oid) = {
        let mcx = fcinfo.result_mcx();
        let Some(db) = pg_database::get_database_tuple_by_name(mcx, &name)? else {
            return Err(crate::seal::seal_target_missing_error(&name));
        };
        if !aclchk::object_ownercheck(DATABASE_RELATION_ID, db.oid, caller_role)? {
            return Err(ereport(ERROR)
                .errcode(ERRCODE_INSUFFICIENT_PRIVILEGE)
                .errmsg(format!(
                    "must be owner of database {name} or superuser to call pgrust_seal_template"
                ))
                .into_error()
                .into());
        }
        if db.datistemplate {
            return Err(crate::seal::already_template_error(&name));
        }
        // Resolved catalog datname (the owner_or_superuser_check rationale:
        // the scan key truncates, the seal keys must not) plus the owner-
        // checked oid, the janitor's re-validation anchor.
        (db.datname.as_str().to_owned(), db.oid)
    };
    crate::seal::request_seal(&datname, expected_oid, caller_role)?;
    // RETURNS void (the fc_pg_sleep convention).
    Ok(Datum::null())
}

/// The extra-builtin table seams_init appends to EXTRA_BUILTINS.

/// `pgrust_runtime_status()` -> text (a JSON object): the runtime facts a
/// control plane needs without parsing logs or shelling into the host.
/// Superuser only. Cheap: one pg_database scan, one procarray pass, two
/// /proc reads. Field names are a stable interface (docs/pgx/pgrun-interface.md);
/// add fields, do not rename them.
pub fn fc_pgrust_runtime_status(
    _flinfo: Option<&mut FmgrInfo>,
    fcinfo: &mut Fcinfo,
) -> PgResult<Datum> {
    if !superuser_seams::superuser::call()? {
        return Err(ereport(ERROR)
            .errcode(ERRCODE_INSUFFICIENT_PRIVILEGE)
            .errmsg("must be superuser to call pgrust_runtime_status".to_string())
            .into_error()
            .into());
    }
    let prefix = crate::ephemeral_db_prefix();
    let mut all: Vec<Oid> = Vec::new();
    let rows = if prefix.is_empty() {
        Vec::new()
    } else {
        crate::dbscan::scan_prefix_rows_collect(&prefix, Some(&mut all))?
    };
    let spare_prefix = format!("{prefix}spare_");
    let ephemeral = rows.iter().filter(|r| !r.istemplate && !r.name.starts_with(&spare_prefix)).count();
    let spares = rows.iter().filter(|r| r.name.starts_with(&spare_prefix)).count();
    let connections = procarray::CountDBConnections(types_core::InvalidOid)?;
    let (vm_rss_kb, threads) = proc_status();
    let pss_kb = proc_pss_kb();
    let (retired_pending, retired_reclaimed) = mcx::retired_session_root_counts();
    let cold = crate::counters::get(&crate::counters::COLD_MINTS);
    let json = format!(
        concat!(
            "{{\"pid\":{pid},",
            "\"memory\":{{\"rss_bytes\":{rss},\"pss_bytes\":{pss},\"context_bytes\":{ctx},",
            "\"session_limit_mb\":{sl},\"database_limit_mb\":{dl},\"runtime_limit_mb\":{rl},",
            "\"retired_session_roots_pending\":{rp},\"retired_session_roots_reclaimed\":{rr}}},",
            "\"threads\":{thr},\"connections\":{conn},",
            "\"databases\":{{\"total\":{dbt},\"ephemeral\":{eph},\"spares\":{spr},\"pinned\":{pin}}},",
            "\"janitor\":{{\"prefix\":\"{pfx}\",\"pool_size\":{pool},\"pending_mints\":{pend},",
            "\"connection_limit\":{cl},\"pool_handouts\":{ph},\"cold_mints\":{cm},\"spares_minted\":{sm},",
            "\"mint_failures\":{mf},\"cold_mint_ms_mean\":{cmean},\"cold_mint_ms_max\":{cmax}}}}}"
        ),
        pid = std::process::id(),
        rss = vm_rss_kb.map_or("null".into(), |k| (k * 1024).to_string()),
        pss = pss_kb.map_or("null".into(), |k| (k * 1024).to_string()),
        ctx = mcx::global_footprint::bytes(),
        sl = guc_tables::vars::pgrust_session_memory_limit.read(),
        dl = guc_tables::vars::pgrust_database_memory_limit.read(),
        rl = guc_tables::vars::pgrust_runtime_memory_limit.read(),
        rp = retired_pending,
        rr = retired_reclaimed,
        thr = threads.map_or("null".into(), |t| t.to_string()),
        conn = connections,
        dbt = all.len(),
        eph = ephemeral,
        spr = spares,
        pin = registry::pinned_names().len(),
        pfx = prefix.replace('\\', "\\\\").replace('"', "\\\""),
        pool = crate::ephemeral_db_pool_size(),
        pend = registry::pending_ensure_count(),
        cl = crate::ephemeral_db_connection_limit(),
        ph = crate::counters::get(&crate::counters::POOL_HANDOUTS),
        cm = cold,
        sm = crate::counters::get(&crate::counters::SPARES_MINTED),
        mf = crate::counters::get(&crate::counters::MINT_FAILURES),
        cmean = if cold > 0 {
            format!("{:.1}", crate::counters::get(&crate::counters::COLD_MINT_US_TOTAL) as f64 / cold as f64 / 1000.0)
        } else {
            "null".to_string()
        },
        cmax = format!("{:.1}", crate::counters::get(&crate::counters::COLD_MINT_US_MAX) as f64 / 1000.0),
    );
    Ok(types_fmgr::varlena_result(varlena::cstring_to_text(fcinfo.result_mcx(), json.as_bytes())?))
}

fn proc_status() -> (Option<u64>, Option<u64>) {
    let Ok(s) = std::fs::read_to_string("/proc/self/status") else { return (None, None) };
    let field = |name: &str| {
        s.lines()
            .find(|l| l.starts_with(name))
            .and_then(|l| l.split_whitespace().nth(1))
            .and_then(|v| v.parse::<u64>().ok())
    };
    (field("VmRSS:"), field("Threads:"))
}

fn proc_pss_kb() -> Option<u64> {
    let s = std::fs::read_to_string("/proc/self/smaps_rollup").ok()?;
    s.lines()
        .find(|l| l.starts_with("Pss:"))
        .and_then(|l| l.split_whitespace().nth(1))
        .and_then(|v| v.parse::<u64>().ok())
}

pub static JANITOR_BUILTINS: &[FmgrBuiltin] = &[
    FmgrBuiltin {
        foid: PGRUST_PIN_DATABASE_FOID,
        name: "pgrust_pin_database",
        nargs: 1,
        strict: true,
        retset: false,
        func: fc_pgrust_pin_database,
    },
    FmgrBuiltin {
        foid: PGRUST_UNPIN_DATABASE_FOID,
        name: "pgrust_unpin_database",
        nargs: 1,
        strict: true,
        retset: false,
        func: fc_pgrust_unpin_database,
    },
    FmgrBuiltin {
        foid: PGRUST_SEAL_TEMPLATE_FOID,
        name: "pgrust_seal_template",
        nargs: 1,
        strict: true,
        retset: false,
        func: fc_pgrust_seal_template,
    },
    FmgrBuiltin {
        foid: PGRUST_RUNTIME_STATUS_FOID,
        name: "pgrust_runtime_status",
        nargs: 0,
        strict: true,
        retset: false,
        func: fc_pgrust_runtime_status,
    },
];

#[cfg(test)]
mod tests {
    use super::*;

    /// Reserved-oid law (the coverage.rs test, replicated for this table):
    /// every janitor foid sits inside the documented pgrust range
    /// (9000..=9099), below the user oid space, is distinct within the
    /// table, avoids 9000 (pgrust_lane_coverage), and collides with no
    /// canonical C 18.3 builtin by oid or name. `install_extra_builtins`
    /// re-asserts the canonical half against live rows at startup, and the
    /// e2e probes the initdb'd pg_proc for the whole range.
    #[test]
    fn reserved_oids_are_clear_of_canonical() {
        let range = 9000u32..=9099;
        let mut seen = Vec::new();
        for b in JANITOR_BUILTINS {
            assert!(
                range.contains(&b.foid),
                "{} outside the pgrust reserved range",
                b.foid
            );
            assert!(
                b.foid < 16384,
                "user oid space starts at FirstNormalObjectId"
            );
            assert_ne!(b.foid, 9000, "9000 belongs to pgrust_lane_coverage");
            assert!(!seen.contains(&b.foid), "duplicate foid {}", b.foid);
            seen.push(b.foid);
        }
        for &(oid, name, ..) in ::fmgr_core::CANONICAL.iter() {
            assert!(!range.contains(&oid), "CANONICAL claims reserved oid {oid}");
            for b in JANITOR_BUILTINS {
                assert_ne!(name, b.name, "CANONICAL claims the name {name}");
            }
        }
    }
}
