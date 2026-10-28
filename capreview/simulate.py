"""Fictional demo: Fabrikam Engineering's private cloud in two data centres, Madrid and Guadalajara. Two vSphere production clusters, a
Nutanix AHV engineering cluster and a small management cluster, 13 months of utilisation, the approved projects and September's
availability events. Every name is made up; the problems are placed on purpose."""
from __future__ import annotations

import csv
import json
import random
from datetime import date, datetime, timedelta
from pathlib import Path

ORG = "Fabrikam Engineering"
AS_OF = date(2026, 10, 27)
SEED = 35
FIRST, MONTHS = (2025, 9), 13                     # Sep 2025 to Sep 2026

# cluster, site, platform, failover hosts, HA admission control policy
CLUSTERS = [
    ("MAD-PROD-01", "Madrid", "vSphere 8.0 U3", 1, "Cluster resource percentage (1 host)"),
    ("MAD-ENG-01", "Madrid", "Nutanix AHV (AOS 6.10)", 1, "Nutanix HA, reserve 1 node (RF2)"),
    ("GDL-PROD-01", "Guadalajara", "vSphere 8.0 U2", 1, "Cluster resource percentage (1 host)"),
    ("GDL-MGMT-01", "Guadalajara", "vSphere 8.0 U3", 1, "Host failures cluster tolerates: 1"),
]

# prefix, cluster, count, vendor, model, cores, GHz per core, RAM GB, version, firmware, baseline, support end
HOSTS = [
    ("esx-mad-p", "MAD-PROD-01", 8, "Dell", "PowerEdge R760", 64, 2.5, 1024, "8.0.3 (24585383)", "2.3.5", "2.3.5", "2029-03-31"),
    ("ntx-mad-e", "MAD-ENG-01", 6, "Nutanix", "NX-3170-G8", 56, 2.6, 768, "AHV 10.0 / AOS 6.10", "", "", "2028-06-30"),
    ("esx-gdl-p", "GDL-PROD-01", 4, "HPE", "ProLiant DL380 Gen11", 64, 2.4, 1024, "8.0.2 (22380479)", "2.30", "2.30", "2028-11-30"),
    ("esx-gdl-p", "GDL-PROD-01", 2, "HPE", "ProLiant DL380 Gen10", 40, 2.4, 768, "8.0.2 (22380479)", "3.10", "3.10", "2026-06-30"),
    ("esx-gdl-m", "GDL-MGMT-01", 3, "Dell", "PowerEdge R660", 32, 2.8, 512, "8.0.3 (24585383)", "2.2.4", "2.3.5", "2027-02-28"),
]
HOST_STATE = {"esx-gdl-m03": "Maintenance"}
FIRMWARE_LAG = {"esx-mad-p07": "2.2.4"}

# cluster: (cpu p95 at the end, cpu slope pp/month, ram p95 at the end, ram slope, storage slope GB/month)
TRENDS = {
    "MAD-PROD-01": (47.0, 0.4, 74.0, 0.6, 600),
    "MAD-ENG-01": (60.0, 0.7, 55.0, 0.3, 500),
    "GDL-PROD-01": (35.0, 0.2, 58.0, 0.3, 400),
    "GDL-MGMT-01": (38.0, 0.2, 52.0, 0.5, 60),
}

# datastore, cluster, type, capacity GB, free %, provisioned GB
DATASTORES = [
    ("MAD-NFS-01", "MAD-PROD-01", "NFS (NetApp)", 61440, 39.0, 74000),
    ("MAD-NFS-02", "MAD-PROD-01", "NFS (NetApp)", 61440, 37.0, 76500),
    ("MAD-NFS-03", "MAD-PROD-01", "NFS (NetApp)", 61440, 38.0, 71800),
    ("ENG-CTR-01", "MAD-ENG-01", "Nutanix storage container", 92160, 35.0, 158500),
    ("GDL-VMFS-01", "GDL-PROD-01", "VMFS 6 (FC)", 49152, 40.0, 52000),
    ("GDL-VMFS-02", "GDL-PROD-01", "VMFS 6 (FC)", 49152, 35.0, 55400),
    ("GDL-VMFS-03", "GDL-PROD-01", "VMFS 6 (FC)", 16384, 7.0, 21900),
    ("GDL-MGMT-vSAN", "GDL-MGMT-01", "vSAN", 23040, 31.0, 24100),
]

