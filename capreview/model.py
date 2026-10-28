"""Loading: clusters and hosts, VMs and snapshots, datastores and storage containers, the monthly utilisation history, the demand
pipeline, the availability events and settings.json, with forgiving column names, into plain dataclasses."""
from __future__ import annotations

import csv
import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

CLUSTER_ALIASES = {
    "cluster": ["cluster", "name", "cluster name", "cluster nombre"],
    "site": ["site", "datacenter", "data centre", "data center", "location", "cpd", "sede", "ubicacion"],
    "platform": ["platform", "hypervisor", "type", "plataforma", "tipo"],
    "failover_hosts": ["failover hosts", "ha failover level", "host failures tolerated", "failover level", "n plus", "tolerancia"],
    "ha_policy": ["ha policy", "admission control", "admission control policy", "politica ha"],
    "cpu_max_pct": ["cpu max pct", "cpu threshold", "umbral cpu"],
    "ram_max_pct": ["ram max pct", "memory threshold", "umbral ram"],
    "vcpu_ratio_max": ["vcpu ratio max", "vcpu pcpu policy", "ratio vcpu"],
}
HOST_ALIASES = {
    "host": ["host", "name", "hostname", "vmhost", "node", "nodo"],
    "cluster": ["cluster"],
    "vendor": ["vendor", "manufacturer", "fabricante"],
    "model": ["model", "modelo"],
    "cores": ["cores", "cpu cores", "num cpu", "numcpu", "physical cores", "nucleos"],
    "cpu_ghz": ["cpu ghz", "cpu total ghz", "total ghz", "ghz"],
    "cpu_mhz": ["cpu total mhz", "cputotalmhz", "mhz"],
    "ram_gb": ["ram gb", "memory gb", "memorytotalgb", "memory total gb", "memoria gb"],
    "state": ["state", "connection state", "connectionstate", "status", "estado"],
    "version": ["version", "build", "hypervisor version", "esxi version", "aos version"],
    "firmware": ["firmware", "bios", "bios version", "firmware version"],
    "firmware_target": ["firmware target", "target firmware", "baseline", "firmware baseline"],
    "support_end": ["support end", "warranty end", "end of support", "fin soporte", "fin garantia"],
}
VM_ALIASES = {
    "vm": ["vm", "name", "vm name", "virtual machine", "maquina"],
    "cluster": ["cluster"],
    "power": ["power state", "powerstate", "state", "power", "estado"],
    "vcpu": ["vcpu", "num cpu", "numcpu", "cpus", "vcpus"],
    "ram_gb": ["ram gb", "memory gb", "memorygb", "vram gb", "memoria gb"],
    "provisioned_gb": ["provisioned gb", "provisionedspacegb", "provisioned space gb", "aprovisionado gb"],
    "used_gb": ["used gb", "usedspacegb", "used space gb", "usado gb"],
    "datastore": ["datastore", "storage container", "container"],
    "owner": ["owner", "responsable", "propietario", "contact"],
    "service": ["service", "application", "app", "servicio", "aplicacion"],
    "last_on": ["last powered on", "powered off since", "last power on", "last powered", "ultimo encendido"],
    "cpu_p95": ["cpu p95", "cpu p95 pct", "cpu usage p95", "cpu 95"],
    "mem_p95": ["mem p95", "ram p95", "mem p95 pct", "memory usage p95", "mem 95"],
}
SNAP_ALIASES = {
    "vm": ["vm", "virtual machine"],
    "snapshot": ["snapshot", "name", "snapshot name"],
    "created": ["created", "date", "creado", "fecha"],
    "size_gb": ["size gb", "sizegb", "size", "tamano gb"],
}
DS_ALIASES = {
    "datastore": ["datastore", "name", "storage container", "container"],
    "cluster": ["cluster"],
    "kind": ["type", "kind", "tipo"],
    "capacity_gb": ["capacity gb", "capacitygb", "capacity", "capacidad gb"],
    "free_gb": ["free gb", "freespacegb", "free space gb", "libre gb"],
    "provisioned_gb": ["provisioned gb", "provisioned", "aprovisionado gb"],
}
UTIL_ALIASES = {
    "cluster": ["cluster"],
    "month": ["month", "period", "mes", "periodo"],
    "cpu_avg": ["cpu avg", "cpu avg pct", "cpu usage average", "cpu media"],
    "cpu_p95": ["cpu p95", "cpu p95 pct", "cpu 95"],
    "mem_avg": ["mem avg", "ram avg", "mem avg pct", "mem usage average", "ram media"],
    "mem_p95": ["mem p95", "ram p95", "mem p95 pct", "mem 95"],
    "cpu_used_ghz": ["cpu used ghz", "cpu ghz used"],
    "ram_used_gb": ["ram used gb", "mem used gb"],
    "storage_used_gb": ["storage used gb", "used storage gb", "almacenamiento usado gb"],
}
DEMAND_ALIASES = {
    "project": ["project", "name", "request", "proyecto", "peticion"],
    "cluster": ["cluster", "target cluster", "destino"],
    "go_live": ["go live", "date", "needed by", "required by", "fecha", "fecha necesaria"],
    "status": ["status", "state", "estado"],
    "vms": ["vms", "vm count", "num vms", "maquinas"],
    "vcpu": ["vcpu", "vcpus", "cpus"],
    "ram_gb": ["ram gb", "memory gb", "memoria gb"],
    "storage_gb": ["storage gb", "disk gb", "almacenamiento gb"],
    "owner": ["owner", "requester", "responsable", "solicitante"],
    "ticket": ["ticket", "change", "rfc", "cambio"],
}
EVENT_ALIASES = {
    "start": ["start", "opened", "date", "inicio", "fecha"],
    "end": ["end", "resolved", "restored", "fin"],
    "cluster": ["cluster"],
    "host": ["host", "node"],
    "kind": ["type", "kind", "event", "event type", "tipo"],
    "vms": ["vms", "vms affected", "vms restarted", "afectadas"],
    "ticket": ["ticket", "incident", "incidencia"],
    "summary": ["summary", "description", "full formatted message", "resumen", "descripcion"],
}
DATE_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%d/%m/%Y %H:%M", "%d/%m/%Y", "%m/%d/%Y %H:%M:%S")
EVENT_KINDS = {"cluster outage": "cluster_outage", "outage": "cluster_outage", "service outage": "cluster_outage", "caida": "cluster_outage",
               "host failure": "host_failure", "host down": "host_failure", "dashostfailedevent": "host_failure", "hostnotrespondingevent": "host_failure",
               "host disconnected": "host_failure", "hostconnectionlostevent": "host_failure", "ha restart": "ha_restart",
               "vmrestartedbyhaevent": "ha_restart", "com vmware vc ha vmrestartedbyhaevent": "ha_restart", "maintenance": "maintenance",
               "mantenimiento": "maintenance"}
