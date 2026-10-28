<#
.SYNOPSIS
    Read-only export of vCenter and Nutanix Prism Central into the CSV files that capreview reads.

.DESCRIPTION
    Writes clusters.csv, hosts.csv, vms.csv, snapshots.csv, datastores.csv, utilisation.csv and events.csv to -OutputFolder.
    demand.csv (the approved projects) and settings.json are yours to write; see docs/data-format.md.

    vCenter, with VMware PowerCLI (VCF.PowerCLI or VMware.PowerCLI):
      - Get-Cluster: HA failover level and admission control policy;
      - Get-VMHost: cores, CPU MHz, memory, connection state, version and build, BIOS version;
      - Get-VM: vCPU, memory, provisioned and used space, power state, boot time, owner and service tags or custom attributes;
        for powered-off VMs, the last VmPoweredOffEvent still in the event history (vCenter keeps events for a limited time);
      - Get-Stat cpu.usage.average and mem.usage.average: per VM the 95th percentile of the 2-hour rollup of the last 30 days,
        per cluster the monthly average and 95th percentile of the daily rollup (vCenter keeps daily values for one year);
      - Get-Snapshot, Get-Datastore (provisioned = capacity - free + uncommitted);
      - Get-VIEvent: host failures (DasHostFailedEvent, HostNotRespondingEvent) and VMs restarted by vSphere HA
        (com.vmware.vc.ha.VmRestartedByHAEvent) in -EventDays.
    Nutanix Prism Central:
      - v3 list calls (POST .../list, they only read): clusters, hosts, VMs with their disks;
      - v4 GET calls: storage containers and their capacity and free space, cluster CPU and memory statistics
        (hypervisorCpuUsagePpm, aggregateHypervisorMemoryUsagePpm, daily average).

    utilisation.csv keeps the months already in the file: run it every month and the history grows past what vCenter keeps.
    Storage used per cluster is only known for the month of the run, so the storage trend builds up month by month.

    It only reads: a read-only vCenter role and a Prism Central viewer account are enough. Nothing is changed in vCenter or Nutanix.

.PARAMETER VCenter
    One or more vCenter servers.

.PARAMETER PrismCentral
    Prism Central address (optional). Uses -PrismCredential.

.PARAMETER HostInventory
    Optional CSV with Host, Support end, Firmware target from the vendor portal or the CMDB, merged into hosts.csv.

