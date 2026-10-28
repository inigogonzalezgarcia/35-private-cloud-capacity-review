import csv
import hashlib
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

from capreview import cli, report
from capreview.checks import (CRITICAL, INFO, ORDER, WARNING, availability, capacity, cluster_rows, datastore_rows, demand_fit, forecast,
                              reclaim, reclaim_totals, run_all, slope)
from capreview.model import (VM, Cluster, Data, Datastore, Demand, Event, Host, Snapshot, Util, add_months, event_kind, load, load_events,
                             load_hosts, load_util, load_vms, parse_month, settings_from_dict)

AS_OF = date(2026, 10, 27)


def write(path: Path, header: list[str], rows: list[list], delimiter: str = ",") -> Path:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, delimiter=delimiter)
        w.writerow(header)
        w.writerows(rows)
    return path


def hosts(cluster="C1", n=4, cores=32, ghz=80.0, ram=512.0, **kw) -> list[Host]:
    return [Host(f"{cluster.lower()}-h{k + 1}", cluster, cores, ghz, ram, **kw) for k in range(n)]


def history(cluster="C1", cpu=(40.0, 0.0), mem=(60.0, 0.0), storage=None, months=12) -> list[Util]:
    """Months ending in Sep 2026; cpu/mem = (value at the end, slope per month in points)."""
    out = []
    for k in range(months):
        back = months - 1 - k
        out.append(Util(cluster, add_months((2026, 9), -back), None, cpu[0] - cpu[1] * back, None, mem[0] - mem[1] * back,
                        storage_used_gb=None if storage is None else storage[0] - storage[1] * back))
    return out


def vm(name, cluster="C1", power="on", vcpu=4, ram=16.0, prov=100.0, owner="App team", days_on=5, cpu=50.0, mem=60.0, ds="") -> VM:
    return VM(name, cluster, power, vcpu, ram, prov, prov / 2, ds, owner, "Service", datetime(2026, 10, 27) - timedelta(days=days_on), cpu, mem)


def data(clusters=None, hs=None, vms=None, util=None, datastores=None, demand=None, events=None, snaps=None, **raw) -> Data:
    s = settings_from_dict(raw)
    return Data(s, AS_OF, clusters or [Cluster("C1")], hs if hs is not None else hosts(), vms or [], snaps or [], datastores or [],
                util if util is not None else history(), demand or [], events or [])


def codes(d: Data, cluster="C1") -> dict:
    r = next(x for x in cluster_rows(d) if x["cluster"].cluster == cluster)
    out = {}
    for i in sorted(r["issues"], key=lambda i: ORDER[i.severity]):        # the worst issue of each code
        out.setdefault(i.code, i)
    return out