DOWN_STATES = {"disconnected", "notresponding", "not responding", "down", "offline", "desconectado"}
MAINT_STATES = {"maintenance", "inmaintenance", "in maintenance", "maintenance mode", "mantenimiento"}


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s).lower())
    return re.sub(r"[^a-z0-9]+", " ", "".join(ch for ch in s if not unicodedata.combining(ch))).strip()


def parse_dt(v) -> datetime | None:
    s = str(v or "").strip().replace("Z", "")
    if not s or _norm(s) in {"never", "n a", "na", "none", "nunca"}:
        return None
    for f in DATE_FORMATS:
        try:
            return datetime.strptime(s[:19] if "T" in s else s, f)
        except ValueError:
            continue
    return None


def parse_date(v) -> date | None:
    d = parse_dt(v)
    return d.date() if d else None


def parse_month(v) -> tuple[int, int] | None:
    s = str(v or "").strip()
    m = re.match(r"^(\d{4})[-/](\d{1,2})$", s) or re.match(r"^(\d{1,2})[-/](\d{4})$", s)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        return (a, b) if a > 12 else (b, a)
    d = parse_date(s)
    return (d.year, d.month) if d else None


def add_months(ym: tuple[int, int], k: int) -> tuple[int, int]:
    n = ym[0] * 12 + ym[1] - 1 + k
    return n // 12, n % 12 + 1


def month_diff(a: tuple[int, int], b: tuple[int, int]) -> int:
    """Months from a to b."""
    return (b[0] - a[0]) * 12 + b[1] - a[1]


def mlabel(ym: tuple[int, int] | None) -> str:
    return f"{date(ym[0], ym[1], 1):%b %Y}" if ym else ""


def mkey(ym: tuple[int, int]) -> str:
    return f"{ym[0]:04d}-{ym[1]:02d}"


def num(v, default: float | None = 0.0) -> float | None:
    s = str(v or "").strip().replace(" ", "").lower().replace("%", "").replace("gb", "").replace("ghz", "")
    if not s:
        return default
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return default


