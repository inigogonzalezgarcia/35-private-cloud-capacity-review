"""Outputs: report.html, capacity-review.md and the CSVs (clusters, forecast, reclaim, datastores, demand, actions, findings)."""
from __future__ import annotations

import csv
import html
import math
from pathlib import Path

from .checks import (AREAS, CRITICAL, INFO, LABEL, METRICS, UNIT, WARNING, Finding, area_status, cluster_rows, counted, datastore_rows,
                     demand_fit, demand_load, now_label, order_by, reclaim, runway_text)
from .model import Data, _norm, add_months, mkey, mlabel, month_diff

e = html.escape


def build(d: Data) -> dict:
    rows = cluster_rows(d)
    rec = reclaim(d, {_norm(r["cluster"].cluster): r["cap"] for r in rows})
    return dict(rows=rows, reclaim=rec, datastores=datastore_rows(d))


def _g(v: float | None, nd: int = 0) -> str:
    return "" if v is None else f"{v:,.{nd}f}"


def hosts_needed(d: Data, r: dict) -> int:
    """Hosts like the largest in the cluster to add so it stays under its limits for `plan_horizon_months`, with reclaim and demand."""
    cap, fc = r["cap"], r["forecast"]
    if not cap["hosts"]:
        return 0
    target = add_months(d.now_month, d.settings.plan_horizon_months)
    best = 0
    for m in ("cpu", "ram"):
        row = next((x for x in fc["series"].get(m, []) if x["month"] == target), None)
        if not row:
            continue
        per = max(h.cpu_ghz if m == "cpu" else h.ram_gb for h in cap["hosts"]) * cap["max"][m] / 100
        need = row["reclaim"] - cap["limit"][m]
        if need > 0 and per:
            best = max(best, math.ceil(need / per))
    return best


def decision(d: Data, r: dict) -> str:
    s, fc = d.settings, r["forecast"]
    rw = fc["runway"]
    ob = order_by(d, r)
    k = hosts_needed(d, r)
    hosts = f"{k} host{'s' if k != 1 else ''}" if k else "hosts"
    if rw.get("pipeline") is None:
        return f"No purchase needed in the next {s.forecast_months} months."
    if rw["pipeline"] <= s.purchase_lead_months:
        if rw.get("reclaim") is None or rw["reclaim"] > s.purchase_lead_months:
            return (f"Approve the reclaim list now and order {hosts} by {mlabel(ob)}: the reclaim buys the time the purchase lead time needs."
                    if ob and month_diff(d.now_month, ob) >= 0 else f"Approve the reclaim list and order {hosts} now.")
        return f"Order {hosts} now (expedite) and stop placing new VMs: the reclaim list alone does not cover the lead time."
    if rw["pipeline"] <= s.plan_horizon_months:
        return f"Plan the order of {hosts} by {mlabel(ob)}; review the trend next month." if ob else "Review the trend next month."
    return "No purchase needed in the planning horizon."


def summary(d: Data, findings: list[Finding], b: dict) -> dict:
    rows = b["rows"]
    with_rw = [r for r in rows if r["forecast"]["runway"].get("pipeline") is not None]
    tight = min(with_rw, key=lambda r: (r["forecast"]["runway"]["pipeline"], r["cluster"].cluster)) if with_rw else None
    rec = b["reclaim"]
    tot = dict(ram=0.0, storage=0.0, cpu=0.0, vms=0)
    for r in rows:
        for k in ("ram", "storage", "cpu", "vms"):
            tot[k] += r["saving"][k]
    av = min(rows, key=lambda r: (r["availability"]["pct"], r["cluster"].cluster))
    return dict(clusters=len(rows), hosts=len(d.hosts), vms=len(d.vms), at_risk=sum(r["verdict"] == "at risk" for r in rows),
                attention=sum(r["verdict"] == "attention" for r in rows), ok=sum(r["verdict"] == "ok" for r in rows),
                tight=tight, n1_over=sum(any(i.code == "CAP-01" and i.severity == CRITICAL for i in r["issues"]) for r in rows),
                n1_near=sum(any(i.code == "CAP-01" and i.severity == WARNING for i in r["issues"]) and
                            not any(i.code == "CAP-01" and i.severity == CRITICAL for i in r["issues"]) for r in rows),
                reclaim_vms=len({_norm(x["vm"].vm) for x in rec}), reclaim=tot, worst_av=av,
                ds_low=sum(x["status"] in ("critical", "warning") for x in b["datastores"]), datastores=len(b["datastores"]),
                demand=sum(counted(d, x) for x in d.demand),
                critical=sum(f.severity == CRITICAL for f in findings), warning=sum(f.severity == WARNING for f in findings),
                info=sum(f.severity == INFO for f in findings))


