#!/usr/bin/env python3
"""
Build a self-service Azure Monitor Workbook for Azure VM lifecycle,
retirements and modernization readiness.

Source of truth:
  ../compute-fleet-modernization/references/sku-migration-map.json
  ../compute-fleet-modernization/references/readiness-checklist.json

Output:
  out/azure-vm-retirement-workbook.json   (paste into Workbooks Advanced Editor)
  out/deploy-workbook.json                (ARM template, one-click deploy)

SCOPE: public information only. No pricing percentages.
"""

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# Reference data lives in the repo so a fresh clone can build. Fall back to the
# sibling compute-fleet-modernization checkout when working from the authoring
# tree, where that folder is the upstream source of truth.
_LOCAL_REFS = os.path.join(ROOT, "references")
_SIBLING_REFS = os.path.join(os.path.dirname(ROOT), "compute-fleet-modernization", "references")
REFS = _LOCAL_REFS if os.path.exists(
    os.path.join(_LOCAL_REFS, "sku-migration-map.json")) else _SIBLING_REFS

OUT = os.path.join(ROOT, "out")
WORKBOOK_DIR = os.path.join(ROOT, "workbook")

# Preview mode: restrict the embedded lookup to a named set of sizes so the
# generated workbook is small enough to paste into the portal for visual review.
# Layout, formatters and scoring are identical to the full build.
ONLY_SIZES = None

# Announced 28 Sep 2026: Dv3/Dsv3/Ev3/Esv3 enter End of Life, retire 2029-11-15.
V3_EOL_GROUPS = {"Dv3_Dsv3", "Ev3_Esv3", "Isolated_E64i_v3"}
V3_EOL_DATE = "2029-11-15"

# Azure VM price update effective 2027-02-01, per the Azure Service Health
# notification of 28 Sep 2026. The increase is scoped by SIZE SERIES, not by region: it applies
# across Azure public cloud and Azure operated by 21Vianet, and exempts only
# Azure Government and Azure Sovereign Cloud.
#
# Series names below are the catalogue's, mapped from the names used in the
# notification (for example "Bv1" covers Bsv1/Bmsv1/Blsv1, and "D"/"Ds" are
# Dv1/Dsv1 here). The workbook reports only that an update applies - the
# percentage is communicated to each tenant through its own Azure Service
# Health notification, and is deliberately not restated here.
PRICE_SERIES = {
    # notification "v1": Bv1, D, Ds, F, Fs, G, Gs, Ls, NP, HC
    "Bsv1", "Bmsv1", "Blsv1", "Dv1", "Dsv1", "Fv1", "Fsv1",
    "Gv1", "Gsv1", "Lsv1", "NPsv1", "HCv1", "HC",
    # notification "v2": Av2, Amv2, Dv2, Dsv2, Fsv2, Lsv2
    "Av2", "Amv2", "Dv2", "Dsv2", "Fsv2", "Lsv2",
}
PRICE_EFFECTIVE = "2027-02-01"

# Tag keys that commonly carry the environment, in the casings seen in the
# wild. Checked in order; the first non-empty value wins.
_ENV_TAG_KEYS = [
    "Environment", "environment", "ENVIRONMENT",
    "Env", "env", "ENV",
    "Tier", "tier", "Stage", "stage", "Usage", "usage",
]
ENV_TAGS = ("tolower(tostring(coalesce("
            + ", ".join("{col}['" + k + "']" for k in _ENV_TAG_KEYS)
            + ")))")

BLOCKING = {"gen1_not_supported", "temp_disk_removed", "memory_downgrade",
            "cpu_architecture_change"}

# Warning codes are embedded in every query, so they are stored as single
# characters and expanded back to full codes only where they are displayed.
WARN_SHORT = {
    "gen1_not_supported": "g",
    "nvme_required": "n",
    "no_exact_vcpu_match": "v",
    "memory_downgrade": "m",
    "sovereign_cloud_restriction": "s",
    "narrow_regional_availability": "r",
    "temp_disk_removed": "t",
    "no_three_az_regions": "z",
    "cpu_architecture_change": "a",
}
SHORT_WARN = {v: k for k, v in WARN_SHORT.items()}

# Per-warning readiness penalty. gen1 is handled specially (only penalised when
# the VM's *actual* OS disk is Generation 1).
PENALTY = {
    "gen1_not_supported": 45,
    "cpu_architecture_change": 50,
    "memory_downgrade": 25,
    "temp_disk_removed": 20,
    "nvme_required": 15,
    "narrow_regional_availability": 10,
    "no_three_az_regions": 10,
    "no_exact_vcpu_match": 5,
    "sovereign_cloud_restriction": 0,
}

# Prerequisite copy, keyed by warning code. Derived from readiness-checklist.json
# and kept to public information.
PREREQ = {
    "gen1_not_supported": {
        "title": "Generation 1 to Generation 2 (BIOS to UEFI) conversion",
        "sev": "Blocking",
        "what": "Convert the OS disk from Generation 1 to Generation 2, or rebuild the VM from a Gen2 image. Confirm the OS version is supported for Gen2 and that any custom or Marketplace image has a Gen2 variant.",
        "why": "Most v6 target sizes are Generation 2 only. A Gen1 VM cannot be resized into them. This is a prerequisite, not a step in the resize, and it is the most commonly underestimated item.",
        "how": "Windows: MBR2GPT. Linux: distro-specific. Or redeploy from a Gen2 image.",
        "doc": "https://learn.microsoft.com/azure/virtual-machines/generation-2",
    },
    "nvme_required": {
        "title": "NVMe disk controller and MANA networking support",
        "sev": "Validate",
        "what": "Confirm the OS image includes the NVMe driver and supports MANA (Microsoft Azure Network Adapter). Test boot on the target size before bulk migration.",
        "why": "v6 and v7 sizes use an NVMe disk controller and MANA networking. An image without the driver will not boot after resize, or will lose accelerated networking. Hardened, custom and older golden images are the usual failures.",
        "how": "Check the supported OS list, then validate one VM per image lineage.",
        "doc": "https://learn.microsoft.com/azure/virtual-machines/nvme-overview",
    },
    "temp_disk_removed": {
        "title": "Local temp disk dependency removal",
        "sev": "Blocking",
        "what": "Find every dependency on the local temp disk (D:\\ on Windows, /mnt on Linux) - pagefile/swap, SQL Server tempdb, application scratch and cache directories, log staging. Relocate them, or choose a 'd' variant of the target series to keep local storage.",
        "why": "Non-'d' target series have no local disk. Anything pinned to that path breaks silently on first boot, and SQL tempdb failures take the instance down.",
        "how": "Move the page file and tempdb to a managed data disk, or select the 'd' variant target.",
        "doc": "https://learn.microsoft.com/azure/virtual-machines/managed-disks-overview",
    },
    "memory_downgrade": {
        "title": "Memory parity check",
        "sev": "Blocking",
        "what": "The same-vCPU target carries less memory than the current size. Confirm real memory utilisation before accepting it, or move to the memory-preserving alternative shown in the Target mapping tab.",
        "why": "Common on G-series and the large memory-optimised sizes. Silently under-provisioning RAM on a database or in-memory workload causes outages.",
        "how": "Baseline 30-90 days of memory metrics, then choose between the default and the memory-preserving target.",
        "doc": "https://learn.microsoft.com/azure/virtual-machines/sizes/overview",
    },
    "cpu_architecture_change": {
        "title": "CPU architecture port (x64 to Arm64)",
        "sev": "Blocking",
        "what": "The target uses a different CPU architecture. Every binary, agent, driver and third-party package must be rebuilt or re-sourced for the target architecture.",
        "why": "x64 and Arm64 binaries are not interchangeable. This is a porting project, not a resize, and needs its own timeline.",
        "how": "Inventory all installed software and confirm Arm64 availability before committing.",
        "doc": "https://learn.microsoft.com/azure/virtual-machines/sizes/overview",
    },
    "no_exact_vcpu_match": {
        "title": "vCPU count change",
        "sev": "Plan",
        "what": "No target exists at the current vCPU count, so the nearest size is larger. Right-size against actual utilisation rather than the current nameplate, and re-check per-core software licensing.",
        "why": "A vCPU increase can trigger a licensing true-up (SQL Server, Oracle, SAP, third-party agents) that outweighs the compute saving.",
        "how": "Baseline CPU utilisation for 30-90 days, then select the size that fits real demand.",
        "doc": "https://learn.microsoft.com/azure/virtual-machines/sizes/resize-vm",
    },
    "narrow_regional_availability": {
        "title": "Limited target region availability",
        "sev": "Plan",
        "what": "The recommended target is offered in a small number of regions. Confirm availability in the region this workload runs in before planning the move.",
        "why": "Recommending a size that cannot be deployed in the workload's region wastes a migration window.",
        "how": "az vm list-skus --location <region> --resource-type virtualMachines --size <target> --all",
        "doc": "https://learn.microsoft.com/azure/virtual-machines/sizes/resize-vm",
    },
    "no_three_az_regions": {
        "title": "Availability zone parity",
        "sev": "Plan",
        "what": "The target is not offered across three availability zones in the regions where it is available. If this workload is zone-redundant, validate the zone footprint before moving.",
        "why": "Losing a zone silently reduces the availability SLA of the design.",
        "how": "Check zone availability for the target size in the workload's region.",
        "doc": "https://learn.microsoft.com/azure/reliability/availability-zones-overview",
    },
    "target_not_in_region": {
        "title": "Target size not available in this region",
        "sev": "Blocking",
        "what": "The recommended target size is not currently offered in this VM's region. Select an alternate target from the Target mapping tab, or plan a regional move.",
        "why": "The default recommendation cannot be deployed here. Proceeding would fail at resize time.",
        "how": "Review the alternate targets listed for this size, then confirm with az vm list-skus.",
        "doc": "https://learn.microsoft.com/azure/virtual-machines/sizes/resize-vm",
    },
    "quota_headroom": {
        "title": "Target family quota headroom",
        "sev": "Plan",
        "what": "Verify per-family vCPU quota for the target family in the target region and raise a quota request early if short.",
        "why": "Target families have separate quota buckets from the source. A fleet-wide resize can fail midway on quota, and approval is not instant.",
        "how": "az vm list-usage --location <region> -o table",
        "doc": "https://learn.microsoft.com/azure/quotas/view-quotas",
    },
    "availability_set": {
        "title": "Availability set resize constraint",
        "sev": "Plan",
        "what": "This VM is in an availability set. The target size must be available on the same cluster, or every VM in the set must be stopped and resized together.",
        "why": "Availability set members are pinned to a cluster that may not offer the target size.",
        "how": "Stop all VMs in the availability set, resize, then restart.",
        "doc": "https://learn.microsoft.com/azure/virtual-machines/sizes/resize-vm",
    },
    "scale_set": {
        "title": "Scale set SKU change",
        "sev": "Plan",
        "what": "This is a virtual machine scale set. Change the SKU on the scale set model and roll instances through the upgrade policy rather than resizing instances individually.",
        "why": "Scale set instances are governed by the model; editing instances directly drifts the set.",
        "how": "Update the scale set SKU, then apply the rolling upgrade policy.",
        "doc": "https://learn.microsoft.com/azure/virtual-machine-scale-sets/virtual-machine-scale-sets-upgrade-scale-set",
    },
}