.EXAMPLE
    .\scripts\Export-PrivateCloudCapacity.ps1 -VCenter vc-mad.fabrikam.local, vc-gdl.fabrikam.local -Credential (Get-Credential) `
        -PrismCentral pc.fabrikam.local -PrismCredential (Get-Credential) -HostInventory .\data\host-support.csv -OutputFolder .\data
#>
[CmdletBinding()]
param(
    [string[]] $VCenter = @(),
    [pscredential] $Credential,
    [string] $PrismCentral,
    [pscredential] $PrismCredential,
    [string] $OutputFolder = '.\data',
    [string] $HostInventory,
    [string] $OwnerCategory = 'Owner',
    [string] $ServiceCategory = 'Service',
    [int] $Months = 12,
    [int] $EventDays = 31,
    [switch] $SkipVmStats
)

$ErrorActionPreference = 'Stop'
New-Item -ItemType Directory -Force -Path $OutputFolder | Out-Null
$now = Get-Date
$fmt = 'yyyy-MM-dd HH:mm'
$clusters = [System.Collections.Generic.List[object]]::new()
$hosts = [System.Collections.Generic.List[object]]::new()
$vms = [System.Collections.Generic.List[object]]::new()
$snaps = [System.Collections.Generic.List[object]]::new()
$stores = [System.Collections.Generic.List[object]]::new()
$util = [System.Collections.Generic.List[object]]::new()
$events = [System.Collections.Generic.List[object]]::new()

function Get-P95([double[]] $values) {
    if (-not $values -or $values.Count -eq 0) { return $null }
    $s = $values | Sort-Object
    return [math]::Round($s[[math]::Min($s.Count - 1, [math]::Ceiling(0.95 * $s.Count) - 1)], 1)
}

function Get-Label($entity, [string] $category) {
    $tag = Get-TagAssignment -Entity $entity -Category $category -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($tag) { return $tag.Tag.Name }
    $ann = Get-Annotation -Entity $entity -CustomAttribute $category -ErrorAction SilentlyContinue
    if ($ann) { return $ann.Value }
    return ''
}

$support = @{}
if ($HostInventory) { foreach ($r in Import-Csv $HostInventory) { $support[$r.Host] = $r } }

# ------------------------------------------------------------------ vCenter
foreach ($vc in $VCenter) {
    $server = Connect-VIServer -Server $vc -Credential $Credential
    try {
        foreach ($c in Get-Cluster -Server $server) {
            $policy = $c.ExtensionData.Configuration.DasConfig.AdmissionControlPolicy
            $clusters.Add([pscustomobject] @{
                'Cluster' = $c.Name; 'Site' = (Get-Datacenter -Cluster $c).Name; 'Platform' = "vSphere $($server.Version)"
                'Failover hosts' = $c.HAFailoverLevel
                'HA policy' = if ($c.HAAdmissionControlEnabled -and $policy) { $policy.GetType().Name -replace 'Cluster|AdmissionControlPolicy', '' } else { 'disabled' }
            })
            foreach ($h in Get-VMHost -Location $c) {
                $inv = $support[$h.Name]
                $hosts.Add([pscustomobject] @{
                    'Host' = $h.Name; 'Cluster' = $c.Name; 'Vendor' = $h.Manufacturer; 'Model' = $h.Model; 'Cores' = $h.NumCpu
                    'CPU total MHz' = $h.CpuTotalMhz; 'Memory GB' = [math]::Round($h.MemoryTotalGB); 'Connection state' = $h.ConnectionState
                    'Version' = "$($h.Version) ($($h.Build))"; 'Firmware' = $h.ExtensionData.Hardware.BiosInfo.BiosVersion
                    'Firmware target' = $inv.'Firmware target'; 'Support end' = $inv.'Support end'
                })
            }
            # monthly CPU and memory of the cluster from the daily rollup
            $start = (Get-Date -Day 1).Date.AddMonths(-$Months)
            $stat = Get-Stat -Entity $c -Stat 'cpu.usage.average', 'mem.usage.average' -Start $start -Finish $now -IntervalMins 1440 -ErrorAction SilentlyContinue
            $clusterDs = @($c | Get-VMHost | Get-Datastore | Sort-Object Name -Unique)
            $used = ($clusterDs | Measure-Object -Property CapacityGB -Sum).Sum - ($clusterDs | Measure-Object -Property FreeSpaceGB -Sum).Sum
            foreach ($g in $stat | Where-Object { $_.Instance -eq '' } | Group-Object { $_.Timestamp.ToString('yyyy-MM') }) {
                if ($g.Name -eq $now.ToString('yyyy-MM')) { continue }              # only complete months
                $cpu = [double[]] ($g.Group | Where-Object MetricId -eq 'cpu.usage.average' | ForEach-Object Value)
                $mem = [double[]] ($g.Group | Where-Object MetricId -eq 'mem.usage.average' | ForEach-Object Value)
                $util.Add([pscustomobject] @{
                    'Cluster' = $c.Name; 'Month' = $g.Name
                    'CPU avg' = [math]::Round(($cpu | Measure-Object -Average).Average, 1); 'CPU p95' = Get-P95 $cpu
                    'Mem avg' = [math]::Round(($mem | Measure-Object -Average).Average, 1); 'Mem p95' = Get-P95 $mem
                    'Storage used GB' = if ($g.Name -eq $now.AddMonths(-1).ToString('yyyy-MM')) { [math]::Round($used) } else { '' }
                })
            }
            foreach ($ds in $clusterDs) {
                $sum = $ds.ExtensionData.Summary
                $stores.Add([pscustomobject] @{
                    'Datastore' = $ds.Name; 'Cluster' = $c.Name; 'Type' = $ds.Type; 'Capacity GB' = [math]::Round($ds.CapacityGB)
                    'Free GB' = [math]::Round($ds.FreeSpaceGB)
                    'Provisioned GB' = [math]::Round(($sum.Capacity - $sum.FreeSpace + $sum.Uncommitted) / 1GB)
                })
            }
            $clusterVms = @(Get-VM -Location $c)
            $p95 = @{}
            if (-not $SkipVmStats -and $clusterVms) {
                $on = @($clusterVms | Where-Object PowerState -eq 'PoweredOn')
                if ($on) {
                    $vs = Get-Stat -Entity $on -Stat 'cpu.usage.average', 'mem.usage.average' -Start $now.AddDays(-30) -Finish $now -IntervalMins 120 -ErrorAction SilentlyContinue
                    foreach ($g in $vs | Where-Object { $_.Instance -eq '' } | Group-Object { "$($_.Entity.Name)|$($_.MetricId)" }) {
                        $p95[$g.Name] = Get-P95 ([double[]] ($g.Group | ForEach-Object Value))
                    }
                }
            }
            foreach ($v in $clusterVms) {
                $last = $v.ExtensionData.Runtime.BootTime
                if ($v.PowerState -ne 'PoweredOn') {
                    $off = Get-VIEvent -Entity $v -MaxSamples 500 -ErrorAction SilentlyContinue |
                        Where-Object { $_ -is [VMware.Vim.VmPoweredOffEvent] } | Sort-Object CreatedTime -Descending | Select-Object -First 1
                    $last = if ($off) { $off.CreatedTime } else { $null }
                }
                $vms.Add([pscustomobject] @{
                    'VM' = $v.Name; 'Cluster' = $c.Name; 'Power state' = $v.PowerState; 'vCPU' = $v.NumCpu; 'Memory GB' = $v.MemoryGB
                    'Provisioned GB' = [math]::Round($v.ProvisionedSpaceGB); 'Used GB' = [math]::Round($v.UsedSpaceGB)
                    'Datastore' = (Get-Datastore -RelatedObject $v | Select-Object -First 1).Name
                    'Owner' = Get-Label $v $OwnerCategory; 'Service' = Get-Label $v $ServiceCategory
                    'Last powered on' = if ($last) { $last.ToString($fmt) } else { '' }
                    'CPU p95' = $p95["$($v.Name)|cpu.usage.average"]; 'Mem p95' = $p95["$($v.Name)|mem.usage.average"]
                })
            }
            foreach ($s in $clusterVms | Get-Snapshot) {
                $snaps.Add([pscustomobject] @{ 'VM' = $s.VM.Name; 'Snapshot' = $s.Name; 'Created' = $s.Created.ToString($fmt); 'Size GB' = [math]::Round($s.SizeGB, 1) })
            }
        }
        # host failures and HA restarts
        $ev = Get-VIEvent -Server $server -Start $now.AddDays(-$EventDays) -Finish $now -MaxSamples 200000 -ErrorAction SilentlyContinue
        foreach ($x in $ev) {
            $type = $x.GetType().Name
            if ($type -eq 'EventEx' -or $type -eq 'ExtendedEvent') { $type = $x.EventTypeId }
            if ($type -notin 'DasHostFailedEvent', 'HostNotRespondingEvent', 'com.vmware.vc.ha.VmRestartedByHAEvent') { continue }
            $events.Add([pscustomobject] @{
                'Start' = $x.CreatedTime.ToString($fmt); 'End' = ''; 'Cluster' = $x.ComputeResource.Name; 'Host' = $x.Host.Name
                'Type' = $type; 'VMs' = if ($type -like '*VmRestartedByHAEvent') { 1 } else { 0 }; 'Ticket' = ''; 'Summary' = $x.FullFormattedMessage
            })
        }
    } finally {
        Disconnect-VIServer -Server $server -Confirm:$false
    }
}

# ------------------------------------------------------------------ Nutanix Prism Central
if ($PrismCentral) {
    $pair = "$($PrismCredential.UserName):$($PrismCredential.GetNetworkCredential().Password)"
    $headers = @{ Authorization = 'Basic ' + [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($pair)); Accept = 'application/json' }
    $base = "https://$($PrismCentral):9440"
    function Get-V3List([string] $kind) {
        $all, $offset = @(), 0
        do {
            $body = @{ kind = $kind; length = 250; offset = $offset } | ConvertTo-Json
            $r = Invoke-RestMethod -Method Post -Uri "$base/api/nutanix/v3/$($kind)s/list" -Headers $headers -ContentType 'application/json' -Body $body
            $all += $r.entities
            $offset += $r.entities.Count
        } while ($r.entities.Count -gt 0 -and $offset -lt $r.metadata.total_matches)
        return $all
    }
    function Get-V4List([string] $path) {
        $all, $page = @(), 0
        do {
            $r = Invoke-RestMethod -Method Get -Uri "$base$($path)?`$page=$page&`$limit=100" -Headers $headers
            $all += $r.data
            $page++
        } while ($r.data.Count -eq 100)
        return $all
    }
    $ntxClusters = Get-V3List 'cluster' | Where-Object { 'PRISM_CENTRAL' -notin $_.status.resources.config.service_list }
    foreach ($c in $ntxClusters) {
        $clusters.Add([pscustomobject] @{ 'Cluster' = $c.status.name; 'Site' = ''; 'Platform' = 'Nutanix AHV'; 'Failover hosts' = 1
                                          'HA policy' = "Nutanix HA (RF$($c.status.resources.config.redundancy_factor))" })
    }
    $names = $ntxClusters | ForEach-Object { $_.status.name }
    foreach ($h in Get-V3List 'host' | Where-Object { $_.status.cluster_reference.name -in $names }) {
        $res = $h.status.resources
        $inv = $support[$h.status.name]
        $hosts.Add([pscustomobject] @{
            'Host' = $h.status.name; 'Cluster' = $h.status.cluster_reference.name; 'Vendor' = 'Nutanix'; 'Model' = $res.block.block_model
            'Cores' = $res.num_cpu_cores; 'CPU total MHz' = [math]::Round($res.cpu_capacity_hz / 1e6); 'Memory GB' = [math]::Round($res.memory_capacity_mib / 1024)
            'Connection state' = 'Connected'; 'Version' = $res.hypervisor.hypervisor_full_name; 'Firmware' = ''
            'Firmware target' = $inv.'Firmware target'; 'Support end' = $inv.'Support end'
        })
    }
    foreach ($v in Get-V3List 'vm' | Where-Object { $_.spec.cluster_reference.name -in $names }) {
        $r = $v.spec.resources
        $disks = @($r.disk_list | Where-Object { $_.device_properties.device_type -eq 'DISK' })
        $vms.Add([pscustomobject] @{
            'VM' = $v.spec.name; 'Cluster' = $v.spec.cluster_reference.name; 'Power state' = if ($r.power_state -eq 'ON') { 'PoweredOn' } else { 'PoweredOff' }
            'vCPU' = $r.num_sockets * $r.num_vcpus_per_socket; 'Memory GB' = [math]::Round($r.memory_size_mib / 1024)
            'Provisioned GB' = [math]::Round((($disks | Measure-Object -Property disk_size_mib -Sum).Sum) / 1024); 'Used GB' = ''
            'Datastore' = ($disks | Select-Object -First 1).storage_config.storage_container_reference.name
            'Owner' = $v.metadata.owner_reference.name; 'Service' = ($v.metadata.categories.$ServiceCategory)
            'Last powered on' = ''; 'CPU p95' = ''; 'Mem p95' = ''
        })
    }
    $iso = { param($d) $d.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ') }
    foreach ($ct in Get-V4List '/api/clustermgmt/v4.0/config/storage-containers') {
        if ($ct.clusterName -notin $names) { continue }
        $id = if ($ct.containerExtId) { $ct.containerExtId } else { $ct.extId }
        $q = "`$startTime=$(& $iso $now.AddHours(-1))&`$endTime=$(& $iso $now)&`$samplingInterval=3600&`$statType=LAST"
        $st = (Invoke-RestMethod -Method Get -Uri "$base/api/clustermgmt/v4.0/stats/storage-containers/$($id)?$q" -Headers $headers).data
        $cap = ($st.storageCapacityBytes | Select-Object -Last 1).value
        $free = ($st.storageFreeBytes | Select-Object -Last 1).value
        $stores.Add([pscustomobject] @{ 'Datastore' = $ct.name; 'Cluster' = $ct.clusterName; 'Type' = 'Nutanix storage container'
                                        'Capacity GB' = [math]::Round($cap / 1GB); 'Free GB' = [math]::Round($free / 1GB); 'Provisioned GB' = '' })
    }
    $start = (Get-Date -Day 1).Date.AddMonths(-$Months)
    foreach ($c in Get-V4List '/api/clustermgmt/v4.0/config/clusters' | Where-Object { $_.name -in $names }) {
        $q = "`$startTime=$(& $iso $start)&`$endTime=$(& $iso $now)&`$samplingInterval=86400&`$statType=AVG" +
             "&`$select=hypervisorCpuUsagePpm,aggregateHypervisorMemoryUsagePpm"
        $st = (Invoke-RestMethod -Method Get -Uri "$base/api/clustermgmt/v4.0/stats/clusters/$($c.extId)?$q" -Headers $headers).data
        $points = @($st.hypervisorCpuUsagePpm | ForEach-Object { [pscustomobject] @{ T = [datetime] $_.timestamp; M = 'cpu'; V = $_.value / 10000 } }) +
                  @($st.aggregateHypervisorMemoryUsagePpm | ForEach-Object { [pscustomobject] @{ T = [datetime] $_.timestamp; M = 'mem'; V = $_.value / 10000 } })
        $used = ($stores | Where-Object Cluster -eq $c.name | ForEach-Object { $_.'Capacity GB' - $_.'Free GB' } | Measure-Object -Sum).Sum
        foreach ($g in $points | Group-Object { $_.T.ToString('yyyy-MM') }) {
            if ($g.Name -eq $now.ToString('yyyy-MM')) { continue }
            $cpu = [double[]] ($g.Group | Where-Object M -eq 'cpu' | ForEach-Object V)
            $mem = [double[]] ($g.Group | Where-Object M -eq 'mem' | ForEach-Object V)
            $util.Add([pscustomobject] @{
                'Cluster' = $c.name; 'Month' = $g.Name; 'CPU avg' = [math]::Round(($cpu | Measure-Object -Average).Average, 1); 'CPU p95' = Get-P95 $cpu
                'Mem avg' = [math]::Round(($mem | Measure-Object -Average).Average, 1); 'Mem p95' = Get-P95 $mem
                'Storage used GB' = if ($g.Name -eq $now.AddMonths(-1).ToString('yyyy-MM')) { [math]::Round($used) } else { '' }
            })
        }
    }
}