def headline(d: Data, sm: dict) -> str:
    t = sm["tight"]
    rt = (f"tightest runway {t['cluster'].cluster} {runway_text(t['forecast']['runway']['pipeline'], d.settings.forecast_months)} "
          f"({mlabel(t['forecast']['breach']['pipeline'])}, {LABEL[t['forecast']['binding']['pipeline']]}) vs {d.settings.purchase_lead_months}-month "
          f"lead time" if t else "no cluster reaches its limit")
    av = sm["worst_av"]
    return (f"{sm['clusters']} clusters ({sm['at_risk']} at risk, {sm['attention']} attention, {sm['ok']} ok) · {rt} · reclaim "
            f"{sm['reclaim_vms']} VMs: {sm['reclaim']['ram']:,.0f} GB RAM, {sm['reclaim']['storage'] / 1024:,.1f} TB storage · availability "
            f"{mlabel(d.period)} lowest {av['cluster'].cluster} {av['availability']['pct']:.3f}% · {sm['critical']} critical, {sm['warning']} warnings")


# ---------------------------------------------------------------- csv

def _w(path: Path, header: list[str], rows: list[list]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)


def _f(v, nd: int = 1) -> str:
    return "" if v is None else f"{v:.{nd}f}"


def write_findings(findings: list[Finding], path: Path) -> None:
    _w(path, ["severity", "area", "code", "subject", "detail", "next_step", "owner", "items"],
       [[f.severity, f.area, f.code, f.subject, f.detail, f.fix, f.owner, "; ".join(f.items)] for f in findings])


def write_actions(findings: list[Finding], path: Path) -> None:
    _w(path, ["ref", "action", "next_step", "owner", "severity", "due", "done"],
       [[f.code, f.subject, f.fix, f.owner, f.severity, "", ""] for f in findings if f.severity != INFO])


def write_clusters(d: Data, b: dict, path: Path) -> None:
    out = []
    for r in b["rows"]:
        c, cap, fc, av = r["cluster"], r["cap"], r["forecast"], r["availability"]
        rw, br = fc["runway"], fc["breach"]
        out.append([c.cluster, c.site, c.platform, len(cap["hosts"]), cap["failover"], cap["cores"], _f(cap["ghz"]), _f(cap["ram"], 0),
                    _f(cap["after"]["cpu"]), _f(cap["after"]["ram"], 0), _f(cap["now"]["cpu"]), _f(cap["now"]["ram"], 0),
                    _f(cap["pct_after"]["cpu"]), _f(cap["pct_after"]["ram"]), _f(cap["headroom"]["cpu"]), _f(cap["headroom"]["ram"], 0),
                    cap["running"], cap["vcpu"], _f(cap["ratio"], 2), _f(cap["vram"], 0), _f(cap["overcommit"], 2), _f(av["pct"], 3),
                    "" if rw.get("trend") is None else rw["trend"], "" if rw.get("pipeline") is None else rw["pipeline"],
                    "" if rw.get("reclaim") is None else rw["reclaim"], mkey(br["pipeline"]) if br.get("pipeline") else "",
                    fc["binding"].get("pipeline") or "", mkey(order_by(d, r)) if order_by(d, r) else "", hosts_needed(d, r), r["verdict"],
                    " | ".join(f"{i.code} {i.text}" for i in r["issues"])])
    _w(path, ["cluster", "site", "platform", "hosts", "failover_hosts", "cores", "cpu_ghz", "ram_gb", "cpu_ghz_after_failure",
              "ram_gb_after_failure", "cpu_used_ghz_p95", "ram_used_gb_p95", "cpu_pct_after_failure", "ram_pct_after_failure",
              "cpu_headroom_ghz", "ram_headroom_gb", "vms_running", "vcpu_running", "vcpu_pcpu_ratio", "vram_running_gb",
              "ram_overcommit_after_failure", "availability_pct", "runway_trend_months", "runway_pipeline_months", "runway_reclaim_months",
              "limit_month_pipeline", "binding_metric", "order_by", "hosts_to_add", "verdict", "issues"], out)


def write_forecast(b: dict, path: Path) -> None:
    out = []
    for r in b["rows"]:
        cap, fc = r["cap"], r["forecast"]
        for m in METRICS:
            for x in fc["series"].get(m, []):
                out.append([r["cluster"].cluster, m, UNIT[m], mkey(x["month"]), "actual" if x["actual"] is not None else "forecast",
                            _f(x["actual"]), _f(x["trend"]), _f(x["pipeline"]), _f(x["reclaim"]), _f(cap["limit"][m])])
    _w(path, ["cluster", "metric", "unit", "month", "kind", "actual", "trend", "with_pipeline", "with_reclaim", "limit"], out)


def write_reclaim(b: dict, path: Path) -> None:
    _w(path, ["vm", "cluster", "owner", "service", "power_state", "reason", "detail", "vcpu", "ram_gb", "provisioned_gb", "saving_vcpu",
              "saving_ram_gb", "saving_cpu_ghz", "saving_storage_gb", "counted_in_forecast", "action"],
       [[v.vm, v.cluster, v.owner, v.service, v.power, x["reason"], x["detail"], v.vcpu, f"{v.ram_gb:g}", f"{v.provisioned_gb:g}", x["vcpu"],
         f"{x['ram']:g}", _f(x["cpu"]), f"{x['storage']:g}", "yes" if x["counted"] else "no", x["action"]]
        for x in b["reclaim"] for v in [x["vm"]]])