def load():
    with open(os.path.join(REFS, "sku-migration-map.json"), encoding="utf-8") as f:
        return json.load(f)


def esc(s):
    """Escape a python string for embedding in a single-quoted KQL literal."""
    if s is None:
        return ""
    return str(s).replace("\\", "\\\\").replace("'", "\\'")


def num(v):
    """Trim trailing .0 so '8.0' serialises as '8'."""
    if v is None or v == "":
        return ""
    try:
        f = float(v)
        return str(int(f)) if f == int(f) else str(f)
    except (TypeError, ValueError):
        return str(v)


def build_lookups(data):
    skus = data["skus"]
    sku_map = {}
    spec_map = {}
    url_map = {}
    region_ids = {}
    tgt_regions = {}

    def rid(region):
        if region not in region_ids:
            region_ids[region] = format(len(region_ids), "x")
        return region_ids[region]

    for r in skus:
        if ONLY_SIZES is not None and r["sku"] not in ONLY_SIZES:
            continue
        group = r.get("group") or ""
        prio = r.get("priority") or ""
        retire = r.get("retirement_date") or ""
        gen = (r.get("generation") or "").lower()

        if group in V3_EOL_GROUPS and not retire:
            retire = V3_EOL_DATE
            tier = "EOL"
        elif prio == "P1_must_move":
            tier = "RETIRE"
        elif retire:
            tier = "RETIRE"
        else:
            tier = "OPTIONAL"

        # Pricing update flag: scoped to the series named in the Service Health
        # notification, not to every v1/v2 generation size. The generation
        # field alone over-matches (it pulls in M-series isolated and HBrs).
        price = "1" if (r.get("series") or "") in PRICE_SERIES else "0"

        cur = r.get("current") or {}
        mp = r.get("memory_preserving_target") or {}
        targets = r.get("targets") or []
        preferred = next((t for t in targets
                          if t.get("sku") == r.get("recommended_target")), None)
        if preferred is None and targets:
            preferred = targets[0]
        preferred = preferred or {}

        codes = sorted({WARN_SHORT.get(w["code"], "")
                        for w in (r.get("warnings") or [])} - {""})
        alts = [t["sku"] for t in targets
                if t.get("sku") != r.get("recommended_target")][:3]

        impacted = tier in ("RETIRE", "EOL")

        # OPTIONAL sizes carry a slim record: they have no retirement date, so the
        # extra fields would only inflate every embedded query.
        fields = [
            tier,                                             # 0
            group,                                            # 1
            retire,                                           # 2
            r.get("recommended_target") or "",                # 3
            mp.get("sku") or "" if impacted else "",          # 4
            num(preferred.get("vcpu")),                       # 5
            num(preferred.get("memory_gb")),                  # 6
            num(cur.get("vcpu")),                             # 7
            num(cur.get("memory_gb")),                        # 8
            ",".join(codes),                                  # 9
            ",".join(alts) if impacted else "",               # 10
            r.get("ri_1yr_expiration") or "" if impacted else "",   # 11
            r.get("series") or "",                            # 12
            price,                                            # 13
            num(mp.get("vcpu")) if impacted else "",          # 14
            num(mp.get("memory_gb")) if impacted else "",     # 15
        ]
        sku_map[r["sku"]] = "|".join(fields).rstrip("|")

        series = r.get("series") or ""
        if series and r.get("spec_deltas") and series not in spec_map:
            spec_map[series] = r["spec_deltas"]
        if group and r.get("announcement_url") and group not in url_map:
            url_map[group] = r["announcement_url"]

        # Region availability, for impacted sizes only, encoded as region indices
        # to keep the embedded literal small.
        if impacted:
            for t in [preferred, mp]:
                sku = t.get("sku")
                if sku and t.get("deployable_regions") and sku not in tgt_regions:
                    tgt_regions[sku] = "|" + "|".join(
                        rid(x) for x in t["deployable_regions"]) + "|"

    return sku_map, spec_map, url_map, tgt_regions, region_ids


def kql_bag(d):
    """Compact single-line JSON suitable for a KQL parse_json('...') literal."""
    return esc(json.dumps(d, separators=(",", ":"), sort_keys=True))