class TestLoading(unittest.TestCase):
    def test_powercli_hosts_and_vms_with_semicolons(self):
        with tempfile.TemporaryDirectory() as t:
            h = write(Path(t) / "h.csv", ["Name", "Cluster", "NumCpu", "CpuTotalMhz", "MemoryTotalGB", "ConnectionState", "Fin soporte"],
                      [["esx01", "C1", "64", "160000", "1023,6", "Maintenance", "30/06/2026"],
                       ["esx02", "C1", "64", "160000", "1024", "NotResponding", ""]], delimiter=";")
            hs = load_hosts(h)
            v = write(Path(t) / "v.csv", ["Name", "Cluster", "PowerState", "NumCpu", "MemoryGB", "ProvisionedSpaceGB", "Responsable", "CPU p95"],
                      [["vm1", "C1", "PoweredOff", "4", "16", "1.250,5", "", ""], ["vm2", "C1", "PoweredOn", "2", "8", "60", "Ana", "12,5"]])
            vs = load_vms(v)
        self.assertEqual([(x.cores, x.cpu_ghz, x.ram_gb, x.state) for x in hs], [(64, 160.0, 1023.6, "maintenance"), (64, 160.0, 1024.0, "disconnected")])
        self.assertEqual(hs[0].support_end, date(2026, 6, 30))
        self.assertEqual([(x.power, x.provisioned_gb, x.owner, x.cpu_p95) for x in vs], [("off", 1250.5, "", None), ("on", 60.0, "Ana", 12.5)])

    def test_months_and_event_types(self):
        self.assertEqual([parse_month(x) for x in ("2026-09", "09/2026", "2026-09-01", "x")], [(2026, 9), (2026, 9), (2026, 9), None])
        self.assertEqual(add_months((2026, 11), 3), (2027, 2))
        self.assertEqual([event_kind(x) for x in ("com.vmware.vc.ha.VmRestartedByHAEvent", "DasHostFailedEvent", "Cluster outage", "Mantenimiento")],
                         ["ha_restart", "host_failure", "cluster_outage", "maintenance"])
        with tempfile.TemporaryDirectory() as t:
            u = write(Path(t) / "u.csv", ["Cluster", "Mes", "CPU p95", "RAM p95", "Storage used GB"], [["C1", "09/2026", "41,5", "70", "1000"]])
            e = write(Path(t) / "e.csv", ["Start", "End", "Cluster", "Event type", "VMs"], [["2026-09-08 14:00", "", "C1", "VmRestartedByHAEvent", "3"]])
            self.assertEqual((load_util(u)[0].month, load_util(u)[0].cpu_p95), ((2026, 9), 41.5))
            self.assertEqual((load_events(e)[0].kind, load_events(e)[0].vms, load_events(e)[0].end), ("ha_restart", 3, None))

    def test_settings_and_required_files(self):
        s = settings_from_dict({"as_of": "2026-10-27", "period": "2026-09", "owners": {"storage": "SAN team"}, "ram_max_pct": "85",
                                "demand_statuses": ["Approved"]})
        self.assertEqual((s.as_of, s.period, s.owner("storage"), s.owner("capacity"), s.ram_max_pct, s.demand_statuses),
                         (AS_OF, (2026, 9), "SAN team", "Capacity manager", 85.0, ["approved"]))
        self.assertEqual(data().period, (2026, 9))                                    # default: the month before the meeting
        with tempfile.TemporaryDirectory() as t, self.assertRaises(FileNotFoundError):
            load(Path(t))


class TestCapacity(unittest.TestCase):
    def test_headroom_after_failover_hosts(self):
        # 4 x 512 GB; RAM p95 62% = 1270 GB; after one host 1536 GB -> 83% (warning band 80-90); after two hosts 1024 GB -> 124%
        u = history(mem=(62.0, 0.0))
        cap = capacity(data(util=u), Cluster("C1"))
        self.assertEqual((cap["after"]["ram"], round(cap["now"]["ram"]), round(cap["pct_after"]["ram"])), (1536, 1270, 83))
        self.assertEqual(codes(data(util=u))["CAP-01"].severity, WARNING)
        self.assertEqual(codes(data(util=u, clusters=[Cluster("C1", failover_hosts=2)]))["CAP-01"].severity, CRITICAL)
        self.assertNotIn("CAP-01", codes(data(util=history(mem=(50.0, 0.0)))))
        mixed = hosts(n=3) + [Host("big", "C1", 64, 160.0, 1024.0)]                    # the largest host is the one that fails
        self.assertEqual(capacity(data(hs=mixed), Cluster("C1"))["after"]["ram"], 1536)

    def test_vcpu_ratio_ram_overcommit_and_hosts_down(self):
        many = [vm(f"v{k}", vcpu=16, ram=64) for k in range(40)] + [vm("off", power="off", vcpu=64, ram=512)]
        iss = codes(data(vms=many, util=history(mem=(40.0, 0.0))))
        self.assertEqual(iss["CAP-02"].severity, WARNING)                             # 640 vCPU / 128 cores = 5:1
        self.assertIn("vCPU:pCPU 5.0:1", iss["CAP-02"].text)
        self.assertIn("1.67x", iss["CAP-03"].text)                                      # 2560 GB vRAM / 1536 GB
        self.assertEqual(codes(data(vms=many * 2, util=history(mem=(40.0, 0.0))))["CAP-02"].severity, CRITICAL)
        hs = hosts()
        hs[1].state = "maintenance"
        self.assertIn("C1-h2 (maintenance)".lower(), codes(data(hs=hs, util=history(mem=(40.0, 0.0))))["CAP-04"].text.lower())