def write_datastores(b: dict, path: Path) -> None:
    _w(path, ["datastore", "cluster", "type", "capacity_gb", "free_gb", "free_pct", "provisioned_gb", "overcommit", "status"],
       [[x.datastore, x.cluster, x.kind, f"{x.capacity_gb:g}", f"{x.free_gb:g}", _f(r["free_pct"]), _g(r["provisioned"]).replace(",", ""),
         _f(r["overcommit"], 2), r["status"]] for r in b["datastores"] for x in [r["ds"]]])


def _fits(d: Data, b: dict) -> dict:
    out = {}
    for r in b["rows"]:
        for x, short in demand_fit(d, r["cap"], r["forecast"]):
            out[id(x)] = (r, short)
    return out


def write_demand(d: Data, b: dict, path: Path) -> None:
    fits = _fits(d, b)
    rows = []
    for x in sorted(d.demand, key=lambda x: (x.go_live is None, x.go_live, x.project)):
        r, short = fits.get(id(x), (None, None))
        ld = demand_load(r["cap"], x, d.settings.demand_ram_factor) if r else None
        rows.append([x.project, x.cluster, x.go_live.isoformat() if x.go_live else "", x.status, x.vms, x.vcpu, f"{x.ram_gb:g}", f"{x.storage_gb:g}",
                     _f(ld["cpu"]) if ld else "", "yes" if r else "no",
                     ("no" if short else "yes") if r else "", f"{LABEL[short[0]]}: needs {short[1]:,.0f} {UNIT[short[0]]}, {max(0, short[2]):,.0f} left"
                     if short else "", x.owner, x.ticket])
    _w(path, ["project", "cluster", "go_live", "status", "vms", "vcpu", "ram_gb", "storage_gb", "cpu_ghz_estimate", "in_forecast", "fits",
              "shortfall", "owner", "ticket"], rows)


# ---------------------------------------------------------------- capacity review pack