def base_query(sku_map, tgt_regions, region_ids, include_optional=True):
    """
    Core Azure Resource Graph query: fleet inventory joined to the lifecycle
    lookup, with a readiness score computed from real fleet attributes.
    """
    m = kql_bag(sku_map)
    reg = kql_bag(tgt_regions)
    rids = kql_bag(region_ids)

    opt_filter = "" if include_optional else "\n| where Lifecycle != 'OPTIONAL'"

    return f"""resources
| where type =~ 'microsoft.compute/virtualmachines' or type =~ 'microsoft.compute/virtualmachinescalesets'
| extend IsVmss = (type =~ 'microsoft.compute/virtualmachinescalesets')
// A Flexible-orchestration scale set surfaces BOTH the scale set (carrying
// sku.capacity) and every member VM as separate Resource Graph rows. Counting
// both double-reports the fleet, so member VMs are dropped here and the scale
// set alone represents them. Uniform scale sets do not expose member VMs at
// all, so they are unaffected. Flexible is the current Azure default, so this
// matters for most modern fleets.
| where IsVmss or isempty(tostring(properties.virtualMachineScaleSet.id))
| extend VmSize = iff(IsVmss, tostring(sku.name), tostring(properties.hardwareProfile.vmSize))
| extend Instances = iff(IsVmss, toint(sku.capacity), 1)
| extend OsType = iff(IsVmss, tostring(properties.virtualMachineProfile.storageProfile.osDisk.osType), tostring(properties.storageProfile.osDisk.osType))
| extend OsDiskId = tolower(tostring(properties.storageProfile.osDisk.managedDisk.id))
| extend AvSetId = tostring(properties.availabilitySet.id)
| extend PowerState = tostring(split(tostring(properties.extended.instanceView.powerState.code), '/')[1])
| extend Zone = tostring(zones[0])
| extend ImageSku = iff(IsVmss, tostring(properties.virtualMachineProfile.storageProfile.imageReference.sku), tostring(properties.storageProfile.imageReference.sku))
| project ResourceId = id, Name = name, ResourceGroup = resourceGroup, SubscriptionId = subscriptionId,
          Region = location, VmSize, Instances, OsType, OsDiskId, AvSetId, PowerState, Zone, ImageSku, IsVmss, Tags = tags
| join kind=leftouter (
    resources
    | where type =~ 'microsoft.compute/disks'
    | project OsDiskId = tolower(id), DiskGen = tostring(properties.hyperVGeneration)
  ) on OsDiskId
| extend VmGeneration = case(
        isnotempty(DiskGen), DiskGen,
        ImageSku has 'gen2' or ImageSku has '-g2', 'V2',
        'Unknown')
| extend Rec = tostring(parse_json('{m}')[VmSize])
| where isnotempty(Rec)
| extend P = split(Rec, '|')
| extend Lifecycle = tostring(P[0]),
         SeriesGroup = tostring(P[1]),
         RetirementDate = tostring(P[2]),
         RecommendedTarget = tostring(P[3]),
         MemoryPreservingTarget = tostring(P[4]),
         TargetVcpu = tostring(P[5]),
         TargetMemoryGB = tostring(P[6]),
         CurrentVcpu = toint(P[7]),
         CurrentMemoryGB = toreal(P[8]),
         WarnCodes = tostring(P[9]),
         AlternateTargets = tostring(P[10]),
         RiEnded = tostring(P[11]),
         Series = tostring(P[12]),
         PriceUpdate = tostring(P[13]),
         MpVcpu = tostring(P[14]),
         MpMemoryGB = tostring(P[15]){opt_filter}
| extend RetiresOn = iff(isempty(RetirementDate), datetime(null), todatetime(RetirementDate))
| extend DaysToRetirement = iff(isempty(RetirementDate), toint(-1), datetime_diff('day', todatetime(RetirementDate), now()))
| extend RegionId = tostring(parse_json('{rids}')[tolower(Region)])
| extend TargetRegions = tostring(parse_json('{reg}')[RecommendedTarget])
| extend TargetInRegion = case(
        isempty(RecommendedTarget) or isempty(TargetRegions), 'Unverified',
        isempty(RegionId), 'Unverified',
        TargetRegions has strcat('|', RegionId, '|'), 'Yes',
        'No')
| extend PriceUpdate2027 = iff(PriceUpdate == '1', 'Yes', 'No')
// Reserved Instance purchases and renewals for these series ended 2026-07-01.
// Once an existing RI expires the workload bills at pay-as-you-go, even when
// the reservation is set to auto-renew. Public source: Azure update 560948.
| extend RiEndsOn = iff(isempty(RiEnded), datetime(null), todatetime(RiEnded))
| extend RiStatus = case(
        isempty(RiEnded), 'Not applicable',
        todatetime(RiEnded) < now(), 'Expired - billing at pay-as-you-go',
        'Expires - no renewal available')
// ---- readiness penalties, evaluated against the REAL fleet attributes ----
| extend NeedsGen2 = iff(WarnCodes has 'g' and VmGeneration == 'V1', 1, 0)
| extend GenUnknown = iff(WarnCodes has 'g' and VmGeneration == 'Unknown', 1, 0)
| extend PenGen = iff(NeedsGen2 == 1, {PENALTY['gen1_not_supported']}, iff(GenUnknown == 1, 20, 0))
| extend PenArch = iff(WarnCodes has 'a', {PENALTY['cpu_architecture_change']}, 0)
| extend PenMem = iff(WarnCodes has 'm', {PENALTY['memory_downgrade']}, 0)
| extend PenTemp = iff(WarnCodes has 't', {PENALTY['temp_disk_removed']}, 0)
| extend PenNvme = iff(WarnCodes has 'n', {PENALTY['nvme_required']}, 0)
| extend PenNarrow = iff(WarnCodes has 'r', {PENALTY['narrow_regional_availability']}, 0)
| extend PenAz = iff(WarnCodes has 'z' and isnotempty(Zone), {PENALTY['no_three_az_regions']}, 0)
| extend PenVcpu = iff(WarnCodes has 'v', {PENALTY['no_exact_vcpu_match']}, 0)
| extend PenRegion = iff(TargetInRegion == 'No', 30, 0)
| extend PenAvSet = iff(isnotempty(AvSetId), 5, 0)
| extend PenVmss = iff(IsVmss, 5, 0)
| extend ReadinessScore = toint(iff(100 - PenGen - PenArch - PenMem - PenTemp - PenNvme - PenNarrow - PenAz - PenVcpu - PenRegion - PenAvSet - PenVmss < 0, 0, 100 - PenGen - PenArch - PenMem - PenTemp - PenNvme - PenNarrow - PenAz - PenVcpu - PenRegion - PenAvSet - PenVmss))
| extend Complexity = case(
        ReadinessScore >= 85, 'Low - in-place resize',
        ReadinessScore >= 60, 'Medium - validate then resize',
        ReadinessScore >= 35, 'High - prerequisites required',
        'Very high - re-architecture')
| extend Urgency = case(
        DaysToRetirement < 0, 'No retirement date',
        DaysToRetirement <= 180, 'Critical - under 6 months',
        DaysToRetirement <= 365, 'High - under 12 months',
        DaysToRetirement <= 730, 'Medium - under 24 months',
        'Planned - over 24 months')
| extend ActionCategory = case(
        Lifecycle == 'RETIRE', 'Retirement announced - action required',
        Lifecycle == 'EOL', 'End of life - plan migration',
        'Optional modernization')
| extend Blockers = trim(', ', strcat(
        iff(NeedsGen2 == 1, 'Gen1 to Gen2 conversion, ', ''),
        iff(GenUnknown == 1, 'Confirm VM generation, ', ''),
        iff(PenArch > 0, 'Arm64 port, ', ''),
        iff(PenMem > 0, 'Memory downgrade, ', ''),
        iff(PenTemp > 0, 'Temp disk dependency, ', ''),
        iff(PenNvme > 0, 'NVMe and MANA validation, ', ''),
        iff(PenRegion > 0, 'Target not in region, ', ''),
        iff(PenVcpu > 0, 'vCPU count change, ', '')))
| extend Blockers = iff(isempty(Blockers), 'None identified', Blockers)
// Size-level considerations, independent of any individual VM's configuration.
// Used by the size-by-size mapping so rows do not split on per-VM attributes.
| extend SizeConsiderations = trim(', ', strcat(
        iff(WarnCodes has 'g', 'Target is Gen2-only, ', ''),
        iff(WarnCodes has 'a', 'Arm64 architecture change, ', ''),
        iff(WarnCodes has 'm', 'Memory reduction at same vCPU, ', ''),
        iff(WarnCodes has 't', 'Local temp disk removed, ', ''),
        iff(WarnCodes has 'n', 'Target requires NVMe and MANA, ', ''),
        iff(WarnCodes has 'r', 'Narrow target region footprint, ', ''),
        iff(WarnCodes has 'z', 'Limited zone coverage, ', ''),
        iff(WarnCodes has 'v', 'No target at current vCPU count, ', '')))
| extend SizeConsiderations = iff(isempty(SizeConsiderations), 'Straight resize', SizeConsiderations)"""


# --------------------------------------------------------------------------
# Workbook item helpers
# --------------------------------------------------------------------------

def text_item(md, name, width=None):
    item = {"type": 1, "content": {"json": md}, "name": name,
            "styleSettings": {"margin": "0", "padding": "0"}}
    if width:
        item["customWidth"] = width
    return item


def arg_query(query, name, visualization="table", size=0, title=None,
              grid=None, chart=None, tile=None, extra=None, width=None,
              max_width=None, export=False):
    content = {
        "version": "KqlItem/1.0",
        "query": query,
        "size": size,
        "queryType": 1,
        "resourceType": "microsoft.resourcegraph/resources",
        "crossComponentResources": ["{Subscriptions}"],
        "visualization": visualization,
    }
    if title:
        content["title"] = title
    if grid:
        content["gridSettings"] = grid
    if chart:
        content["chartSettings"] = chart
    if tile:
        content["tileSettings"] = tile
    # Grids can offer a "Download to Excel" toolbar button. Enabled on the
    # detailed tables so the inventory can be taken into your own
    # planning spreadsheet; "all" exports every column, not just visible ones,
    # which matters because several prose columns are truncated on screen.
    if export:
        content["showExportToExcel"] = True
        content["exportToExcelOptions"] = "all"
    if extra:
        content.update(extra)
    item = {"type": 3, "content": content, "name": name}
    if width:
        item["customWidth"] = width
    # A grid always fills its container and no column renders below ~130px, so
    # a table with few columns in a wide container dumps all the slack into its
    # widest column. Capping the container is the only way to stop that.
    #
    # The cap must stay ABOVE the sum of the natural column widths. Set it any
    # lower and the grid does not shrink to fit - it overflows and grows a
    # horizontal scrollbar that renders detached below the row.
    if max_width:
        item["styleSettings"] = {"maxWidth": max_width}
    return item


def fmt(col, formatter=None, width=None, **kw):
    d = {"columnMatch": col}
    if formatter is not None:
        d["formatter"] = formatter
    d.update(kw)
    if width:
        d.setdefault("formatOptions", {})["customColumnWidthSetting"] = width
    return d


def widths(spec):
    """Turn {column: width} into plain width-only formatter rules."""
    return [fmt(col, 0, width=w) for col, w in spec.items()]


# Date columns render as "4/30/2028, 7:00:00 AM" unless told otherwise.
DATE_FMT = {"showUtcTime": True, "formatName": "shortDatePattern"}


def date_col(col):
    return fmt(col, 6, dateFormat=DATE_FMT)


