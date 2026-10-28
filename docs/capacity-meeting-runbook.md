# Monthly capacity meeting runbook

A 45-minute meeting in the first week of every month, with the capacity manager, the platform and storage leads and the head of infrastructure, and the service owners whose VMs are on the reclaim list when the list is long. The tool only reads exports; VMs, hosts and orders are changed through the usual changes and the CAB.

## Before the meeting (the week before)

1. Export from vCenter and Prism Central with a read-only account:
   ```powershell
   .\scripts\Export-PrivateCloudCapacity.ps1 -VCenter vc-mad.fabrikam.local, vc-gdl.fabrikam.local -Credential (Get-Credential) `
       -PrismCentral pc.fabrikam.local -PrismCredential (Get-Credential) -HostInventory .\data\host-support.csv -OutputFolder .\data
   ```
   Keep `data/utilisation.csv` from month to month: the exporter adds the new month and keeps the old ones.
2. Add the end time and the ticket of every outage of the month to `events.csv` from the incident records, and mark the ones where the whole cluster was down as `Cluster outage`.
3. Update `demand.csv` with the project portfolio: what was approved, moved or cancelled since last month.
4. Build the outputs and send `capacity-review.md` with the agenda:
   ```bash
   python -m capreview review --data data/ --title "Capacity review · November"
   ```
5. Send each service owner their rows of `reclaim.csv` so they come with an answer.

## Agenda

| Minutes | Topic | Output |
|---|---|---|
| 10 | Clusters at risk: N+1 headroom today and runway against the purchase lead time | `report.html` → Decisions, cluster cards |
| 10 | Demand: new and moved projects, the ones that do not fit at go-live | `demand.csv` |
| 10 | Reclaim: what the owners accepted, what goes to the CAB this month | `reclaim.csv` |
| 5 | Storage: datastores below the threshold, thin provisioning | `datastores.csv` |
| 5 | Availability of the month against the target, host failures, problems open | `report.html` → Availability |
| 5 | Decisions and owners | `actions.csv` |

## How to read it

- **N+1 is the capacity.** A cluster at 80% of its RAM is fine until a host fails; what matters is what is left after the largest host is gone, and whether HA can restart everything on it.
- **The runway is a date, compared with another date.** Months until the limit with the approved demand, against the months it takes to get hosts in production. When the first is shorter, the decision is today.
- **Reclaim buys time, it does not replace the order.** Count it only when the owners have accepted it; a reclaim list nobody executes moves the date on paper only.
- **Demand counts when it is approved.** Proposed projects are listed apart; when one is approved it moves the date at the next review.
- **p95, not average.** The average hides the month-end close and the batch window; the cluster has to hold the peak.
- **Availability is per cluster and per month.** A host failure that HA recovered is a hardware case; a cluster outage is a problem record and a line in the availability report.

## After the meeting

- Raise the changes for the right-sizing and the deletions accepted by the owners, through the CAB.
- Start the purchase for every cluster whose *order by* month is this month or next, with the host count from the decisions table.
- Keep each month's `output/` folder: the next meeting opens with what changed.