def review_md(d: Data, findings: list[Finding], b: dict) -> str:
    s, sm = d.settings, summary(d, findings, b)
    hz = s.forecast_months
    out = [f"# Capacity and availability review · {s.organisation}", "",
           f"Monthly capacity meeting of {now_label(d.as_of)} · utilisation and availability of {mlabel(d.period)} · forecast {hz} months · "
           f"purchase lead time {s.purchase_lead_months} months.", "", "## Summary", "",
           f"- Clusters: {sm['clusters']} ({sm['hosts']} hosts, {sm['vms']} VMs): {sm['at_risk']} at risk, {sm['attention']} need attention, "
           f"{sm['ok']} ok.",
           f"- N+1 today: {sm['n1_over']} cluster(s) over the limit after a host failure, {sm['n1_near']} within {s.warn_margin_pct:g} points of it.",
           (f"- Tightest runway: {sm['tight']['cluster'].cluster}, {runway_text(sm['tight']['forecast']['runway']['pipeline'], hz)} "
            f"({mlabel(sm['tight']['forecast']['breach']['pipeline'])}) with the approved demand." if sm["tight"] else "- No cluster reaches its limit."),
           f"- Reclaim: {sm['reclaim_vms']} VMs, {sm['reclaim']['ram']:,.0f} GB RAM and {sm['reclaim']['storage']:,.0f} GB storage counted in the forecast.",
           f"- Storage: {sm['ds_low']} of {sm['datastores']} datastores below the free space threshold.",
           f"- Availability {mlabel(d.period)}: lowest {sm['worst_av']['cluster'].cluster} {sm['worst_av']['availability']['pct']:.3f}% "
           f"(target {s.availability_target_pct:g}%).",
           f"- Findings: {sm['critical']} critical, {sm['warning']} warnings, {sm['info']} for information.", "",
           "## Decisions needed", "",
           "| Cluster | Binding | Limit crossed (approved demand) | With reclaim | Trend only | Order by | Hosts to add | Decision |",
           "|---|---|---|---|---|---|---|---|"]
    for r in b["rows"]:
        fc = r["forecast"]
        rw, br = fc["runway"], fc["breach"]
        ob = order_by(d, r)
        out.append(f"| {r['cluster'].cluster} | {LABEL.get(fc['binding'].get('pipeline') or '', '–')} | "
                   f"{mlabel(br.get('pipeline')) or '–'} ({runway_text(rw.get('pipeline'), hz)}) | {runway_text(rw.get('reclaim'), hz)} | "
                   f"{runway_text(rw.get('trend'), hz)} | {mlabel(ob) or '–'} | {hosts_needed(d, r) or '–'} | {decision(d, r)} |")
    out += ["", "Runway counts from this month. *Order by* is the month with the reclaim list applied minus the purchase lead time.", "",
            "## Actions", ""]
    out += [f"- [ ] **{f.code} {f.subject}** ({f.owner or 'owner?'}) → {f.fix}" for f in findings if f.severity != INFO] or ["- None."]
    out += ["", "## Clusters", ""]
    for r in b["rows"]:
        c, cap, fc, av = r["cluster"], r["cap"], r["forecast"], r["availability"]
        out += [f"### {c.cluster} · {c.site} · {c.platform} — {r['verdict'].upper()}", "",
                f"- {len(cap['hosts'])} hosts, {cap['cores']} cores, {cap['ghz']:,.0f} GHz, {cap['ram']:,.0f} GB RAM; N+{cap['failover']} "
                f"({c.ha_policy or 'policy not given'}).",
                f"- After {cap['failover']} host failure: CPU {_f(cap['pct_after']['cpu'], 0)}% (max {cap['max']['cpu']:g}%), RAM "
                f"{_f(cap['pct_after']['ram'], 0)}% (max {cap['max']['ram']:g}%); headroom {_g(cap['headroom']['cpu'])} GHz and "
                f"{_g(cap['headroom']['ram'])} GB.",
                f"- {cap['running']} of {cap['vms']} VMs running, vCPU:pCPU {cap['ratio']:.1f}:1 (policy {cap['ratio_max']:g}:1), vRAM "
                f"{cap['overcommit']:.2f}x the RAM left after a failure.",
                f"- Runway: trend {runway_text(fc['runway'].get('trend'), hz)}, approved demand {runway_text(fc['runway'].get('pipeline'), hz)}, "
                f"with reclaim {runway_text(fc['runway'].get('reclaim'), hz)}. Reclaim gives back {r['saving']['ram']:,.0f} GB RAM and "
                f"{r['saving']['storage']:,.0f} GB storage.",
                f"- Availability {mlabel(d.period)}: {av['pct']:.3f}%, {len(av['failures'])} host failure(s), {av['restarted']} VMs restarted by HA."]
        for i in sorted(r["issues"], key=lambda i: i.severity != CRITICAL):
            out.append(f"- [ ] {i.code}: {i.text}")
        out.append("")
    out += ["## Demand pipeline", "", "| Go-live | Project | Cluster | Status | vCPU | RAM GB | Storage GB | Fits |", "|---|---|---|---|---|---|---|---|"]
    fits = _fits(d, b)
    for x in sorted(d.demand, key=lambda x: (x.go_live is None, x.go_live, x.project)):
        r, short = fits.get(id(x), (None, None))
        fit = "not counted" if not r else (f"no ({LABEL[short[0]]})" if short else "yes")
        out.append(f"| {x.go_live:%b %Y} | {x.project} | {x.cluster or '–'} | {x.status} | {x.vcpu} | {x.ram_gb:g} | {x.storage_gb:g} | {fit} |"
                   if x.go_live else f"| – | {x.project} | {x.cluster or '–'} | {x.status} | {x.vcpu} | {x.ram_gb:g} | {x.storage_gb:g} | {fit} |")
    out += ["", "## Reclaim by cluster", "", "| Cluster | VMs | RAM GB | Storage GB |", "|---|---|---|---|"]
    out += [f"| {r['cluster'].cluster} | {r['saving']['vms']} | {r['saving']['ram']:,.0f} | {r['saving']['storage']:,.0f} |" for r in b["rows"]]
    out += ["", "Full list with the owner and the action for each VM: `reclaim.csv`.", "", "## For information", ""]
    out += [f"- {f.code} {f.subject}: {f.detail}" for f in findings if f.severity == INFO] or ["- Nothing."]
    out += ["", "Generated by capreview from the cluster, host, VM, datastore, utilisation, demand and event exports.", ""]
    return "\n".join(out)


# ---------------------------------------------------------------- html