class TestForecast(unittest.TestCase):
    def test_slope_and_trend_runway(self):
        self.assertAlmostEqual(slope([1, 3, 5, 7]), 2.0)
        # RAM 60% of 2048 GB rising 1 point (20.48 GB) a month; limit 90% of 1536 = 1382 GB -> 153 GB headroom -> 8th month after Sep
        d = data(util=history(mem=(60.0, 1.0)))
        r = cluster_rows(d)[0]
        self.assertEqual(r["forecast"]["breach"]["trend"], (2027, 5))
        self.assertEqual((r["forecast"]["runway"]["trend"], r["forecast"]["binding"]["trend"]), (7, "ram"))
        self.assertIsNone(cluster_rows(data(util=history(mem=(40.0, 0.0))))[0]["forecast"]["runway"]["trend"])

    def test_demand_and_reclaim_move_the_runway(self):
        dem = [Demand("ERP", "C1", date(2026, 12, 1), "approved", 4, 16, 200.0), Demand("BI", "C1", date(2027, 2, 1), "proposed", 4, 16, 500.0)]
        idle = [vm(f"idle{k}", ram=64, cpu=1.0, mem=5.0) for k in range(3)]             # 192 GB back from November
        d = data(util=history(mem=(60.0, 1.0)), demand=dem, vms=idle)
        r = cluster_rows(d)[0]
        rw = r["forecast"]["runway"]
        self.assertEqual((rw["trend"], rw["pipeline"]), (7, 2))
        self.assertGreater(rw["reclaim"], rw["pipeline"])
        self.assertEqual(r["saving"]["ram"], 192)
        self.assertEqual(codes(d)["FCT-01"].severity, CRITICAL)                        # 2 months < 4-month lead time
        fit = demand_fit(d, r["cap"], r["forecast"])
        self.assertEqual([(x.project, short is None) for x, short in fit], [("ERP", False)])
        f = {x.code: x for x in run_all(d)}
        self.assertEqual((f["DEM-02"].severity, f["DEM-02"].items), (INFO, ["BI"]))
        self.assertTrue(report.decision(d, r).startswith("Approve the reclaim list now and order 1 host by Jan 2027"))

    def test_short_history_and_storage_limit(self):
        ds = [Datastore("DS1", "C1", 10000, 3000)]
        d = data(util=history(months=4, storage=(7000, 100)), datastores=ds)
        r = cluster_rows(d)[0]
        self.assertEqual(codes(d)["FCT-03"].severity, WARNING)
        self.assertEqual(r["cap"]["limit"]["storage"], 8000)                          # 20% free kept
        self.assertEqual(r["forecast"]["breach"][("storage", "trend")], (2027, 8))           # 7000 + 100 a month > 8000