def grid_settings(width_spec, formatters=(), rows=10, **kw):
    """Column widths first, then the typed formatters that override them.

    Workbooks applies the last matching rule per property, so a width-only
    rule followed by a typed rule keeps both the width and the formatting
    only when the typed rule carries the width itself. Any column present in
    both is therefore emitted once, with the width folded into the typed rule.

    `rows` sets gridSettings.rowLimit; without it a grid renders about three
    rows and scrolls, leaving dead space under short tables.
    """
    typed = {f["columnMatch"]: f for f in formatters}
    rules = []
    for col, w in width_spec.items():
        if col in typed:
            f = dict(typed.pop(col))
            f.setdefault("formatOptions", {})
            f["formatOptions"] = dict(f["formatOptions"])
            f["formatOptions"]["customColumnWidthSetting"] = w
            rules.append(f)
        else:
            rules.append(fmt(col, 0, width=w))
    rules.extend(typed.values())
    out = {"formatters": rules, "rowLimit": rows}
    out.update(kw)
    return out


def group_item(name, tab_value, items):
    return {
        "type": 12,
        "content": {"version": "NotebookGroup/1.0", "groupType": "editable",
                    "items": items},
        "conditionalVisibility": {"parameterName": "SelectedTab", "comparison": "isEqualTo",
                                  "value": tab_value},
        "name": name,
    }


# --------------------------------------------------------------------------
# Build
# --------------------------------------------------------------------------