# size menus (vCPU, RAM GB, weight) and how many ordinary VMs per cluster
MENU = {
    "MAD-PROD-01": [(2, 8, 2), (4, 16, 3), (8, 32, 4), (8, 48, 2), (16, 64, 2)],
    "MAD-ENG-01": [(32, 64, 4), (16, 32, 3), (8, 32, 2)],
    "GDL-PROD-01": [(2, 8, 2), (4, 16, 3), (8, 32, 4), (8, 48, 2), (16, 64, 2)],
    "GDL-MGMT-01": [(8, 24, 2), (12, 32, 2), (16, 32, 1), (4, 16, 1)],
}
COUNT = {"MAD-PROD-01": 190, "MAD-ENG-01": 64, "GDL-PROD-01": 128, "GDL-MGMT-01": 42}
ROLES = {
    "MAD-PROD-01": [("plm", "PLM", "PLM team"), ("erp", "ERP", "ERP team"), ("cad", "CAD vault", "Engineering IT"), ("web", "Intranet", "Digital team"),
                    ("sql", "SQL Server", "Database team"), ("app", "Line-of-business apps", "Applications team")],
    "MAD-ENG-01": [("sim", "Simulation farm", "Engineering IT"), ("cae", "CAE pre/post", "Engineering IT"), ("lic", "Licence servers", "Engineering IT")],
    "GDL-PROD-01": [("dr", "DR replicas", "Platform team"), ("bkp", "Backup proxies", "Backup team"), ("file", "File services", "Workplace team"),
                    ("app", "Line-of-business apps", "Applications team"), ("sql", "SQL Server", "Database team")],
    "GDL-MGMT-01": [("mon", "Monitoring", "Platform team"), ("ad", "Directory services", "Identity team"), ("mgmt", "Management tools", "Platform team")],
}