class TestReclaim(unittest.TestCase):
    def test_powered_off_idle_and_oversized(self):
        vms = [vm("off200", power="off", prov=900, days_on=200), vm("off45", power="off", days_on=45), vm("off10", power="off", days_on=10),
               vm("idle", ram=32, cpu=1.0, mem=8.0), vm("big", vcpu=16, ram=64, cpu=9.0, mem=18.0), vm("busy", vcpu=16, ram=64, cpu=70, mem=80)]
        d = data(vms=vms)
        rows = {(x["vm"].vm, x["reason"]): x for x in reclaim(d, {"c1": capacity(d, Cluster("C1"))})}
        self.assertEqual(sorted(rows), [("big", "oversized"), ("idle", "idle"), ("off200", "powered off 90+ days"), ("off45", "powered off 30+ days")])
        self.assertEqual((rows[("off200", "powered off 90+ days")]["storage"], rows[("off45", "powered off 30+ days")]["counted"]), (900, False))
        self.assertEqual(rows[("idle", "idle")]["ram"], 32)
        self.assertIn("16 vCPU / 64 GB → 3 vCPU / 20 GB", rows[("big", "oversized")]["detail"])
        self.assertEqual((rows[("big", "oversized")]["vcpu"], rows[("big", "oversized")]["ram"]), (13, 44))

    def test_snapshots_owner_and_savings_counted_once(self):
        vms = [vm("a", ram=32, cpu=1.0, mem=5.0, owner=""), vm("b", owner="unknown")]
        snaps = [Snapshot("a", "old", datetime(2026, 8, 1), 300), Snapshot("a", "new", datetime(2026, 10, 25), 50),
                 Snapshot("b", "week", datetime(2026, 10, 15), 40)]
        d = data(vms=vms, snaps=snaps)
        rec = reclaim(d, {"c1": capacity(d, Cluster("C1"))})
        self.assertEqual(sorted((x["vm"].vm, x["reason"]) for x in rec),
                         [("a", "idle"), ("a", "no owner"), ("a", "old snapshot"), ("b", "no owner"), ("b", "old snapshot")])
        self.assertEqual(reclaim_totals(rec, "C1"), dict(ram=32, cpu=reclaim_totals(rec)["cpu"], vcpu=4, storage=340, vms=2))
        f = {x.code: x for x in run_all(d)}
        self.assertEqual((f["RCL-04"].severity, f["RCL-05"].items), (CRITICAL, ["a", "b"]))   # 87 days > 30


class TestStorageAvailabilityLifecycle(unittest.TestCase):
    def test_datastore_thresholds_and_thin_provisioning(self):
        ds = [Datastore("ok", "C1", 1000, 400, 1200), Datastore("low", "C1", 1000, 150), Datastore("full", "C1", 1000, 50),
              Datastore("thin", "C1", 1000, 300)]
        vms = [vm("x", prov=1800, ds="thin")]                                          # provisioned from the VMs when not exported
        d = data(datastores=ds, vms=vms)
        st = {r["ds"].datastore: (r["status"], None if r["overcommit"] is None else round(r["overcommit"], 2)) for r in datastore_rows(d)}
        self.assertEqual(st, {"ok": ("ok", 1.2), "low": ("warning", None), "full": ("critical", None), "thin": ("overcommitted", 1.8)})
        r = cluster_rows(d)[0]
        self.assertEqual(sorted((i.code, i.severity) for i in r["issues"] if i.code.startswith("STO")),
                         [("STO-01", CRITICAL), ("STO-01", WARNING), ("STO-02", WARNING)])

    def test_availability_in_the_month(self):
        ev = [Event(datetime(2026, 8, 31, 23, 30), datetime(2026, 9, 1, 0, 30), "C1", "cluster_outage"),      # 30 min inside September
              Event(datetime(2026, 9, 17, 2, 10), datetime(2026, 9, 17, 2, 25), "C1", "cluster_outage"),
              Event(datetime(2026, 9, 8, 14, 0), datetime(2026, 9, 8, 20, 0), "C1", "host_failure", "h2", 12),
              Event(datetime(2026, 9, 24, 21, 0), datetime(2026, 9, 24, 23, 0), "C1", "maintenance"),
              Event(datetime(2026, 10, 2), datetime(2026, 10, 3), "C1", "cluster_outage")]
        d = data(events=ev)
        av = availability(d, Cluster("C1"))
        self.assertEqual((av["outage"], len(av["failures"]), av["restarted"]), (45, 1, 12))
        self.assertAlmostEqual(av["pct"], 100 * (1 - 45 / 43200))
        iss = codes(d)
        self.assertEqual((iss["AVL-01"].severity, iss["AVL-02"].severity), (CRITICAL, WARNING))
        self.assertNotIn("AVL-01", codes(data(events=ev[2:])))

    def test_support_and_firmware(self):
        hs = hosts(n=4)
        hs[0].support_end, hs[1].support_end = date(2026, 6, 30), date(2027, 2, 28)
        hs[2].firmware, hs[2].firmware_target = "2.2.4", "2.3.5"
        d = data(hs=hs, util=history(mem=(40.0, 0.0)))
        self.assertEqual(codes(d)["LCM-01"].severity, WARNING)
        f = [(x.code, x.severity, x.items) for x in run_all(d) if x.code.startswith("LCM")]
        self.assertEqual(f, [("LCM-01", WARNING, ["C1"]), ("LCM-01", INFO, ["c1-h2"]), ("LCM-02", INFO, ["c1-h3"])])


