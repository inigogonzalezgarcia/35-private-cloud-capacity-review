# Data format

Put the files in one folder and run `python -m capreview review --data <folder>`. Only `clusters.csv` and `hosts.csv` are required; every other file adds its checks when present. Comma, semicolon or tab separated, UTF-8. Column names are matched loosely (case, accents and punctuation are ignored), and the common alternatives below, including the PowerCLI property names and Spanish names, are accepted. `scripts/Export-PrivateCloudCapacity.ps1` writes every file except `demand.csv` and `settings.json`.

## clusters.csv (required)

| Column | Also accepted | Notes |
|---|---|---|
| Cluster | name | Must match the Cluster column of the other files |
| Site | datacenter, cpd, sede | Data centre |
| Platform | hypervisor, plataforma | Free text: `vSphere 8.0 U3`, `Nutanix AHV (AOS 6.10)`, `OpenShift Virtualization` |
| Failover hosts | ha failover level, host failures tolerated | Hosts the cluster must survive losing (N+1 = 1). Empty: `failover_hosts` in settings |
| HA policy | admission control policy | For the report; the check always removes the largest hosts |
| CPU max pct, RAM max pct, vCPU ratio max | | Optional per-cluster overrides of the settings |

A cluster that appears only in `hosts.csv` is added with the defaults.

## hosts.csv (required)

| Column | Also accepted | Notes |
|---|---|---|
| Host, Cluster | name, node | |
| Vendor, Model | manufacturer | Dell, HPE, Supermicro, Nutanix… |
| Cores | NumCpu, cpu cores | Physical cores |
| CPU total MHz or CPU GHz | CpuTotalMhz | Cores × clock |
| Memory GB | MemoryTotalGB | |
| Connection state | state, estado | Connected, Maintenance, Disconnected / NotResponding. Anything but connected raises CAP-04 |
| Version | build | ESXi or AOS version, for the report |
| Firmware, Firmware target | bios version, baseline | Hosts behind the baseline are listed (LCM-02) |
| Support end | warranty end, fin soporte | Vendor support or warranty end (LCM-01) |

Capacity counts every host in the cluster; the N+1 capacity removes the `Failover hosts` largest ones, separately for CPU and RAM.

## vms.csv

| Column | Also accepted | Notes |
|---|---|---|
| VM, Cluster | name | |
| Power state | PowerState | PoweredOn, PoweredOff (Suspended counts as off) |
| vCPU, Memory GB | NumCpu, MemoryGB | Running VMs make the vCPU:pCPU ratio and the vRAM overcommit |
| Provisioned GB, Used GB | ProvisionedSpaceGB, UsedSpaceGB | Provisioned space of powered-off VMs is what deleting them gives back |
| Datastore | storage container | Used to estimate thin provisioning when `datastores.csv` has no provisioned column |
| Owner, Service | responsable, aplicacion | From a vSphere tag, custom attribute or Nutanix category. Empty or `unknown` raises RCL-05 |
| Last powered on | powered off since | Running VMs: boot time. Powered-off VMs: the last time they were switched off; empty means not seen in the event history |
| CPU p95, Mem p95 | | 95th percentile of `cpu.usage.average` and `mem.usage.average` over 30 days, % of the VM's own vCPU and vRAM. Empty: the VM is not judged idle or oversized |

## snapshots.csv

| Column | Also accepted | Notes |
|---|---|---|
| VM, Snapshot | name | |
| Created | date | Older than `snapshot_max_days` is a reclaim candidate; older than `snapshot_crit_days` makes RCL-04 critical |
| Size GB | SizeGB | Storage given back when consolidated |

## datastores.csv

One row per datastore or Nutanix storage container. For Nutanix, containers share the storage pool: either one row per cluster with the pool, or the containers with their own maximum capacity.

| Column | Also accepted | Notes |
|---|---|---|
| Datastore, Cluster | storage container | |
| Type | | VMFS, NFS, vSAN, Nutanix storage container |
| Capacity GB, Free GB | CapacityGB, FreeSpaceGB | Free below `datastore_warn_free_pct` or `datastore_crit_free_pct` raises STO-01 |
| Provisioned GB | | vSphere: capacity − free + uncommitted. Empty: the sum of the VMs on it. Above `thin_overcommit_max` × capacity raises STO-02 |

The storage limit of a cluster for the forecast is its datastore capacity × (100 − `datastore_warn_free_pct`) %.