def build(data):
    sku_map, spec_map, url_map, tgt_regions, region_ids = build_lookups(data)
    base = base_query(sku_map, tgt_regions, region_ids)
    base_impacted = base + "\n| where Lifecycle != 'OPTIONAL'"

    prereq_bag = kql_bag({
        WARN_SHORT.get(k, k):
            "|".join([v["title"], v["sev"], v["what"], v["why"], v["how"], v["doc"]])
        for k, v in PREREQ.items()
    })
    spec_bag = kql_bag(spec_map)
    url_bag = kql_bag(url_map)

    items = []

    # ---------------- header ----------------
    items.append(text_item(
        "# Azure VM Lifecycle & Modernization Readiness\n\n"
        "This workbook inventories the virtual machines and scale sets in the "
        "subscriptions you select, identifies which sizes are affected by announced "
        "Azure VM retirements and lifecycle changes, recommends a target size for each, "
        "and scores how ready each workload is to move.\n\n"
        "Readiness is calculated from your **actual** fleet attributes - the real Hyper-V "
        "generation of each OS disk, the region and zone each VM runs in, and whether it "
        "sits in an availability set or scale set - not from size metadata alone.\n\n"
        "> All data stays in your tenant. The workbook runs read-only Azure Resource Graph "
        "queries and stores nothing.",
        "header"))

    items.append({
        "type": 9,
        "content": {
            "version": "KqlParameterItem/1.0",
            "parameters": [
                {
                    "id": "b0a1f2c3-1111-4a11-9c01-000000000001",
                    "version": "KqlParameterItem/1.0",
                    "name": "Subscriptions",
                    "label": "Subscriptions",
                    "type": 6,
                    "isRequired": True,
                    "multiSelect": True,
                    "quote": "'",
                    "delimiter": ",",
                    "typeSettings": {"additionalResourceOptions": ["value::all"],
                                     "includeAll": True, "showDefault": False},
                    "value": ["value::all"],
                },
                {
                    "id": "b0a1f2c3-2222-4a22-9c02-000000000002",
                    "version": "KqlParameterItem/1.0",
                    "name": "LifecycleFilter",
                    "label": "Lifecycle status",
                    "type": 2,
                    "isRequired": True,
                    "multiSelect": True,
                    "quote": "",
                    "delimiter": ",",
                    "jsonData": json.dumps([
                        {"value": "RETIRE", "label": "Retirement announced", "selected": True},
                        {"value": "EOL", "label": "End of life", "selected": True},
                        {"value": "OPTIONAL", "label": "Optional modernization"},
                    ]),
                    "value": ["RETIRE", "EOL"],
                    "timeContext": {"durationMs": 86400000},
                    "typeSettings": {"additionalResourceOptions": [], "showDefault": False},
                },
            ],
            "style": "pills",
        },
        "name": "parameters",
    })

    tabs = [("Overview", "overview"), ("Impacted resources", "impacted"),
            ("Target mapping", "mapping"), ("Prerequisites", "prereq"),
            ("Migration plan", "plan"), ("About", "about")]
    items.append({
        "type": 11,
        "content": {
            "version": "LinkItem/1.0",
            "style": "tabs",
            "links": [
                {"id": f"c0a1f2c3-3333-4a33-9c03-00000000000{i}",
                 "cellValue": "SelectedTab", "linkTarget": "parameter",
                 "linkLabel": label, "subTarget": key, "preText": "", "style": "link"}
                for i, (label, key) in enumerate(tabs)
            ],
        },
        "name": "tabs",
    })

    # ---------------- TAB 1: Overview ----------------
    ov = []
    ov.append(arg_query(
        base + """
| summarize Impacted = countif(Lifecycle != 'OPTIONAL'),
            Instances = sumif(Instances, Lifecycle != 'OPTIONAL'),
            Retiring = countif(Lifecycle == 'RETIRE'),
            EndOfLife = countif(Lifecycle == 'EOL'),
            Blocked = countif(Lifecycle != 'OPTIONAL' and ReadinessScore < 60),
            ScoreSum = sumif(ReadinessScore, Lifecycle != 'OPTIONAL'),
            SoonestDays = min(iff(DaysToRetirement >= 0, DaysToRetirement, 99999))
| extend Score = iff(Impacted == 0, 100, toint(round(todouble(ScoreSum) / todouble(Impacted), 0)))
| extend SoonestDays = iff(SoonestDays == 99999, -1, SoonestDays)
| extend M = pack_array('Impacted resources', 'Instances affected', 'Retirement announced',
                        'End of life', 'Need prerequisite work', 'Fleet readiness score',
                        'Days to next retirement')
| mv-expand Metric = M to typeof(string)
| extend Value = case(
        Metric == 'Impacted resources', Impacted,
        Metric == 'Instances affected', Instances,
        Metric == 'Retirement announced', Retiring,
        Metric == 'End of life', EndOfLife,
        Metric == 'Need prerequisite work', Blocked,
        Metric == 'Fleet readiness score', Score,
        SoonestDays)
| extend Detail = case(
        Metric == 'Fleet readiness score', case(Score >= 85, 'Mostly in-place resizes',
                                                Score >= 60, 'Validation needed',
                                                Score >= 35, 'Prerequisite work required',
                                                'Significant re-architecture'),
        Metric == 'Days to next retirement', iff(Value < 0, 'No dated retirements', 'days remaining'),
        Metric == 'Need prerequisite work', 'readiness below 60',
        'resources')
| extend Ord = case(
        Metric == 'Impacted resources', 1,
        Metric == 'Instances affected', 2,
        Metric == 'Retirement announced', 3,
        Metric == 'End of life', 4,
        Metric == 'Need prerequisite work', 5,
        Metric == 'Fleet readiness score', 6,
        7)
| order by Ord asc
| project Metric, Value, Detail""",
        "ov-tiles", visualization="tiles", size=4, title="Fleet summary",
        tile={"titleContent": {"columnMatch": "Metric", "formatter": 1},
              "leftContent": {"columnMatch": "Value", "formatter": 12,
                              "formatOptions": {"palette": "auto"},
                              "numberFormat": {"unit": 17, "options": {"maximumFractionDigits": 0}}},
              "subtitleContent": {"columnMatch": "Detail", "formatter": 1},
              "showBorder": True, "size": "fixed"}))

    ov.append(arg_query(
        base_impacted + """
| summarize Instances = sum(Instances) by Urgency
| order by Instances desc""",
        "ov-urgency", visualization="piechart", size=1, width="50",
        title="Impacted instances by urgency"))

    ov.append(arg_query(
        base_impacted + """
| summarize Resources = count(), Instances = sum(Instances) by VmGeneration
| order by Instances desc""",
        "ov-gen", visualization="piechart", size=1, width="50",
        title="Impacted resources by actual VM generation"))

    ov.append(arg_query(
        base_impacted + """
| summarize Resources = count(), Instances = sum(Instances), AvgReadiness = round(avg(todouble(ReadinessScore)), 0) by Complexity
| order by AvgReadiness asc""",
        "ov-complexity", visualization="table", size=1, width="50",
        max_width="700px",
        title="Migration complexity distribution",
        grid=grid_settings(
            {"Complexity": "37%", "Resources": "21%", "Instances": "21%",
             "AvgReadiness": "21%"},
            [
             fmt("AvgReadiness", 8, formatOptions={"palette": "redGreen", "min": 0, "max": 100})],
            labelSettings=[{"columnId": "AvgReadiness", "label": "Avg readiness"}])))

    ov.append(arg_query(
        base_impacted + """
| summarize Instances = sum(Instances) by Series, PriceUpdate2027, RiStatus
| where PriceUpdate2027 == 'Yes' or RiStatus != 'Not applicable'
| order by Instances desc""",
        "ov-commercial", size=1, width="50", max_width="760px",
        title="Commercial impact - pricing and reservations",
        grid=grid_settings(
            {"Series": "17%", "PriceUpdate2027": "24%", "RiStatus": "42%",
             "Instances": "17%"},
            [fmt("PriceUpdate2027", 18, formatOptions={"thresholdsOptions": "icons",
                 "thresholdsGrid": [
                     {"operator": "==", "thresholdValue": "Yes", "representation": "2",
                      "text": "Applies 1 Feb 2027"},
                     {"operator": "Default", "representation": "success",
                      "text": "Not affected"}]}),
             fmt("RiStatus", 18, formatOptions={"thresholdsOptions": "colors",
                 "thresholdsGrid": [
                     {"operator": "contains", "thresholdValue": "pay-as-you-go",
                      "representation": "redBright", "text": "{0}{1}"},
                     {"operator": "contains", "thresholdValue": "no renewal",
                      "representation": "orange", "text": "{0}{1}"},
                     {"operator": "Default", "representation": "blue", "text": "{0}{1}"}]})],
            labelSettings=[
                {"columnId": "PriceUpdate2027", "label": "Pricing update"},
                {"columnId": "RiStatus", "label": "Reserved Instance status"},
            ])))

    ov.append(arg_query(
        base_impacted + """
| summarize Resources = count(), Instances = sum(Instances), AvgReadiness = round(avg(todouble(ReadinessScore)), 0),
            NextRetirement = min(RetiresOn)
    by VmSize, Series, ActionCategory, RecommendedTarget
| order by Instances desc
| take 25""",
        "ov-topsizes", size=1, export=True, max_width="1320px", title="Top affected sizes",
        grid=grid_settings(
            {"VmSize": "11%", "Series": "11%", "ActionCategory": "20%",
             "RecommendedTarget": "12%", "Resources": "11%", "Instances": "11%",
             "AvgReadiness": "11%", "NextRetirement": "13%"},
            [fmt("AvgReadiness", 8, formatOptions={"palette": "redGreen", "min": 0, "max": 100}),
             date_col("NextRetirement")],
            labelSettings=[{"columnId": "VmSize", "label": "Current size"},
                           {"columnId": "ActionCategory", "label": "Lifecycle status"},
                           {"columnId": "RecommendedTarget", "label": "Recommended target"},
                           {"columnId": "AvgReadiness", "label": "Avg readiness"},
                           {"columnId": "NextRetirement", "label": "Next retirement"}])))

    items.append(group_item("grp-overview", "overview", ov))

    # ---------------- TAB 2: Impacted resources ----------------
    imp = []
    imp.append(text_item(
        "### Impacted resources\n"
        "Every VM and scale set whose current size carries an announced retirement or "
        "end-of-life status. **Readiness** is scored 0-100 against your real configuration; "
        "**Blockers** lists the prerequisite work that must happen before the move.",
        "imp-intro"))
    imp.append(arg_query(
        base + """
| where '{LifecycleFilter}' has Lifecycle
| project Name, ResourceGroup, Region, VmSize, Instances, VmGeneration,
          RecommendedTarget, TargetInRegion, RetiresOn, ReadinessScore,
          Blockers, ResourceId
| order by ReadinessScore asc, RetiresOn asc""",
        "imp-grid", size=1, export=True, title="Impacted virtual machines and scale sets",
        grid=grid_settings(
            # Sized in ch to the actual content. Percentage widths bottom out at
            # roughly 130px per column, which leaves short values like "V1" or
            # "eastus2" swimming in white space; ch units size to the text.
            {"Name": "18ch", "ResourceGroup": "17ch", "Region": "11ch",
             "VmSize": "18ch", "Instances": "12ch", "VmGeneration": "8ch",
             "RecommendedTarget": "21ch", "TargetInRegion": "14ch",
             "RetiresOn": "13ch", "ReadinessScore": "12ch", "Blockers": "32ch"},
            [
                fmt("Name", 1, formatOptions={"linkColumn": "ResourceId",
                                              "linkTarget": "Resource",
                                              "linkIsContextBlade": True}),
                date_col("RetiresOn"),
                fmt("ReadinessScore", 8, formatOptions={"palette": "redGreen",
                                                        "min": 0, "max": 100}),
                fmt("TargetInRegion", 18, formatOptions={"thresholdsOptions": "icons",
                    "thresholdsGrid": [
                        {"operator": "==", "thresholdValue": "No", "representation": "failed",
                         "text": "Not available"},
                        {"operator": "==", "thresholdValue": "Unverified",
                         "representation": "unknown", "text": "Unverified"},
                        {"operator": "Default", "representation": "success",
                         "text": "Available"}]}),
                fmt("ResourceId", 5),
            ],
            filter=True,
            sortBy=[{"itemKey": "ReadinessScore", "sortOrder": 1}],
            labelSettings=[
                {"columnId": "ResourceGroup", "label": "Resource group"},
                {"columnId": "VmSize", "label": "Current size"},
                {"columnId": "VmGeneration", "label": "Gen"},
                {"columnId": "RecommendedTarget", "label": "Recommended target"},
                {"columnId": "TargetInRegion", "label": "In region"},
                {"columnId": "RetiresOn", "label": "Retires on"},
                {"columnId": "ReadinessScore", "label": "Readiness"},
            ])))
    items.append(group_item("grp-impacted", "impacted", imp))

    # ---------------- TAB 3: Target mapping ----------------
    mapping = []
    mapping.append(text_item(
        "### Recommended target mapping\n"
        "One row per affected size in your fleet. **Recommended target** is the default "
        "same-or-better replacement. **Memory-preserving target** is the alternative when "
        "the default reduces RAM. Always right-size against 30-90 days of real utilisation "
        "rather than the current nameplate.",
        "map-intro"))
    map_base = f"""
| where '{{LifecycleFilter}}' has Lifecycle
| extend SpecNotes = tostring(parse_json('{spec_bag}')[Series])
| extend AnnouncementUrl = tostring(parse_json('{url_bag}')[SeriesGroup])
| summarize Resources = count(), Instances = sum(Instances),
            Regions = strcat_array(make_set(Region, 20), ', '),
            AvgReadiness = round(avg(todouble(ReadinessScore)), 0)
    by VmSize, Series, SeriesGroup, ActionCategory, RetirementDate, CurrentVcpu, CurrentMemoryGB,
       RecommendedTarget, TargetVcpu, TargetMemoryGB, MemoryPreservingTarget, MpVcpu, MpMemoryGB,
       AlternateTargets, SizeConsiderations, SpecNotes, RiEnded, RiStatus, PriceUpdate2027, AnnouncementUrl
| extend Current = strcat(CurrentVcpu, ' vCPU / ', CurrentMemoryGB, ' GB')
| extend Target = strcat(TargetVcpu, ' vCPU / ', TargetMemoryGB, ' GB')
| extend MemoryPreserving = iff(isempty(MemoryPreservingTarget), '-',
            strcat(MemoryPreservingTarget, '  (', MpVcpu, ' vCPU / ', MpMemoryGB, ' GB)'))"""

    mapping.append(arg_query(
        base + map_base + """
| project VmSize, ActionCategory, RetirementDate, Instances, Current,
          RecommendedTarget, Target, MemoryPreserving, AvgReadiness, AnnouncementUrl
| order by Instances desc""",
        "map-grid", size=1, export=True, title="Recommended targets",
        grid=grid_settings(
            {"VmSize": "12%", "ActionCategory": "11%", "RetirementDate": "9%",
             "Instances": "6%", "Current": "11%", "RecommendedTarget": "14%",
             "Target": "11%", "MemoryPreserving": "21%", "AvgReadiness": "5%"},
            [fmt("VmSize", 1, formatOptions={"linkColumn": "AnnouncementUrl",
                                             "linkTarget": "Url"}),
             date_col("RetirementDate"),
             fmt("AvgReadiness", 8, formatOptions={"palette": "redGreen", "min": 0, "max": 100}),
             fmt("AnnouncementUrl", 5)],
            filter=True,
            labelSettings=[
                {"columnId": "VmSize", "label": "Current size"},
                {"columnId": "ActionCategory", "label": "Lifecycle status"},
                {"columnId": "RetirementDate", "label": "Retires on"},
                {"columnId": "Current", "label": "Current spec"},
                {"columnId": "RecommendedTarget", "label": "Recommended target"},
                {"columnId": "Target", "label": "Target spec"},
                {"columnId": "MemoryPreserving", "label": "Memory-preserving target"},
                {"columnId": "AvgReadiness", "label": "Avg readiness"},
            ])))
    mapping.append(text_item(
        "#### Alternates, series notes and commercial impact\n"
        "The same sizes again, with the other targets worth evaluating, what changes at the "
        "series level, and the commercial position: whether the 1 February 2027 pricing update "
        "applies to the series, and when Reserved Instance coverage ends.\n\n"
        "> Reserved Instance purchases and renewals for these series ended **1 July 2026**. "
        "When an existing reservation expires the workload bills at pay-as-you-go rates, "
        "**even if it is set to auto-renew** "
        "([Azure update 560948](https://azure.microsoft.com/updates?id=560948)). "
        "For the rates that apply to your agreement, see the official "
        "[Windows](https://azure.microsoft.com/pricing/details/virtual-machines/windows/) and "
        "[Linux](https://azure.microsoft.com/pricing/details/virtual-machines/linux/) VM "
        "pricing pages, and your own Azure Service Health notification.",
        "map-detail-intro"))
    mapping.append(arg_query(
        base + map_base + """
| extend AlternateTargets = replace_string(AlternateTargets, ',', ', ')
| project VmSize, Series, AlternateTargets, SizeConsiderations, SpecNotes,
          PriceUpdate2027, RiEnded, RiStatus
| order by VmSize asc""",
        "map-detail", size=1, export=True, title="Alternates, series detail and commercial impact",
        grid=grid_settings(
            {"VmSize": "10%", "Series": "6%", "AlternateTargets": "16%",
             "SizeConsiderations": "20%", "SpecNotes": "20%",
             "PriceUpdate2027": "10%", "RiEnded": "8%", "RiStatus": "10%"},
            [date_col("RiEnded"),
             fmt("PriceUpdate2027", 18, formatOptions={"thresholdsOptions": "icons",
                 "thresholdsGrid": [
                     {"operator": "==", "thresholdValue": "Yes", "representation": "2",
                      "text": "1 Feb 2027"},
                     {"operator": "Default", "representation": "success",
                      "text": "Not affected"}]})],
            filter=True,
            labelSettings=[
                {"columnId": "VmSize", "label": "Current size"},
                {"columnId": "AlternateTargets", "label": "Alternate targets"},
                {"columnId": "SizeConsiderations", "label": "Considerations"},
                {"columnId": "SpecNotes", "label": "Series notes"},
                {"columnId": "PriceUpdate2027", "label": "Pricing update"},
                {"columnId": "RiEnded", "label": "RI ends"},
                {"columnId": "RiStatus", "label": "RI status"},
            ])))
    items.append(group_item("grp-mapping", "mapping", mapping))

    # ---------------- TAB 4: Prerequisites ----------------
    pre = []
    pre.append(text_item(
        "### Prerequisites and architecture work\n"
        "Some size transitions are a simple resize. Others require work **before** the "
        "resize can succeed - converting a Gen1 VM to Gen2, validating NVMe and MANA driver "
        "support in the OS image, or removing a dependency on the local temp disk. Each row "
        "below is a prerequisite that applies to your fleet, with the number of resources "
        "affected.",
        "pre-intro"))
    pre.append(arg_query(
        base_impacted + f"""
| extend Codes = split(WarnCodes, ',')
| extend Codes = array_concat(Codes,
        iff(TargetInRegion == 'No', pack_array('target_not_in_region'), dynamic([])),
        iff(isnotempty(AvSetId), pack_array('availability_set'), dynamic([])),
        iff(IsVmss, pack_array('scale_set'), dynamic([])),
        pack_array('quota_headroom'))
| mv-expand Code = Codes to typeof(string)
| where isnotempty(Code)
| where not(Code == 'g' and VmGeneration == 'V2')
| where Code != 's'
| extend D = split(tostring(parse_json('{prereq_bag}')[Code]), '|')
| where array_length(D) > 1
| summarize Resources = count(), Instances = sum(Instances),
            Sizes = strcat_array(make_set(VmSize, 12), ', ')
    by Prerequisite = tostring(D[0]), Severity = tostring(D[1]),
       WhatToDo = tostring(D[2]), Why = tostring(D[3]), How = tostring(D[4]), Guidance = tostring(D[5])
| extend SevRank = case(Severity == 'Blocking', 1, Severity == 'Validate', 2, 3)
| order by SevRank asc, Instances desc
| project-away SevRank""",
        "pre-grid", size=1, export=True, title="Prerequisites triggered by your fleet",
        grid=grid_settings(
            {"Prerequisite": "16%", "Severity": "7%", "Resources": "5%",
             "Instances": "5%", "WhatToDo": "21%", "Why": "18%", "How": "18%",
             "Sizes": "10%"},
            [fmt("Prerequisite", 1, formatOptions={"linkColumn": "Guidance",
                                                   "linkTarget": "Url"}),
             fmt("Severity", 18, formatOptions={"thresholdsOptions": "colors",
                 "thresholdsGrid": [
                     {"operator": "==", "thresholdValue": "Blocking",
                      "representation": "redBright", "text": "{0}{1}"},
                     {"operator": "==", "thresholdValue": "Validate",
                      "representation": "orange", "text": "{0}{1}"},
                     {"operator": "Default", "representation": "blue", "text": "{0}{1}"}]}),
             fmt("Guidance", 5)],
            filter=True,
            labelSettings=[
                {"columnId": "WhatToDo", "label": "What to do"},
                {"columnId": "Sizes", "label": "Sizes affected"},
            ])))
    items.append(group_item("grp-prereq", "prereq", pre))

    # ---------------- TAB 5: Migration plan ----------------
    plan = []
    plan.append(text_item(
        "### Suggested migration sequence\n"
        "Waves are ordered to bank low-risk wins first and to leave enough runway for the "
        "workloads that need prerequisite work. Front-load anything already Generation 2 and "
        "NVMe-capable; start the Gen1 conversion track in parallel because it has the longest "
        "lead time.\n\n"
        "| Wave | Scope | Why it goes here |\n| --- | --- | --- |\n"
        "| 1 | Low complexity, confirmed non-production | Proves the runbook with no business risk |\n"
        "| 2 | Low complexity, production or unverified | In-place resize, rollback via restore point |\n"
        "| 3 | Medium complexity | Validate NVMe/MANA and temp-disk dependencies first |\n"
        "| 4 | High complexity | Gen1 to Gen2 conversion, memory or region changes |\n"
        "| 5 | Very high complexity | Re-architecture - treat as a project, not a resize |\n\n"
        "**How environment is determined.** Most fleets do not tag every VM, so the "
        "workbook resolves environment through a cascade and reports which signal it "
        "used:\n\n"
        "| Order | Signal | Notes |\n| ---: | --- | --- |\n"
        "| 1 | **Resource tag** | `Environment`, `Env`, `Tier`, `Stage` or `Usage`, any casing |\n"
        "| 2 | **Resource group tag** | Same keys on the parent resource group |\n"
        "| 3 | **Subscription tag** | Same keys on the subscription |\n"
        "| 4 | **Dev/Test subscription offer** | A Visual Studio or Enterprise Dev/Test subscription cannot host production under the Azure offer terms |\n"
        "| 5 | **Naming convention** | `dev`, `test`, `qa`, `uat`, `sandbox`, `staging` in the subscription or resource group name. Only ever read as non-production, never as production, because a name is weaker evidence than a tag |\n\n"
        "Values are normalised, so `non-prod`, `non_prod` and `nonprod` read alike. "
        "**Anything the cascade cannot resolve is sequenced as production**, not "
        "non-production, because the cost of guessing wrong is a production outage "
        "during what was sold as a no-risk pilot.\n\n"
        "The **Environment** and **Signal** columns below show the verdict and its "
        "source. If most rows read *Unverified*, the fastest fix is one "
        "`Environment` tag on each resource group - that alone resolves the whole "
        "estate through step 2.",
        "plan-intro", width="50"))
    plan.append(arg_query(
        base_impacted + """
| where DaysToRetirement >= 0
| extend Quarter = strcat(format_datetime(RetiresOn, 'yyyy'), ' Q', toint((getmonth(RetiresOn) - 1) / 3) + 1)
| summarize Instances = sum(Instances) by Quarter
| order by Quarter asc""",
        "plan-burndown", visualization="barchart", size=1, width="50",
        title="Instances to migrate by retirement quarter"))
    plan.append(arg_query(
        base_impacted + f"""
// Environment is resolved through a cascade, because most fleets do not tag
// every VM. The most specific signal wins, and the source is reported so the
// the signal it used is visible.
//
// A note on why this is not a tag-bag scan: the earlier `tostring(Tags) has
// 'prod'` test was wrong in both directions. KQL `has` matches whole terms, so
// "Production" did not match the term "prod" and a production VM was reported
// as non-production, while "non-prod" tokenised to ["non","prod"] and matched,
// and an unrelated tag such as Product=prod-catalog matched too.
| extend EnvSelf = {ENV_TAGS.format(col='Tags')}
| extend RgKey = tolower(strcat(SubscriptionId, '/', ResourceGroup))
| join kind=leftouter (
    resourcecontainers
    | where type =~ 'microsoft.resources/subscriptions/resourcegroups'
    | project RgKey = tolower(strcat(subscriptionId, '/', name)),
              EnvRg = {ENV_TAGS.format(col='tags')}
  ) on RgKey
| join kind=leftouter (
    resourcecontainers
    | where type =~ 'microsoft.resources/subscriptions'
    | project SubscriptionId = subscriptionId,
              EnvSub = {ENV_TAGS.format(col='tags')},
              QuotaId = tolower(tostring(properties.subscriptionPolicies.quotaId)),
              SubName = tolower(name)
  ) on SubscriptionId
// A Visual Studio or Enterprise Dev/Test subscription cannot host production
// under the Azure offer terms, so the offer itself is a reliable signal.
| extend IsDevTestOffer = (QuotaId has 'devtest' or QuotaId startswith 'msdn')
// Last resort: many estates encode environment in the subscription or resource
// group name. Only read as non-production, never as production, because a name
// is weaker evidence than a tag.
| extend NameHint = strcat(' ', replace_regex(strcat(SubName, ' ', tolower(ResourceGroup)), '[^a-z0-9]', ' '), ' ')
| extend NameSaysNonProd = (NameHint has ' dev ' or NameHint has ' test ' or NameHint has ' qa '
        or NameHint has ' uat ' or NameHint has ' sandbox ' or NameHint has ' nonprod '
        or NameHint has ' staging ' or NameHint has ' demo ')
| extend EnvSource = case(
        isnotempty(EnvSelf), 'Resource tag',
        isnotempty(EnvRg), 'Resource group tag',
        isnotempty(EnvSub), 'Subscription tag',
        IsDevTestOffer, 'Dev/Test subscription offer',
        NameSaysNonProd, 'Naming convention',
        'No signal')
| extend EnvRaw = case(
        isnotempty(EnvSelf), EnvSelf,
        isnotempty(EnvRg), EnvRg,
        isnotempty(EnvSub), EnvSub,
        IsDevTestOffer or NameSaysNonProd, 'devtest',
        '')
// Normalise so non-prod, non_prod and nonprod collapse to one token.
| extend EnvNorm = replace_regex(EnvRaw, '[^a-z0-9]', '')
| extend Environment = case(
        isempty(EnvNorm), 'Unverified - no signal',
        EnvNorm startswith 'non' or EnvNorm startswith 'pre'
            or EnvNorm in ('dev','devtest','development','test','testing','qa','uat','stg',
                           'stage','staging','sandbox','sbx','demo','poc','lab',
                           'sit','training'), 'Non-production',
        EnvNorm in ('prod','production','prd','live','prdn'), 'Production',
        'Unverified - unrecognised value')
// Only a confirmed non-production signal earns the wave 1 pilot slot. Anything
// unverified is sequenced as production, because the cost of being wrong is a
// production outage during what was sold as a no-risk pilot.
| extend Wave = case(
        ReadinessScore >= 85 and Environment == 'Non-production', 'Wave 1 - low risk, non-production',
        ReadinessScore >= 85, 'Wave 2 - low risk, production or unverified',
        ReadinessScore >= 60, 'Wave 3 - validate then resize',
        ReadinessScore >= 35, 'Wave 4 - prerequisites required',
        'Wave 5 - re-architecture')
| summarize Resources = count(), Instances = sum(Instances),
            EarliestRetirement = min(RetiresOn),
            AvgReadiness = round(avg(todouble(ReadinessScore)), 0),
            Sizes = strcat_array(make_set(VmSize, 10), ', ')
    by Wave, Environment, EnvSource
| order by Wave asc""",
        "plan-waves", size=4, export=True, max_width="1480px", title="Migration waves",
        grid=grid_settings(
            {"Wave": "18%", "Environment": "14%", "EnvSource": "14%", "Resources": "9%",
             "Instances": "9%", "EarliestRetirement": "12%", "AvgReadiness": "9%",
             "Sizes": "15%"},
            [fmt("AvgReadiness", 8, formatOptions={"palette": "redGreen", "min": 0, "max": 100}),
             date_col("EarliestRetirement"),
             fmt("Environment", 18, formatOptions={"thresholdsOptions": "colors",
                 "thresholdsGrid": [
                     {"operator": "==", "thresholdValue": "Production",
                      "representation": "orange", "text": "{0}{1}"},
                     {"operator": "==", "thresholdValue": "Non-production",
                      "representation": "green", "text": "{0}{1}"},
                     {"operator": "Default", "representation": "gray", "text": "{0}{1}"}]})],
            labelSettings=[
                {"columnId": "EnvSource", "label": "Signal"},
                {"columnId": "EarliestRetirement", "label": "Earliest retirement"},
                {"columnId": "AvgReadiness", "label": "Avg readiness"},
                {"columnId": "Sizes", "label": "Sizes in wave"},
            ])))
    plan.append(arg_query(
        base_impacted + """
| where ReadinessScore < 60
| project Name, ResourceGroup, Region, VmSize, VmGeneration, RecommendedTarget,
          ReadinessScore, Blockers, RetiresOn, DaysToRetirement, ResourceId
| order by DaysToRetirement asc, ReadinessScore asc""",
        "plan-longpole", size=1, export=True, title="Long-pole workloads - start these first",
        grid=grid_settings(
            {"Name": "12%", "ResourceGroup": "10%", "Region": "9%", "VmSize": "10%",
             "VmGeneration": "8%", "RecommendedTarget": "12%", "ReadinessScore": "8%",
             "Blockers": "14%", "RetiresOn": "9%", "DaysToRetirement": "8%"},
            [fmt("Name", 1, formatOptions={"linkColumn": "ResourceId",
                                           "linkTarget": "Resource",
                                           "linkIsContextBlade": True}),
             fmt("ReadinessScore", 8, formatOptions={"palette": "redGreen",
                                                     "min": 0, "max": 100}),
             date_col("RetiresOn"),
             fmt("ResourceId", 5)],
            filter=True,
            labelSettings=[
                {"columnId": "ResourceGroup", "label": "Resource group"},
                {"columnId": "VmSize", "label": "Current size"},
                {"columnId": "VmGeneration", "label": "Gen"},
                {"columnId": "RecommendedTarget", "label": "Recommended target"},
                {"columnId": "ReadinessScore", "label": "Readiness"},
                {"columnId": "RetiresOn", "label": "Retires on"},
                {"columnId": "DaysToRetirement", "label": "Days left"},
                {"columnId": "ResourceId", "label": "Portal"},
            ])))
    items.append(group_item("grp-plan", "plan", plan))

    # ---------------- TAB 6: About ----------------
    about = []
    about.append(text_item(
        "### How this workbook works\n\n"
        "**Data source.** Read-only Azure Resource Graph queries across the subscriptions you "
        "select. Nothing is written, nothing leaves your tenant, and no agent or Log Analytics "
        "workspace is required. You need `Reader` on the subscriptions in scope.\n\n"
        "**Lifecycle status.**\n\n"
        "| Status | Meaning |\n| --- | --- |\n"
        "| Retirement announced | The size has a published retirement date. VMs still running "
        "that size on the date will stop. Action is mandatory. |\n"
        "| End of life | The series has entered the End of Life lifecycle stage with a published "
        "retirement date. Plan the migration inside the notice window. |\n"
        "| Optional modernization | Previous-generation or current-generation sizes with no "
        "announced retirement. Moving is a price-performance decision, not a compliance one. |\n\n"
        "**Readiness score.** Every impacted resource starts at 100 and loses points for each "
        "prerequisite that genuinely applies to it:\n\n"
        "| Factor | Penalty | Evaluated against |\n| --- | ---: | --- |\n"
        "| Arm64 architecture change | 50 | Target CPU architecture |\n"
        "| Gen1 to Gen2 conversion | 45 | **Actual `hyperVGeneration` of the OS disk** |\n"
        "| Target size not offered in this region | 30 | VM region vs target availability |\n"
        "| Memory reduction at same vCPU | 25 | Source vs target memory |\n"
        "| Local temp disk removed | 20 | Source has temp disk, target does not |\n"
        "| Unconfirmed VM generation | 20 | OS disk metadata unavailable |\n"
        "| NVMe and MANA validation | 15 | Target requires NVMe controller |\n"
        "| Narrow target region footprint | 10 | Target region count |\n"
        "| Availability zone parity | 10 | Only when the VM uses a zone |\n"
        "| vCPU count change | 5 | No size exists at the current vCPU count |\n"
        "| Availability set membership | 5 | Cluster-pinned resize constraint |\n"
        "| Scale set | 5 | Model-level SKU change |\n\n"
        "A VM already running Generation 2 takes **no** Gen1 penalty even when its target is "
        "Gen2-only. That is the difference between scoring the size and scoring your fleet.\n\n"
        "**Lifecycle and pricing changes.** A pricing update takes effect **1 February 2027** "
        "for selected v1 and v2 size series. The workbook flags which of your series are in "
        "scope; it does not restate the rate change, because the applicable figures are "
        "communicated to each tenant through its own Azure Service Health notification "
        "and depend on the agreement. For authoritative rates see the official "
        "[Windows](https://azure.microsoft.com/pricing/details/virtual-machines/windows/) and "
        "[Linux](https://azure.microsoft.com/pricing/details/virtual-machines/linux/) VM "
        "pricing pages.\n\n"
        "**Reserved Instances.** Purchases and renewals ended **1 July 2026** for one-year "
        "reservations on Av2, Amv2, Bv1, D, Ds, Dv2, Dsv2, F, Fs, Fsv2, G, Gs, Ls and Lsv2, "
        "and for both one- and three-year reservations on Dv3, Dsv3, Ev3 and Esv3. Existing "
        "reservations run to the end of their term; after that the usage bills at "
        "pay-as-you-go rates **even if the reservation is set to auto-renew** "
        "([Azure update 560948](https://azure.microsoft.com/updates?id=560948)).\n\n"
        "**Cloud coverage.** The two changes do not have the same scope:\n\n"
        "| | Azure public | 21Vianet | Sovereign / Government |\n| --- | :---: | :---: | :---: |\n"
        "| v3 retirement | Applies | Exempt | Exempt |\n"
        "| 1 Feb 2027 pricing update | Applies | Applies | Exempt |\n\n"
        "This workbook does not detect cloud type. If you are running in Azure Government, "
        "a sovereign cloud, or 21Vianet, read the rows above before acting on a flag.\n\n"
        "**Capacity growth restrictions.** Separate from retirement. Existing subscriptions "
        "can keep deploying End of Life series within already-approved quota, subject to "
        "capacity, but additional quota is not approved and new subscriptions cannot deploy "
        "them at all. Restrictions on the v4 D and E families were **lifted** in the "
        "28 September 2026 notification, so quota requests for those families can be "
        "submitted again through the standard process, subject to capacity. Note that "
        "Microsoft's modernization guidance for the v3 series still points at v5, v6 and v7 - "
        "the lifting of v4 restrictions removes a constraint, it does not make v4 the "
        "recommended destination.\n\n"
        "**Caveats.**\n"
        "- Target recommendations are a starting point. Right-size against 30-90 days of real "
        "CPU, memory, disk and network utilisation before committing.\n"
        "- Region availability is checked against a captured size catalogue. Confirm with "
        "`az vm list-skus --location <region> --resource-type virtualMachines --size <target> --all` "
        "before you schedule a wave.\n"
        "- Reserved instance and savings plan coverage is not evaluated here. Review it alongside "
        "the migration plan, because savings plans flex across families while reservations do not.\n"
        "- Per-core licensed software (SQL Server, Oracle, SAP) must be re-checked whenever the "
        "vCPU count changes.\n\n"
        "**References**\n"
        "- [VM size series retirements, capacity restrictions and modernization guidance](https://learn.microsoft.com/azure/virtual-machines/sizes/lifecycle/retirements-and-capacity-restrictions)\n"
        "- [VM lifecycle overview](https://learn.microsoft.com/azure/virtual-machines/sizes/lifecycle/lifecycle-overview)\n"
        "- [End of Life VM size series](https://learn.microsoft.com/azure/virtual-machines/sizes/lifecycle/end-of-life-sizes-list)\n"
        "- [Retired VM sizes modernization guide](https://learn.microsoft.com/azure/virtual-machines/sizes/lifecycle/retirement/retired-sizes-modernization-guide)\n"
        "- [Modernize to the v5 VM series](https://learn.microsoft.com/azure/virtual-machines/sizes/lifecycle/sizes-v5-modernization-overview)\n"
        "- [Modernize to the v6 and v7 VM series](https://learn.microsoft.com/azure/virtual-machines/sizes/lifecycle/sizes-v6-v7-modernization-overview)\n"
        "- [Enhancing the Azure Virtual Machine lifecycle](https://azure.microsoft.com/blog/enhancing-microsoft-azure-virtual-machine-lifecycle/)\n"
        "- [Generation 2 VMs](https://learn.microsoft.com/azure/virtual-machines/generation-2)\n"
        "- [NVMe on Azure VMs](https://learn.microsoft.com/azure/virtual-machines/nvme-overview)\n"
        "- [MANA accelerated networking](https://learn.microsoft.com/azure/virtual-network/accelerated-networking-mana-overview)\n"
        "- [Resize a virtual machine](https://learn.microsoft.com/azure/virtual-machines/sizes/resize-vm)\n"
        "- [Manage Reserved VM Instances](https://learn.microsoft.com/azure/cost-management-billing/reservations/manage-reserved-vm-instance)\n"
        "- [Azure updates - retirements](https://azure.microsoft.com/updates/?updateType=retirements)\n",
        "about-text"))
    about.append(arg_query(
        base + """
| summarize Resources = count(), Instances = sum(Instances) by Lifecycle, ActionCategory
| order by Instances desc""",
        "about-coverage", size=4, width="50", max_width="620px",
        title="Coverage - resources matched to the lifecycle catalogue",
        grid=grid_settings(
            {"Lifecycle": "26%", "ActionCategory": "32%", "Resources": "21%",
             "Instances": "21%"},
            labelSettings=[{"columnId": "ActionCategory", "label": "Lifecycle status"}])))
    about.append(arg_query(
        """resources
| where type =~ 'microsoft.compute/virtualmachines' or type =~ 'microsoft.compute/virtualmachinescalesets'
| extend IsVmss = (type =~ 'microsoft.compute/virtualmachinescalesets')
| where IsVmss or isempty(tostring(properties.virtualMachineScaleSet.id))
| extend VmSize = iff(IsVmss, tostring(sku.name), tostring(properties.hardwareProfile.vmSize))
| extend Instances = iff(IsVmss, toint(sku.capacity), 1)
| summarize Resources = count(), Instances = sum(Instances) by VmSize
| order by Instances desc""",
        "about-allsizes", size=4, width="50", max_width="560px",
        title="All sizes in scope (including sizes with no lifecycle action)",
        grid=grid_settings({"VmSize": "40%", "Resources": "30%", "Instances": "30%"},
                           labelSettings=[{"columnId": "VmSize", "label": "Size"}])))
    items.append(group_item("grp-about", "about", about))

    return {
        "version": "Notebook/1.0",
        "items": items,
        "styleSettings": {},
        "fallbackResourceIds": ["Azure Monitor"],
        "$schema": "https://github.com/Microsoft/Application-Insights-Workbooks/blob/master/schema/workbook.json",
    }