class TestDemo(unittest.TestCase):
    def test_demo_end_to_end_is_deterministic(self):
        hashes = []
        with tempfile.TemporaryDirectory() as t:
            for k in range(2):
                out = Path(t) / f"out{k}"
                self.assertEqual(cli.main(["demo", "--data", str(Path(t) / f"data{k}"), "--out", str(out)]), 0)
                self.assertEqual(sorted(p.name for p in out.iterdir()), sorted(cli.OUTPUTS))
                hashes.append({n: hashlib.sha1((out / n).read_bytes()).hexdigest() for n in cli.OUTPUTS})
                self.assertNotIn(b"\r\n", (Path(t) / f"data{k}" / "vms.csv").read_bytes())
            self.assertEqual(hashes[0], hashes[1])
            d = load(Path(t) / "data0")
            html = (Path(t) / "out0" / "report.html").read_text(encoding="utf-8")
            md = (Path(t) / "out0" / "capacity-review.md").read_text(encoding="utf-8")
            with open(Path(t) / "out0" / "forecast.csv", encoding="utf-8") as fh:
                fc = list(csv.DictReader(fh))
        self.assertEqual(html.count("<svg class='chart'"), 4)
        self.assertIn("| MAD-PROD-01 | RAM | Jan 2027 (3 months) | 5 months | 7 months | Nov 2026 |", md)
        self.assertEqual({(x["cluster"], x["metric"]) for x in fc if x["month"] == "2027-01" and float(x["with_pipeline"]) > float(x["limit"])},
                         {("MAD-PROD-01", "ram")})
        rows = {r["cluster"].cluster: r for r in cluster_rows(d)}
        f = run_all(d)
        self.assertEqual([x.severity for x in f], sorted((x.severity for x in f), key=ORDER.get))
        for c in ("CAP-01", "CAP-02", "CAP-04", "FCT-01", "FCT-02", "DEM-01", "DEM-02", "STO-01", "STO-02", "RCL-01", "RCL-02", "RCL-03",
                  "RCL-04", "RCL-05", "AVL-01", "AVL-02", "AVL-03", "LCM-01", "LCM-02"):
            self.assertIn(c, {x.code for x in f})
        mad = rows["MAD-PROD-01"]["forecast"]
        self.assertEqual((mad["breach"]["pipeline"], mad["binding"]["pipeline"]), ((2027, 1), "ram"))     # fails N+1 within 4 months
        self.assertGreater(mad["runway"]["reclaim"], d.settings.purchase_lead_months)                      # the reclaim list buys the time
        self.assertEqual({k: r["verdict"] for k, r in rows.items()},
                         {"MAD-PROD-01": "at risk", "MAD-ENG-01": "attention", "GDL-PROD-01": "at risk", "GDL-MGMT-01": "attention"})
        self.assertEqual(d.settings.organisation, "Fabrikam Engineering")

    def test_review_reports_missing_files(self):
        with tempfile.TemporaryDirectory() as t:
            self.assertEqual(cli.main(["review", "--data", t, "--out", str(Path(t) / "o")]), 2)


if __name__ == "__main__":
    unittest.main()