CSS = """
:root{--bg:#f6f7f8;--card:#fff;--ink:#1b2128;--muted:#66717c;--line:#e2e5e9;--ok:#1e7a4c;--warn:#a46200;--bad:#b42318;--info:#0b6bcb;
--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--hist:#52514e;--grid:#e8eaed}
@media (prefers-color-scheme:dark){:root{--bg:#13171b;--card:#1b2127;--ink:#e7eaee;--muted:#9aa5b0;--line:#2b333b;--ok:#4cc38a;--warn:#f0a83a;
--bad:#f2685c;--info:#5aa9f0;--s1:#3987e5;--s2:#d95926;--s3:#199e70;--hist:#c3c2b7;--grid:#2b333b}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:1100px;margin:0 auto;padding:28px 18px 60px}h1{font-size:26px;margin:0 0 4px}h2{font-size:18px;margin:34px 0 12px}
.muted{color:var(--muted)}.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px;margin-top:20px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}.tile b{display:block;font-size:24px;white-space:nowrap}
.areas{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.area{background:var(--card);border:1px solid var(--line);border-left:5px solid var(--ok);border-radius:10px;padding:10px 14px}
.area.critical{border-left-color:var(--bad)}.area.warning{border-left-color:var(--warn)}
.area .lbl{display:block;font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);margin-bottom:4px}.area .cnt{display:block;font-size:13px;color:var(--muted);margin-top:4px}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}th{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}
tr:last-child td{border-bottom:0}.wrap{overflow-x:auto}.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.pill{display:inline-block;white-space:nowrap;padding:1px 8px;border-radius:99px;font-size:12px;font-weight:600;border:1px solid currentColor}
.critical{color:var(--bad)}.warning{color:var(--warn)}.info{color:var(--info)}.ok{color:var(--ok)}
.area.critical,.area.warning,.area.ok{color:inherit}
.pill.ok{color:var(--ok)}.pill.attention{color:var(--warn)}.pill.at-risk{color:var(--bad)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(480px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}.card h3{margin:0;font-size:16px}
.card .head{display:flex;justify-content:space-between;gap:8px;align-items:flex-start;margin-bottom:8px}
.kv{display:grid;grid-template-columns:repeat(3,1fr);gap:6px 12px;font-size:13px;margin:10px 0}.kv b{display:block;font-size:16px}
.meter{font-size:13px;margin:6px 0}.meter .track{position:relative;height:10px;border-radius:5px;background:var(--grid);margin-top:3px}
.meter .fill{position:absolute;left:0;top:0;bottom:0;border-radius:5px;background:var(--s1)}.meter .fill.warning{background:var(--warn)}.meter .fill.critical{background:var(--bad)}
.meter .cap{position:absolute;top:-3px;bottom:-3px;width:2px;background:var(--ink)}
.legend{display:flex;flex-wrap:wrap;gap:12px;font-size:12px;color:var(--muted);margin:8px 0 2px}.legend i{display:inline-block;width:14px;height:3px;border-radius:2px;vertical-align:middle;margin-right:5px}
svg.chart{width:100%;height:auto;display:block}svg.chart text{font:11px system-ui,sans-serif;fill:var(--muted)}
.iss{font-size:13px;margin:8px 0 0;padding-left:0;list-style:none}.iss li{margin:3px 0}
td.nw{white-space:nowrap}td.fix{font-size:13px;color:var(--muted);max-width:380px;min-width:220px}
footer{margin-top:40px;font-size:13px}
@media (max-width:560px){.cards{grid-template-columns:1fr}.kv{grid-template-columns:repeat(2,1fr)}}
"""

SERIES = (("actual", "var(--hist)", "Actual p95"), ("trend", "var(--s1)", "Trend"), ("pipeline", "var(--s2)", "Trend + approved demand"),
          ("reclaim", "var(--s3)", "… minus reclaim"))


def _pill(cls: str, text: str) -> str:
    return f"<span class='pill {cls}'>{e(text)}</span>"


def _vpill(v: str) -> str:
    return _pill(v.replace(" ", "-"), v.upper())


def chart(d: Data, r: dict, metric: str) -> str:
    """Runway chart: the actual p95 of the last months, the three forecasts and the N+1 limit, as inline SVG."""
    fc, cap = r["forecast"], r["cap"]
    rows = fc["series"].get(metric) or []
    if not rows:
        return "<p class='muted'>No utilisation history.</p>"
    br = fc["breach"].get((metric, "pipeline"))
    last_hist = max(i for i, x in enumerate(rows) if x["actual"] is not None)
    upto = min(len(rows), max(last_hist + 13, (rows.index(next(x for x in rows if x["month"] == br)) + 4) if br else 0))
    rows = rows[:upto]
    lim = cap["limit"][metric]
    vals = [v for x in rows for k, *_ in SERIES for v in [x[k]] if v is not None] + [lim]
    lo, hi = min(vals) * 0.92, max(vals) * 1.04
    W, H, L, R, T, B = 520, 210, 46, 14, 10, 24
    X = lambda i: L + (W - L - R) * i / max(1, len(rows) - 1)  # noqa: E731
    Y = lambda v: T + (H - T - B) * (1 - (v - lo) / (hi - lo))  # noqa: E731
    g = [f"<svg class='chart' viewBox='0 0 {W} {H}' role='img' aria-label='{e(LABEL[metric])} runway of {e(r['cluster'].cluster)}'>"]
    for k in range(5):
        v = lo + (hi - lo) * k / 4
        g.append(f"<line x1='{L}' x2='{W - R}' y1='{Y(v):.1f}' y2='{Y(v):.1f}' stroke='var(--grid)' stroke-width='1'/>"
                 f"<text x='{L - 6}' y='{Y(v) + 4:.1f}' text-anchor='end'>{v:,.0f}</text>")
    for i, x in enumerate(rows):
        if x["month"][1] in (1, 7):
            g.append(f"<text x='{X(i):.1f}' y='{H - 6}' text-anchor='middle'>{mlabel(x['month'])}</text>")
    now = next((i for i, x in enumerate(rows) if x["month"] == d.now_month), None)
    if now is not None:
        g.append(f"<line x1='{X(now):.1f}' x2='{X(now):.1f}' y1='{T}' y2='{H - B}' stroke='var(--muted)' stroke-width='1' stroke-dasharray='2 3'/>"
                 f"<text x='{X(now) + 4:.1f}' y='{T + 10}'>now</text>")
    g.append(f"<line x1='{L}' x2='{W - R}' y1='{Y(lim):.1f}' y2='{Y(lim):.1f}' stroke='var(--bad)' stroke-width='1.5' stroke-dasharray='6 4'/>"
             f"<text x='{L + 6}' y='{Y(lim) - 5:.1f}' style='fill:var(--bad)'>N+{cap['failover']} limit {lim:,.0f} {UNIT[metric]}</text>")
    for key, color, name in SERIES:
        pts = [(i, x[key]) for i, x in enumerate(rows) if x[key] is not None]
        if len(pts) < 2:
            continue
        path = " ".join(f"{'M' if j == 0 else 'L'}{X(i):.1f},{Y(v):.1f}" for j, (i, v) in enumerate(pts))
        g.append(f"<path d='{path}' fill='none' stroke='{color}' stroke-width='2' stroke-linejoin='round' stroke-linecap='round'/>")
        for i, v in pts:
            g.append(f"<circle cx='{X(i):.1f}' cy='{Y(v):.1f}' r='6' fill='transparent'><title>{e(name)} · {mlabel(rows[i]['month'])}: "
                     f"{v:,.0f} {UNIT[metric]}</title></circle>")
    if br:
        i = next((i for i, x in enumerate(rows) if x["month"] == br), None)
        if i is not None:
            g.append(f"<circle cx='{X(i):.1f}' cy='{Y(rows[i]['pipeline']):.1f}' r='4.5' fill='var(--s2)' stroke='var(--card)' stroke-width='2'/>"
                     f"<text x='{X(i):.1f}' y='{Y(rows[i]['pipeline']) - 9:.1f}' text-anchor='middle' style='fill:var(--ink)'>{mlabel(br)}</text>")
    g.append("</svg>")
    leg = "".join(f"<span><i style='background:{c}'></i>{e(n)}</span>" for _, c, n in SERIES)
    return f"<div class='legend'>{leg}<span><i style='background:var(--bad)'></i>N+{cap['failover']} limit</span></div>" + "".join(g)