# ------------------------------------------------------------------ write, keeping the months already exported
$utilFile = Join-Path $OutputFolder 'utilisation.csv'
if (Test-Path $utilFile) {
    $have = @{}
    foreach ($u in $util) { $have["$($u.Cluster)|$($u.Month)"] = $true }
    foreach ($old in Import-Csv $utilFile) {
        $key = "$($old.Cluster)|$($old.Month)"
        if (-not $have[$key]) { $util.Add($old) }
        elseif (-not ($util | Where-Object { "$($_.Cluster)|$($_.Month)" -eq $key }).'Storage used GB') {
            ($util | Where-Object { "$($_.Cluster)|$($_.Month)" -eq $key }).'Storage used GB' = $old.'Storage used GB'
        }
    }
}
$out = @{ 'clusters' = $clusters; 'hosts' = $hosts; 'vms' = $vms; 'snapshots' = $snaps; 'datastores' = $stores
          'utilisation' = ($util | Sort-Object Cluster, Month); 'events' = $events }
foreach ($name in $out.Keys) {
    $out[$name] | Export-Csv -Path (Join-Path $OutputFolder "$name.csv") -NoTypeInformation -Encoding utf8
}
Write-Host ("{0} clusters, {1} hosts, {2} VMs, {3} snapshots, {4} datastores, {5} cluster-months, {6} events -> {7}" -f
    $clusters.Count, $hosts.Count, $vms.Count, $snaps.Count, $stores.Count, $util.Count, $events.Count, $OutputFolder)