## utilisation.csv

One row per cluster and month, 12 months or more. The forecast uses the last `history_months`.

| Column | Also accepted | Notes |
|---|---|---|
| Cluster, Month | mes, period | `YYYY-MM` or `MM/YYYY` |
| CPU avg, CPU p95 | | Monthly average and 95th percentile of the daily `cpu.usage.average` of the cluster, % of the cluster's total |
| Mem avg, Mem p95 | ram p95 | Same for `mem.usage.average` |
| CPU used GHz, RAM used GB | | Optional absolute values. Use them if hosts were added or removed during the year, otherwise the percentages are applied to today's capacity |
| Storage used GB | | Used capacity of the cluster's datastores that month. The exporter fills the last complete month on every run |

## demand.csv

The demand pipeline from the project portfolio or the change requests.

| Column | Also accepted | Notes |
|---|---|---|
| Project, Cluster | proyecto, destino | Items without a cluster are listed (DEM-02) but not forecast |
| Go-live | needed by, fecha | The month the VMs land; a date already past lands this month |
| Status | estado | Only `demand_statuses` (approved, committed) count in the forecast |
| VMs, vCPU, RAM GB, Storage GB | | CPU demand is vCPU × the GHz each running vCPU of the cluster uses today (capped at one core); RAM is `demand_ram_factor` × RAM |
| Owner, Ticket | requester, rfc | |

## events.csv

Host failures, HA restarts and outages, from the vCenter events and the incident records.

| Column | Also accepted | Notes |
|---|---|---|
| Start, End | inicio, fin | An outage without an end counts no minutes: add it from the incident record |
| Cluster, Host | node | |
| Type | event type | `Cluster outage` (all VMs of the cluster down: counts against availability), `Host failure` / DasHostFailedEvent / HostNotRespondingEvent, `HA restart` / com.vmware.vc.ha.VmRestartedByHAEvent, `Maintenance` (not counted) |
| VMs | vms affected | VMs restarted by HA |
| Ticket, Summary | incidencia, FullFormattedMessage | |

Availability of a cluster = 1 − cluster outage minutes inside the reviewed month / minutes in the month.

## settings.json

| Key | Default | What |
|---|---|---|
| `organisation`, `as_of`, `period` | | Name for the reports; date of the capacity meeting (otherwise today); month reviewed for availability (otherwise the month before) |
| `failover_hosts` | 1 | N+1 by default |
| `cpu_max_pct`, `ram_max_pct`, `warn_margin_pct` | 80, 90, 10 | Maximum use of what is left after the failover hosts fail; attention within the margin |
| `vcpu_ratio_max`, `ram_overcommit_max` | 4.0, 1.2 | vCPU:pCPU policy (critical above 1.5 × policy); running vRAM / RAM after a failure |
| `datastore_warn_free_pct`, `datastore_crit_free_pct`, `thin_overcommit_max` | 20, 10, 1.5 | |
| `history_months`, `history_min_months`, `forecast_months` | 12, 6, 24 | Trend window, minimum history, forecast length |
| `purchase_lead_months`, `plan_horizon_months` | 4, 12 | From order to hosts in production; runway inside the lead time is critical, inside the horizon a warning |
| `demand_statuses`, `demand_ram_factor` | approved, committed · 1.0 | |
| `poweredoff_warn_days`, `poweredoff_reclaim_days` | 30, 90 | Listed · counted as reclaimable storage |
| `idle_cpu_pct`, `idle_mem_pct` | 3, 15 | Running VMs below both p95 values are idle |
| `oversize_cpu_pct`, `oversize_mem_pct`, `oversize_min_vcpu`, `oversize_min_ram_gb`, `rightsize_target_pct` | 25, 30, 4, 16, 60 | Right-size so the p95 lands at the target, in steps of 4 GB, never below 2 vCPU or 4 GB |
| `snapshot_max_days`, `snapshot_crit_days` | 7, 30 | |
| `availability_target_pct`, `support_warn_days` | 99.95, 180 | |
| `owners` | | Owner per area: capacity, forecast, storage, reclaim, availability, lifecycle |

What the reclaim list gives back in the forecast, from the month after the meeting: the RAM and CPU of idle VMs, the RAM removed by right-sizing, the storage of VMs off for `poweredoff_reclaim_days` and of old snapshots. Each VM counts once per resource.
