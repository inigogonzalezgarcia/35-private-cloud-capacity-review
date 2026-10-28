"""The checks: for every cluster, the CPU and RAM left after its failover hosts are gone, the vCPU:pCPU ratio and RAM overcommit, the
12-month trend with the approved demand and the reclaim list (runway against the purchase lead time), the datastores, the month's
availability and the hosts' support. Each finding says what to do and who owns it."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime

from .model import Cluster, Data, Demand, _norm, add_months, clip_minutes, mlabel, month_diff, month_minutes, version

CRITICAL, WARNING, INFO = "critical", "warning", "info"
ORDER = {CRITICAL: 0, WARNING: 1, INFO: 2}
AREAS = ("Capacity", "Forecast", "Storage", "Reclaim", "Availability", "Lifecycle")
VERDICT = {CRITICAL: "at risk", WARNING: "attention", None: "ok"}
METRICS = ("cpu", "ram", "storage")
UNIT = {"cpu": "GHz", "ram": "GB", "storage": "GB"}
LABEL = {"cpu": "CPU", "ram": "RAM", "storage": "Storage"}
NO_OWNER = {"", "unknown", "n a", "na", "none", "tbd", "desconocido", "sin responsable", "nobody"}


@dataclass
class Finding:
    severity: str
    area: str
    code: str
    subject: str
    detail: str
    fix: str
    owner: str = ""
    items: list[str] = field(default_factory=list)


@dataclass
class Issue:
    severity: str
    code: str
    text: str


def _n(k: int, word: str, plural: str | None = None) -> str:
    return f"{k} {word if k == 1 else (plural or word + 's')}"


def _top(items: list[str], k: int = 6) -> str:
    return ", ".join(items[:k]) + (f" and {len(items) - k} more" if len(items) > k else "")


def _g(v: float) -> str:
    return f"{v:,.0f}"


def slope(values: list[float]) -> float:
    """Least-squares slope per step of an evenly spaced series."""
    n = len(values)
    if n < 2:
        return 0.0
    mx, my = (n - 1) / 2, sum(values) / n
    den = sum((i - mx) ** 2 for i in range(n))
    return sum((i - mx) * (y - my) for i, y in enumerate(values)) / den


# ---------------------------------------------------------------- capacity per cluster

def capacity(d: Data, c: Cluster) -> dict:
    s = d.settings
    hosts = d.of(d.hosts, c.cluster)
    n = c.failover_hosts if c.failover_hosts is not None else s.failover_hosts
    ghz, ram, cores = sum(h.cpu_ghz for h in hosts), sum(h.ram_gb for h in hosts), sum(h.cores for h in hosts)
    big = lambda key: sum(sorted((key(h) for h in hosts), reverse=True)[:n])  # noqa: E731
    after = dict(cpu=ghz - big(lambda h: h.cpu_ghz), ram=ram - big(lambda h: h.ram_gb), cores=cores - big(lambda h: h.cores))
    hist = sorted(d.of(d.util, c.cluster), key=lambda u: u.month)[-s.history_months:]
    last = hist[-1] if hist else None
    dss = d.of(d.datastores, c.cluster)
    ds_cap = sum(x.capacity_gb for x in dss)

    def used(u, metric):
        if u is None:
            return None
        if metric == "cpu":
            return u.cpu_used_ghz if u.cpu_used_ghz is not None else (u.cpu_p95 * ghz / 100 if u.cpu_p95 is not None else None)
        if metric == "ram":
            return u.ram_used_gb if u.ram_used_gb is not None else (u.mem_p95 * ram / 100 if u.mem_p95 is not None else None)
        return u.storage_used_gb
    now = {m: used(last, m) for m in METRICS}
    if now["storage"] is None and dss:
        now["storage"] = sum(x.capacity_gb - x.free_gb for x in dss)
    mx = dict(cpu=c.cpu_max_pct or s.cpu_max_pct, ram=c.ram_max_pct or s.ram_max_pct, storage=100 - s.datastore_warn_free_pct)
    limit = dict(cpu=after["cpu"] * mx["cpu"] / 100, ram=after["ram"] * mx["ram"] / 100, storage=ds_cap * mx["storage"] / 100)
    pct_after = {m: (now[m] / after[m] * 100 if now[m] is not None and after[m] else None) for m in ("cpu", "ram")}
    running = [v for v in d.of(d.vms, c.cluster) if v.power == "on"]
    vcpu, vram = sum(v.vcpu for v in running), sum(v.ram_gb for v in running)
    return dict(cluster=c, hosts=hosts, failover=n, cores=cores, ghz=ghz, ram=ram, after=after, hist=hist, last=last, now=now, max=mx,
                limit=limit, pct_after=pct_after, headroom={m: (limit[m] - now[m]) if now[m] is not None else None for m in METRICS},
                vcpu=vcpu, vram=vram, vms=len(d.of(d.vms, c.cluster)), running=len(running),
                ratio=vcpu / cores if cores else 0.0, ratio_after=vcpu / after["cores"] if after["cores"] else 0.0,
                overcommit=vram / after["ram"] if after["ram"] else 0.0, ratio_max=c.vcpu_ratio_max or s.vcpu_ratio_max,
                ghz_per_vcpu=min(now["cpu"] / vcpu, ghz / cores) if now["cpu"] and vcpu and cores else (ghz / cores if cores else 0.0),
                ghz_per_core=ghz / cores if cores else 0.0,
                ds_cap=ds_cap, not_connected=[h for h in hosts if h.state != "connected"], storage=dss)


# ---------------------------------------------------------------- reclaim

def reclaim(d: Data, caps: dict[str, dict]) -> list[dict]:
    """One row per VM and reason, with what it gives back. Savings that feed the forecast: RAM and CPU of idle VMs, RAM of oversized VMs,
    storage of VMs off for `poweredoff_reclaim_days` and of old snapshots."""
    s = d.settings
    out = []
    asof = datetime.combine(d.as_of, datetime.min.time())
    snaps: dict[str, list] = {}
    for sn in d.snapshots:
        snaps.setdefault(_norm(sn.vm), []).append(sn)
    for v in sorted(d.vms, key=lambda x: (_norm(x.cluster), _norm(x.vm))):
        cap = caps.get(_norm(v.cluster))
        row = lambda reason, detail, action, **sv: out.append(dict(vm=v, reason=reason, detail=detail, action=action,  # noqa: E731
                                                                     vcpu=sv.get("vcpu", 0), ram=sv.get("ram", 0.0), cpu=sv.get("cpu", 0.0),
                                                                     storage=sv.get("storage", 0.0), counted=sv.get("counted", True),
                                                                     age=sv.get("age", 0)))
        if v.power == "off":
            days = (asof - v.last_on).days if v.last_on else None
            if days is None or days >= s.poweredoff_reclaim_days:
                row("powered off 90+ days" if days is not None else "powered off, never seen on",
                    f"off {days} days" if days is not None else "no power-on in the event history",
                    "Confirm with the owner, archive to backup and delete", vcpu=v.vcpu, storage=v.provisioned_gb)
            elif days >= s.poweredoff_warn_days:
                row("powered off 30+ days", f"off {days} days", "Ask the owner: power on, or plan its deletion", counted=False)
        else:
            idle = (v.cpu_p95 is not None and v.mem_p95 is not None and v.cpu_p95 < s.idle_cpu_pct and v.mem_p95 < s.idle_mem_pct)
            if idle:
                cpu = v.vcpu * (cap["ghz_per_core"] if cap else 0) * v.cpu_p95 / 100
                row("idle", f"CPU p95 {v.cpu_p95:g}%, RAM p95 {v.mem_p95:g}% over 30 days", "Confirm with the owner, power off, delete after 30 days",
                    vcpu=v.vcpu, ram=v.ram_gb, cpu=cpu)
            else:
                rec_cpu, rec_ram = v.vcpu, v.ram_gb
                if v.cpu_p95 is not None and v.vcpu >= s.oversize_min_vcpu and v.cpu_p95 < s.oversize_cpu_pct:
                    rec_cpu = max(2, math.ceil(v.vcpu * v.cpu_p95 / s.rightsize_target_pct))
                if v.mem_p95 is not None and v.ram_gb >= s.oversize_min_ram_gb and v.mem_p95 < s.oversize_mem_pct:
                    rec_ram = max(4.0, math.ceil(v.ram_gb * v.mem_p95 / s.rightsize_target_pct / 4) * 4)
                if rec_cpu < v.vcpu or rec_ram < v.ram_gb:
                    row("oversized", f"CPU p95 {v.cpu_p95:g}%, RAM p95 {v.mem_p95:g}%: {v.vcpu} vCPU / {v.ram_gb:g} GB → {rec_cpu} vCPU / "
                        f"{rec_ram:g} GB", "Right-size in the next maintenance window (change via CAB)", vcpu=v.vcpu - rec_cpu,
                        ram=v.ram_gb - rec_ram)
        for sn in sorted(snaps.get(_norm(v.vm), []), key=lambda x: x.created):
            age = (asof - sn.created).days
            if age > s.snapshot_max_days:
                row("old snapshot", f"'{sn.snapshot}' {age} days old, {sn.size_gb:g} GB",
                    "Consolidate (delete the snapshot) after checking the backup", storage=sn.size_gb, age=age)
        if _norm(v.owner) in NO_OWNER:
            row("no owner", "no owner in the inventory", "Find the owner in the CMDB or the change that created it; tag it", counted=False)
    return out


def reclaim_totals(rows: list[dict], cluster: str | None = None) -> dict:
    """Savings per cluster, counting each VM once per resource (the largest of its reasons)."""
    best: dict[str, dict] = {}
    for r in rows:
        if not r["counted"] or (cluster is not None and _norm(r["vm"].cluster) != _norm(cluster)):
            continue
        b = best.setdefault(_norm(r["vm"].vm), dict(ram=0.0, cpu=0.0, vcpu=0, storage_vm=0.0, snaps=0.0))
        b["ram"], b["cpu"], b["vcpu"] = max(b["ram"], r["ram"]), max(b["cpu"], r["cpu"]), max(b["vcpu"], r["vcpu"])
        if r["reason"] == "old snapshot":
            b["snaps"] += r["storage"]
        else:
            b["storage_vm"] = max(b["storage_vm"], r["storage"])
    return dict(ram=sum(b["ram"] for b in best.values()), cpu=sum(b["cpu"] for b in best.values()),
                vcpu=sum(b["vcpu"] for b in best.values()), storage=sum(b["storage_vm"] + b["snaps"] for b in best.values()),
                vms=len(best))


# ---------------------------------------------------------------- forecast

def counted(d: Data, x: Demand) -> bool:
    return x.status in d.settings.demand_statuses and x.go_live is not None


def demand_load(cap: dict, x: Demand, ram_factor: float) -> dict:
    return dict(cpu=x.vcpu * cap["ghz_per_vcpu"], ram=x.ram_gb * ram_factor, storage=x.storage_gb)


def forecast(d: Data, cap: dict, saving: dict) -> dict:
    """Month by month from the last month with data: the latest p95 plus the trend of the last `history_months`, then the approved
    demand at its go-live month, then minus what the reclaim list gives back from the month after the review."""
    s = d.settings
    hist = cap["hist"]
    out = dict(series={}, breach={}, runway={}, slope={}, binding={}, months=len(hist))
    if not hist:
        return out
    m0 = hist[-1].month
    start = add_months(d.now_month, 1)
    items = [x for x in d.of(d.demand, cap["cluster"].cluster) if counted(d, x)]
    for m in METRICS:
        vals = []
        for u in hist:
            v = None
            if m == "cpu":
                v = u.cpu_used_ghz if u.cpu_used_ghz is not None else (u.cpu_p95 * cap["ghz"] / 100 if u.cpu_p95 is not None else None)
            elif m == "ram":
                v = u.ram_used_gb if u.ram_used_gb is not None else (u.mem_p95 * cap["ram"] / 100 if u.mem_p95 is not None else None)
            else:
                v = u.storage_used_gb
            vals.append((u.month, v))
        known = [v for _, v in vals if v is not None]
        lim = cap["limit"][m]
        if not known or not lim:
            continue
        b = slope(known)
        anchor = known[-1]
        rows = [dict(month=mo, actual=v, trend=None, pipeline=None, reclaim=None) for mo, v in vals]
        rows[-1].update(trend=anchor, pipeline=anchor, reclaim=anchor)
        sv = dict(cpu=saving["cpu"], ram=saving["ram"], storage=saving["storage"])[m]
        for k in range(1, s.forecast_months + 1):
            mo = add_months(m0, k)
            trend = anchor + b * k
            add = sum(demand_load(cap, x, s.demand_ram_factor)[m] for x in items
                      if month_diff(max((x.go_live.year, x.go_live.month), d.now_month), mo) >= 0)
            pipe = trend + add
            rows.append(dict(month=mo, actual=None, trend=trend, pipeline=pipe, reclaim=pipe - (sv if month_diff(start, mo) >= 0 else 0.0)))
        out["series"][m] = rows
        out["slope"][m] = b
        for sc in ("trend", "pipeline", "reclaim"):
            hit = next((r["month"] for r in rows if r[sc] is not None and r[sc] > lim), None)
            out["breach"][(m, sc)] = hit
    for sc in ("trend", "pipeline", "reclaim"):
        best = None
        for m in METRICS:
            if m not in out["series"]:
                continue
            hit = out["breach"].get((m, sc))
            if hit and (best is None or month_diff(hit, best[1]) > 0):
                best = (m, hit)
        out["binding"][sc] = best[0] if best else None
        out["runway"][sc] = None if best is None else max(0, month_diff(d.now_month, best[1]))
        out["breach"][sc] = best[1] if best else None
    return out


def demand_fit(d: Data, cap: dict, fc: dict) -> list[tuple[Demand, tuple | None]]:
    """For each approved item of the cluster: None if it fits at its go-live month, else (metric, needed, left before it)."""
    out = []
    for x in sorted((x for x in d.of(d.demand, cap["cluster"].cluster) if counted(d, x)), key=lambda x: (x.go_live, x.project)):
        gl = max((x.go_live.year, x.go_live.month), d.now_month)
        load = demand_load(cap, x, d.settings.demand_ram_factor)
        short = None
        for m in METRICS:
            row = next((r for r in fc["series"].get(m, []) if r["month"] == gl), None)
            if row and row["pipeline"] is not None and row["pipeline"] > cap["limit"][m] and load[m] > 0:
                short = (m, load[m], cap["limit"][m] - (row["pipeline"] - load[m]))
                break
        out.append((x, short))
    return out


def runway_text(months: int | None, horizon: int) -> str:
    if months is None:
        return f"> {horizon} months"
    return "now" if months == 0 else _n(months, "month")


# ---------------------------------------------------------------- availability and lifecycle

def availability(d: Data, c: Cluster) -> dict:
    ym = d.period
    ev = [e for e in d.of(d.events, c.cluster) if e.end is None or e.end >= datetime(ym[0], ym[1], 1)]
    ev = [e for e in ev if (e.start.year, e.start.month) <= ym]
    outage = sum(clip_minutes(e, ym) for e in ev if e.kind == "cluster_outage")
    month_ev = [e for e in ev if (e.start.year, e.start.month) == ym]
    fails = [e for e in month_ev if e.kind == "host_failure"]
    restarted = sum(e.vms for e in month_ev if e.kind in ("ha_restart", "host_failure"))
    pct = 100 * (1 - outage / month_minutes(ym))
    return dict(pct=pct, outage=outage, failures=fails, restarted=restarted, events=month_ev,
                outages=[e for e in month_ev if e.kind == "cluster_outage"])


def lifecycle(d: Data, cap: dict) -> dict:
    s = d.settings
    ended, ending, behind = [], [], []
    for h in cap["hosts"]:
        if h.support_end:
            days = (h.support_end - d.as_of).days
            (ended if days < 0 else ending if days <= s.support_warn_days else []).append(h)
        if h.firmware and h.firmware_target and version(h.firmware) < version(h.firmware_target):
            behind.append(h)
    return dict(ended=ended, ending=ending, behind=behind)


def datastore_rows(d: Data) -> list[dict]:
    s = d.settings
    prov_by = {}
    for v in d.vms:
        if v.datastore:
            prov_by[_norm(v.datastore)] = prov_by.get(_norm(v.datastore), 0.0) + v.provisioned_gb
    out = []
    for x in sorted(d.datastores, key=lambda x: (_norm(x.cluster), _norm(x.datastore))):
        free_pct = 100 * x.free_gb / x.capacity_gb
        prov = x.provisioned_gb if x.provisioned_gb is not None else prov_by.get(_norm(x.datastore))
        oc = prov / x.capacity_gb if prov else None
        status = "critical" if free_pct < s.datastore_crit_free_pct else "warning" if free_pct < s.datastore_warn_free_pct else "ok"
        if status == "ok" and oc and oc > s.thin_overcommit_max:
            status = "overcommitted"
        out.append(dict(ds=x, free_pct=free_pct, provisioned=prov, overcommit=oc, status=status))
    return out


# ---------------------------------------------------------------- per cluster

def cluster_rows(d: Data) -> list[dict]:
    s = d.settings
    caps = {_norm(c.cluster): capacity(d, c) for c in d.clusters}
    rec = reclaim(d, caps)
    dss = datastore_rows(d)
    rows = []
    for c in d.clusters:
        cap = caps[_norm(c.cluster)]
        sav = reclaim_totals(rec, c.cluster)
        fc = forecast(d, cap, sav)
        av = availability(d, c)
        lc = lifecycle(d, cap)
        iss: list[Issue] = []
        add = lambda sev, code, text: iss.append(Issue(sev, code, text))  # noqa: E731
        n = cap["failover"]
        # CAP-01 headroom after N host failures
        for m in ("cpu", "ram"):
            p = cap["pct_after"][m]
            if p is None:
                continue
            mx = cap["max"][m]
            txt = (f"{LABEL[m]} p95 {_g(cap['now'][m])} {UNIT[m]} = {p:.0f}% of what is left after {_n(n, 'host')} fails "
                   f"({_g(cap['after'][m])} {UNIT[m]}; max {mx:g}%)")
            if p > mx:
                add(CRITICAL, "CAP-01", txt)
            elif p > mx - s.warn_margin_pct:
                add(WARNING, "CAP-01", txt + f", {_g(cap['headroom'][m])} {UNIT[m]} headroom")
        # CAP-02 vCPU:pCPU
        if cap["cores"] and cap["ratio"] > cap["ratio_max"]:
            sev = CRITICAL if cap["ratio"] > cap["ratio_max"] * 1.5 else WARNING
            add(sev, "CAP-02", f"vCPU:pCPU {cap['ratio']:.1f}:1 ({cap['vcpu']} vCPU on {cap['cores']} cores; {cap['ratio_after']:.1f}:1 after a "
                               f"host failure), policy {cap['ratio_max']:g}:1")
        # CAP-03 RAM overcommit
        if cap["after"]["ram"] and cap["overcommit"] > s.ram_overcommit_max:
            add(WARNING, "CAP-03", f"{_g(cap['vram'])} GB vRAM on running VMs = {cap['overcommit']:.2f}x the RAM left after a host failure "
                                   f"(max {s.ram_overcommit_max:g}x)")
        # CAP-04 hosts not connected
        if cap["not_connected"]:
            add(WARNING, "CAP-04", f"{_top([f'{h.host} ({h.state})' for h in cap['not_connected']])}: the cluster is running without its "
                                   f"failover capacity")
        # forecast
        if fc["months"] < s.history_min_months:
            add(WARNING, "FCT-03", f"{_n(fc['months'], 'month')} of utilisation history (minimum {s.history_min_months}): the trend is not reliable")
        lead, hz = s.purchase_lead_months, s.plan_horizon_months
        rw = fc["runway"]
        if fc["series"]:
            bm = fc["binding"].get("pipeline")
            txt = (f"{LABEL[bm] if bm else 'No metric'} reaches the N+{n} limit "
                   + (f"in {mlabel(fc['breach']['pipeline'])} ({runway_text(rw['pipeline'], s.forecast_months)})" if bm else
                      f"within {s.forecast_months} months") +
                   f" with the approved demand; trend alone {runway_text(rw['trend'], s.forecast_months)}, with the reclaim list "
                   f"{runway_text(rw['reclaim'], s.forecast_months)}; purchase lead time {lead} months")
            if rw["pipeline"] is not None and rw["pipeline"] <= lead:
                add(CRITICAL, "FCT-01", txt)
            elif rw["pipeline"] is not None and rw["pipeline"] <= hz:
                add(WARNING, "FCT-01", txt)
        # demand that does not fit at its go-live
        for x, short in demand_fit(d, cap, fc):
            if short:
                m, need, left = short
                add(WARNING, "DEM-01", f"{x.project} (go-live {x.go_live:%b %Y}) needs {_g(need)} {UNIT[m]} {LABEL[m]} and {_g(max(0, left))} "
                                       f"{UNIT[m]} is left under the N+{n} limit that month")
        # storage
        for r in dss:
            if _norm(r["ds"].cluster) != _norm(c.cluster):
                continue
            x = r["ds"]
            if r["free_pct"] < s.datastore_crit_free_pct:
                add(CRITICAL, "STO-01", f"{x.datastore} {r['free_pct']:.0f}% free ({_g(x.free_gb)} of {_g(x.capacity_gb)} GB)")
            elif r["free_pct"] < s.datastore_warn_free_pct:
                add(WARNING, "STO-01", f"{x.datastore} {r['free_pct']:.0f}% free ({_g(x.free_gb)} of {_g(x.capacity_gb)} GB)")
            if r["overcommit"] and r["overcommit"] > s.thin_overcommit_max:
                add(WARNING, "STO-02", f"{x.datastore} thin provisioned {r['overcommit']:.2f}x its capacity ({_g(r['provisioned'])} GB "
                                       f"provisioned, max {s.thin_overcommit_max:g}x)")
        # availability
        if av["pct"] < s.availability_target_pct:
            add(CRITICAL, "AVL-01", f"{av['pct']:.3f}% in {mlabel(d.period)} (target {s.availability_target_pct:g}%): "
                                    f"{av['outage']:.0f} min of cluster outage" +
                (f" ({_top([(o.ticket + ' ' if o.ticket else '') + o.summary for o in av['outages']], 2)})" if av["outages"] else ""))
        if av["failures"]:
            add(WARNING, "AVL-02", f"{_n(len(av['failures']), 'host failure')} in {mlabel(d.period)}, {av['restarted']} VMs restarted by HA: "
                                   + _top([f"{e.host or '?'} {e.start:%d %b} ({e.summary or e.ticket})" for e in av["failures"]], 3))
        # lifecycle
        if lc["ended"]:
            add(WARNING, "LCM-01", f"{_n(len(lc['ended']), 'host')} out of vendor support: " +
                _top([f"{h.host} {h.model} (ended {h.support_end:%b %Y})" for h in lc["ended"]]))
        sev = CRITICAL if any(i.severity == CRITICAL for i in iss) else WARNING if iss else None
        rows.append(dict(cluster=c, cap=cap, forecast=fc, saving=sav, availability=av, lifecycle=lc, issues=iss, severity=sev,
                         verdict=VERDICT[sev]))
    return rows


# ---------------------------------------------------------------- findings

def _collect(rows: list[dict], code: str) -> list[tuple[dict, Issue]]:
    return [(r, i) for r in rows for i in r["issues"] if i.code == code]


def run_all(d: Data, rows: list[dict] | None = None, rec: list[dict] | None = None) -> list[Finding]:
    s = d.settings
    rows = rows if rows is not None else cluster_rows(d)
    if rec is None:
        rec = reclaim(d, {_norm(r["cluster"].cluster): r["cap"] for r in rows})
    o = s.owner
    out: list[Finding] = []

    def per_cluster(code, area, owner, word, fix):
        hits = _collect(rows, code)
        for sev in (CRITICAL, WARNING):
            hs = [(r, i) for r, i in hits if i.severity == sev]
            if not hs:
                continue
            ids = sorted({r["cluster"].cluster for r, _ in hs})
            det = [f"{r['cluster'].cluster}: {i.text}" for r, i in hs]
            out.append(Finding(sev, area, code, f"{_n(len(ids), 'cluster')} {word}", _top(det, 8), fix, owner, ids))

    per_cluster("CAP-01", "Capacity", o("capacity"), "short of headroom after a host failure",
                "Do not place new VMs here until the headroom is back: apply the reclaim list, move workloads to a cluster with room, "
                "or add hosts. Check the HA admission control policy matches the N+1 used here.")
    per_cluster("CAP-02", "Capacity", o("capacity"), "above the vCPU:pCPU policy",
                "Right-size the oversized VMs first; then check CPU ready on the busiest VMs before placing more vCPUs.")
    per_cluster("CAP-03", "Capacity", o("capacity"), "with RAM overcommitted after a host failure",
                "After a host failure the restarted VMs will balloon and swap: right-size vRAM or add RAM before the next project lands.")
    per_cluster("CAP-04", "Capacity", o("capacity"), "running without their failover host",
                "Bring the host back or finish the maintenance before the next change window; no new VMs until then.")
    per_cluster("FCT-01", "Forecast", o("forecast"), "reaching the N+1 limit within the planning horizon",
                "Decide at this meeting: approve the reclaim list, and order the hosts so they are in production before the month the "
                "forecast with reclaim crosses the limit (lead time from settings).")
    per_cluster("FCT-03", "Forecast", o("forecast"), "without enough utilisation history",
                "Export at least 6 months of daily statistics (vCenter keeps the 1-day rollup for a year) before trusting the runway.")
    per_cluster("DEM-01", "Forecast", o("forecast"), "with approved projects that do not fit at go-live",
                "Condition the approval of these projects on the capacity being there, or place them on another cluster.")
    per_cluster("STO-01", "Storage", o("storage"), "with datastores below the free space threshold",
                "Move VMs to a datastore with room (Storage vMotion), delete old snapshots and powered-off VMs on it, or grow the volume.")
    per_cluster("STO-02", "Storage", o("storage"), "with thin provisioning above the policy",
                "Stop placing thin disks there and keep the free space above the threshold: a few disks filling up at once fill the datastore.")
    per_cluster("AVL-01", "Availability", o("availability"), "below the availability target this month",
                "Problem record with the root cause and the fix date; report it in the monthly availability report against the target.")
    per_cluster("AVL-02", "Availability", o("availability"), "with host failures this month",
                "Hardware case with the vendor, and check HA restarted every VM; repeat failures on the same host mean replace it.")
    per_cluster("LCM-01", "Lifecycle", o("lifecycle"), "with hosts out of vendor support",
                "Renew support or plan their replacement in the next capacity order; they should not count as failover capacity.")
    # reclaim, estate-wide
    groups = [("RCL-01", ("powered off 90+ days", "powered off, never seen on", "powered off 30+ days"), "powered off for 30 days or more",
               "Send each owner the list; delete what is off for 90+ days after an archive backup, unless the owner justifies it."),
              ("RCL-02", ("idle",), "idle (powered on, doing nothing)",
               "Confirm with the owner, power off for 30 days, then delete; the RAM and CPU come back as soon as they are off."),
              ("RCL-03", ("oversized",), "oversized against their p95",
               "Right-size in the next maintenance window through the CAB; vRAM and vCPU changes need a reboot."),
              ("RCL-04", ("old snapshot",), "with snapshots older than the policy",
               "Consolidate after checking the backup; a snapshot is not a backup and grows every day."),
              ("RCL-05", ("no owner",), "without an owner",
               "Find the owner in the CMDB, the change record or the creator in the vCenter events; tag it, or power it off after notice.")]
    for code, reasons, word, fix in groups:
        hits = [r for r in rec if r["reason"] in reasons]
        if not hits:
            continue
        vms = sorted({r["vm"].vm for r in hits})
        ram = sum(r["ram"] for r in hits if r["counted"])
        sto = sum(r["storage"] for r in hits if r["counted"])
        gain = ", ".join(x for x in (f"{_g(ram)} GB RAM" if ram else "", f"{_g(sto)} GB storage" if sto else "") if x)
        sev = CRITICAL if code == "RCL-04" and any(r["age"] > s.snapshot_crit_days for r in hits) else WARNING
        out.append(Finding(sev, "Reclaim", code, f"{_n(len(vms), 'VM')} {word}" + (f" ({gain})" if gain else ""),
                           _top([f"{r['vm'].vm} [{r['vm'].cluster}] {r['detail']}" for r in hits], 6), fix, o("reclaim"), vms))
    # information
    for r in rows:
        fc, c = r["forecast"], r["cluster"]
        if fc["series"] and not any(i.code == "FCT-01" for i in r["issues"]):
            out.append(Finding(INFO, "Forecast", "FCT-02", f"{c.cluster}: runway {runway_text(fc['runway']['pipeline'], s.forecast_months)} "
                                                          f"with the approved demand",
                               f"trend alone {runway_text(fc['runway']['trend'], s.forecast_months)}; binding metric "
                               f"{LABEL.get(fc['binding']['pipeline'] or '', 'none')}", "Review again next month.", o("forecast"), [c.cluster]))
    other = [x for x in d.demand if not counted(d, x) or not any(_norm(c.cluster) == _norm(x.cluster) for c in d.clusters)]
    if other:
        out.append(Finding(INFO, "Forecast", "DEM-02", f"{_n(len(other), 'demand item')} not in the forecast",
                           _top([f"{x.project} ({x.status or 'no status'}, {x.cluster or 'no cluster'}"
                                 f"{', no date' if not x.go_live else ''})" for x in other]),
                           "Only approved items with a cluster and a date count; confirm the rest at the demand review.", o("forecast"),
                           [x.project for x in other]))
    met = [r for r in rows if r["availability"]["pct"] >= s.availability_target_pct]
    if met:
        out.append(Finding(INFO, "Availability", "AVL-03", f"{_n(len(met), 'cluster')} met the {s.availability_target_pct:g}% target in "
                                                           f"{mlabel(d.period)}",
                           ", ".join(f"{r['cluster'].cluster} {r['availability']['pct']:.3f}%" for r in met), "Nothing to do.",
                           o("availability"), [r["cluster"].cluster for r in met]))
    ending = [(r["cluster"].cluster, h) for r in rows for h in r["lifecycle"]["ending"]]
    if ending:
        out.append(Finding(INFO, "Lifecycle", "LCM-01", f"{_n(len(ending), 'host')} leaving vendor support within {s.support_warn_days} days",
                           _top([f"{h.host} [{c}] {h.support_end:%d %b %Y}" for c, h in ending]),
                           "Renew or include the replacement in the next capacity order.", o("lifecycle"), [h.host for _, h in ending]))
    behind = [(r["cluster"].cluster, h) for r in rows for h in r["lifecycle"]["behind"]]
    if behind:
        out.append(Finding(INFO, "Lifecycle", "LCM-02", f"{_n(len(behind), 'host')} behind the firmware baseline",
                           _top([f"{h.host} [{c}] {h.firmware} (baseline {h.firmware_target})" for c, h in behind]),
                           "Plan the update host by host in maintenance mode, through the CAB.", o("lifecycle"), [h.host for _, h in behind]))
    out.sort(key=lambda f: (ORDER[f.severity], AREAS.index(f.area), f.code))
    return out


def area_status(findings: list[Finding]) -> dict[str, str]:
    st = {}
    for a in AREAS:
        sev = [f.severity for f in findings if f.area == a and f.severity != INFO]
        st[a] = CRITICAL if CRITICAL in sev else WARNING if sev else "ok"
    return st


def order_by(d: Data, r: dict) -> tuple[int, int] | None:
    """Last month to order the hosts so they are in production before the limit is crossed with the reclaim list applied."""
    b = r["forecast"]["breach"].get("reclaim")
    return add_months(b, -d.settings.purchase_lead_months) if b else None


def now_label(when: date) -> str:
    return f"{when:%a %d %b %Y}"

