#!/usr/bin/env python3
"""Branch economics from measured PGX resource use (an estimate, not a quote).

MEASURED inputs (docs/pgx/linux-*.md, this host) are separated from
ASSUMED inputs (workload shape, overheads), and every assumption is printed
with the result. Prints a markdown table; --json writes the numbers.
"""

import argparse
import json
import math

MEASURED = {
    "host_cost_month_usd": 8.50,          # OVH VPS, linux-host.md
    "host_ram_gib": 7.6,
    "host_vcpu": 4,
    "pool_gib": 47.0,                      # usable ZFS pool
    "runtime_base_mb": 86.0,               # PGX runtime with 0 DBs (density, fixed build)
    "idle_db_mb": 0.55,                    # per idle database (autovacuum on); 0.50 with it off
    "branch_fresh_physical_mb": 2.0,       # zfs-cow.md
    "active_db_core_share": 1.0 / 20,      # 20 active DBs (query + 10 ms think time) ~ 1 core, p99 2.3 ms
    "safe_active_dbs_per_host": 50,        # p99 < 16 ms on 4 vCPU with co-located load generator
    "bulk_write_amplification": 1.2,       # physical / logical for inserts
}

ASSUMED = {
    "ram_budget_gib": 6.0,                 # leave OS + ARC headroom (linux-host.md rule)
    "pool_budget_fraction": 0.7,           # keep 30% of the pool free
    "active_fraction": 0.2,                # share of a branch's life spent running queries
    "writes_per_branch_mb": 10.0,          # logical data an agent branch writes
    "golden_gib_per_host": 5.0,            # cached templates per host (logical; counted once)
    "control_plane_month_usd": 50.0,       # API + metadata DB, shared by all hosts (estimate)
    "backup_gib_month_usd": 0.015,         # object storage for golden snapshots
    "golden_backup_gib": 5.0,
    "price_branch_hour_usd": 0.012,
    "price_gib_month_usd": 0.28,
}

SCENARIOS = [
    # name, branches per day, lifetime minutes
    ("1000 x 5-minute branches/day", 1000, 5),
    ("1000 x 30-minute branches/day", 1000, 30),
    ("100 x 1-hour branches/day", 100, 60),
    ("10k creates/day, 15-minute life", 10_000, 15),
    ("100k creates/day, 15-minute life", 100_000, 15),
]


def host_capacity(m, a):
    ram_dbs = (a["ram_budget_gib"] * 1024 - m["runtime_base_mb"]) / m["idle_db_mb"]
    cpu_dbs = m["safe_active_dbs_per_host"] / a["active_fraction"]
    per_branch_disk_mb = m["branch_fresh_physical_mb"] + a["writes_per_branch_mb"] * m["bulk_write_amplification"]
    disk_dbs = (m["pool_gib"] * a["pool_budget_fraction"] - a["golden_gib_per_host"]) * 1024 / per_branch_disk_mb
    return {"by_ram": int(ram_dbs), "by_cpu": int(cpu_dbs), "by_disk": int(disk_dbs),
            "tested_max": 1000, "concurrent_branches": int(min(ram_dbs, cpu_dbs, disk_dbs, 1000)),
            "per_branch_disk_mb": per_branch_disk_mb}


def scenario(name, per_day, minutes, m, a, cap):
    branch_hours_month = per_day * minutes / 60 * 30
    avg_concurrent = per_day * minutes / (24 * 60)
    peak_concurrent = avg_concurrent * 3          # assumed 3x daytime peak
    hosts = max(1, math.ceil(peak_concurrent / cap["concurrent_branches"]))
    revenue = branch_hours_month * a["price_branch_hour_usd"]
    # storage revenue: branch data held on average (GB-month) - charged at list price
    gb_months = avg_concurrent * cap["per_branch_disk_mb"] / 1024
    revenue_storage = gb_months * a["price_gib_month_usd"]
    cost_hosts = hosts * m["host_cost_month_usd"]
    cost = cost_hosts + a["golden_backup_gib"] * a["backup_gib_month_usd"]
    return {"scenario": name, "branch_hours_month": round(branch_hours_month), "avg_concurrent": round(avg_concurrent, 1),
            "peak_concurrent_assumed": round(peak_concurrent, 1), "hosts": hosts,
            "revenue_month_usd": round(revenue + revenue_storage, 2), "revenue_branch_hours_usd": round(revenue, 2),
            "revenue_storage_usd": round(revenue_storage, 2), "host_cost_month_usd": round(cost, 2),
            "margin_before_control_plane": round(1 - cost / (revenue + revenue_storage), 3) if revenue else None}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json")
    args = ap.parse_args()
    cap = host_capacity(MEASURED, ASSUMED)
    rows = [scenario(n, d, mins, MEASURED, ASSUMED, cap) for n, d, mins in SCENARIOS]
    print("Per-host concurrent-branch capacity: RAM %(by_ram)d, CPU %(by_cpu)d, disk %(by_disk)d, "
          "tested %(tested_max)d -> plan for %(concurrent_branches)d" % cap)
    print()
    print("| Scenario | Branch-hours/month | Avg / assumed peak concurrent | Hosts | Revenue/month | Host cost/month | Margin before control plane |")
    print("|---|---|---|---|---|---|---|")
    for r in rows:
        print("| %s | %s | %s / %s | %d | $%.2f | $%.2f | %s |" % (
            r["scenario"], f'{r["branch_hours_month"]:,}', r["avg_concurrent"], r["peak_concurrent_assumed"], r["hosts"],
            r["revenue_month_usd"], r["host_cost_month_usd"],
            "%.0f%%" % (r["margin_before_control_plane"] * 100) if r["margin_before_control_plane"] is not None else "-"))
    print()
    print("Shared control plane (assumed): $%.0f/month, not in the per-scenario margin." % ASSUMED["control_plane_month_usd"])
    if args.json:
        with open(args.json, "w") as f:
            json.dump({"measured": MEASURED, "assumed": ASSUMED, "capacity": cap, "scenarios": rows}, f, indent=2)


if __name__ == "__main__":
    main()