def arm_template(workbook):
    return {
        "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#",
        "contentVersion": "1.0.0.0",
        "parameters": {
            "workbookDisplayName": {
                "type": "string",
                "defaultValue": "Azure VM Lifecycle & Modernization Readiness",
                "metadata": {"description": "Name shown in the Workbooks gallery."},
            },
            "workbookId": {
                "type": "string",
                "defaultValue": "[newGuid()]",
                "metadata": {"description": "Unique id for the workbook resource."},
            },
        },
        "resources": [{
            "name": "[parameters('workbookId')]",
            "type": "microsoft.insights/workbooks",
            "location": "[resourceGroup().location]",
            "apiVersion": "2022-04-01",
            "kind": "shared",
            "properties": {
                "displayName": "[parameters('workbookDisplayName')]",
                "serializedData": json.dumps(workbook),
                "version": "1.0",
                "sourceId": "azure monitor",
                "category": "workbook",
            },
        }],
        "outputs": {
            "workbookId": {"type": "string",
                           "value": "[resourceId('microsoft.insights/workbooks', parameters('workbookId'))]"}
        },
    }


def main():
    import argparse
    global ONLY_SIZES
    ap = argparse.ArgumentParser()
    ap.add_argument("--only-sizes", help="File with one VM size per line. Restricts "
                                         "the embedded lookup (preview builds).")
    ap.add_argument("--suffix", default="", help="Suffix for output file names.")
    args = ap.parse_args()

    if args.only_sizes:
        with open(args.only_sizes, encoding="utf-8") as f:
            ONLY_SIZES = {ln.strip() for ln in f if ln.strip()}
        print(f"PREVIEW BUILD - lookup restricted to {len(ONLY_SIZES)} sizes")

    data = load()
    wb = build(data)
    os.makedirs(OUT, exist_ok=True)
    os.makedirs(WORKBOOK_DIR, exist_ok=True)
    sfx = args.suffix

    wb_path = os.path.join(OUT, f"azure-vm-retirement-workbook{sfx}.json")
    with open(wb_path, "w", encoding="utf-8") as f:
        json.dump(wb, f, indent=2)

    arm_path = os.path.join(OUT, f"deploy-workbook{sfx}.json")
    with open(arm_path, "w", encoding="utf-8") as f:
        json.dump(arm_template(wb), f, indent=2)

    # Published copies. The "Deploy to Azure" button resolves a raw
    # githubusercontent URL, so the ARM template has to live at a stable path
    # in the repo rather than only under out/.
    published = []
    if not sfx:
        pub_arm = os.path.join(WORKBOOK_DIR, "azuredeploy.json")
        with open(pub_arm, "w", encoding="utf-8") as f:
            json.dump(arm_template(wb), f, indent=2)
        pub_wb = os.path.join(WORKBOOK_DIR, "vm-lifecycle-readiness.workbook")
        with open(pub_wb, "w", encoding="utf-8") as f:
            json.dump(wb, f, indent=2)
        published = [pub_arm, pub_wb]

    sku_map, spec_map, url_map, tgt_regions, region_ids = build_lookups(data)
    tiers = {}
    for rec in sku_map.values():
        t = rec.split("|")[0]
        tiers[t] = tiers.get(t, 0) + 1

    # Emit the raw base query so it can be validated directly against ARG.
    q_path = os.path.join(OUT, "base-query.kql")
    with open(q_path, "w", encoding="utf-8") as f:
        f.write(base_query(sku_map, tgt_regions, region_ids))

    print(f"SKUs in lookup      : {len(sku_map)}  {tiers}")
    print(f"Target region map   : {len(tgt_regions)} targets / {len(region_ids)} regions")
    print(f"Spec notes          : {len(spec_map)} series, {len(url_map)} announcement urls")
    print(f"Base query          : {os.path.getsize(q_path)/1024:.0f} KB")
    print(f"Workbook            : {os.path.getsize(wb_path)/1024:.0f} KB -> {wb_path}")
    print(f"ARM template        : {os.path.getsize(arm_path)/1024:.0f} KB -> {arm_path}")
    for p in published:
        print(f"Published           : {os.path.getsize(p)/1024:.0f} KB -> {p}")


if __name__ == "__main__":
    main()