# placed VMs: name, cluster, power, vCPU, RAM GB, provisioned GB, owner, service, last powered on (days ago), cpu p95, mem p95
PLACED = [
    # idle (powered on, doing nothing)
    ("mad-test-plm09", "MAD-PROD-01", "PoweredOn", 8, 32, 300, "PLM team", "PLM test", 210, 1.2, 6.0),
    ("mad-poc-ai01", "MAD-PROD-01", "PoweredOn", 8, 32, 500, "Data & analytics", "Proof of concept", 160, 0.8, 4.0),
    ("mad-poc-ai02", "MAD-PROD-01", "PoweredOn", 8, 32, 500, "Data & analytics", "Proof of concept", 160, 0.9, 4.0),
    ("mad-old-erp-rpt", "MAD-PROD-01", "PoweredOn", 8, 32, 400, "ERP team", "ERP reporting (old)", 400, 1.5, 9.0),
    ("mad-mig-tmp03", "MAD-PROD-01", "PoweredOn", 8, 32, 250, "", "Migration helper", 300, 0.5, 5.0),
    ("mad-train-cad02", "MAD-PROD-01", "PoweredOn", 8, 32, 300, "Engineering IT", "CAD training", 95, 2.1, 11.0),
    # oversized
    ("mad-plm-app01", "MAD-PROD-01", "PoweredOn", 16, 64, 400, "PLM team", "PLM", 40, 11.0, 20.0),
    ("mad-plm-app02", "MAD-PROD-01", "PoweredOn", 16, 64, 400, "PLM team", "PLM", 40, 12.0, 21.0),
    ("mad-plm-idx01", "MAD-PROD-01", "PoweredOn", 16, 64, 600, "PLM team", "PLM", 40, 9.0, 18.0),
    ("mad-erp-app03", "MAD-PROD-01", "PoweredOn", 16, 64, 300, "ERP team", "ERP", 62, 10.0, 19.0),
    ("mad-erp-app04", "MAD-PROD-01", "PoweredOn", 16, 64, 300, "ERP team", "ERP", 62, 8.0, 22.0),
    ("mad-cad-vault02", "MAD-PROD-01", "PoweredOn", 16, 64, 900, "Engineering IT", "CAD vault", 33, 12.0, 20.0),
    ("mad-web-cms01", "MAD-PROD-01", "PoweredOn", 16, 64, 200, "Digital team", "Intranet", 18, 7.0, 17.0),
    ("mad-app-qms01", "MAD-PROD-01", "PoweredOn", 16, 64, 250, "Applications team", "Quality system", 51, 9.5, 21.0),
    # powered off
    ("mad-old-plm-v10", "MAD-PROD-01", "PoweredOff", 8, 32, 1800, "PLM team", "PLM v10 (retired)", 410, None, None),
    ("mad-old-erp-test", "MAD-PROD-01", "PoweredOff", 8, 32, 900, "ERP team", "ERP test (old)", 260, None, None),
    ("mad-tmp-copy01", "MAD-PROD-01", "PoweredOff", 4, 16, 2000, "", "", 190, None, None),
    ("mad-tmp-copy02", "MAD-PROD-01", "PoweredOff", 4, 16, 2000, "", "", 190, None, None),
    ("mad-poc-gis01", "MAD-PROD-01", "PoweredOff", 4, 16, 600, "Digital team", "GIS proof of concept", 125, None, None),
    ("mad-dev-sql07", "MAD-PROD-01", "PoweredOff", 8, 32, 500, "Database team", "SQL Server dev", 64, None, None),
    ("mad-app-hr-test", "MAD-PROD-01", "PoweredOff", 4, 16, 200, "Applications team", "HR test", 41, None, None),
    ("mad-web-stg02", "MAD-PROD-01", "PoweredOff", 4, 16, 150, "Digital team", "Intranet staging", 2, None, None),
    ("ntx-old-sim-gpu", "MAD-ENG-01", "PoweredOff", 32, 64, 1500, "Engineering IT", "Old simulation node", 150, None, None),
    ("ntx-old-sim-gpu2", "MAD-ENG-01", "PoweredOff", 32, 64, 1500, "Engineering IT", "Old simulation node", 150, None, None),
    ("ntx-cae-pre05", "MAD-ENG-01", "PoweredOn", 16, 128, 400, "Engineering IT", "CAE pre/post", 70, 14.0, 16.0),
    ("ntx-cae-pre06", "MAD-ENG-01", "PoweredOn", 16, 128, 400, "Engineering IT", "CAE pre/post", 70, 15.0, 18.0),
    ("gdl-drtest-erp01", "GDL-PROD-01", "PoweredOff", 8, 32, 800, "Platform team", "DR test leftover", 230, None, None),
    ("gdl-drtest-erp02", "GDL-PROD-01", "PoweredOff", 8, 32, 800, "Platform team", "DR test leftover", 230, None, None),
    ("gdl-drtest-plm01", "GDL-PROD-01", "PoweredOff", 8, 64, 1200, "Platform team", "DR test leftover", 230, None, None),
    ("gdl-tmp-restore", "GDL-PROD-01", "PoweredOff", 4, 16, 1500, "", "", 330, None, None),
    ("gdl-file-old01", "GDL-PROD-01", "PoweredOn", 4, 16, 2500, "Workplace team", "File services (migrated)", 500, 1.0, 8.0),
]
# snapshots: vm, name, days ago, size GB
SNAPSHOTS = [
    ("mad-erp-app03", "before ERP patch 2025-Q4", 210, 640),
    ("mad-plm-idx01", "pre-upgrade", 95, 420),
    ("mad-sql-prod02", "before CU update", 45, 310),
    ("mad-cad-vault02", "test", 12, 85),
    ("mad-web-cms01", "Backup temporary snapshot", 1, 6),
    ("gdl-app-ivr01", "before OS upgrade", 60, 220),
    ("gdl-mon-prx01", "before agent update", 40, 35),
]

# start, end, cluster, host, type, VMs, ticket, summary
EVENTS = [
    ("2026-08-29 22:00", "2026-08-30 01:30", "MAD-ENG-01", "", "Maintenance", 0, "CHG-2207", "AOS upgrade, rolling, one node at a time"),
    ("2026-09-08 14:22", "2026-09-08 20:40", "MAD-PROD-01", "esx-mad-p05", "Host failure", 31, "INC-48812",
     "Uncorrectable memory error (PSOD); vSphere HA restarted 31 VMs, DIMM replaced"),
    ("2026-09-17 02:10", "2026-09-17 02:45", "GDL-PROD-01", "", "Cluster outage", 96, "INC-48907",
     "Array controller failover stalled the VMFS datastores; all VMs unresponsive for 35 minutes"),
    ("2026-09-24 21:00", "2026-09-24 23:30", "GDL-MGMT-01", "esx-gdl-m02", "Maintenance", 0, "CHG-2244", "ESXi patch"),
    ("2026-10-12 08:00", "", "GDL-MGMT-01", "esx-gdl-m03", "Maintenance", 0, "CHG-2262", "Motherboard replacement, waiting for the part"),
]