def _rows(path: Path, aliases: dict[str, list[str]]) -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        sample = fh.read(4096)
        fh.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(fh, dialect=dialect)
        heads = {_norm(h): h for h in (reader.fieldnames or [])}
        cols = {}
        for key, names in aliases.items():
            for n in [key.replace("_", " ")] + names:
                if _norm(n) in heads and heads[_norm(n)] not in cols.values():
                    cols[key] = heads[_norm(n)]
                    break
        return [{k: (r.get(c) or "").strip() for k, c in cols.items()} for r in reader]


def power_of(v: str) -> str:
    n = _norm(v)
    return "off" if n in {"poweredoff", "powered off", "off", "apagada", "apagado", "suspended"} else "on"


def host_state(v: str) -> str:
    n = _norm(v)
    if n in MAINT_STATES or n.replace(" ", "") in MAINT_STATES:
        return "maintenance"
    if n in DOWN_STATES or n.replace(" ", "") in DOWN_STATES:
        return "disconnected"
    return "connected"


def event_kind(v: str) -> str:
    n = _norm(v)
    if n in EVENT_KINDS:
        return EVENT_KINDS[n]
    for k, out in EVENT_KINDS.items():
        if k in n:
            return out
    return "other"


def version(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", str(v or ""))[:4])


@dataclass
class Cluster:
    cluster: str
    site: str = ""
    platform: str = "vSphere"
    failover_hosts: int | None = None   # None: settings.failover_hosts
    ha_policy: str = ""
    cpu_max_pct: float | None = None
    ram_max_pct: float | None = None
    vcpu_ratio_max: float | None = None


@dataclass
class Host:
    host: str
    cluster: str
    cores: int = 0
    cpu_ghz: float = 0.0                # total GHz of the host (cores x clock)
    ram_gb: float = 0.0
    vendor: str = ""
    model: str = ""
    state: str = "connected"            # connected | maintenance | disconnected
    version: str = ""
    firmware: str = ""
    firmware_target: str = ""
    support_end: date | None = None


@dataclass
class VM:
    vm: str
    cluster: str
    power: str = "on"                   # on | off
    vcpu: int = 0
    ram_gb: float = 0.0
    provisioned_gb: float = 0.0
    used_gb: float = 0.0
    datastore: str = ""
    owner: str = ""
    service: str = ""
    last_on: datetime | None = None     # last time it was powered on (or the time it was powered off)
    cpu_p95: float | None = None        # % of its vCPUs, last 30 days
    mem_p95: float | None = None        # % of its vRAM, last 30 days


@dataclass
class Snapshot:
    vm: str
    snapshot: str
    created: datetime
    size_gb: float = 0.0


@dataclass
class Datastore:
    datastore: str
    cluster: str
    capacity_gb: float
    free_gb: float
    provisioned_gb: float | None = None
    kind: str = ""


@dataclass
class Util:
    cluster: str
    month: tuple[int, int]
    cpu_avg: float | None = None
    cpu_p95: float | None = None
    mem_avg: float | None = None
    mem_p95: float | None = None
    cpu_used_ghz: float | None = None
    ram_used_gb: float | None = None
    storage_used_gb: float | None = None


@dataclass
class Demand:
    project: str
    cluster: str
    go_live: date | None
    status: str = "approved"
    vms: int = 0
    vcpu: int = 0
    ram_gb: float = 0.0
    storage_gb: float = 0.0
    owner: str = ""
    ticket: str = ""


@dataclass
class Event:
    start: datetime
    end: datetime | None
    cluster: str
    kind: str                           # cluster_outage | host_failure | ha_restart | maintenance | other
    host: str = ""
    vms: int = 0
    ticket: str = ""
    summary: str = ""

    @property
    def minutes(self) -> float:
        return max(0.0, (self.end - self.start).total_seconds() / 60) if self.end else 0.0


@dataclass
class Settings:
    organisation: str = "Private cloud"
    as_of: date | None = None
    period: tuple[int, int] | None = None          # month reviewed for availability; default the month before as_of
    failover_hosts: int = 1
    cpu_max_pct: float = 80.0                      # max CPU use of what is left after the failover hosts are gone
    ram_max_pct: float = 90.0
    warn_margin_pct: float = 10.0                  # attention when within this many points of the max
    vcpu_ratio_max: float = 4.0
    ram_overcommit_max: float = 1.2                # running vRAM / RAM left after the failover hosts
    datastore_warn_free_pct: float = 20.0
    datastore_crit_free_pct: float = 10.0
    thin_overcommit_max: float = 1.5
    history_months: int = 12
    history_min_months: int = 6
    forecast_months: int = 24
    purchase_lead_months: int = 4                  # order to hosts in production
    plan_horizon_months: int = 12
    demand_statuses: list = field(default_factory=lambda: ["approved", "committed"])
    demand_ram_factor: float = 1.0                 # share of the requested vRAM that will be consumed
    poweredoff_warn_days: int = 30
    poweredoff_reclaim_days: int = 90
    idle_cpu_pct: float = 3.0
    idle_mem_pct: float = 15.0
    oversize_cpu_pct: float = 25.0
    oversize_mem_pct: float = 30.0
    oversize_min_vcpu: int = 4
    oversize_min_ram_gb: float = 16.0
    rightsize_target_pct: float = 60.0
    snapshot_max_days: int = 7
    snapshot_crit_days: int = 30
    availability_target_pct: float = 99.95
    support_warn_days: int = 180
    owners: dict = field(default_factory=lambda: {"capacity": "Capacity manager", "forecast": "Head of infrastructure",
                                                  "storage": "Storage team", "reclaim": "Service owners", "availability": "Platform team",
                                                  "lifecycle": "Platform team"})

    def owner(self, key: str) -> str:
        return self.owners.get(key, "")


def settings_from_dict(raw: dict) -> Settings:
    s = Settings()
    for k, v in raw.items():
        if not hasattr(s, k):
            continue
        cur = getattr(s, k)
        if k == "as_of":
            v = parse_date(v)
        elif k == "period":
            v = parse_month(v)
        elif k == "owners":
            v = {**cur, **v}
        elif k == "demand_statuses":
            v = [_norm(x) for x in v]
        elif isinstance(cur, bool):
            v = bool(v)
        elif isinstance(cur, int):
            v = int(v)
        elif isinstance(cur, float):
            v = float(v)
        setattr(s, k, v)
    return s


def _int(v) -> int:
    return int(num(v, 0) or 0)


def load_clusters(path: Path) -> list[Cluster]:
    out = []
    for r in _rows(path, CLUSTER_ALIASES):
        if not r.get("cluster"):
            continue
        fh = num(r.get("failover_hosts"), None)
        out.append(Cluster(r["cluster"], r.get("site", ""), r.get("platform") or "vSphere", None if fh is None else int(fh),
                           r.get("ha_policy", ""), num(r.get("cpu_max_pct"), None), num(r.get("ram_max_pct"), None),
                           num(r.get("vcpu_ratio_max"), None)))
    return out


def load_hosts(path: Path) -> list[Host]:
    out = []
    for r in _rows(path, HOST_ALIASES):
        if not r.get("host"):
            continue
        ghz = num(r.get("cpu_ghz"), None)
        if ghz is None:
            ghz = (num(r.get("cpu_mhz"), 0) or 0) / 1000
        out.append(Host(r["host"], r.get("cluster", ""), _int(r.get("cores")), ghz, num(r.get("ram_gb")) or 0.0, r.get("vendor", ""),
                        r.get("model", ""), host_state(r.get("state", "")), r.get("version", ""), r.get("firmware", ""),
                        r.get("firmware_target", ""), parse_date(r.get("support_end"))))
    return out


def load_vms(path: Path) -> list[VM]:
    out = []
    for r in _rows(path, VM_ALIASES):
        if not r.get("vm"):
            continue
        out.append(VM(r["vm"], r.get("cluster", ""), power_of(r.get("power", "")), _int(r.get("vcpu")), num(r.get("ram_gb")) or 0.0,
                      num(r.get("provisioned_gb")) or 0.0, num(r.get("used_gb")) or 0.0, r.get("datastore", ""), r.get("owner", ""),
                      r.get("service", ""), parse_dt(r.get("last_on")), num(r.get("cpu_p95"), None), num(r.get("mem_p95"), None)))
    return out


def load_snapshots(path: Path) -> list[Snapshot]:
    out = []
    for r in _rows(path, SNAP_ALIASES):
        t = parse_dt(r.get("created"))
        if r.get("vm") and t:
            out.append(Snapshot(r["vm"], r.get("snapshot", ""), t, num(r.get("size_gb")) or 0.0))
    return out


def load_datastores(path: Path) -> list[Datastore]:
    out = []
    for r in _rows(path, DS_ALIASES):
        cap = num(r.get("capacity_gb"), None)
        if not r.get("datastore") or not cap:
            continue
        out.append(Datastore(r["datastore"], r.get("cluster", ""), cap, num(r.get("free_gb")) or 0.0, num(r.get("provisioned_gb"), None),
                             r.get("kind", "")))
    return out


def load_util(path: Path) -> list[Util]:
    out = []
    for r in _rows(path, UTIL_ALIASES):
        m = parse_month(r.get("month"))
        if not r.get("cluster") or not m:
            continue
        out.append(Util(r["cluster"], m, *(num(r.get(k), None) for k in ("cpu_avg", "cpu_p95", "mem_avg", "mem_p95", "cpu_used_ghz",
                                                                       "ram_used_gb", "storage_used_gb"))))
    return out


def load_demand(path: Path) -> list[Demand]:
    out = []
    for r in _rows(path, DEMAND_ALIASES):
        if not r.get("project"):
            continue
        out.append(Demand(r["project"], r.get("cluster", ""), parse_date(r.get("go_live")), _norm(r.get("status", "")) or "approved",
                          _int(r.get("vms")), _int(r.get("vcpu")), num(r.get("ram_gb")) or 0.0, num(r.get("storage_gb")) or 0.0,
                          r.get("owner", ""), r.get("ticket", "")))
    return out


def load_events(path: Path) -> list[Event]:
    out = []
    for r in _rows(path, EVENT_ALIASES):
        t = parse_dt(r.get("start"))
        if not t:
            continue
        out.append(Event(t, parse_dt(r.get("end")), r.get("cluster", ""), event_kind(r.get("kind", "")), r.get("host", ""), _int(r.get("vms")),
                         r.get("ticket", ""), r.get("summary", "")))
    return out


@dataclass
class Data:
    settings: Settings
    as_of: date
    clusters: list[Cluster]
    hosts: list[Host] = field(default_factory=list)
    vms: list[VM] = field(default_factory=list)
    snapshots: list[Snapshot] = field(default_factory=list)
    datastores: list[Datastore] = field(default_factory=list)
    util: list[Util] = field(default_factory=list)
    demand: list[Demand] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)

    @property
    def period(self) -> tuple[int, int]:
        return self.settings.period or add_months((self.as_of.year, self.as_of.month), -1)

    @property
    def now_month(self) -> tuple[int, int]:
        return self.as_of.year, self.as_of.month

    def of(self, items: list, cluster: str) -> list:
        key = _norm(cluster)
        return [x for x in items if _norm(x.cluster) == key]


