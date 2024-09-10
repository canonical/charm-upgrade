# In-place charm refreshes
This document specifies the product requirements and user experience for in-place, rolling refreshes of stateful charmed applications—particularly for charmed databases maintained by the Data Platform team.

This document will be used as reference for & versioned alongside a Python package containing shared refresh code for Data Platform charmed databases.

Overview:
- The [Glossary](#glossary) defines terms used in this document
- [What happens after a Juju application is refreshed](#what-happens-after-a-juju-application-is-refreshed) describes the behavior of Juju and Kubernetes after the user runs `juju refresh`. These are the constraints in which the product requirements are implemented.
- [Product requirements](#product-requirements) describes the functionality & behavior that Data Platform charmed databases need to support for in-place refreshes.
- [User experience](#user-experience) is a full description—excluding user documentation—of how the user interacts with and experiences an in-place refresh of a single Juju application. The user experience satisfies the product requirements.

# Glossary
[Application](https://juju.is/docs/juju/application), [unit](https://juju.is/docs/juju/unit), [leader](https://juju.is/docs/juju/leader), [charm](https://juju.is/docs/juju/charmed-operator), [revision](https://juju.is/docs/sdk/revision), and [relation/integration](https://juju.is/docs/juju/relation) have the same meaning as in the Juju documentation.

User: User of Juju (e.g. user of juju CLI). Same meaning as "user" in diagram [in the Juju documentation](https://juju.is/docs/juju)

Event: Same meaning as "[Juju event](https://juju.is/docs/juju/hook)" or "hook" in the Juju documentation. Does not refer to an "ops event"

Workload: A software component that the charm operates (e.g. PostgreSQL)
- Note: a charm can have 0, 1, or multiple workloads

Charm code: Contents of *.charm file or `charm` directory (e.g. `/var/lib/juju/agents/unit-postgresql-k8s-0/charm/`) on a unit. Contains charm source code and (specific versions of) Python dependencies

Charm code version: Same meaning as charm [revision](https://juju.is/docs/sdk/revision)

Outdated version:
- Charm code version on a unit that **does not** match the application's charm code version (revision) and/or
- Workload version on a unit that **does not** match the application's workload version
    - On Kubernetes, the application's workload version is the [OCI resource](https://juju.is/docs/juju/charm-resource) specified by the user
    - On machines, the application's workload version is pinned in the application's charm code version (revision)

Up-to-date version:
- Charm code version on a unit that **does** match the application's charm code version (revision) and/or
- Workload version on a unit that **does** match the application's workload version
    - On Kubernetes, the application's workload version is the [OCI resource](https://juju.is/docs/juju/charm-resource) specified by the user
    - On machines, the application's workload version is pinned in the application's charm code version (revision)

Original version: workload and/or charm code version of all units immediately after the last completed refresh—or, if no completed refreshes, immediately after `juju deploy` and (on machines) initial installation

## For an application (or if not specified)
Refresh: `juju refresh` to a different workload and/or charm code version
- Note: "rollback" and "downgrade" are specific types of "refresh"

In-progress refresh: 1+ units have an outdated workload and/or charm code version

Completed refresh: All units have the up-to-date workload and charm code version

Rollback: While a refresh is in-progress and 1+ units have the **original** workload (and, on Kubernetes, charm code) version, `juju refresh` to the original workload and charm code version
 - Note: If all units have already refreshed, then it would be a downgrade, not a rollback
 - Note: If `juju refresh` is not to the original workload and charm code version, then it is not a rollback

Downgrade: Refresh to older (lower) workload and/or charm code version

## For a unit
Charm code refresh: Contents of `charm` directory are replaced with up-to-date charm code version

Workload refresh: Workload is stopped (if running) and updated to up-to-date workload version

Refresh:
- For Kubernetes: charm code and workload are refreshed
- For machines: workload is refreshed

Rollback:
- For Kubernetes: charm code and workload are refreshed to original versions
- For machines: workload is refreshed to original version

Downgrade:
- For Kubernetes: charm code and/or workload are refreshed to older (lower) version
- For machines: workload is refreshed to older (lower) version

# What happens after a Juju application is refreshed
This section describes the behavior of Juju and Kubernetes after the user runs `juju refresh`. These are the constraints in which the [product requirements](#product-requirements) are implemented.

## Kubernetes
On Kubernetes, each Juju application is a [StatefulSet](https://kubernetes.io/docs/concepts/workloads/controllers/statefulset/) configured with the [`RollingUpdate` update strategy](https://kubernetes.io/docs/concepts/workloads/controllers/statefulset/#rolling-updates). Each Juju unit is a [Pod](https://kubernetes.io/docs/concepts/workloads/pods/).

When the user runs `juju refresh`, Juju updates the application's StatefulSet.

Then:
1. Kubernetes [sends a SIGTERM signal](https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/#pod-termination) to the pod with the highest [ordinal](https://kubernetes.io/docs/concepts/workloads/controllers/statefulset/#ordinal-index) (unit number)
1. Juju emits a [stop event](https://juju.is/docs/sdk/stop-event) on the unit
1. After the unit processes the stop event **or** after the pod's `terminationGracePeriodSeconds` have elapsed, whichever comes first, Kubernetes deletes the pod
    - `terminationGracePeriodSeconds` is set to 30 seconds as of Juju 3.3 (300 seconds in Juju <=3.2). It is [not recommended](https://chat.charmhub.io/charmhub/pl/i4czczen7f8i9cecdzpfmazs6a) for charms to patch this value. Details: https://bugs.launchpad.net/juju/+bug/2035102

1. Kubernetes re-creates the pod using the updated StatefulSet
    - This refreshes the unit's charm code and container image(s) (i.e. workload(s))
1. Juju emits an [upgrade-charm event](https://juju.is/docs/sdk/upgrade-charm-event) on the unit
    - Note: Receiving an upgrade-charm event does not guarantee that a unit has refreshed. If, at any time, a pod is deleted and re-created, Juju may emit an upgrade-charm event on that unit. Details: https://bugs.launchpad.net/juju/+bug/2021891
1. After the pod's [readiness probe](https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/#container-probes) succeeds, the previous steps are repeated for the pod with the next highest ordinal
    - For a Juju unit, [pebble's health endpoint](https://github.com/canonical/pebble?tab=readme-ov-file#health-endpoint) is used for the readiness probe. By default, pebble will always succeed the probe

Charms can interrupt this process by setting the [`RollingUpdate` partition](https://kubernetes.io/docs/concepts/workloads/controllers/statefulset/#partitions).
> If a partition is specified, all Pods with an ordinal that is greater than or equal to the partition will be updated when the StatefulSet's `.spec.template` is updated. All Pods with an ordinal that is less than the partition will not be updated, and, even if they are deleted, they will be recreated at the previous version.

For example, in a 3-unit Juju application (unit numbers: 0, 1, 2), as unit 2's pod is being deleted, the charm can set the partition to 2. Unit 2 will refresh but units 1 and 0 will not. Then, after the charm verifies that all units are healthy, it can set the partition to 1 and unit 1 will refresh.

Note: after the user runs `juju refresh`, the charm cannot prevent refresh of the highest unit number.

> [!WARNING]
> Charms should not set the partition greater than the highest unit number. If they do, `juju refresh` will not trigger any [Juju events](https://juju.is/docs/juju/hook).

> [!IMPORTANT]
> During rollback, all pods—even those that have not refreshed—will be deleted (workload will restart). This is a Juju bug: https://bugs.launchpad.net/juju/+bug/2036246

> [!CAUTION]
> If a pod (unit) with an outdated (workload or charm code) version is deleted and re-created on the same version (e.g. because the pod is [evicted](https://kubernetes.io/docs/concepts/scheduling-eviction/node-pressure-eviction/)), it will not start. This is a Juju bug: https://bugs.launchpad.net/juju/+bug/2073506

## Machines
After the user runs `juju refresh`, for each unit of the Juju application:
> [!NOTE]
> If the unit failed to execute the last event (raised uncaught exception), Juju may retry that event. Then, Juju will refresh the unit's charm code without emitting an upgrade-charm event on that unit. This is a Juju bug: https://bugs.launchpad.net/juju/+bug/2068500

1. If the unit is currently executing another event, Juju [waits for the unit to finish executing that event](https://matrix.to/#/!xzmWHtGpPfVCXKivIh:ubuntu.com/$firps4AV5YInSDQh4izbPTZ0B0e0QwAbQVMaURT0T3o?via=ubuntu.com&via=matrix.org&via=fsfe.org)
1. Juju refreshes the unit's charm code
1. Juju emits an [upgrade-charm event](https://juju.is/docs/sdk/upgrade-charm-event) on that unit

This process happens concurrently and independently for each unit. For example, if one unit is executing another event, that will not prevent Juju from refreshing other units' charm code.

Refreshing the workload(s) (e.g. snap or apt packages) is left to the charm.

## Key differences between Kubernetes and machines
On Kubernetes, the charm code and workload are refreshed at the same time (for a unit). On machines, they are refreshed at different times.

On Kubernetes, while a refresh is in progress, units will have different charm code versions. The leader unit may have the old or new charm code version.

On machines, while a refresh is in progress, the charm code version may be out of sync with the workload version. (For example, if the charm code is written for workload version B, it may not know how to operate workload version A [e.g. to maintain high availability].)

After `juju refresh`, on machines, the charm can prevent workload refresh (e.g. if the new version is incompatible) for all units. On Kubernetes, the charm cannot prevent workload refresh of the highest unit number.

# Product requirements
This section describes the functionality & behavior that Data Platform charmed databases need to support for in-place refreshes.

Top-level bullet points are requirements. Sub-level bullet points are the rationale for a requirement.
- Refresh units in place
  - To avoid replicating large amounts of data
  - To avoid additional hardware costs
  - To keep existing configuration & integrations with other Juju applications
- Refresh units one at a time
  - To serve read & write traffic to database during refresh
  - To reduce downtime
  - To test new version with subset of traffic (e.g. on one unit) before switching all traffic to new version
- Rollback refreshed units (one at a time) at any time during refresh
  - If there are any issues with new version of charm code or workload
- Maintain high availability while refresh is in progress (for up to multiple weeks)
  - To allow user to monitor new version with subset of traffic for extended period of time before switching all traffic to new version
  - For large databases (terabytes, petabytes)
- Pause refresh to allow user to perform manual checks after refresh of units: all, first, or none
  - Automated checks within the charm are not sufficient—for example, if a database client is outdated & incompatible with the new database version
  - Needs to be configurable for different user risk levels
- Allow user to change which units (all, first, or none) the refresh pauses after while a refresh is in progress
  - To allow user to pause after each of the first few units and then proceed with the remaining units
  - To allow user to interrupt a refresh (e.g. to rollback) when a pause was not originally planned
- Warn the user if a refresh is incompatible. Allow them to proceed if they accept potential data loss and downtime
- Automatically check the health of the application and all units after each unit refreshes. If anything is unhealthy, pause the refresh and notify the user. Allow them to proceed if they accept potential data loss and downtime
- Provide pre-refresh health checks (e.g. backup created) & preparations (e.g. switch primary) that the user can run before `juju refresh` and, when possible, that are automatically run after `juju refresh`
- Provide accurate, up-to-date information about the current refresh status, workload status for each unit, workload and charm code versions for each unit, which units' workloads will restart, and what action, if any, the user should take next
- If a unit (e.g. the leader) is in error state (charm raises uncaught exception), allow rollback on other units
  - In case there is a bug in the new charm code version
  - In case the user accidentally refreshed to a different charm code version than they intended
- If a unit (e.g. the leader) is in error state (charm raises uncaught exception), allow refresh on other units with manual user confirmation
  - For an application with several units refreshed, it may be safer to ignore one unhealthy unit and complete the refresh then to rollback all refreshed units
- For all workloads supported by Canonical, allow charms to have a 1:1 mapping between charm revision to workload version (i.e. snap revision or OCI image hash)—or allow charms to have a 1:many mapping if the charm uses immutable (cannot change after charm is deployed) config options that create a 1:1 mapping between charm revision with those config values to workload version.
  - To keep the Data Platform team's options open in the future. For example, the PostgreSQL charm may be compatible with an open-source and an enterprise version of a plugin. The Data Platform team may ship the open-source and enterprise versions separately by using (1) different Charmhub tracks or (2) config values (using a single charm revision). This requirement keeps the choice of option 2 available (hopefully) without requiring breaking changes to the refresh implementation.
- Allow refreshes to and from workloads not supported by Canonical. (This is not officially supported—it is only permitted.)
  - To allow user to manually apply an urgent security patch to a workload supported by Canonical (making it become a workload not supported by Canonical) and then later refresh to a workload supported by Canonical

# User experience
This section is a full description—excluding user documentation—of how the user interacts with and experiences an in-place refresh of a single Juju application. The user experience satisfies the [product requirements](#product-requirements).

## `pause_after_unit_refresh` config option
```yaml
# config.yaml
options:
  # [...]
  pause_after_unit_refresh:
    description: |
      Wait for manual confirmation to resume refresh after these units refresh

      Allowed values: "all", "first", "none"
    type: string
    default: first
```
If a refresh is not in progress, changing this value will have no effect until the next refresh.

If a refresh is in progress, changes to this value will take effect before the next unit refreshes. (Any units that are refreshing when the value is changed will finish refreshing.)

Example 1:
- 4-unit Juju application
  - Unit 0: v1
  - Unit 1: v1
  - Unit 2: v2
  - Unit 3: v2
- `pause_after_unit_refresh` changed from `all` to `first`
- Unit 1 will immediately refresh. If it is healthy after refreshing, unit 0 will refresh

Example 2:
- 4-unit Juju application
  - Unit 0: v1
  - Unit 1: refreshing from v1 to v2
  - Unit 2: v2
  - Unit 3: v2
- `pause_after_unit_refresh` changed from `none` to `all`
- Unit 1 will finish refreshing to v2. After that, no units will refresh until the user runs the `resume-refresh` action or runs `juju refresh` (e.g. to rollback)

### App status if `pause_after_unit_refresh` set to invalid value
If `pause_after_unit_refresh` is not set to `all`, `first`, or `none`, this app status will be displayed—regardless of whether a refresh is in progress.

This status will have higher priority than any other app status in a charm.

```
$ juju status
[...]
App             [...]  Status   [...]  Message
postgresql-k8s         blocked         pause_after_unit_refresh config must be set to "all", "first", or "none"
[...]
```

## `pre-refresh-check` action (optional)
Before the user runs `juju refresh`, they should run the `pre-refresh-check` action on the leader unit. The leader unit will run pre-refresh health checks (e.g. backup created) & preparations (e.g. switch primary).

Optional: In the user documentation, this step will not be marked as optional (since it improves the safety of the refresh—especially on Kubernetes). However, since forgetting to run the action is a common mistake (it has already happened on a production PostgreSQL charm), it is not required.

This action will fail if run before a rollback.

```yaml
# actions.yaml
pre-refresh-check:
  description: Check if charm is ready to refresh
```

### If pre-refresh health checks & preparations are successful
#### Kubernetes
```
$ juju run postgresql-k8s/leader pre-refresh-check
Running operation 1 with 1 task
  - task 2 on unit-postgresql-k8s-0

Waiting for task 2...
result: |-
  Charm is ready for refresh. For refresh instructions, see https://charmhub.io/postgresql-k8s/docs/h-upgrade-intro
  After the refresh has started, use this command to rollback (copy this down in case you need it later):
  `juju refresh postgresql-k8s --revision 10007 --resource postgresql-image=registry.jujucharms.com/charm/kotcfrohea62xreenq1q75n1lyspke0qkurhk/postgresql-image@sha256:76ef26c7d11a524bcac206d5cb042ebc3c8c8ead73fa0cd69d21921552db03b6`
```
where `https://charmhub.io/postgresql-k8s/docs/h-upgrade-intro` is replaced with the link to the charm's refresh documentation, `postgresql-k8s` is replaced with the Juju application name, `10007` is replaced with the original (current) charm code revision, `postgresql-image` is replaced with the [OCI resource name](https://juju.is/docs/sdk/charmcraft-yaml#heading--resources), and `registry.jujucharms.com/charm/kotcfrohea62xreenq1q75n1lyspke0qkurhk/postgresql-image@sha256:76ef26c7d11a524bcac206d5cb042ebc3c8c8ead73fa0cd69d21921552db03b6` is replaced with the original (current) workload version

#### Machines
```
result: |-
  Charm is ready for refresh. For refresh instructions, see https://charmhub.io/postgresql/docs/h-upgrade-intro
  After the refresh has started, use this command to rollback:
  `juju refresh postgresql --revision 10007`
```
where `https://charmhub.io/postgresql/docs/h-upgrade-intro` is replaced with the link to the charm's refresh documentation, `postgresql` is replaced with the Juju application name, and `10007` is replaced with the original (current) charm code revision

### If pre-refresh health checks & preparations are not successful
```
$ juju run postgresql-k8s/leader pre-refresh-check
Running operation 1 with 1 task
  - task 2 on unit-postgresql-k8s-0

Waiting for task 2...
Action id 2 failed: Charm is not ready for refresh. Pre-refresh check failed: Backup in progress
```
where `Backup in progress` is replaced with a message that is specific to the pre-refresh health check or preparation that failed
### If action ran while refresh is in progress
```
Action id 2 failed: Refresh already in progress
```

### If action ran on non-leader unit
```
Action id 2 failed: Must run action on leader unit. (e.g. `juju run postgresql-k8s/leader pre-refresh-check`)
```
where `postgresql-k8s` is replaced with the Juju application name

## Status messages while refresh in progress
After the user runs `juju refresh`, these status messages will be displayed until the refresh is complete.

> [!NOTE]
> Status messages over 120 characters are truncated in `juju status` (tested on Juju 3.1.6 and 2.9.45)

### App status
All of these app statuses will have higher priority than any other app status in a charm—except for [App status if `pause_after_unit_refresh` set to invalid value](#app-status-if-pause_after_unit_refresh-set-to-invalid-value).

#### (Machines only) If it is not possible to determine if a refresh is in progress
On machines, in certain cases, it is not possible to determine if a refresh is in progress.

For example, it is not (easily) possible to immediately differentiate between the following cases:
- After `juju refresh`, if the charm code is refreshed and the workload version is identical (i.e. same snap revision). (A refresh is not in progress)
- User refreshes from charm revision 10007 to 10008. Highest unit's charm code refreshes snap from revision 20001 to 20002 and raises an uncaught exception in the same Juju event. User refreshes (rollback) to charm revision 10007. (A refresh is in progress)

In both of these examples, after a few Juju events (usually a few seconds), it will be possible to determine if a refresh is in progress—as long as no units' charm code is raising an uncaught exception.
```
$ juju status
[...]
App             Version  Status       [...]    Rev  [...]  Message
postgresql-k8s  14.12    maintenance         10008         Determining if a refresh is in progress
[...]
```

#### If refresh will pause for manual confirmation
(`pause_after_unit_refresh` is set to `all` or set to `first` and second unit has not started to refresh)

##### Kubernetes
```
App             Version  Status     Rev  Message
postgresql-k8s  14.12    blocked  10008  Refreshing. Check units >=11 are healthy & run `resume-refresh` on leader. To rollback, see docs or `juju debug-log`
```
where `>=11` is replaced with the units that have refreshed or are currently refreshing
<!-- TODO: version field? -->

During every Juju event, the leader unit will also log an INFO level message to `juju debug-log`. For example:
```
unit-postgresql-0: 11:34:35 INFO unit.postgresql/0.juju-log Refresh in progress. To rollback, run `juju refresh postgresql-k8s --revision 10007 --resource postgresql-image=registry.jujucharms.com/charm/kotcfrohea62xreenq1q75n1lyspke0qkurhk/postgresql-image@sha256:76ef26c7d11a524bcac206d5cb042ebc3c8c8ead73fa0cd69d21921552db03b6`
```
where `postgresql-k8s` is replaced with the Juju application name, `10007` is replaced with the original charm code revision, `postgresql-image` is replaced with the [OCI resource name](https://juju.is/docs/sdk/charmcraft-yaml#heading--resources), and `registry.jujucharms.com/charm/kotcfrohea62xreenq1q75n1lyspke0qkurhk/postgresql-image@sha256:76ef26c7d11a524bcac206d5cb042ebc3c8c8ead73fa0cd69d21921552db03b6` is replaced with the original workload version

##### Machines
```
App         Version  Status     Rev  Message
postgresql  14.12    blocked  10008  Refreshing. Check units >=11 are healthy & run `resume-refresh` on unit 10. To rollback, `juju refresh --revision 10007`
```
where `>=11` is replaced with the units that have refreshed or are currently refreshing, `10` is replaced with the next unit to refresh, and `10007` is replaced with the original charm code revision
<!-- TODO: version field? -->

#### If refresh will not pause for manual confirmation
(`pause_after_unit_refresh` is set to `none` or set to `first` and second unit has refreshed or started to refresh)

```
App             Status       Message
postgresql-k8s  maintenance  Refreshing. To pause refresh, run `juju config postgresql-k8s pause_after_unit_refresh=all`
```
where `postgresql-k8s` is replaced with the Juju application name

#### (Machines only) If refresh is incompatible
On machines, after the user runs `juju refresh` and before any workload is refreshed, the new charm code checks if it supports refreshing from the previous workload & charm code version.

If the refresh is not supported, no workload will be refreshed and the app status will be
```
App         Status     Rev  Message
postgresql  blocked  10008  Refresh incompatible. Rollback with `juju refresh --revision 10007`
```
where `10007` is replaced with the original charm code revision

This status will only show if an incompatible refresh has not been forced on the first unit to refresh with the `force-refresh-start` action.

The leader unit will also log an INFO level message to `juju debug-log`. For example:
```
unit-postgresql-0: 11:34:35 INFO unit.postgresql/0.juju-log Refresh incompatible. Rollback with `juju refresh`. Continuing this refresh may cause data loss and/or downtime. The refresh can be forced to continue with the `force-refresh-start` action and the `check-compatibility` parameter. Run `juju show-action postgresql force-refresh-start` for more information
```
where `postgresql` is replaced with the Juju application name

### Unit status
#### Higher priority statuses
These statuses will have higher priority than any other unit status in a charm.

##### (Kubernetes only) If workload version does not match charm code version
If the user runs `juju refresh` with `--revision` and without `--resource`, the workload(s) will not be refreshed. This is not supported—Data Platform charms pin a specific workload version for each charm code version.

Similarly, these additional cases are not supported and will have the same user experience:
- If the user runs `juju refresh` with `--resource` and with a `--revision` of the charm code that does not pin that specified resource (workload) version, the workload(s) & charm code will be refreshed but will not match.
- If the user runs `juju refresh` with `--resource` and with `--channel` (they should instead only use `--channel`), the workload(s) & charm code will be refreshed but may not match.
- If the user runs `juju refresh` with `--resource` and without `--channel` or `--revision`, Juju will use the currently tracked channel—which is the same as the previous case.
```
Unit              Workload  [...]  Message
postgresql-k8s/2  blocked          `juju refresh` was run with missing/incorrect OCI resource. Rollback with instructions in docs or see `juju debug-log`
```

The unit will also log an ERROR level message to `juju debug-log`. For example:
```
unit-postgresql-k8s-2: 11:34:35 ERROR unit.postgresql-k8s/2.juju-log `juju refresh` was run with missing or incorrect OCI resource. Rollback by running `juju refresh postgresql-k8s --revision 10007 --resource postgresql-image=registry.jujucharms.com/charm/kotcfrohea62xreenq1q75n1lyspke0qkurhk/postgresql-image@sha256:76ef26c7d11a524bcac206d5cb042ebc3c8c8ead73fa0cd69d21921552db03b6`. If you are intentionally attempting to refresh to a PostgreSQL container version that is not validated with this release, you may experience data loss and/or downtime as a result of refreshing. The refresh can be forced to continue with the `force-refresh-start` action and the `check-workload-container` parameter. Run `juju show-action postgresql-k8s force-refresh-start` for more information
```
where `postgresql-k8s` is replaced with the Juju application name, `10007` is replaced with the original charm code revision, `postgresql-image` is replaced with the [OCI resource name](https://juju.is/docs/sdk/charmcraft-yaml#heading--resources), and `registry.jujucharms.com/charm/kotcfrohea62xreenq1q75n1lyspke0qkurhk/postgresql-image@sha256:76ef26c7d11a524bcac206d5cb042ebc3c8c8ead73fa0cd69d21921552db03b6` is replaced with the original workload version

##### (Kubernetes only) If refresh is incompatible
On Kubernetes, after the first unit refreshes and before that unit starts its workload, that unit (new charm code) checks if it supports refreshing from the previous workload & charm code version.

If the refresh is not supported, that unit will not start its workload and its status will be
```
Unit              Workload  [...]  Message
postgresql-k8s/2  blocked          Refresh incompatible. Rollback with instructions in Charmhub docs or see `juju debug-log`
```

This status will only show on the first unit to refresh and only if the workload has not been forced to (attempt to) start with the `force-refresh-start` action.

The unit will also log an INFO level message to `juju debug-log`. For example:
```
unit-postgresql-k8s-2: 11:34:35 INFO unit.postgresql-k8s/2.juju-log Refresh incompatible. Rollback by running `juju refresh postgresql-k8s --revision 10007 --resource postgresql-image=registry.jujucharms.com/charm/kotcfrohea62xreenq1q75n1lyspke0qkurhk/postgresql-image@sha256:76ef26c7d11a524bcac206d5cb042ebc3c8c8ead73fa0cd69d21921552db03b6`. Continuing this refresh may cause data loss and/or downtime. The refresh can be forced to continue with the `force-refresh-start` action and the `check-compatibility` parameter. Run `juju show-action postgresql-k8s force-refresh-start` for more information
```
where `postgresql-k8s` is replaced with the Juju application name, `10007` is replaced with the original charm code revision, `postgresql-image` is replaced with the [OCI resource name](https://juju.is/docs/sdk/charmcraft-yaml#heading--resources), and `registry.jujucharms.com/charm/kotcfrohea62xreenq1q75n1lyspke0qkurhk/postgresql-image@sha256:76ef26c7d11a524bcac206d5cb042ebc3c8c8ead73fa0cd69d21921552db03b6` is replaced with the original workload version

##### If automatic pre-refresh health checks & preparations fail
Regardless of whether the user runs the `pre-refresh-check` action before `juju refresh`, the charm will run pre-refresh health checks & preparations after `juju refresh`—unless it is a rollback.

On machines, the checks & preparations run before any workload is refreshed. These checks & preparations are identical to those in the `pre-refresh-check` action—except that they are from the new charm code version.

On Kubernetes, the checks & preparations run after the first unit has refreshed. These checks & preparations are a subset of those in the `pre-refresh-check` action (since some checks & preparations may require that all units have the same workload version). These checks & preparations run on the refreshed unit (i.e. on the new charm code version).

```
Unit              Workload  Message
postgresql-k8s/2  blocked   Rollback with `juju refresh`. Pre-refresh check failed: Backup in progress
```
where `Backup in progress` is replaced with a message that is specific to the pre-refresh health check or preparation that failed

This status will only show on the first unit to refresh and only if the workload has not been forced to refresh (machines) or to attempt to start (Kubernetes) with the `force-refresh-start` action.

The unit will also log an ERROR level message to `juju debug-log`. For example:

Kubernetes
```
unit-postgresql-k8s-2: 11:34:35 ERROR unit.postgresql-k8s/2.juju-log Pre-refresh check failed: Backup in progress. Rollback by running `juju refresh postgresql-k8s --revision 10007 --resource postgresql-image=registry.jujucharms.com/charm/kotcfrohea62xreenq1q75n1lyspke0qkurhk/postgresql-image@sha256:76ef26c7d11a524bcac206d5cb042ebc3c8c8ead73fa0cd69d21921552db03b6`. Continuing this refresh may cause data loss and/or downtime. The refresh can be forced to continue with the `force-refresh-start` action and the `run-pre-refresh-checks` parameter. Run `juju show-action postgresql-k8s force-refresh-start` for more information
```
where `Backup in progress` is replaced with a message that is specific to the pre-refresh health check or preparation that failed, `postgresql-k8s` is replaced with the Juju application name, `10007` is replaced with the original charm code revision, `postgresql-image` is replaced with the [OCI resource name](https://juju.is/docs/sdk/charmcraft-yaml#heading--resources), and `registry.jujucharms.com/charm/kotcfrohea62xreenq1q75n1lyspke0qkurhk/postgresql-image@sha256:76ef26c7d11a524bcac206d5cb042ebc3c8c8ead73fa0cd69d21921552db03b6` is replaced with the original workload version

Machines
```
unit-postgresql-k8s-2: 11:34:35 ERROR unit.postgresql-k8s/2.juju-log Pre-refresh check failed: Backup in progress. Rollback with `juju refresh`. The refresh can be forced to continue with the `force-refresh-start` action and the `run-pre-refresh-checks` parameter. Run `juju show-action postgresql-k8s force-refresh-start` for more information
```
where `Backup in progress` is replaced with a message that is specific to the pre-refresh health check or preparation that failed

#### Lower priority statuses
These statuses will have lower priority than any other unit status with a message in a charm.

In all the following examples, all units are healthy. If a unit was unhealthy, that unit's status would take priority.

##### Kubernetes
###### Example: Normal refresh
Unit 2 has refreshed. Units 1 and 0 have not refreshed.
```
Unit               Workload  Message
postgresql-k8s/0*  active    PostgreSQL 14.11 running (restart pending); Charm revision 10007
postgresql-k8s/1   active    PostgreSQL 14.11 running (restart pending); Charm revision 10007
postgresql-k8s/2   active    PostgreSQL 14.12 running; Charm revision 10008
```
where `PostgreSQL 14.12` and `PostgreSQL 14.11` are replaced with the name & version of the workload(s) installed on that unit and `10008` and `10007` are replaced with the revision of the charm code on that unit

###### Example: Rollback
Units 2 and 1 refreshed from revision 10007 & OCI resource 76ef26 to revision 10008 & OCI resource 6be83f. Then, the user ran `juju refresh` to revision 10007 & OCI resource 76ef26. Unit 2 has rolled back.
```
Unit               Workload  Message
postgresql-k8s/0*  active    PostgreSQL 14.11 running (restart pending); Charm revision 10007
postgresql-k8s/1   active    PostgreSQL 14.12 running (restart pending); Charm revision 10008
postgresql-k8s/2   active    PostgreSQL 14.11 running; Charm revision 10007
```
where `PostgreSQL 14.12` and `PostgreSQL 14.11` are replaced with the name & version of the workload(s) installed on that unit and `10008` and `10007` are replaced with the revision of the charm code on that unit

Unit 0 will restart even though the workload & charm code version will not change. This is a Juju bug: https://bugs.launchpad.net/juju/+bug/2036246

###### Example: Charm code refresh without workload refresh
If the charm code is refreshed and the workload version is unchanged, all units will restart. This happens because `juju refresh` updates the Kubernetes StatefulSet.

Unit 2 has refreshed. Units 1 and 0 have not refreshed.
```
Unit               Workload  Message
postgresql-k8s/0*  active    PostgreSQL 14.12 running (restart pending); Charm revision 10008
postgresql-k8s/1   active    PostgreSQL 14.12 running (restart pending); Charm revision 10008
postgresql-k8s/2   active    PostgreSQL 14.12 running; Charm revision 10009
```
where `PostgreSQL 14.12` is replaced with the name & version of the workload(s) installed on that unit and `10009` and `10008` are replaced with the revision of the charm code on that unit

###### Example: Workload is not running before & during refresh
These statuses are only applicable if the workload would also not be running if there was no refresh in progress.

For example, MySQL Router will only run if its charm is related to a MySQL charm. If a MySQL Router charm—that is not related to a MySQL charm—is refreshed, these statuses would be shown.

Unit 2 has refreshed. Units 1 and 0 have not refreshed.
```
Unit                 Workload  Message
mysql-router-k8s/0*  waiting   Router 8.0.36; Charm revision 10007 (restart pending)
mysql-router-k8s/1   waiting   Router 8.0.36; Charm revision 10007 (restart pending)
mysql-router-k8s/2   waiting   Router 8.0.37; Charm revision 10008
```
where `Router 8.0.37` and `Router 8.0.36` are replaced with the name & version of the workload(s) installed on that unit and `10008` and `10007` are replaced with the revision of the charm code on that unit

###### Example: Refresh to unsupported workload version
The user refreshed to an unsupported workload version (OCI image 68ed80) and forced the refresh to continue with `force-refresh-start check-workload-container=false`. Unit 2 has refreshed. Units 1 and 0 have not refreshed.
```
Unit               Workload  Message
postgresql-k8s/0*  active    PostgreSQL 14.11 running (restart pending); Charm revision 10007
postgresql-k8s/1   active    PostgreSQL 14.11 running (restart pending); Charm revision 10007
postgresql-k8s/2   active    PostgreSQL 14.12 running; Charm revision 10008; Unexpected container 68ed80
```
where `PostgreSQL 14.12` and `PostgreSQL 14.11` are replaced with the name & version of the workload(s) installed on that unit, `10008` and `10007` are replaced with the revision of the charm code on that unit, and `68ed80` is replaced with the first 6 characters of the OCI image hash

##### Machines
###### Example: Normal refresh
Unit 2 has refreshed. Units 1 and 0 have not refreshed.
```
Unit           Workload  Message
postgresql/0*  active    PostgreSQL 14.11 running; Snap revision 20001 (outdated); Charm revision 10008
postgresql/1   active    PostgreSQL 14.11 running; Snap revision 20001 (outdated); Charm revision 10008
postgresql/2   active    PostgreSQL 14.12 running; Snap revision 20002; Charm revision 10008
```
where `PostgreSQL 14.12` and `PostgreSQL 14.11` are replaced with the name & version of the workload(s) installed on that unit, `20002` and `20001` are replaced with the revision of the snap(s) installed on that unit, and `10008` is replaced with the revision of the charm code on that unit

###### Example: Rollback
The user ran `juju refresh` to revision 10008. Units 2 and 1 refreshed from snap revision 20001 to 20002. Then, the user ran `juju refresh` to revision 10007. Unit 2 has rolled back to snap revision 20001.
```
Unit           Workload  Message
postgresql/0*  active    PostgreSQL 14.11 running; Snap revision 20001; Charm revision 10007
postgresql/1   active    PostgreSQL 14.12 running; Snap revision 20002 (outdated); Charm revision 10007
postgresql/2   active    PostgreSQL 14.11 running; Snap revision 20001; Charm revision 10007
```
where `PostgreSQL 14.12` and `PostgreSQL 14.11` are replaced with the name & version of the workload(s) installed on that unit, `20002` and `20001` are replaced with the revision of the snap(s) installed on that unit, and `10007` is replaced with the revision of the charm code on that unit

###### Example: Workload is not running before & during refresh
These statuses are only applicable if the workload would also not be running if there was no refresh in progress.

For example, MySQL Router will only run if its charm is related to a MySQL charm. If a MySQL Router charm—that is not related to a MySQL charm—is refreshed, these statuses would be shown.

Unit 2 has refreshed. Units 1 and 0 have not refreshed.
```
Unit             Workload  Message
mysql-router/0*  waiting   Router 8.0.36; Snap revision 20001 (outdated); Charm revision 10008
mysql-router/1   waiting   Router 8.0.36; Snap revision 20001 (outdated); Charm revision 10008
mysql-router/2   waiting   Router 8.0.37; Snap revision 20002; Charm revision 10008
```
where `Router 8.0.37` and `Router 8.0.36` are replaced with the name & version of the workload(s) installed on that unit, `20002` and `20001` are replaced with the revision of the snap(s) installed on that unit, and `10008` is replaced with the revision of the charm code on that unit

## `force-refresh-start` action
If the refresh is incompatible, the automatic pre-refresh health checks & preparations fail, or the refresh is to a workload version not supported by Canonical, the user will be prompted to rollback. If they accept potential data loss & downtime and want to proceed anyways (e.g. to force a downgrade), the user can run the `force-refresh-start` action on the first unit to refresh.

After `force-refresh-start` is run and the first unit's workload refreshes (machines) or attempts to start (Kubernetes), the compatibility, pre-refresh, and workload support checks will not run again (unless the user runs `juju refresh` [and if `juju refresh` is a rollback, the pre-refresh and workload support checks will still not run again]).

```yaml
# actions.yaml
force-refresh-start:
  description: |
    Potential of data loss and downtime
    
    Force refresh of first unit
    
    Must run with at least one of the parameters `=false`
  params:
    check-compatibility:
      type: boolean
      default: true
      description: |
        Potential of data loss and downtime
        
        If `false`, force refresh if new version of PostgreSQL and/or charm is not compatible with previous version
    run-pre-refresh-checks:
      type: boolean
      default: true
      description: |
        Potential of data loss and downtime
        
        If `false`, force refresh if app is unhealthy or not ready to refresh (and unit status shows "Pre-refresh check failed")
    check-workload-container:
      type: boolean
      default: true
      description: |
        Potential of data loss and downtime during and after refresh
        
        If `false`, allow refresh to PostgreSQL container version that has not been validated to work with the charm revision
  required: []
```
where `PostgreSQL` is replaced with the name of the workload(s)

### If action ran while refresh not in progress
```
Action id 2 failed: No refresh in progress
```

### If action ran on unit other than first unit to refresh
```
Action id 2 failed: Must run action on unit 2
```
where `2` is replaced with the first unit to refresh

### If action ran without 1+ parameters as `false`
```
$ juju run postgresql-k8s/2 force-refresh-start
Running operation 1 with 1 task
  - task 2 on unit-postgresql-k8s-2

Waiting for task 2...
Action id 2 failed: Must run with at least one of `check-compatibility`, `run-pre-refresh-checks`, or `check-workload-container` parameters `=false`
```

### If action ran with 1+ parameters as `false`
#### Part 1: `check-workload-container`
##### If `check-workload-container=true` and check successful
```
$ juju run postgresql-k8s/2 force-refresh-start [...]
Running operation 1 with 1 task
  - task 2 on unit-postgresql-k8s-2

Waiting for task 2...
12:15:34 Checked that refresh is to PostgreSQL container version that has been validated to work with the charm revision
```
where `PostgreSQL` is replaced with the name of the workload(s)

##### (Kubernetes only) If `check-workload-container=true` and check not successful
```
$ juju run postgresql-k8s/2 force-refresh-start [...]
Running operation 1 with 1 task
  - task 2 on unit-postgresql-k8s-2

Waiting for task 2...
Action id 2 failed: Refresh is to PostgreSQL container version that has not been validated to work with the charm revision. Rollback by running `juju refresh postgresql-k8s --revision 10007 --resource postgresql-image=registry.jujucharms.com/charm/kotcfrohea62xreenq1q75n1lyspke0qkurhk/postgresql-image@sha256:76ef26c7d11a524bcac206d5cb042ebc3c8c8ead73fa0cd69d21921552db03b6`
```
where `PostgreSQL` is replaced with the name of the workload(s), `postgresql-k8s` is replaced with the Juju application name, `10007` is replaced with the original charm code revision, `postgresql-image` is replaced with the [OCI resource name](https://juju.is/docs/sdk/charmcraft-yaml#heading--resources), and `registry.jujucharms.com/charm/kotcfrohea62xreenq1q75n1lyspke0qkurhk/postgresql-image@sha256:76ef26c7d11a524bcac206d5cb042ebc3c8c8ead73fa0cd69d21921552db03b6` is replaced with the original workload version

##### If `check-workload-container=false`
```
$ juju run postgresql-k8s/2 force-refresh-start [...] check-workload-container=false
Running operation 1 with 1 task
  - task 2 on unit-postgresql-k8s-2

Waiting for task 2...
12:15:34 Skipping check that refresh is to PostgreSQL container version that has been validated to work with the charm revision
```
where `PostgreSQL` is replaced with the name of the workload(s)

#### Part 2: `check-compatibility`
##### If `check-compatibility=true` and check successful
```
$ juju run postgresql-k8s/2 force-refresh-start [...]
[...]  # check-workload-container
12:15:34 Checked that refresh from previous PostgreSQL version and charm revision to current versions is compatible
```
where `PostgreSQL` is replaced with the name of the workload(s)

##### If `check-compatibility=true` and check not successful
```
$ juju run postgresql-k8s/2 force-refresh-start [...]
[...]  # check-workload-container
```
Kubernetes
```
Action id 2 failed: Refresh incompatible. Rollback by running `juju refresh postgresql-k8s --revision 10007 --resource postgresql-image=registry.jujucharms.com/charm/kotcfrohea62xreenq1q75n1lyspke0qkurhk/postgresql-image@sha256:76ef26c7d11a524bcac206d5cb042ebc3c8c8ead73fa0cd69d21921552db03b6`
```
Machines
```
Action id 2 failed: Refresh incompatible. Rollback with `juju refresh`
```
where `postgresql-k8s` is replaced with the Juju application name, `10007` is replaced with the original charm code revision, `postgresql-image` is replaced with the [OCI resource name](https://juju.is/docs/sdk/charmcraft-yaml#heading--resources), and `registry.jujucharms.com/charm/kotcfrohea62xreenq1q75n1lyspke0qkurhk/postgresql-image@sha256:76ef26c7d11a524bcac206d5cb042ebc3c8c8ead73fa0cd69d21921552db03b6` is replaced with the original workload version

##### If `check-compatibility=false`
```
$ juju run postgresql-k8s/2 force-refresh-start [...] check-compatibility=false
[...]  # check-workload-container
12:15:34 Skipping check for compatibility with previous PostgreSQL version and charm revision
```
where `PostgreSQL` is replaced with the name of the workload(s)

#### Part 3: `run-pre-refresh-checks`
##### If `run-pre-refresh-checks=true` and check successful
```
$ juju run postgresql-k8s/2 force-refresh-start [...]
[...]  # check-workload-container
[...]  # check-compatibility
12:15:34 Running pre-refresh checks
12:15:39 Pre-refresh checks successful
```

##### If `run-pre-refresh-checks=true` and check not successful
```
$ juju run postgresql-k8s/2 force-refresh-start [...]
[...]  # check-workload-container
[...]  # check-compatibility
12:15:34 Running pre-refresh checks
```
Kubernetes
```
Action id 2 failed: Pre-refresh check failed: Backup in progress. Rollback by running `juju refresh postgresql-k8s --revision 10007 --resource postgresql-image=registry.jujucharms.com/charm/kotcfrohea62xreenq1q75n1lyspke0qkurhk/postgresql-image@sha256:76ef26c7d11a524bcac206d5cb042ebc3c8c8ead73fa0cd69d21921552db03b6`
```
Machines
```
Action id 2 failed: Pre-refresh check failed: Backup in progress. Rollback with `juju refresh`
```
where `Backup in progress` is replaced with a message that is specific to the pre-refresh health check or preparation that failed, `postgresql-k8s` is replaced with the Juju application name, `10007` is replaced with the original charm code revision, `postgresql-image` is replaced with the [OCI resource name](https://juju.is/docs/sdk/charmcraft-yaml#heading--resources), and `registry.jujucharms.com/charm/kotcfrohea62xreenq1q75n1lyspke0qkurhk/postgresql-image@sha256:76ef26c7d11a524bcac206d5cb042ebc3c8c8ead73fa0cd69d21921552db03b6` is replaced with the original workload version

##### If `run-pre-refresh-checks=false`
```
$ juju run postgresql-k8s/2 force-refresh-start [...] run-pre-refresh-checks=false
[...]  # check-workload-container
[...]  # check-compatibility
12:15:39 Skipping pre-refresh checks
```

#### Part 4: If all checks that ran were successful (or no checks ran)
```
$ juju run postgresql-k8s/2 force-refresh-start [...]
[...]  # check-workload-container
[...]  # check-compatibility
[...]  # run-pre-refresh-checks
```
Kubernetes
```
12:15:39 PostgreSQL refreshed. Attempting to start PostgreSQL

result: Refreshed unit 2
```
Machines
```
12:15:39 Refreshing unit 2

result: Refreshed unit 2
```
where `PostgreSQL` is replaced with the name of the workload(s) and `2` is replaced with that unit (the first unit to refresh)

## `resume-refresh` action
After the user runs `juju refresh`, if `pause_after_unit_refresh` is set to `all` or `first`, the refresh will pause.

The user is expected to manually check that refreshed units are healthy and that clients connected to the refreshed units are healthy. For example, the user could check that the transactions per second, over a period of several days, are similar on refreshed and non-refreshed units. These manual checks supplement the automatic checks in the charm. (If the automatic checks fail, the charm will pause the refresh regardless of the value of `pause_after_unit_refresh`.)

When the user is ready to continue the refresh, they should run the `resume-refresh` action.

```yaml
# actions.yaml
resume-refresh:
  description: |
    Refresh next unit(s) (after you have manually verified that refreshed units are healthy)
    
    If the `pause_after_unit_refresh` config is set to `all`, this action will refresh the next unit.
    
    If `pause_after_unit_refresh` is set to `first`, this action will refresh all remaining units.
    Exception: if automatic health checks fail after a unit has refreshed, the refresh will pause.
    
    If `pause_after_unit_refresh` is set to `none`, this action will have no effect unless it is called with `check-health-of-refreshed-units` as `false`.
  params:
    check-health-of-refreshed-units:
      type: boolean
      default: true
      description: |
        Potential of data loss and downtime
        
        If `false`, force refresh (of next unit) if 1 or more refreshed units are unhealthy
        
        Warning: if first unit to refresh is unhealthy, consider running `force-refresh-start` action on that unit instead of using this parameter.
        If first unit to refresh is unhealthy because compatibility checks, pre-refresh checks, or workload container checks are failing, this parameter is more destructive than the `force-refresh-start` action.
  required: []
```

The user can also change the value of the `pause_after_unit_refresh` config (e.g. from `all` to `none`) to resume the refresh.

### Which unit the action is run on
#### Kubernetes
On Kubernetes, the user should run `resume-refresh` on the leader unit.

If the StatefulSet partition is lowered and then quickly raised, the Juju agent may hang. This is a Juju bug: https://bugs.launchpad.net/juju/+bug/2073473. To avoid a race condition, only the leader unit lowers the partition. (If that bug were resolved, the `resume-refresh` action could be run on any unit.)

To improve the robustness of rollbacks, `resume-refresh` runs on the leader unit instead of the next unit to refresh. If a unit is refreshed to an incorrect or buggy charm code version, its charm code may raise an uncaught exception and may not be able to process the `resume-refresh` action to rollback its unit. (The improvement in robustness comes from `resume-refresh` running on a unit that is different from the unit that needs to rollback.) This is different from machines, where the charm code is rolled back separately from the workload and the charm code on a unit needs to run to rollback the workload (i.e. snap) for that unit.

If the charm code on the leader unit raises an uncaught exception, the user can manually patch (e.g. using kubectl) the StatefulSet partition to rollback the leader unit (after `juju refresh` has been run to start the rollback). From the perspective of the refresh design, if the user is instructed properly, this is safe (since it uses the same mechanism as a normal rollback). However, any rollback has risk and there may be additional risk if the leader unit did something (e.g. modified a relation databag in a previous Juju event) before it raised an uncaught exception.

#### Machines
On machines, the user should run `resume-refresh` on the next unit to refresh. This unit is shown in the app status.

This improves the robustness of rollbacks by requiring only the charm code on the unit that is rolling back to be healthy (i.e. not raising an uncaught exception). (If the action was run on the leader unit, rolling back a unit would require the charm code on both the leader unit & the unit rolling back to be healthy.)

If `check-health-of-refreshed-units=true` (default), a unit rolling back will also check that units that have already rolled back are healthy.

In case a refreshed unit is unhealthy and the user wants to force the refresh to continue, `check-health-of-refreshed-units=false` allows the user to run this action on any unit that is not up-to-date—so that they can skip over the unhealthy unit. However, the user should be instructed to follow the refresh order (usually highest to lowest unit number) even though they have the power to refresh any unit that is not up-to-date.

### If action ran while refresh not in progress
```
Action id 2 failed: No refresh in progress
```

### If action ran on incorrect unit
#### Kubernetes
```
Action id 2 failed: Must run action on leader unit. (e.g. `juju run postgresql-k8s/leader resume-refresh`)
```
where `postgresql-k8s` is replaced with the Juju application name

#### Machines
##### If action ran with `check-health-of-refreshed-units=true`
```
Action id 2 failed: Must run action on unit 1
```
where `1` is replaced with the next unit to refresh

##### If action ran with `check-health-of-refreshed-units=false` and unit already up-to-date
```
Action id 2 failed: Unit already refreshed
```

### If action ran with `check-health-of-refreshed-units=true`
#### If `pause_after_unit_refresh` is `none`
```
Action id 2 failed: `pause_after_unit_refresh` config is set to `none`. This action is not applicable.
```

#### (Machines only) If first unit has not refreshed
(Refresh is incompatible or automatic pre-refresh health checks & preparations failed)
```
Action id 2 failed: Unit 2 is unhealthy. Refresh will not resume.
```
where `2` is replaced with the first unit to refresh

#### If 1 or more refreshed units are unhealthy
```
Action id 2 failed: Unit 2 is unhealthy. Refresh will not resume.
```
where `2` is replaced with the first refreshed unit that is unhealthy

#### If refresh is successfully resumed
##### If `pause_after_unit_refresh` is `first`
###### Kubernetes
```
result: Refresh resumed. Unit 1 is refreshing next
```
where `1` is replaced with the unit that is refreshing
###### Machines
```
12:15:39 Refresh resumed. Refreshing unit 1

result: Refresh resumed. Unit 1 has refreshed
```
where `1` is replaced with the unit that is refreshing (the unit the action ran on)

##### If `pause_after_unit_refresh` is `all`
###### Kubernetes
```
result: Unit 1 is refreshing next
```
where `1` is replaced with the unit that is refreshing
###### Machines
```
12:15:39 Refreshing unit 1

result: Refreshed unit 1
```
where `1` is replaced with the unit that is refreshing (the unit the action ran on)

### If action ran with `check-health-of-refreshed-units=false` and refresh is successfully resumed
#### Kubernetes
```
12:15:39 Ignoring health of refreshed units

result: Attempting to refresh unit 1
```
where `1` is replaced with the unit that is refreshing

"Attempting to" is included because on Kubernetes we only control the partition, not which units refresh. Kubernetes may not refresh a unit even if the partition allows it (e.g. if the charm container of a higher unit is not ready).
#### Machines
```
12:15:39 Ignoring health of refreshed units
12:15:39 Refreshing unit 1

result: Refreshed unit 1
```
where `1` is replaced with the unit that is refreshing (the unit the action ran on)