# project, cluster, go-live, status, VMs, vCPU, RAM GB, storage GB, owner, ticket
DEMAND = [
    ("Monitoring stack upgrade", "GDL-MGMT-01", "2026-11-16", "Approved", 4, 16, 64, 300, "Platform team", "CHG-2270"),
    ("PLM 2027 upgrade", "MAD-PROD-01", "2027-01-11", "Approved", 18, 96, 384, 6000, "PLM team", "CHG-2291"),
    ("PLM DR copy", "GDL-PROD-01", "2027-01-25", "Approved", 18, 48, 192, 6000, "PLM team", "CHG-2292"),
    ("CAE licence servers consolidation", "MAD-PROD-01", "2027-02-01", "Proposed", 4, 16, 64, 400, "Engineering IT", ""),
    ("Engineering data platform", "MAD-PROD-01", "2027-03-01", "Approved", 12, 128, 512, 20000, "Data & analytics", "CHG-2310"),
    ("Simulation farm expansion", "MAD-ENG-01", "2027-04-05", "Approved", 6, 96, 192, 3000, "Engineering IT", "CHG-2318"),
    ("Supplier portal", "", "2027-05-03", "Approved", 6, 24, 96, 1200, "Digital team", ""),
]


def _ago(days: float) -> str:
    return (datetime.combine(AS_OF, datetime.min.time()) + timedelta(hours=8) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M")


def _month(k: int) -> str:
    n = FIRST[0] * 12 + FIRST[1] - 1 + k
    return f"{n // 12:04d}-{n % 12 + 1:02d}"


def _rows() -> dict[str, list[list]]:
    rnd = random.Random(SEED)
    clusters = [list(c) for c in CLUSTERS]
    hosts, counter = [], {}
    for prefix, cl, count, vendor, model, cores, clock, ram, ver, fw, base, support in HOSTS:
        for _ in range(count):
            counter[prefix] = counter.get(prefix, 0) + 1
            name = f"{prefix}{counter[prefix]:02d}"
            hosts.append([name, cl, vendor, model, cores, round(cores * clock * 1000), ram, HOST_STATE.get(name, "Connected"), ver,
                          FIRMWARE_LAG.get(name, fw), base, support])
    vms = []
    ds_of = {c[0]: [d[0] for d in DATASTORES if d[1] == c[0]] for c in CLUSTERS}
    placed_names = {p[0] for p in PLACED}
    for cl, n in COUNT.items():
        menu = MENU[cl]
        weights = [w for *_, w in menu]
        seq = {}
        for k in range(n):
            vc, ram, _ = rnd.choices(menu, weights)[0]
            role, service, owner = ROLES[cl][k % len(ROLES[cl])]
            seq[role] = seq.get(role, 0) + 1
            name = f"{cl[:3].lower()}-{role}-{seq[role]:02d}"
            if name in placed_names:
                continue
            vms.append([name, cl, "PoweredOn", vc, ram, rnd.choice([80, 120, 200, 300, 500, 800]), 0, rnd.choice(ds_of[cl]), owner, service,
                        _ago(rnd.randint(3, 120)), round(rnd.uniform(28, 85), 1), round(rnd.uniform(45, 92), 1)])
    for name, cl, power, vc, ram, prov, owner, service, on, cpu, mem in PLACED:
        vms.append([name, cl, power, vc, ram, prov, 0, ds_of[cl][len(name) % len(ds_of[cl])], owner, service, _ago(on),
                    "" if cpu is None else cpu, "" if mem is None else mem])
    for name, cl in (("mad-sql-prod02", "MAD-PROD-01"), ("gdl-app-ivr01", "GDL-PROD-01"), ("gdl-mon-prx01", "GDL-MGMT-01")):
        vms.append([name, cl, "PoweredOn", 8, 32, 400, 0, ds_of[cl][0], "Database team" if "sql" in name else "Applications team"
                    if "ivr" in name else "Platform team", "SQL Server" if "sql" in name else "IVR" if "ivr" in name else "Monitoring",
                    _ago(30), 41.0, 66.0])
    for v in vms:
        v[6] = round(v[5] * rnd.uniform(0.45, 0.8))              # used GB
    vms.sort(key=lambda v: (v[1], v[0]))
    snaps = [[vm, nm, _ago(days), size] for vm, nm, days, size in SNAPSHOTS]
    dss = [[n, cl, kind, cap, round(cap * free / 100), prov] for n, cl, kind, cap, free, prov in DATASTORES]
    util = []
    for cl, (cpu, cs, mem, ms, ss) in TRENDS.items():
        used_now = sum(d[3] - d[4] for d in dss if d[1] == cl)
        for k in range(MONTHS):
            back = MONTHS - 1 - k
            c95 = cpu - cs * back + (rnd.uniform(-0.4, 0.4) if back else 0)
            m95 = mem - ms * back + (rnd.uniform(-0.4, 0.4) if back else 0)
            util.append([cl, _month(k), round(c95 - rnd.uniform(14, 18), 1), round(c95, 1), round(m95 - rnd.uniform(4, 7), 1), round(m95, 1),
                         round(used_now - ss * back + (rnd.uniform(-150, 150) if back else 0))])
    events = [list(e) for e in EVENTS]
    demand = [list(x) for x in DEMAND]
    return dict(clusters=clusters, hosts=hosts, vms=vms, snapshots=snaps, datastores=dss, utilisation=util, events=events, demand=demand)


HEADERS = {
    "clusters": ["Cluster", "Site", "Platform", "Failover hosts", "HA policy"],
    "hosts": ["Host", "Cluster", "Vendor", "Model", "Cores", "CPU total MHz", "Memory GB", "Connection state", "Version", "Firmware",
              "Firmware target", "Support end"],
    "vms": ["VM", "Cluster", "Power state", "vCPU", "Memory GB", "Provisioned GB", "Used GB", "Datastore", "Owner", "Service",
            "Last powered on", "CPU p95", "Mem p95"],
    "snapshots": ["VM", "Snapshot", "Created", "Size GB"],
    "datastores": ["Datastore", "Cluster", "Type", "Capacity GB", "Free GB", "Provisioned GB"],
    "utilisation": ["Cluster", "Month", "CPU avg", "CPU p95", "Mem avg", "Mem p95", "Storage used GB"],
    "events": ["Start", "End", "Cluster", "Host", "Type", "VMs", "Ticket", "Summary"],
    "demand": ["Project", "Cluster", "Go-live", "Status", "VMs", "vCPU", "RAM GB", "Storage GB", "Owner", "Ticket"],
}

SETTINGS = {
    "organisation": ORG,
    "as_of": AS_OF.isoformat(),
    "period": "2026-09",
    "failover_hosts": 1,
    "cpu_max_pct": 80,
    "ram_max_pct": 90,
    "warn_margin_pct": 10,
    "vcpu_ratio_max": 4.0,
    "ram_overcommit_max": 1.2,
    "datastore_warn_free_pct": 20,
    "datastore_crit_free_pct": 10,
    "thin_overcommit_max": 1.5,
    "history_months": 12,
    "history_min_months": 6,
    "forecast_months": 24,
    "purchase_lead_months": 4,
    "plan_horizon_months": 12,
    "demand_statuses": ["approved", "committed"],
    "demand_ram_factor": 1.0,
    "poweredoff_warn_days": 30,
    "poweredoff_reclaim_days": 90,
    "idle_cpu_pct": 3,
    "idle_mem_pct": 15,
    "oversize_cpu_pct": 25,
    "oversize_mem_pct": 30,
    "rightsize_target_pct": 60,
    "snapshot_max_days": 7,
    "snapshot_crit_days": 30,
    "availability_target_pct": 99.95,
    "support_warn_days": 180,
    "owners": {"capacity": "Capacity manager", "forecast": "Head of infrastructure", "storage": "Storage team", "reclaim": "Service owners",
               "availability": "Platform team", "lifecycle": "Platform team"},
}


def write(folder: Path) -> Path:
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    for name, rows in _rows().items():
        with open(folder / f"{name}.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh, lineterminator="\n")
            w.writerow(HEADERS[name])
            w.writerows(rows)
    (folder / "settings.json").write_text(json.dumps(SETTINGS, indent=2) + "\n", encoding="utf-8")
    return folder