def _meter(label: str, pct: float | None, mx: float, margin: float) -> str:
    if pct is None:
        return f"<div class='meter'>{e(label)}: <span class='muted'>no data</span></div>"
    cls = "critical" if pct > mx else "warning" if pct > mx - margin else ""
    return (f"<div class='meter'>{e(label)} after a host failure: <b class='{cls}'>{pct:.0f}%</b> <span class='muted'>(max {mx:g}%)</span>"
            f"<div class='track'><div class='fill {cls}' style='width:{min(100, pct):.1f}%'></div><div class='cap' style='left:{mx:g}%'></div></div></div>")


def render_html(d: Data, findings: list[Finding], title: str, b: dict) -> str:
    s, sm, st = d.settings, summary(d, findings, b), area_status(findings)
    hz = s.forecast_months
    h = [f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
         f"<title>{e(title)}</title><style>{CSS}</style></head><body><main>",
         f"<h1>{e(title)}</h1><p class='muted'>N+1 headroom, runway with the approved demand and the reclaim list, storage and availability · "
         f"capacity meeting {now_label(d.as_of)} · data to {mlabel(d.period)}</p><div class='tiles'>"]
    t = sm["tight"]
    tiles = [("Clusters", str(sm["clusters"]), f"{sm['hosts']} hosts · {sm['vms']} VMs"),
             ("At risk", str(sm["at_risk"]), f"{sm['attention']} attention · {sm['ok']} ok"),
             ("Tightest runway", runway_text(t["forecast"]["runway"]["pipeline"], hz) if t else "–",
              f"{t['cluster'].cluster} · lead {s.purchase_lead_months} months" if t else ""),
             ("Over N+1 limit", str(sm["n1_over"]), f"{sm['n1_near']} within {s.warn_margin_pct:g} points"),
             ("Reclaim RAM", f"{sm['reclaim']['ram']:,.0f} GB", f"{sm['reclaim_vms']} VMs to review"),
             ("Reclaim storage", f"{sm['reclaim']['storage'] / 1024:,.1f} TB", "off VMs + snapshots"),
             ("Lowest availability", f"{sm['worst_av']['availability']['pct']:.3f}%", f"{sm['worst_av']['cluster'].cluster} · target {s.availability_target_pct:g}%"),
             ("Critical", str(sm["critical"]), f"{sm['warning']} warnings · {sm['info']} info")]
    h += [f"<div class='tile'><span class='muted'>{e(a)}</span><b>{e(x)}</b><span class='muted'>{e(y)}</span></div>" for a, x, y in tiles]
    h.append("</div><h2>Status by area</h2><div class='areas'>")
    for a in AREAS:
        n = sum(f.area == a and f.severity != INFO for f in findings)
        h.append(f"<div class='area {st[a]}'><span class='lbl'>{e(a)}</span>{_pill(st[a], 'OK' if st[a] == 'ok' else st[a].upper())}"
                 f"<span class='cnt'>{n} to act on</span></div>")
    h.append("</div>")
    # decisions
    h.append("<h2>Decisions for the capacity meeting</h2><div class='wrap'><table><tr><th>Cluster</th><th>Limit crossed</th><th>With reclaim</th>"
             "<th>Order by</th><th class='num'>Hosts</th><th>Decision</th></tr>")
    for r in b["rows"]:
        fc = r["forecast"]
        rw, br = fc["runway"], fc["breach"]
        cls = "critical" if rw.get("pipeline") is not None and rw["pipeline"] <= s.purchase_lead_months else \
            "warning" if rw.get("pipeline") is not None and rw["pipeline"] <= s.plan_horizon_months else ""
        h.append(f"<tr><td class='nw'><b>{e(r['cluster'].cluster)}</b><br><span class='muted'>{e(LABEL.get(fc['binding'].get('pipeline') or '', '–'))}"
                 f"</span></td><td class='nw {cls}'>{e(mlabel(br.get('pipeline')) or '–')}<br><span class='muted'>"
                 f"{e(runway_text(rw.get('pipeline'), hz))}</span></td><td class='nw'>{e(runway_text(rw.get('reclaim'), hz))}</td>"
                 f"<td class='nw'>{e(mlabel(order_by(d, r)) or '–')}</td><td class='num'>{hosts_needed(d, r) or '–'}</td>"
                 f"<td class='fix'>{e(decision(d, r))}</td></tr>")
    h.append("</table></div>")
    # cluster cards
    h.append("<h2>Clusters</h2><div class='cards'>")
    for r in b["rows"]:
        c, cap, fc, av = r["cluster"], r["cap"], r["forecast"], r["availability"]
        metric = fc["binding"].get("pipeline") or ("ram" if "ram" in fc["series"] else next(iter(fc["series"]), "ram"))
        avc = "critical" if av["pct"] < s.availability_target_pct else ""
        h.append(f"<div class='card'><div class='head'><div><h3>{e(c.cluster)}</h3><span class='muted'>{e(c.site)} · {e(c.platform)} · "
                 f"{len(cap['hosts'])} hosts · N+{cap['failover']}</span></div>{_vpill(r['verdict'])}</div>")
        h.append(_meter("CPU", cap["pct_after"]["cpu"], cap["max"]["cpu"], s.warn_margin_pct))
        h.append(_meter("RAM", cap["pct_after"]["ram"], cap["max"]["ram"], s.warn_margin_pct))
        kv = [("vCPU:pCPU", f"{cap['ratio']:.1f}:1", "warning" if cap["ratio"] > cap["ratio_max"] else ""),
              ("vRAM / RAM after failure", f"{cap['overcommit']:.2f}x", "warning" if cap["overcommit"] > s.ram_overcommit_max else ""),
              (f"Availability {mlabel(d.period)}", f"{av['pct']:.3f}%", avc),
              ("Runway, trend", runway_text(fc["runway"].get("trend"), hz), ""),
              ("With approved demand", runway_text(fc["runway"].get("pipeline"), hz),
               "critical" if fc["runway"].get("pipeline") is not None and fc["runway"]["pipeline"] <= s.purchase_lead_months else ""),
              ("With reclaim", runway_text(fc["runway"].get("reclaim"), hz), "")]
        h.append("<div class='kv'>" + "".join(f"<div><span class='muted'>{e(a)}</span><b class='{cl}'>{e(v)}</b></div>" for a, v, cl in kv) + "</div>")
        h.append(f"<div class='muted' style='font-size:13px'>{e(LABEL[metric])} runway ({UNIT[metric]})</div>" + chart(d, r, metric))
        if r["issues"]:
            h.append("<ul class='iss'>" + "".join(f"<li><span class='{i.severity}'>●</span> <b>{e(i.code)}</b> {e(i.text)}</li>"
                                                 for i in sorted(r["issues"], key=lambda i: i.severity != CRITICAL)) + "</ul>")
        h.append("</div>")
    h.append("</div>")
    crit = [f for f in findings if f.severity == CRITICAL]
    if crit:
        h.append("<h2>Fix first</h2><div class='wrap'><table><tr><th>Subject</th><th>What</th><th>Next step</th><th>Owner</th></tr>")
        h += [f"<tr><td><b>{e(f.subject)}</b><br><span class='muted'>{e(f.area)} · {e(f.code)}</span></td><td>{e(f.detail)}</td>"
              f"<td class='fix'>{e(f.fix)}</td><td class='nw'>{e(f.owner)}</td></tr>" for f in crit]
        h.append("</table></div>")
    # demand
    if d.demand:
        fits = _fits(d, b)
        h.append("<h2>Demand pipeline</h2><div class='wrap'><table><tr><th>Go-live</th><th>Project</th><th>Cluster</th><th>Status</th>"
                 "<th class='num'>vCPU</th><th class='num'>RAM GB</th><th class='num'>Storage GB</th><th>Fits</th></tr>")
        for x in sorted(d.demand, key=lambda x: (x.go_live is None, x.go_live, x.project)):
            r, short = fits.get(id(x), (None, None))
            fit = ("<span class='muted'>not counted</span>" if not r else
                   f"<span class='warning'>no: {e(LABEL[short[0]])} needs {short[1]:,.0f} {UNIT[short[0]]}, {max(0, short[2]):,.0f} left</span>"
                   if short else "<span class='ok'>yes</span>")
            h.append(f"<tr><td class='nw'>{x.go_live:%d %b %Y}</td>" if x.go_live else "<tr><td>–</td>")
            h.append(f"<td><b>{e(x.project)}</b><br><span class='muted'>{e(x.owner)} {e(x.ticket)}</span></td><td class='nw'>{e(x.cluster) or '–'}</td>"
                     f"<td>{e(x.status)}</td><td class='num'>{x.vcpu}</td><td class='num'>{x.ram_gb:,.0f}</td><td class='num'>{x.storage_gb:,.0f}</td>"
                     f"<td>{fit}</td></tr>")
        h.append("</table></div>")
    # reclaim
    if b["reclaim"]:
        rec = sorted(b["reclaim"], key=lambda x: (-(x["ram"] * 20 + x["storage"]), x["vm"].vm, x["reason"]))
        h.append(f"<h2>Reclaim candidates ({len({_norm(x['vm'].vm) for x in rec})} VMs)</h2><div class='wrap'><table><tr><th>VM</th><th>Cluster</th>"
                 "<th>Reason</th><th>Owner</th><th class='num'>RAM back</th><th class='num'>Storage back</th><th>Action</th></tr>")
        for x in rec[:30]:
            v = x["vm"]
            ram = f"{x['ram']:,.0f} GB" if x["ram"] else "–"
            sto = f"{x['storage']:,.0f} GB" if x["storage"] else "–"
            h.append(f"<tr><td class='nw'><b>{e(v.vm)}</b><br><span class='muted'>{e(v.service) or '–'}</span></td><td class='nw'>{e(v.cluster)}</td>"
                     f"<td>{e(x['reason'])}<br><span class='muted'>{e(x['detail'])}</span></td>"
                     f"<td class='{'warning' if not v.owner else ''}'>{e(v.owner) or 'none'}</td>"
                     f"<td class='num'>{ram}</td><td class='num'>{sto}</td>"
                     f"<td class='fix'>{e(x['action'])}</td></tr>")
        h.append("</table></div>" + (f"<p class='muted'>{len(rec) - 30} more rows in reclaim.csv.</p>" if len(rec) > 30 else ""))
    # datastores
    if b["datastores"]:
        h.append("<h2>Datastores and storage containers</h2><div class='wrap'><table><tr><th>Datastore</th><th>Cluster</th><th class='num'>Capacity GB</th>"
                 "<th class='num'>Free</th><th class='num'>Provisioned</th><th>Status</th></tr>")
        for x in b["datastores"]:
            ds = x["ds"]
            oc = f"{x['overcommit']:.2f}x" if x["overcommit"] else "–"
            cls = {"critical": "critical", "warning": "warning", "overcommitted": "warning"}.get(x["status"], "ok")
            h.append(f"<tr><td class='nw'><b>{e(ds.datastore)}</b><br><span class='muted'>{e(ds.kind)}</span></td><td class='nw'>{e(ds.cluster)}</td>"
                     f"<td class='num'>{ds.capacity_gb:,.0f}</td><td class='num {cls if x['status'] in ('critical', 'warning') else ''}'>{x['free_pct']:.0f}%</td>"
                     f"<td class='num'>{oc}</td>"
                     f"<td>{_pill(cls, x['status'].upper())}</td></tr>")
        h.append("</table></div>")
    # availability
    h.append(f"<h2>Availability in {mlabel(d.period)}</h2><div class='wrap'><table><tr><th>Cluster</th><th class='num'>Availability</th>"
             "<th class='num'>Outage min</th><th class='num'>Host failures</th><th class='num'>VMs restarted by HA</th><th>Events</th></tr>")
    for r in b["rows"]:
        av = r["availability"]
        cls = "critical" if av["pct"] < s.availability_target_pct else "ok"
        evs = "<br>".join(f"{e(x.start.strftime('%d %b %H:%M'))} · {e(x.kind.replace('_', ' '))} · {e(x.ticket)} {e(x.summary)}" for x in av["events"])
        h.append(f"<tr><td class='nw'><b>{e(r['cluster'].cluster)}</b></td><td class='num {cls}'>{av['pct']:.3f}%</td><td class='num'>{av['outage']:.0f}</td>"
                 f"<td class='num'>{len(av['failures'])}</td><td class='num'>{av['restarted']}</td><td class='fix'>{evs or '–'}</td></tr>")
    h.append("</table></div>")
    h.append("<h2>All findings</h2><div class='wrap'><table><tr><th>Severity</th><th>Area</th><th>Subject</th><th>Detail</th><th>Next step</th><th>Owner</th></tr>")
    h += [f"<tr><td>{_pill(f.severity, f.severity.upper())}</td><td class='nw'>{e(f.area)}</td><td>{e(f.subject)}</td>"
          f"<td>{e(f.detail)}</td><td class='fix'>{e(f.fix)}</td><td class='nw'>{e(f.owner)}</td></tr>" for f in findings]
    h.append("</table></div><footer class='muted'>Generated by capreview. Fictional demo data unless you ran it on your own exports.</footer></main></body></html>")
    return "".join(h)