def load(folder: Path, as_of: date | None = None) -> Data:
    folder = Path(folder)
    for need in ("clusters.csv", "hosts.csv"):
        if not (folder / need).exists():
            raise FileNotFoundError(f"{folder / need} not found: the clusters and their hosts are required")
    sp = folder / "settings.json"
    s = settings_from_dict(json.loads(sp.read_text(encoding="utf-8"))) if sp.exists() else Settings()
    clusters, hosts = load_clusters(folder / "clusters.csv"), load_hosts(folder / "hosts.csv")
    names = {_norm(h.cluster) for h in hosts}
    for h in hosts:                                   # a host export alone is enough to know the clusters
        if not any(_norm(c.cluster) == _norm(h.cluster) for c in clusters) and h.cluster:
            clusters.append(Cluster(h.cluster))
    clusters = [c for c in clusters if _norm(c.cluster) in names]
    if not clusters:
        raise ValueError("no cluster with hosts: check the Cluster column of hosts.csv")
    opt = lambda name, fn: fn(folder / name) if (folder / name).exists() else []  # noqa: E731
    when = as_of or s.as_of or date.today()
    return Data(s, when, clusters, hosts, opt("vms.csv", load_vms), opt("snapshots.csv", load_snapshots), opt("datastores.csv", load_datastores),
                opt("utilisation.csv", load_util), opt("demand.csv", load_demand), opt("events.csv", load_events))


def month_minutes(ym: tuple[int, int]) -> float:
    a = datetime(ym[0], ym[1], 1)
    b = datetime(*add_months(ym, 1), 1)
    return (b - a).total_seconds() / 60


def in_month(t: datetime, ym: tuple[int, int]) -> bool:
    return (t.year, t.month) == ym


def clip_minutes(e: Event, ym: tuple[int, int]) -> float:
    """Minutes of the event inside the month."""
    if not e.end:
        return 0.0
    a, b = datetime(ym[0], ym[1], 1), datetime(*add_months(ym, 1), 1)
    lo, hi = max(a, e.start), min(b, e.end)
    return max(0.0, (hi - lo).total_seconds() / 60)

