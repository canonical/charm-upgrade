import abc
import collections.abc
import dataclasses
import enum
import functools
import json
import logging
import pathlib
import platform
import typing

import charm
import charm_json
import httpx
import lightkube
import lightkube.models.authorization_v1
import lightkube.resources.apps_v1
import lightkube.resources.authorization_v1
import lightkube.resources.core_v1
import ops
import packaging.version
import tomli
import yaml

logger = logging.getLogger(__name__)


class Cloud(enum.Enum):
    """Cloud that a charm is deployed in

    https://juju.is/docs/juju/cloud#heading--machine-clouds-vs--kubernetes-clouds
    """

    KUBERNETES = enum.auto()
    MACHINES = enum.auto()


@functools.total_ordering
class CharmVersion:
    """Charm code version

    Stored as a git tag on charm repositories

    TODO: link to docs about versioning spec
    """

    def __init__(self, version: str, /):
        # Example 1: "14/1.12.0"
        # Example 2: "14/1.12.0.post1.dev0+71201f4.dirty"
        self._version = version
        track, pep440_version = self._version.split("/")
        # Example 1: "14"
        self.track = track
        """Charmhub track"""

        if "!" in pep440_version:
            raise ValueError(
                f'Invalid charm version "{self}". PEP 440 epoch ("!" character) not supported'
            )
        try:
            self._pep440_version = packaging.version.Version(pep440_version)
        except packaging.version.InvalidVersion:
            raise ValueError(f'Invalid charm version "{self}"')
        if len(self._pep440_version.release) != 3:
            raise ValueError(
                f'Invalid charm version "{self}". Expected 3 number components after track; got {len(self._pep440_version.release)} components instead: "{self._pep440_version.base_version}"'
            )
        # Example 1: True
        # Example 2: False
        self.released = pep440_version == self._pep440_version.base_version
        """Whether version was released & correctly tagged

        `True` for charm code correctly released to Charmhub
        `False` for development builds
        """

        # Example 1: 1
        self.major = self._pep440_version.release[0]
        """Incremented if refresh not supported or only supported with intermediate charm version

        If a change is made to the charm code that causes refreshes to not be supported or to only
        be supported with the use of an intermediate charm version, this number is incremented.

        If this number is equivalent on two charm code versions with equivalent tracks, refreshing
        from the lower to higher charm code version is supported without the use of an intermediate
        charm version.
        """
        # TODO: add info about intermediate charms & link to docs about versioning spec

    def __str__(self):
        return self._version

    def __repr__(self):
        return f'{type(self).__name__}("{self}")'

    def __eq__(self, other):
        if isinstance(other, str):
            return str(self) == other
        return isinstance(other, CharmVersion) and self._version == other._version

    def __gt__(self, other):
        if not isinstance(other, CharmVersion):
            return NotImplemented
        if self.track != other.track:
            raise ValueError(
                f'Unable to compare versions with different tracks: "{self.track}" and "{other.track}" ("{self}" and "{other}")'
            )
        return self._pep440_version > other._pep440_version


class PrecheckFailed(Exception):
    """Pre-refresh health check or preparation failed"""

    def __init__(self, message: str, /):
        """Pre-refresh health check or preparation failed

        Include a short, descriptive message that explains to the user which health check or
        preparation failed. For example: "Backup in progress"

        The message will be shown to the user in the output of `juju status`, refresh actions,
        and `juju debug-log`.

        Messages longer than 64 characters will be truncated in the output of `juju status`.
        It is recommended that messages are <= 64 characters.

        Do not mention "pre-refresh check" or prompt the user to rollback in the message—that
        information will already be included alongside the message.
        """
        if len(message) == 0:
            raise ValueError(f"{type(self).__name__} message must be longer than 0 characters")
        self.message = message
        super().__init__(message)


@dataclasses.dataclass(eq=False)
class CharmSpecific(abc.ABC):
    """Charm-specific callbacks & configuration for in-place refreshes"""

    cloud: Cloud
    """Cloud that the charm is deployed in

    https://juju.is/docs/juju/cloud#heading--machine-clouds-vs--kubernetes-clouds
    """

    workload_name: str
    """Human readable workload name (e.g. PostgreSQL)"""

    refresh_user_docs_url: str
    """Link to charm's in-place refresh user documentation

    (e.g. https://charmhub.io/postgresql-k8s/docs/h-upgrade-intro)

    Displayed to user in output of `pre-refresh-check` action
    """
    # TODO: add note about link in old version of charm & keeping evergreen

    oci_resource_name: typing.Optional[str] = None
    """Resource name for workload OCI image in metadata.yaml `resources`

    (e.g. postgresql-image)

    Required if `cloud` is `Cloud.KUBERNETES`

    https://juju.is/docs/sdk/metadata-yaml#heading--resources
    """

    # TODO: add note about upstream-source for pinning?
    # TODO: add note about `containers` assumed in metadata.yaml (to find container name)

    def __post_init__(self):
        """Validate values of dataclass fields

        Subclasses should not override these validations
        """
        # TODO: validate length of workload_name?
        if self.cloud is Cloud.KUBERNETES:
            if self.oci_resource_name is None:
                raise ValueError(
                    "`oci_resource_name` argument is required if `cloud` is `Cloud.KUBERNETES`"
                )
        elif self.oci_resource_name is not None:
            raise ValueError(
                "`oci_resource_name` argument is only allowed if `cloud` is `Cloud.KUBERNETES`"
            )

    @staticmethod
    @abc.abstractmethod
    def run_pre_refresh_checks_after_1_unit_refreshed() -> None:
        """Run pre-refresh health checks & preparations after the first unit has already refreshed.

        There are three situations in which the pre-refresh health checks & preparations run:

        1. When the user runs the `pre-refresh-check` action on the leader unit before the refresh
           starts
        2. On machines, after `juju refresh` and before any unit is refreshed, the highest number
           unit automatically runs the checks & preparations
        3. On Kubernetes; after `juju refresh`, after the highest number unit refreshes, and before
           the highest number unit starts its workload; the highest number unit automatically runs
           the checks & preparations

        Note that:

        - In situation #1 the checks & preparations run on the old charm code and in situations #2
          and #3 they run on the new charm code
        - In situations #2 and #3, the checks & preparations run on a unit that may or may not be
          the leader unit
        - In situation #3, the highest number unit's workload is offline
        - Before the refresh starts, situation #1 is not guaranteed to happen
        - Situation #2 or #3 (depending on machines or Kubernetes) will happen regardless of
          whether the user ran the `pre-refresh-check` action
        - In situations #2 and #3, if the user scales up or down the application before all checks
          & preparations are successful, the checks & preparations will run on the new highest
          number unit.
          If the user scaled up the application:
              - In situation #3, multiple units' workloads will be offline
              - In situation #2, the new units may install the new snap version before the checks &
                preparations succeed
        - In situations #2 and #3, if the user scales up the application after all checks &
          preparations succeeded, the checks & preparations will not run again. If they scale down
          the application, the checks & preparations may run again

        This method is called in situation #3.

        If possible, pre-refresh checks & preparations should be written to support all 3
        situations.

        If a pre-refresh check/preparation supports all 3 situations, it should be placed in this
        method and called by the `run_pre_refresh_checks_before_any_units_refreshed` method.

        Otherwise, if it does not support situation #3 but does support situations #1 and #2, it
        should be placed in the `run_pre_refresh_checks_before_any_units_refreshed` method.

        By default, all checks & preparations in this method will also be run in the
        `run_pre_refresh_checks_before_any_units_refreshed` method.

        Checks & preparations are run sequentially. Therefore, it is recommended that:

        - Checks (e.g. backup created) should be run before preparations (e.g. switch primary)
        - More critical checks should be run before less critical checks
        - Less impactful preparations should be run before more impactful preparations

        However, if any checks or preparations fail and the user runs the `force-refresh-start`
        action with `run-pre-refresh-checks=false`, the remaining checks & preparations will be
        skipped—this may impact how you decide to order the checks & preparations.

        If a check or preparation fails, raise the `PrecheckFailed` exception. All of the checks &
        preparations may be run again on the next Juju event.

        If all checks & preparations are successful, they will not run again unless the user runs
        `juju refresh`. Exception: they may run again if the user scales down the application.

        Checks & preparations will not run during a rollback.

        Raises:
            PrecheckFailed: If a pre-refresh health check or preparation fails
        """

    def run_pre_refresh_checks_before_any_units_refreshed(self) -> None:
        """Run pre-refresh health checks & preparations before any unit is refreshed.

        There are three situations in which the pre-refresh health checks & preparations run:

        1. When the user runs the `pre-refresh-check` action on the leader unit before the refresh
           starts
        2. On machines, after `juju refresh` and before any unit is refreshed, the highest number
           unit automatically runs the checks & preparations
        3. On Kubernetes; after `juju refresh`, after the highest number unit refreshes, and before
           the highest number unit starts its workload; the highest number unit automatically runs
           the checks & preparations

        Note that:

        - In situation #1 the checks & preparations run on the old charm code and in situations #2
          and #3 they run on the new charm code
        - In situations #2 and #3, the checks & preparations run on a unit that may or may not be
          the leader unit
        - In situation #3, the highest number unit's workload is offline
        - Before the refresh starts, situation #1 is not guaranteed to happen
        - Situation #2 or #3 (depending on machines or Kubernetes) will happen regardless of
          whether the user ran the `pre-refresh-check` action
        - In situations #2 and #3, if the user scales up or down the application before all checks
          & preparations are successful, the checks & preparations will run on the new highest
          number unit.
          If the user scaled up the application:
              - In situation #3, multiple units' workloads will be offline
              - In situation #2, the new units may install the new snap version before the checks &
                preparations succeed
        - In situations #2 and #3, if the user scales up the application after all checks &
          preparations succeeded, the checks & preparations will not run again. If they scale down
          the application, the checks & preparations may run again

        This method is called in situations #1 and #2.

        If possible, pre-refresh checks & preparations should be written to support all 3
        situations.

        If a pre-refresh check/preparation supports all 3 situations, it should be placed in the
        `run_pre_refresh_checks_after_1_unit_refreshed` method and called by this method.

        Otherwise, if it does not support situation #3 but does support situations #1 and #2, it
        should be placed in this method.

        By default, all checks & preparations in the
        `run_pre_refresh_checks_after_1_unit_refreshed` method will also be run in this method.

        Checks & preparations are run sequentially. Therefore, it is recommended that:

        - Checks (e.g. backup created) should be run before preparations (e.g. switch primary)
        - More critical checks should be run before less critical checks
        - Less impactful preparations should be run before more impactful preparations

        However, if any checks or preparations fail and the user runs the `force-refresh-start`
        action with `run-pre-refresh-checks=false`, the remaining checks & preparations will be
        skipped—this may impact how you decide to order the checks & preparations.

        If a check or preparation fails, raise the `PrecheckFailed` exception. All of the checks &
        preparations may be run again on the next Juju event.

        If all checks & preparations are successful, they will not run again unless the user runs
        `juju refresh`. Exception: they may run again if the user scales down the application.

        Checks & preparations will not run during a rollback.

        Raises:
            PrecheckFailed: If a pre-refresh health check or preparation fails
        """
        self.run_pre_refresh_checks_after_1_unit_refreshed()

    def refresh_snap(self, *, snap_revision: str, refresh: "Refresh") -> None:
        """Refresh workload snap

        `refresh.update_snap_revision()` must be called immediately after the snap is refreshed.

        This method should:

        1. Gracefully stop the workload, if it is running
        2. Refresh the snap
        3. Immediately call `refresh.update_snap_revision()`

        Then, this method should attempt to:

        4. Start the workload
        5. Check if the application and this unit are healthy
        6. If they are both healthy, set `refresh.next_unit_allowed_to_refresh = True`

        If the snap is not refreshed, this method will be called again on the next Juju event—if
        this unit is still supposed to be refreshed.

        Note: if this method was run because the user ran the `resume-refresh` action, this method
        will not be called again even if the snap is not refreshed unless the user runs the action
        again.

        If the workload is successfully stopped (step #1) but refreshing the snap (step #2) fails
        (i.e. the snap revision has not changed), consider starting the workload (in the same Juju
        event). If refreshing the snap fails, retrying in a future Juju event is not recommended
        since the user may decide to rollback. If the user does not decide to rollback, this method
        will be called again on the next Juju event—except in the `resume-refresh` action case
        mentioned above.

        If the snap is successfully refreshed (step #2), this method will not be called again
        (unless the user runs `juju refresh` to a different snap revision).

        Therefore, if `refresh.next_unit_allowed_to_refresh` is not set to `True` (step #6)
        (because starting the workload [step #4] failed, checking if the application and this unit
        were healthy [step #5] failed, either the application or unit was unhealthy in step #5, or
        the charm code raised an uncaught exception later in the same Juju event), then the charm
        code should retry steps #4-#6, as applicable, in future Juju events until
        `refresh.next_unit_allowed_to_refresh` is set to `True` and an uncaught exception is not
        raised by the charm code later in the same Juju event.

        Also, if step #5 fails or if either the application or this unit is unhealthy, the charm
        code should set a unit status to indicate what is unhealthy.

        Implementation of this method is required in subclass if `cloud` is `Cloud.MACHINES`
        """
        if self.cloud is not Cloud.MACHINES:
            raise ValueError("`refresh_snap` can only be called if `cloud` is `Cloud.MACHINES`")

    @staticmethod
    def _is_charm_version_compatible(*, old: CharmVersion, new: CharmVersion):
        """Check that new charm version is higher than old and that major versions are identical

        TODO talk about intermediate charms

        TODO talk about recommendation to not support charm code downgrade
        """
        # TODO implementation: add logging
        if not (old.released and new.released):
            # Unreleased charms contain changes that do not affect the version number
            # Those changes could affect compatability
            return False
        if old.major != new.major:
            return False
        # By default, charm code downgrades are not supported (rollbacks are supported)
        return new >= old

    @classmethod
    @abc.abstractmethod
    def is_compatible(
        cls,
        *,
        old_charm_version: CharmVersion,
        new_charm_version: CharmVersion,
        old_workload_version: str,
        new_workload_version: str,
    ) -> bool:
        """Whether refresh is supported from old to new workload and charm code versions

        This method is called using the new charm code version.

        On Kubernetes, this method runs before the highest number unit starts the new workload
        version.
        On machines, this method runs before any unit is refreshed.

        If this method returns `False`, the refresh will be blocked and the user will be prompted
        to rollback.

        The user can override that block using the `force-refresh-start` action with
        `check-compatibility=false`.

        In order to support rollbacks, this method should always return `True` if the old and new
        charm code versions are identical and the old and new workload versions are identical.

        This method should not use any information beyond its parameters to determine if the
        refresh is compatible.
        """
        if not cls._is_charm_version_compatible(old=old_charm_version, new=new_charm_version):
            return False
        return True


class PeerRelationMissing(Exception):
    """Refresh peer relation is not yet available"""


@dataclasses.dataclass(frozen=True)
class _HistoryEntry:
    charm_revision: str
    """Contents of .juju-charm file (e.g. "ch:amd64/jammy/postgresql-k8s-381")"""

    time_of_refresh: float
    """Modified time of .juju-charm file after last refresh (e.g. 1727768259.4063382)"""


_LOCAL_STATE = pathlib.Path(".charm_refresh_v3")
"""Local state for this unit

On Kubernetes, deleted when pod is deleted
This directory is stored in /var/lib/juju/ on the charm container
(e.g. in /var/lib/juju/agents/unit-postgresql-k8s-0/charm/)
As of Juju 3.5.3, /var/lib/juju/ is stored in a Kubernetes emptyDir volume
https://kubernetes.io/docs/concepts/storage/volumes/#emptydir
This means that it will not be deleted on container restart—it will only be deleted if the pod is
deleted
"""


@dataclasses.dataclass
class _MachineRefreshHistory:
    # TODO comment?
    last_refresh_started_on_this_unit: bool
    last_refresh: typing.Optional[_HistoryEntry]
    last_refresh_to_up_to_date_charm_revision: typing.Optional[_HistoryEntry]
    second_to_last_refresh_to_up_to_date_charm_revision: typing.Optional[_HistoryEntry]

    _PATH = _LOCAL_STATE / "machines_refresh_history.json"

    @classmethod
    def from_file(cls):
        try:
            data: typing.Dict[str, typing.Union[bool, dict, None]] = json.loads(
                cls._PATH.read_text()
            )
        except FileNotFoundError:
            return cls(
                last_refresh_started_on_this_unit=False,
                last_refresh=None,
                last_refresh_to_up_to_date_charm_revision=None,
                second_to_last_refresh_to_up_to_date_charm_revision=None,
            )
        data2 = {}
        for key, value in data.items():
            if isinstance(value, dict):
                value = _HistoryEntry(**value)
            data2[key] = value
        return cls(**data2)

    def save_to_file(self):
        self._PATH.write_text(json.dumps(dataclasses.asdict(self), indent=4))


@functools.total_ordering
class _PauseAfter(str, enum.Enum):
    """`pause_after_unit_refresh` config option"""

    NONE = "none"
    FIRST = "first"
    ALL = "all"
    UNKNOWN = "unknown"

    @classmethod
    def _missing_(cls, value):
        return cls.UNKNOWN

    def __gt__(self, other):
        if not isinstance(other, type(self)):
            raise TypeError
        priorities = {self.NONE: 0, self.FIRST: 1, self.ALL: 2, self.UNKNOWN: 3}
        return priorities[self] > priorities[other]


class _InProgress(enum.Enum):
    FALSE = 0
    TRUE = 1
    UNKNOWN = 2


class _InvalidForceEvent(ValueError):
    """Event is not valid force-refresh-start action event"""


class _ForceRefreshStartAction(charm.ActionEvent):
    def __init__(
        self, event: charm.Event, *, first_unit_to_refresh: charm.Unit, in_progress: _InProgress
    ):
        if not isinstance(event, charm.ActionEvent):
            raise _InvalidForceEvent
        super().__init__()
        if event.action != "force-refresh-start":
            raise _InvalidForceEvent
        if charm.unit != first_unit_to_refresh:
            event.fail(f"Must run action on unit {first_unit_to_refresh.number}")
            raise _InvalidForceEvent
        if in_progress is not _InProgress.TRUE:
            event.fail("No refresh in progress")
            raise _InvalidForceEvent
        self.check_workload_container: bool = event.parameters["check-workload-container"]
        self.check_compatibility: bool = event.parameters["check-compatibility"]
        self.run_pre_refresh_checks: bool = event.parameters["run-pre-refresh-checks"]
        for parameter in (
            self.check_workload_container,
            self.check_compatibility,
            self.run_pre_refresh_checks,
        ):
            if parameter is False:
                break
        else:
            event.fail(
                "Must run with at least one of `check-compatibility`, `run-pre-refresh-checks`, or `check-workload-container` parameters `=false`"
            )
            raise _InvalidForceEvent


class _InvalidResumeEvent(ValueError):
    """Event is not valid resume-refresh action event"""


class _ResumeRefreshAction(charm.ActionEvent):
    def __init__(self, event: charm.Event, *, in_progress: _InProgress):
        if not isinstance(event, charm.ActionEvent):
            raise _InvalidResumeEvent
        super().__init__()
        if event.action != "resume-refresh":
            raise _InvalidResumeEvent
        if in_progress is _InProgress.FALSE:
            # TODO if unknown?
            event.fail("No refresh in progress")
        self.check_health_of_refreshed_units: bool = event.parameters[
            "check-health-of-refreshed-units"
        ]
        # TODO verify which unit (I think done)


def convert_to_ops_status(status: charm.Status) -> ops.StatusBase:
    ops_types = {
        charm.ActiveStatus: ops.ActiveStatus,
        charm.WaitingStatus: ops.WaitingStatus,
        charm.MaintenanceStatus: ops.MaintenanceStatus,
        charm.BlockedStatus: ops.BlockedStatus,
    }
    for charm_type, ops_type in ops_types.items():
        if isinstance(status, charm_type):
            return ops_type(str(status))
    raise ValueError(f"Unknown type '{type(status).__name__}': {repr(status)}")


class Refresh:
    # TODO: add note about putting at end of charm __init__

    @property
    def in_progress(self) -> bool:
        """Whether a refresh is currently in progress"""
        return self._refresh.in_progress
        # if self._in_progress is _InProgress.TRUE or self._in_progress is _InProgress.UNKNOWN:
        #     return True
        # return False

    @property
    def next_unit_allowed_to_refresh(self) -> bool:
        """Whether the next unit is allowed to refresh

        After this unit refreshes, the charm code should check if the application and this unit are
        healthy. If they are healthy, this attribute should be set to `True` to allow the refresh
        to proceed on the next unit.

        Otherwise (if either is unhealthy or if it is not possible to determine that both are
        healthy), the charm code should (in future Juju events) continue to retry the health checks
        and set this attribute to `True` when both are healthy. In this Juju event, the charm code
        should also set a unit status to indicate what is unhealthy.

        If the charm code raises an uncaught exception in the same Juju event where this attribute
        is set to `True`, it will not be saved. In the next Juju events, the charm code should
        retry the health checks until this attribute is set to `True` in a Juju event where an
        uncaught exception is not raised by the charm code.

        This attribute can only be set to `True`. When the unit is refreshed, this attribute will
        automatically be reset to `False`.

        This attribute should only be read to determine if the health checks need to be run again
        so that this attribute can be set to `True`.

        Note: this has no connection to the `pause_after_unit_refresh` user configuration option.
        That user configuration option corresponds to manual checks performed by the user after the
        automatic checks are successful. This attribute is set to `True` when the automatic checks
        succeed. For example:

        - If this attribute is set to `True` and `pause_after_unit_refresh` is set to "all", the
          next unit will not refresh until the user runs the `resume-refresh` action.
        - If `pause_after_unit_refresh` is set to "none" and this attribute is not set to `True`,
          the next unit will not refresh until this attribute is set to `True`.

        The user can override failing automatic health checks by running the `resume-refresh`
        action with `check-health-of-refreshed-units=false`.
        """
        return self._refresh.next_unit_allowed_to_refresh
        # if self.charm_specific.cloud is Cloud.KUBERNETES:
        #     pass  # TODO
        # elif self.charm_specific.cloud is Cloud.MACHINES:
        #     value = self._relation.my_unit.get(
        #         "next_unit_allowed_to_refresh_if_this_units_snap_revision_and_charm_revision_and_databag_are_up_to_date"
        #     )
        #     if value is None:
        #         return False
        #     return json.loads(value)

    @next_unit_allowed_to_refresh.setter
    def next_unit_allowed_to_refresh(self, value: typing.Literal[True]):
        self._refresh.next_unit_allowed_to_refresh = value

        # TODO: reconcile status?
        # if value is not True:
        #     raise ValueError("`next_unit_allowed_to_refresh` can only be set to `True`")
        # if self.charm_specific.cloud is Cloud.KUBERNETES:
        #     pass  # TODO leftoff here
        #     self._relation.my_unit[
        #         "next_unit_allowed_to_refresh_if_app_controller_revision_hash_equals"
        #     ] = self._app_controller_revision
        # elif self.charm_specific.cloud is Cloud.MACHINES:
        #     if self._relation.my_unit.get("snap_revision") != self._get_installed_snap_revision():
        #         raise Exception(
        #             "Must call `update_snap_revision()` before setting `next_unit_allowed_to_refresh = True`"
        #         )
        #     self._relation.my_unit[
        #         "next_unit_allowed_to_refresh_if_this_units_snap_revision_and_charm_revision_and_databag_are_up_to_date"
        #     ] = json.dumps(True)

    def update_snap_revision(self):
        """Must be called immediately after the workload snap is refreshed

        Only applicable if cloud is `Cloud.MACHINES`

        If the charm code raises an uncaught exception in the same Juju event where this method is
        called, this method does not need to be called again. (That situation will be automatically
        handled.)

        Resets `next_unit_allowed_to_refresh` to `False`.
        """
        raise NotImplementedError
        # if self.charm_specific.cloud is not Cloud.MACHINES:
        #     raise ValueError(
        #         "`update_snap_revision` can only be called if cloud is `Cloud.MACHINES`"
        #     )
        # revision = self._get_installed_snap_revision()
        # if revision != self._relation.my_unit.get("snap_revision"):
        #     self._relation.my_unit[
        #         "next_unit_allowed_to_refresh_if_this_units_snap_revision_and_charm_revision_and_databag_are_up_to_date"
        #     ] = json.dumps(False)
        #     self._relation.my_unit["snap_revision"] = revision
        #     if self._force_start:
        #         self._force_start.result = {"result": f"Refreshed unit {charm.unit.number}"}
        #     if self._resume_refresh:
        #         if (
        #             self._resume_refresh.check_health_of_refreshed_units is True
        #             and self._pause_after is _PauseAfter.FIRST
        #         ):
        #             result = f"Refresh resumed. Unit {charm.unit.number} has refreshed"
        #         else:
        #             result = f"Refreshed unit {charm.unit.number}"
        #         self._resume_refresh.result = {"result": result}

    @property
    def pinned_snap_revision(self) -> str:
        # TODO: move to CharmSpecific so it can be accessed during install event where refresh peer relation might be missing?
        """Workload snap revision pinned by this unit's current charm code

        This attribute should only be read during initial snap installation and should not be read
        during a refresh.

        During a refresh, the snap revision should be read from the `refresh_snap` method's
        `snap_revision` parameter.
        """
        raise NotImplementedError
        # if self.charm_specific.cloud is not Cloud.MACHINES:
        #     raise ValueError(
        #         "`pinned_snap_revision` can only be accessed if cloud is `Cloud.MACHINES`"
        #     )
        # # TODO: raise exception if accessed while self.in_progress—but scale up case
        # return self._pinned_snap_revision

    @property
    def workload_allowed_to_start(self) -> bool:
        """Whether this unit's workload is allowed to start

        Only applicable if cloud is `Cloud.KUBERNETES`

        On Kubernetes, the automatic checks (

        - that OCI image hash matches pin in charm code
        - that refresh is compatible from old to new workload and charm code versions
        - pre-refresh health checks & preparations

        ) run after the highest number unit is refreshed but before the highest number unit starts
        its workload.

        After a unit is refreshed, the charm code must check the value of this attribute to
        determine if the workload can be started.

        Note: the charm code should check this attribute for all units (not just the highest unit
        number) in case the user scales up the application during the refresh.

        After a unit is refreshed, the charm code should:

        1. Check the value of this attribute. If it is `True`, continue to step #2
        2. Start the workload
        3. Check if the application and this unit are healthy
        4. If they are both healthy, set `next_unit_allowed_to_refresh = True`

        If `next_unit_allowed_to_refresh` is not set to `True` (because the value of this attribute
        [step #1] was `False`, starting the workload [step #2] failed, checking if the application
        and this unit were healthy [step #3] failed, either the application or unit was unhealthy
        in step #3, or the charm code raised an uncaught exception later in the same Juju event),
        then the charm code should retry these steps, as applicable, in future Juju events until
        `next_unit_allowed_to_refresh` is set to `True` and an uncaught exception is not raised by
        the charm code later in the same Juju event.

        Also, if step #3 fails or if either the application or this unit is unhealthy, the charm
        code should set a unit status to indicate what is unhealthy.

        If the user skips the automatic checks by running the `force-refresh-start` action, the
        value of this attribute will be `True`.
        """
        return self._refresh.workload_allowed_to_start
        # if self.charm_specific.cloud is not Cloud.KUBERNETES:
        #     return True
        # return self._in_progress is _InProgress.FALSE or self._workload_allowed_to_start

    @property
    def app_status_higher_priority(self) -> typing.Optional[ops.StatusBase]:
        """App status with higher priority than any other app status in the charm

        Charm code should ensure that this status is not overriden
        """
        status = self._refresh.app_status_higher_priority
        if status:
            status = convert_to_ops_status(status)
        return status
        # if self._app_status_higher_priority is None:
        #     return
        # return convert_to_ops_status(self._app_status_higher_priority)

    @property
    def unit_status_higher_priority(self) -> typing.Optional[ops.StatusBase]:
        """Unit status with higher priority than any other unit status in the charm

        Charm code should ensure that this status is not overriden
        """
        status = self._refresh.unit_status_higher_priority
        if status:
            status = convert_to_ops_status(status)
        return status
        # if self._unit_status_higher_priority is None:
        #     return
        # return convert_to_ops_status(self._unit_status_higher_priority)

    @property
    def unit_status_lower_priority(self) -> typing.Optional[ops.StatusBase]:
        """Unit status with lower priority than any other unit status with a message in the charm

        This status will not be automatically set. It should be set by the charm code if there is
        no other unit status with a message to display.
        """
        status = self._refresh.unit_status_lower_priority
        if status:
            status = convert_to_ops_status(status)
        return status

    # def _determine_app_status_higher_priority(self) -> typing.Optional[charm.Status]:
    #     # TODO: add status for k8s trust missing
    #     if self._pause_after is _PauseAfter.UNKNOWN:
    #         return charm.BlockedStatus(
    #             'pause_after_unit_refresh config must be set to "all", "first", or "none"'
    #         )
    #     if (
    #         self._in_progress is not _InProgress.FALSE
    #         and self.charm_specific.cloud is Cloud.MACHINES
    #         and self._incompatible_app_status
    #     ):
    #         return self._incompatible_app_status
    #
    # def _set_status(self):
    #     # TODO comment about charm being responsible for clearing status and for setting lower priority status
    #     if self._unit_status_higher_priority:
    #         charm.unit_status = self._unit_status_higher_priority
    #     if charm.is_leader:
    #         self._app_status_higher_priority = self._determine_app_status_higher_priority()
    #         if self._app_status_higher_priority:
    #             charm.app_status = self._app_status_higher_priority
    #
    # def _get_installed_snap_revision(self):
    #     # TODO: error handling if refresh_versions.toml incorrectly formatted
    #     snap_name = self._refresh_versions["snap"]["name"]
    #     # https://snapcraft.io/docs/using-the-api
    #     client = httpx.Client(transport=httpx.HTTPTransport(uds="/run/snapd.socket"))
    #     # https://snapcraft.io/docs/snapd-rest-api#heading--snaps
    #     response = client.get(
    #         "http://localhost/v2/snaps", params={"snaps": snap_name}
    #     ).raise_for_status()
    #     data = response.json()
    #     assert data["type"] == "sync"
    #     snaps = data["result"]
    #     assert len(snaps) == 1
    #     revision = snaps[0]["revision"]
    #     assert isinstance(revision, str)
    #     return revision
    #
    # def _is_in_progress(self) -> _InProgress:
    #     """Check if refresh in progress"""
    #     if self.charm_specific.cloud is Cloud.KUBERNETES:
    #         for revision in self._unit_controller_revisions.values():
    #             if revision != self._app_controller_revision:
    #                 return _InProgress.TRUE
    #         return _InProgress.FALSE
    #     elif self.charm_specific.cloud is Cloud.MACHINES:
    #         if self._get_installed_snap_revision() != self._pinned_snap_revision:
    #             return _InProgress.TRUE
    #         units_with_up_to_date_snap_revision = []
    #         for databag in self._relation.other_units.values():
    #             if databag.get("snap_revision") is None:
    #                 # TODO comment scale up/initial install case
    #                 continue
    #             if databag["snap_revision"] != self._pinned_snap_revision:
    #                 return _InProgress.TRUE
    #             units_with_up_to_date_snap_revision.append(databag)
    #         for databag in units_with_up_to_date_snap_revision:
    #             other_unit = databag.get("last_refresh_to_up_to_date_charm_revision")
    #             if other_unit is None:
    #                 # TODO comment scale up/initial install case??
    #                 continue
    #             other_unit = _HistoryEntry(**json.loads(other_unit))
    #             if other_unit.charm_revision != self._installed_charm_revision_raw:
    #                 # TODO comment
    #                 return _InProgress.UNKNOWN
    #             if (
    #                 self._history.second_to_last_refresh_to_up_to_date_charm_revision
    #                 and other_unit.time_of_refresh
    #                 < self._history.second_to_last_refresh_to_up_to_date_charm_revision.time_of_refresh
    #             ):
    #                 # TODO comment
    #                 # other unit databag is outdated
    #                 return _InProgress.UNKNOWN
    #         return _InProgress.FALSE
    #     else:
    #         raise TypeError
    #
    # def _can_refresh_start(self) -> bool:
    #     # Run automatic checks
    #     # TODO save status messages—return more than bool? I think done
    #     # TODO fstring variables
    #     if self._force_start and self._force_start.check_compatibility is False:
    #         self._force_start.log(
    #             f"Skipping check that refresh is to {self.charm_specific.workload_name} container version that has been validated to work with the charm revision"
    #         )
    #     else:
    #         # Check workload container
    #         # TODO return false if fails & set status & logs in UX spec
    #         # TODO
    #         if self.charm_specific.cloud is Cloud.KUBERNETES:
    #             pass
    #     if self._force_start and self._force_start.check_compatibility is False:
    #         self._force_start.log(
    #             f"Skipping check for compatibility with previous {self.charm_specific.workload_name} version and charm revision"
    #         )
    #     else:
    #         # Check compatibility
    #         # TODO
    #         if self.charm_specific.is_compatible():
    #             if self._force_start:
    #                 self._force_start.log(
    #                     f"Checked that refresh from previous {self.charm_specific.workload_name} version and charm revision to current versions is compatible"
    #                 )
    #         else:
    #             if self.charm_specific.cloud is Cloud.KUBERNETES:
    #                 self._unit_status_higher_priority = charm.BlockedStatus(
    #                     "Refresh incompatible. Rollback with instructions in Charmhub docs or see `juju debug-log`"
    #                 )
    #                 # TODO: variables in fstring
    #                 logger.info(
    #                     f"Refresh incompatible. Rollback by running `juju refresh {charm.app} --revision 10007 --resource postgresql-image=registry.jujucharms.com/charm/kotcfrohea62xreenq1q75n1lyspke0qkurhk/postgresql-image@sha256:76ef26c7d11a524bcac206d5cb042ebc3c8c8ead73fa0cd69d21921552db03b6`. Continuing this refresh may cause data loss and/or downtime. The refresh can be forced to continue with the `force-refresh-start` action and the `check-compatibility` parameter. Run `juju show-action postgresql-k8s force-refresh-start` for more information"
    #                 )
    #             if self._force_start:
    #                 if self.charm_specific.cloud is Cloud.KUBERNETES:
    #                     # TODO: variables in fstring
    #                     self._force_start.fail(
    #                         f"Refresh incompatible. Rollback by running `juju refresh {charm.app} --revision 10007 --resource postgresql-image=registry.jujucharms.com/charm/kotcfrohea62xreenq1q75n1lyspke0qkurhk/postgresql-image@sha256:76ef26c7d11a524bcac206d5cb042ebc3c8c8ead73fa0cd69d21921552db03b6`"
    #                     )
    #                 elif self.charm_specific.cloud is Cloud.MACHINES:
    #                     self._force_start.fail("Refresh incompatible. Rollback with `juju refresh`")
    #                 else:
    #                     raise TypeError
    #             return False
    #     if self._force_start and self._force_start.run_pre_refresh_checks is False:
    #         self._force_start.log("Skipping pre-refresh checks")
    #     else:
    #         # Run pre-refresh checks
    #         if self._force_start:
    #             self._force_start.log("Running pre-refresh checks")
    #         try:
    #             if self.charm_specific.cloud is Cloud.KUBERNETES:
    #                 self.charm_specific.run_pre_refresh_checks_after_1_unit_refreshed()
    #             elif self.charm_specific.cloud is Cloud.MACHINES:
    #                 self.charm_specific.run_pre_refresh_checks_before_any_units_refreshed()
    #             else:
    #                 raise TypeError
    #         except PrecheckFailed as exception:
    #             self._unit_status_higher_priority = charm.BlockedStatus(
    #                 f"Rollback with `juju refresh`. Pre-refresh check failed: {exception.message}"
    #             )
    #             if self.charm_specific.cloud is Cloud.KUBERNETES:
    #                 # TODO: variables in fstring
    #                 logger.error(
    #                     f"Pre-refresh check failed: {exception.message}. Rollback by running `juju refresh {charm.app} --revision 10007 --resource postgresql-image=registry.jujucharms.com/charm/kotcfrohea62xreenq1q75n1lyspke0qkurhk/postgresql-image@sha256:76ef26c7d11a524bcac206d5cb042ebc3c8c8ead73fa0cd69d21921552db03b6`. Continuing this refresh may cause data loss and/or downtime. The refresh can be forced to continue with the `force-refresh-start` action and the `run-pre-refresh-checks` parameter. Run `juju show-action postgresql-k8s force-refresh-start` for more information"
    #                 )
    #                 if self._force_start:
    #                     self._force_start.fail(
    #                         f"Pre-refresh check failed: {exception.message}. Rollback by running `juju refresh {charm.app} --revision 10007 --resource postgresql-image=registry.jujucharms.com/charm/kotcfrohea62xreenq1q75n1lyspke0qkurhk/postgresql-image@sha256:76ef26c7d11a524bcac206d5cb042ebc3c8c8ead73fa0cd69d21921552db03b6`"
    #                     )
    #             elif self.charm_specific.cloud is Cloud.MACHINES:
    #                 logger.error(
    #                     f"Pre-refresh check failed: {exception.message}. Rollback with `juju refresh`. The refresh can be forced to continue with the `force-refresh-start` action and the `run-pre-refresh-checks` parameter. Run `juju show-action postgresql-k8s force-refresh-start` for more information"
    #                 )
    #                 if self._force_start:
    #                     self._force_start.fail(
    #                         f"Pre-refresh check failed: {exception.message}. Rollback with `juju refresh`"
    #                     )
    #             else:
    #                 raise TypeError
    #             return False
    #         if self._force_start:
    #             self._force_start.log("Pre-refresh checks successful")
    #     return True
    #
    # def _refresh_snap(self):
    #     # Set app status because refreshing the snap will likely take more than a
    #     # few seconds—or could fail
    #     self._set_status()
    #     self.charm_specific.refresh_snap()  # TODO
    #     self._snap_refresh_attempted_in_this_juju_event = True
    #     if self._relation.my_unit.get("snap_revision") != self._get_installed_snap_revision():
    #         raise Exception(
    #             "Must call `update_snap_revision()` immediately after the workload snap is refreshed"
    #         )
    #
    # @staticmethod
    # def _set_partition(value: int, /):
    #     """Kubernetes StatefulSet rollingUpdate partition
    #
    #     Specifies which units can refresh
    #
    #     Unit numbers >= partition can refresh
    #     Unit numbers < partition cannot refresh
    #
    #     https://kubernetes.io/docs/concepts/workloads/controllers/statefulset/#partitions
    #     """
    #     lightkube.Client().patch(
    #         lightkube.resources.apps_v1.StatefulSet,
    #         charm.app,
    #         {"spec": {"updateStrategy": {"rollingUpdate": {"partition": value}}}},
    #     )
    #
    # def _determine_partition_and_in_progress_status(
    #     self,
    # ) -> typing.Tuple[int, charm.Status]:
    #     assert len(self._units) > 0
    #     for index, unit in enumerate(self._units):
    #         if self._unit_controller_revisions[unit] != self._app_controller_revision:
    #             # `unit` has outdated controller revision
    #             break
    #         # During scale up, unit may be missing from relation
    #         if (
    #             self._relation.get(unit, {}).get(
    #                 "next_unit_allowed_to_refresh_if_app_controller_revision_hash_equals"
    #             )
    #             != self._app_controller_revision
    #         ):
    #             # `unit` has not allowed the next unit to refresh
    #             if (
    #                 self._resume_refresh
    #                 and self._resume_refresh.check_health_of_refreshed_units is False
    #             ):
    #                 pass
    #             else:
    #                 break
    #     next_unit_to_refresh = unit
    #     next_unit_to_refresh_index = index
    #     if (
    #         self._resume_refresh
    #         or self._pause_after is _PauseAfter.NONE
    #         or (
    #             self._pause_after is _PauseAfter.FIRST
    #             # Whether the first two units have already refreshed
    #             and next_unit_to_refresh_index >= 2
    #         )
    #     ):
    #         unit_allowed_to_refresh = next_unit_to_refresh
    #     else:
    #         # User must run `resume-refresh` action to refresh
    #         # `next_unit_to_refresh`
    #
    #         # Unit before `next_unit_to_refresh`, if it exists
    #         unit_allowed_to_refresh = self._units[max((next_unit_to_refresh_index - 1), 0)]
    #     if self._pause_after is _PauseAfter.ALL or (
    #         self._pause_after is _PauseAfter.FIRST
    #         and self._units.index(unit_allowed_to_refresh) < 1
    #     ):
    #         status = charm.BlockedStatus(
    #             f"Refreshing. Check units >={unit_allowed_to_refresh.number} are healthy & run `resume-refresh` on leader. To rollback, see docs or `juju debug-log`"
    #         )
    #     else:
    #         status = charm.MaintenanceStatus(
    #             f"Refreshing. To pause refresh, run `juju config {charm.app} pause_after_unit_refresh=all`"
    #         )
    #     return unit_allowed_to_refresh.number, status

    def __init__(self, charm_specific: CharmSpecific, /):
        if charm_specific.cloud is Cloud.KUBERNETES:
            self._refresh = _Kubernetes(charm_specific)
        elif charm_specific.cloud is Cloud.MACHINES:
            raise NotImplementedError
        else:
            raise TypeError
        # self.charm_specific = charm_specific
        # _LOCAL_STATE.mkdir(exist_ok=True)
        # if self.charm_specific.cloud is Cloud.KUBERNETES:
        #     # Save state if unit is tearing down.
        #     # Used in future Juju event (stop event) to determine whether to set Kubernetes
        #     # partition
        #     tearing_down = _LOCAL_STATE / "kubernetes_unit_tearing_down"
        #     if (
        #         isinstance(charm.event, charm.RelationDepartedEvent)
        #         and charm.event.departing_unit == charm.unit
        #     ):
        #         # Unit is tearing down and 1+ other units are not tearing down
        #         tearing_down.touch(exist_ok=True)
        # if (
        #     self.charm_specific.cloud is Cloud.KUBERNETES
        #     and isinstance(charm.event, charm.StopEvent)
        #     # If `tearing_down.exists()`, this unit is being removed for scale down.
        #     # Therefore, we should not raise the partition—so that the partition never exceeds
        #     # the highest unit number (which would cause `juju refresh` to not trigger any Juju
        #     # events).
        #     and not tearing_down.exists()
        # ):
        #     # This unit could be refreshing or just restarting.
        #     # Raise StatefulSet partition to prevent other units from refreshing in case a refresh
        #     # is in progress.
        #     # If a refresh is not in progress, the leader unit will reset the partition to 0.
        #     stateful_set = lightkube.Client().get(
        #         lightkube.resources.apps_v1.StatefulSet, charm.app
        #     )
        #     partition = stateful_set.spec.updateStrategy.rollingUpdate.partition
        #     assert partition is not None
        #     if partition < charm.unit.number:
        #         # Raise partition
        #         self._set_partition(charm.unit.number)
        #         logger.info(f"Set StatefulSet partition to {charm.unit.number} during stop event")
        # dot_juju_charm = pathlib.Path(".juju-charm")
        # self._installed_charm_revision_raw = dot_juju_charm.read_text().strip()
        # """Contents of this unit's .juju-charm file (e.g. "ch:amd64/jammy/postgresql-k8s-381")"""
        #
        # if self.charm_specific.cloud is Cloud.MACHINES:
        #     # TODO comment
        #     self._history = _MachineRefreshHistory.from_file()
        #     if (
        #         self._history.last_refresh is None
        #         or self._history.last_refresh.charm_revision != self._installed_charm_revision_raw
        #     ):
        #         self._history.last_refresh_started_on_this_unit = False
        #         self._history.last_refresh = _HistoryEntry(
        #             charm_revision=self._installed_charm_revision_raw,
        #             time_of_refresh=dot_juju_charm.stat().st_mtime,
        #         )
        #     assert self._history.last_refresh is not None
        #     if isinstance(charm.event, charm.UpgradeCharmEvent) or isinstance(
        #         charm.event, charm.ConfigChangedEvent
        #     ):
        #         # Charm revision is up-to-date
        #         # TODO: add link to juju bug about config change only on up-to-date units
        #         # TODO comment: add note that config change will be fired on initial install?
        #         if (
        #             self._history.last_refresh_to_up_to_date_charm_revision is None
        #             or self._history.last_refresh_to_up_to_date_charm_revision.charm_revision
        #             != self._history.last_refresh.charm_revision
        #         ):
        #             self._history.second_to_last_refresh_to_up_to_date_charm_revision = (
        #                 self._history.last_refresh_to_up_to_date_charm_revision
        #             )
        #             self._history.last_refresh_to_up_to_date_charm_revision = (
        #                 self._history.last_refresh
        #             )
        #     self._history.save_to_file()
        # self._relation = charm.Endpoint("refresh-v-three").relation
        # if not self._relation:
        #     raise PeerRelationMissing
        # # TODO comment
        # self._relation.my_unit["pause_after_unit_refresh_config"] = charm.config[
        #     "pause_after_unit_refresh"
        # ]
        # with pathlib.Path("refresh_versions.toml").open("rb") as file:
        #     self._refresh_versions = tomli.load(file)
        # self._force_start: typing.Optional[_ForceRefreshStartAction] = None
        # self._resume_refresh: typing.Optional[_ResumeRefreshAction] = None
        # if self.charm_specific.cloud is Cloud.KUBERNETES:
        #     stateful_set = lightkube.Client().get(
        #         lightkube.resources.apps_v1.StatefulSet, charm.app
        #     )
        #     self._app_controller_revision: str = stateful_set.status.updateRevision
        #     assert self._app_controller_revision is not None
        #     pods = lightkube.Client().list(
        #         lightkube.resources.core_v1.Pod,
        #         labels={"app.kubernetes.io/name": charm.app},
        #     )
        #
        #     def get_unit(pod_name: str):
        #         # Example `pod_name`: "postgresql-k8s-0"
        #         *app_name, unit_number = pod_name.split("-")
        #         # Example: "postgresql-k8s/0"
        #         unit_name = f'{"-".join(app_name)}/{unit_number}'
        #         return charm.Unit(unit_name)
        #
        #     self._unit_controller_revisions: typing.Dict[charm.Unit, str] = {
        #         get_unit(pod.metadata.name): pod.metadata.labels["controller-revision-hash"]
        #         for pod in pods
        #     }
        # elif self.charm_specific.cloud is Cloud.MACHINES:
        #     # TODO: error handling if refresh_versions.toml incorrectly formatted
        #     # TODO: error handling if arch keyerror
        #     self._pinned_snap_revision: str = self._refresh_versions["snap"]["revisions"][
        #         platform.machine()
        #     ]
        # else:
        #     raise TypeError
        # if self.charm_specific.cloud is Cloud.KUBERNETES:
        #     up_to_date_units = (
        #         unit
        #         for unit, revision in self._unit_controller_revisions.items()
        #         if revision == self._app_controller_revision
        #     )
        #     pause_after_values = (
        #         # During scale up or initial install, unit or "pause_after_unit_refresh_config" key
        #         # may be missing from relation
        #         self._relation.get(unit, {}).get("pause_after_unit_refresh_config")
        #         for unit in up_to_date_units
        #     )
        #     pause_after_values = (value for value in pause_after_values if value is not None)
        # elif self.charm_specific.cloud is Cloud.MACHINES:
        #     up_to_date_units = (
        #         unit_or_app
        #         for unit_or_app in self._relation
        #         if isinstance(unit_or_app, charm.Unit)
        #         and self._relation[unit_or_app].get("snap_revision") == self._pinned_snap_revision
        #     )
        #     pause_after_values = (
        #         self._relation[unit]["pause_after_unit_refresh_config"] for unit in up_to_date_units
        #     )
        # else:
        #     raise TypeError
        # pause_after_values = (_PauseAfter(value) for value in pause_after_values)
        # self._pause_after = max(pause_after_values)
        # if self.charm_specific.cloud is Cloud.MACHINES:
        #     # TODO comment about purpose is for uncaught exception
        #     self.update_snap_revision()
        #     if self._history.last_refresh_to_up_to_date_charm_revision:
        #         self._relation.my_unit["last_refresh_to_up_to_date_charm_revision"] = json.dumps(
        #             dataclasses.asdict(self._history.last_refresh_to_up_to_date_charm_revision)
        #         )
        # if self.charm_specific.cloud is Cloud.KUBERNETES:
        #     kubernetes_refresh_started = _LOCAL_STATE / "kubernetes_refresh_started"
        #     if kubernetes_refresh_started.exists():
        #         self._relation.my_unit["refresh_started_if_app_controller_revision_hash_equals"] = (
        #             self._unit_controller_revisions[charm.unit]
        #         )
        # elif self.charm_specific.cloud is Cloud.MACHINES:
        #     self._relation.my_unit[
        #         "refresh_started_if_this_units_snap_revision_and_charm_revision_and_databag_are_up_to_date"
        #     ] = json.dumps(self._history.last_refresh_started_on_this_unit)
        # else:
        #     raise TypeError
        # self._in_progress = self._is_in_progress()
        # if self.charm_specific.cloud is Cloud.KUBERNETES:
        #     # TODO comment about includes units not in databag
        #     unsorted_units = self._unit_controller_revisions.keys()
        # elif self.charm_specific.cloud is Cloud.MACHINES:
        #     unsorted_units = (
        #         unit_or_app for unit_or_app in self._relation if isinstance(unit_or_app, charm.Unit)
        #     )
        # else:
        #     raise TypeError
        # self._units = sorted(unsorted_units, reverse=True)
        # """Sorted from highest to lowest unit number (refresh order)"""
        #
        # try:
        #     self._force_start = _ForceRefreshStartAction(
        #         charm.event,
        #         first_unit_to_refresh=self._units[0],
        #         in_progress=self._in_progress,
        #     )
        # except _InvalidForceEvent:
        #     pass
        # try:
        #     self._resume_refresh = _ResumeRefreshAction(charm.event, in_progress=self._in_progress)
        # except _InvalidResumeEvent:
        #     pass
        # self._unit_status_higher_priority: typing.Optional[charm.Status] = None
        # pre_refresh_check_action: typing.Optional[charm.ActionEvent] = None
        # if isinstance(charm.event, charm.ActionEvent) and charm.event.action == "pre-refresh-check":
        #     pre_refresh_check_action = charm.event
        # if self._in_progress is _InProgress.FALSE:
        #     if self.charm_specific.cloud is Cloud.KUBERNETES and charm.is_leader:
        #         self._set_partition(0)
        #         # TODO log
        #     if pre_refresh_check_action:
        #         if charm.is_leader:
        #             # Run pre-refresh checks
        #             try:
        #                 self.charm_specific.run_pre_refresh_checks_before_any_units_refreshed()
        #             except PrecheckFailed as exception:
        #                 pre_refresh_check_action.fail(
        #                     f"Charm is not ready for refresh. Pre-refresh check failed: {exception.message}"
        #                 )
        #             else:
        #                 if self.charm_specific.cloud is Cloud.KUBERNETES:
        #                     pre_refresh_check_action.result = {
        #                         # TODO fstring
        #                         "result": f"Charm is ready for refresh. For refresh instructions, see {self.charm_specific.refresh_user_docs_url}\n"
        #                         "After the refresh has started, use this command to rollback (copy this down in case you need it later):\n"
        #                         f"`juju refresh {charm.app} --revision 10007 --resource postgresql-image=registry.jujucharms.com/charm/kotcfrohea62xreenq1q75n1lyspke0qkurhk/postgresql-image@sha256:76ef26c7d11a524bcac206d5cb042ebc3c8c8ead73fa0cd69d21921552db03b6`"
        #                     }
        #                 elif self.charm_specific.cloud is Cloud.MACHINES:
        #                     pre_refresh_check_action.result = {
        #                         # TODO fstring
        #                         "result": f"Charm is ready for refresh. For refresh instructions, see {self.charm_specific.refresh_user_docs_url}\n"
        #                         "After the refresh has started, use this command to rollback:\n"
        #                         f"`juju refresh {charm.app} --revision 10007`"
        #                     }
        #                 else:
        #                     raise TypeError
        #         else:
        #             pre_refresh_check_action.fail(
        #                 f"Must run action on leader unit. (e.g. `juju run {charm.app}/leader pre-refresh-check`)"
        #             )
        # else:
        #     if pre_refresh_check_action:
        #         pre_refresh_check_action.fail("Refresh already in progress")
        #     # Check if refresh has already started
        #     # TODO clarify/reword
        #     # TODO rollback exception if charm rev + workload rev equal original
        #     if rollback:
        #         if self.charm_specific.cloud is Cloud.KUBERNETES:
        #             self._workload_allowed_to_start = True
        #         # TODO rename
        #         refresh_started = True
        #     elif self.charm_specific.cloud is Cloud.KUBERNETES:
        #         self._workload_allowed_to_start = any(
        #             # During scale up, unit may be missing from relation
        #             self._relation.get(unit, {}).get(
        #                 "refresh_started_if_app_controller_revision_hash_equals"
        #             )
        #             == self._unit_controller_revisions[charm.unit]
        #             for unit in self._units
        #         )
        #         refresh_started = any(
        #             # During scale up, unit may be missing from relation
        #             self._relation.get(unit).get(
        #                 "refresh_started_if_app_controller_revision_hash_equals"
        #             )
        #             == self._app_controller_revision
        #             for unit in self._units
        #         )
        #     elif self.charm_specific.cloud is Cloud.MACHINES:
        #         for unit in self._units:
        #             databag = self._relation[unit]
        #
        #             def is_up_to_date():
        #                 if databag.get("snap_revision") != self._pinned_snap_revision:
        #                     return False
        #                 # TODO: rename `entry`
        #                 entry = databag.get("last_refresh_to_up_to_date_charm_revision")
        #                 if entry is None:
        #                     return False
        #                 entry = _HistoryEntry(**json.loads(entry))
        #                 if entry.charm_revision != self._installed_charm_revision_raw:
        #                     return False
        #                 if (
        #                     self._history.second_to_last_refresh_to_up_to_date_charm_revision
        #                     and entry.time_of_refresh
        #                     < self._history.second_to_last_refresh_to_up_to_date_charm_revision.time_of_refresh
        #                 ):
        #                     # TODO comment
        #                     # other unit databag is outdated
        #                     return False
        #                 return True
        #
        #             data = databag.get(
        #                 "refresh_started_if_this_units_snap_revision_and_charm_revision_and_databag_are_up_to_date"
        #             )
        #             if data and json.loads(data) is True and is_up_to_date():
        #                 refresh_started = True
        #                 break
        #         else:
        #             refresh_started = False
        #     else:
        #         raise TypeError
        #     if self.charm_specific.cloud is Cloud.MACHINES:
        #         self._incompatible_app_status = None
        #     if (
        #         not refresh_started
        #         and self.charm_specific.cloud is Cloud.MACHINES
        #         and charm.is_leader
        #     ):
        #         # TODO
        #         if not self.charm_specific.is_compatible():
        #             # TODO fstring var
        #             self._incompatible_app_status = charm.BlockedStatus(
        #                 f"Refresh incompatible. Rollback with `juju refresh --revision 10007`"
        #             )
        #             logger.info(
        #                 f"Refresh incompatible. Rollback with `juju refresh`. Continuing this refresh may cause data loss and/or downtime. The refresh can be forced to continue with the `force-refresh-start` action and the `check-compatibility` parameter. Run `juju show-action {charm.app} force-refresh-start` for more information"
        #             )
        #     if self.charm_specific.cloud is Cloud.MACHINES:
        #         self._snap_refresh_attempted_in_this_juju_event = False
        #     if refresh_started and self._force_start:
        #         self._force_start.fail("refresh already started")  # TODO UX
        #     elif not refresh_started and charm.unit == self._units[0]:
        #         # (On Kubernetes, not all `self._units` are guaranteed to have joined the peer
        #         # relation—but this unit is guaranteed to have joined the peer relation)
        #         assert self._relation is not None
        #
        #         if self._can_refresh_start():
        #             refresh_started = True
        #             if self.charm_specific.cloud is Cloud.KUBERNETES:
        #                 kubernetes_refresh_started.touch(exist_ok=False)
        #                 self._relation.my_unit[
        #                     "refresh_started_if_app_controller_revision_hash_equals"
        #                 ] = self._unit_controller_revisions[charm.unit]
        #                 self._workload_allowed_to_start = True
        #                 if self._force_start:
        #                     self._force_start.result = {
        #                         "result": f"{self.charm_specific.workload_name} refreshed on unit {charm.unit.number}. Starting {self.charm_specific.workload_name} on unit {charm.unit.number}"
        #                     }
        #             elif self.charm_specific.cloud is Cloud.MACHINES:
        #                 self._history.last_refresh_started_on_this_unit = True
        #                 self._history.save_to_file()
        #                 self._relation.my_unit[
        #                     "refresh_started_if_this_units_snap_revision_and_charm_revision_and_databag_are_up_to_date"
        #                 ] = json.dumps(True)
        #                 if self._force_start:
        #                     self._force_start.log(f"Refreshing unit {charm.unit.number}")
        #                 # todo Not called again on failure currently? 70% sure fixed, need to confirm
        #                 self._refresh_snap()
        # self._in_progress = self._is_in_progress()
        # if self._in_progress is not _InProgress.FALSE:
        #     if refresh_started or (
        #         self._resume_refresh
        #         and self._resume_refresh.check_health_of_refreshed_units is False
        #     ):
        #         if self.charm_specific.cloud is Cloud.KUBERNETES:
        #             if self._resume_refresh and not charm.is_leader:
        #                 self._resume_refresh.fail(
        #                     f"Must run action on leader unit. (e.g. `juju run {charm.app}/leader resume-refresh`)"
        #                 )
        #             if charm.is_leader:
        #                 partition, self._in_progress_app_status = (
        #                     self._determine_partition_and_in_progress_status()
        #                 )
        #                 self._set_partition(partition)
        #                 # TODO log
        #         elif self.charm_specific.cloud is Cloud.MACHINES:
        #             if (
        #                 self._resume_refresh
        #                 and self._resume_refresh.check_health_of_refreshed_units is False
        #             ):
        #                 if (
        #                     self._relation.my_unit.get("snap_revision")
        #                     == self._pinned_snap_revision
        #                 ):
        #                     self._resume_refresh.fail("Unit already refreshed")
        #                 elif charm.unit == self._units[0] and not refresh_started:
        #                     self._resume_refresh.fail("run force-refresh-start instead")  # TODO
        #                 elif self._snap_refresh_attempted_in_this_juju_event:
        #                     # TODO comment avoid refreshing snap if already attempted in this event because first unit and refresh_started
        #                     self._resume_refresh.fail(
        #                         "snap refresh attempt failed. TODO next user action"
        #                     )  # TODO
        #                 else:
        #                     self._resume_refresh.log("Ignoring health of refreshed units")
        #                     self._resume_refresh.log(f"Refreshing unit {charm.unit.number}")
        #                     self._refresh_snap()
        #             elif not refresh_started:
        #                 if self._resume_refresh:
        #                     assert self._resume_refresh.check_health_of_refreshed_units is True
        #                     # TODO rework ux
        #                     if charm.unit == self._units[0]:
        #                         # TODO
        #                         self._resume_refresh.fail()
        #                     else:
        #                         # TODO change message to refresh not started? (scale up with outdated databag case)
        #                         self._resume_refresh.fail(
        #                             f"Unit {self._units[0].number} is unhealthy. Refresh will not resume."
        #                         )
        #             else:
        #                 assert refresh_started
        #                 first_unit_not_allowing_refresh_of_next_unit = None
        #                 assert len(self._units) > 0
        #                 for unit in self._units:
        #                     databag = self._relation[unit]
        #
        #                     def is_up_to_date():
        #                         if databag.get("snap_revision") != self._pinned_snap_revision:
        #                             return False
        #                         # TODO: rename `entry`
        #                         entry = databag.get("last_refresh_to_up_to_date_charm_revision")
        #                         if entry is None:
        #                             return False
        #                         entry = _HistoryEntry(**json.loads(entry))
        #                         if entry.charm_revision != self._installed_charm_revision_raw:
        #                             return False
        #                         if (
        #                             self._history.second_to_last_refresh_to_up_to_date_charm_revision
        #                             and entry.time_of_refresh
        #                             < self._history.second_to_last_refresh_to_up_to_date_charm_revision.time_of_refresh
        #                         ):
        #                             # TODO comment
        #                             # other unit databag is outdated
        #                             return False
        #                         return True
        #
        #                     if not is_up_to_date():
        #                         break
        #                     data = databag.get(
        #                         "next_unit_allowed_to_refresh_if_this_units_snap_revision_and_charm_revision_and_databag_are_up_to_date"
        #                     )
        #                     if not (data and json.loads(data) is True):
        #                         if first_unit_not_allowing_refresh_of_next_unit is None:
        #                             first_unit_not_allowing_refresh_of_next_unit = unit
        #                 next_unit_to_refresh = unit
        #                 if self._resume_refresh:
        #                     assert self._resume_refresh.check_health_of_refreshed_units is True
        #                     if next_unit_to_refresh == self._units[0]:
        #                         # TODO rework ux
        #                         if charm.unit == self._units[0]:
        #                             # TODO
        #                             self._resume_refresh.fail()
        #                             # TODO comment (avoid logging refresh of snap)
        #                             self._resume_refresh = None
        #                         else:
        #                             # TODO change message to refresh not started? (scale up with outdated databag case)
        #                             self._resume_refresh.fail(
        #                                 f"Unit {self._units[0].number} is unhealthy. Refresh will not resume."
        #                             )
        #                     elif next_unit_to_refresh != charm.unit:
        #                         self._resume_refresh.fail(
        #                             f"Must run action on unit {next_unit_to_refresh.number}"
        #                         )
        #                     elif self._pause_after is _PauseAfter.NONE:
        #                         self._resume_refresh.fail(
        #                             "`pause_after_unit_refresh` config is set to `none`. This action is not applicable."
        #                         )
        #                         # TODO comment (avoid logging refresh of snap)
        #                         self._resume_refresh = None
        #                 if first_unit_not_allowing_refresh_of_next_unit:
        #                     if self._resume_refresh:
        #                         assert self._resume_refresh.check_health_of_refreshed_units is True
        #                         self._resume_refresh.fail(
        #                             f"Unit {first_unit_not_allowing_refresh_of_next_unit.number} is unhealthy. Refresh will not resume."
        #                         )
        #                 elif next_unit_to_refresh == charm.unit:
        #                     # TODO comment avoid refreshing snap if already attempted in this event because first unit
        #                     if (
        #                         self._resume_refresh
        #                         or self._pause_after is _PauseAfter.NONE
        #                         or (
        #                             self._pause_after is _PauseAfter.FIRST
        #                             # Whether the first two units have already refreshed
        #                             and self._units.index(next_unit_to_refresh) >= 2
        #                         )
        #                     ) and not self._snap_refresh_attempted_in_this_juju_event:
        #                         if self._resume_refresh:
        #                             assert (
        #                                 self._resume_refresh.check_health_of_refreshed_units is True
        #                             )
        #                             if self._pause_after is _PauseAfter.FIRST:
        #                                 self._resume_refresh.log(
        #                                     f"Refresh resumed. Refreshing unit {charm.unit.number}"
        #                                 )
        #                             else:
        #                                 assert (
        #                                     self._pause_after is _PauseAfter.ALL
        #                                     or self._pause_after is _PauseAfter.UNKNOWN
        #                                 )
        #                                 self._resume_refresh.log(
        #                                     f"Refreshing unit {charm.unit.number}"
        #                                 )
        #                         self._refresh_snap()
        #
        #         else:
        #             raise TypeError
        # # TODO: when to set unknown in prog status? lower prio that if we know incompat? prob


class _KubernetesUnit(charm.Unit):
    def __new__(cls, name: str, *, controller_revision: str):
        instance: _KubernetesUnit = super().__new__(cls, name)
        instance.controller_revision = controller_revision
        return instance

    def __repr__(self):
        return f"{type(self).__name__}({repr(str(self))}, controller_revision={repr(self.controller_revision)})"

    @classmethod
    def from_pod(cls, pod: lightkube.resources.core_v1.Pod):
        # Example: "postgresql-k8s-0"
        pod_name = pod.metadata.name
        *app_name, unit_number = pod_name.split("-")
        # Example: "postgresql-k8s/0"
        unit_name = f'{"-".join(app_name)}/{unit_number}'
        return cls(unit_name, controller_revision=pod.metadata.labels["controller-revision-hash"])


@dataclasses.dataclass(frozen=True)
class _RefreshVersions:
    """Versions pinned in this unit's refresh_versions.toml"""

    # TODO add note on machines that workload versions pinned are not necc installed
    # TODO add machines subclass with snap
    charm: CharmVersion
    workload: str

    @classmethod
    def from_file(cls):
        with pathlib.Path("refresh_versions.toml").open("rb") as file:
            versions = tomli.load(file)
        try:
            return cls(charm=CharmVersion(versions["charm"]), workload=versions["workload"])
        except KeyError:
            # TODO link to docs with format?
            raise KeyError("Required key missing from refresh_versions.toml")
        except ValueError:
            raise ValueError("Invalid charm version in refresh_versions.toml")


@dataclasses.dataclass(frozen=True)
class _OriginalVersions:
    """Versions (of all units) immediately after the last completed refresh

    Or, if no completed refreshes, immediately after juju deploy and (on machines) initial installation
    """

    workload: str
    workload_container: str
    charm: CharmVersion
    charm_revision: str

    @classmethod
    def from_app_databag(cls, databag: collections.abc.Mapping, /):
        try:
            return cls(
                workload=databag["original_workload_version"],
                workload_container=databag["original_workload_container_version"],
                charm=CharmVersion(databag["original_charm_version"]),
                charm_revision=databag["original_charm_revision"],
            )
        except (KeyError, ValueError):
            # This should only happen if user refreshes from a charm without refresh v3
            raise ValueError(
                "Refresh failed. Automatic recovery not possible. Original versions in app databag are missing or invalid"
            )

    def write_to_app_databag(self, databag: collections.abc.MutableMapping, /):
        databag["original_workload_version"] = self.workload
        databag["original_workload_container_version"] = self.workload_container
        databag["original_charm_version"] = str(self.charm)
        databag["original_charm_revision"] = self.charm_revision


class KubernetesJujuAppNotTrusted(Exception):
    """Juju app is not trusted (needed to patch StatefulSet partition)

    User must run `juju trust` with `--scope=cluster`
    or re-deploy using `juju deploy` with `--trust`
    """


class _Kubernetes:
    @property
    def in_progress(self) -> bool:
        return self._in_progress

    @property
    def next_unit_allowed_to_refresh(self) -> bool:
        return (
            self._relation.my_unit.get(
                "next_unit_allowed_to_refresh_if_app_controller_revision_hash_equals"
            )
            == self._unit_controller_revision
        )

    @next_unit_allowed_to_refresh.setter
    def next_unit_allowed_to_refresh(self, value: typing.Literal[True]):
        if value is not True:
            raise ValueError("`next_unit_allowed_to_refresh` can only be set to `True`")
        self._relation.my_unit[
            "next_unit_allowed_to_refresh_if_app_controller_revision_hash_equals"
        ] = self._unit_controller_revision
        self._set_partition_and_app_status(handle_action=False)

    @property
    def workload_allowed_to_start(self) -> bool:
        if not self._in_progress:
            return True
        for unit in self._units:
            if (
                # During scale up, unit may be missing from relation
                self._unit_controller_revision
                in self._relation.get(unit, {}).get(
                    "refresh_started_if_app_controller_revision_hash_in", tuple()
                )
            ):
                return True
        original_versions = _OriginalVersions.from_app_databag(self._relation.my_app)
        if (
            original_versions.charm == self._installed_charm_version
            and original_versions.workload_container == self._installed_workload_container_version
        ):
            # This unit has not refreshed
            # (If this unit is rolling back, `True` should have been returned earlier)
            return True
        return False

    @property
    def app_status_higher_priority(self) -> typing.Optional[charm.Status]:
        return self._app_status_higher_priority

    @property
    def unit_status_higher_priority(self) -> typing.Optional[charm.Status]:
        return self._unit_status_higher_priority

    @property
    def unit_status_lower_priority(self) -> typing.Optional[charm.Status]:
        # TODO check if workload is running
        if not self._in_progress:
            return
        # TODO comment
        workload_container_matches_pin = (
            self._installed_workload_container_version == self._pinned_workload_container_version
        )
        if workload_container_matches_pin:
            message = (
                f"{self._charm_specific.workload_name} {self._pinned_workload_version} running"
            )
        else:
            # We don't know what workload version is in the workload container
            message = f"{self._charm_specific.workload_name} running"
        if self._unit_controller_revision != self._app_controller_revision:
            message += " (restart pending)"
        if self._installed_charm_revision_raw.startswith("ch:"):
            # Charm was deployed from Charmhub; use revision
            message += f'; Charm revision {self._installed_charm_revision_raw.split("-")[-1]}'
        else:
            # Charmhub revision is not available; fall back to charm version
            message += f"; Charm version {self._installed_charm_version}"
        if not workload_container_matches_pin:
            message += f'; Unexpected container {self._installed_workload_container_version.removeprefix("sha256:")[:6]}'
        return charm.ActiveStatus(message)

    @staticmethod
    def _get_partition() -> int:
        """Kubernetes StatefulSet rollingUpdate partition

        Specifies which units can refresh

        Unit numbers >= partition can refresh
        Unit numbers < partition cannot refresh

        If the partition is lowered (e.g. to 1) and then raised (e.g. to 2), the unit (unit 1) that
        refreshed will stay on the new version unless its pod is deleted. After its pod is deleted,
        it will be re-created on the old version (if the partition is higher than its unit number).

        https://kubernetes.io/docs/concepts/workloads/controllers/statefulset/#partitions
        """
        stateful_set = lightkube.Client().get(lightkube.resources.apps_v1.StatefulSet, charm.app)
        partition = stateful_set.spec.updateStrategy.rollingUpdate.partition
        assert partition is not None
        return partition

    @staticmethod
    def _set_partition(value: int, /):
        """Kubernetes StatefulSet rollingUpdate partition

        Specifies which units can refresh

        Unit numbers >= partition can refresh
        Unit numbers < partition cannot refresh

        If the partition is lowered (e.g. to 1) and then raised (e.g. to 2), the unit (unit 1) that
        refreshed will stay on the new version unless its pod is deleted. After its pod is deleted,
        it will be re-created on the old version (if the partition is higher than its unit number).

        https://kubernetes.io/docs/concepts/workloads/controllers/statefulset/#partitions
        """
        lightkube.Client().patch(
            lightkube.resources.apps_v1.StatefulSet,
            charm.app,
            {"spec": {"updateStrategy": {"rollingUpdate": {"partition": value}}}},
        )

    def _start_refresh(self):
        # TODO: docstring
        # Run workload container check, compatibility checks, and pre-refresh checks after
        # `juju refresh`. Handle force-refresh-start action. Set `self._refresh_started` and
        #  Set `self._unit_status_higher_priority` & unit status.
        # TODO add databag set & file touched
        # TODO conditional set only if in_progress

        # outline:
        # parse action & some validation
        # check if refresh already started & save self._refresh_started for leader
        # if refresh not started, run checks on highest unit
        # set self._unit_status_higher_priority & unit status on highest unit

        class _InvalidForceEvent(ValueError):
            """Event is not valid force-refresh-start action event"""

        class _ForceRefreshStartAction(charm.ActionEvent):
            def __init__(
                self, event: charm.Event, *, first_unit_to_refresh: charm.Unit, in_progress: bool
            ):
                if not isinstance(event, charm.ActionEvent):
                    raise _InvalidForceEvent
                super().__init__()
                if event.action != "force-refresh-start":
                    raise _InvalidForceEvent
                if charm.unit != first_unit_to_refresh:
                    event.fail(f"Must run action on unit {first_unit_to_refresh.number}")
                if not in_progress:
                    event.fail("No refresh in progress")
                    raise _InvalidForceEvent
                self.check_workload_container: bool = event.parameters["check-workload-container"]
                self.check_compatibility: bool = event.parameters["check-compatibility"]
                self.run_pre_refresh_checks: bool = event.parameters["run-pre-refresh-checks"]
                for parameter in (
                    self.check_workload_container,
                    self.check_compatibility,
                    self.run_pre_refresh_checks,
                ):
                    if parameter is False:
                        break
                else:
                    event.fail(
                        "Must run with at least one of `check-compatibility`, `run-pre-refresh-checks`, or `check-workload-container` parameters `=false`"
                    )
                    raise _InvalidForceEvent

        force_start: typing.Optional[_ForceRefreshStartAction]
        try:
            force_start = _ForceRefreshStartAction(
                charm.event, first_unit_to_refresh=self._units[0], in_progress=self.in_progress
            )
        except _InvalidForceEvent:
            force_start = None
        self._unit_status_higher_priority: typing.Optional[charm.Status] = None
        if not self._in_progress:
            return
        self._refresh_started = any(
            # During scale up, unit may be missing from relation
            self._app_controller_revision
            in self._relation.get(unit, {}).get(
                "refresh_started_if_app_controller_revision_hash_in", tuple()
            )
            for unit in self._units
        )
        """TODO"""  # TODO

        if not charm.unit == self._units[0]:
            return
        if not self._refresh_started:
            # Check if this unit is rolling back
            original_versions = _OriginalVersions.from_app_databag(self._relation.my_app)
            if (
                original_versions.charm == self._installed_charm_version
                and original_versions.workload_container
                == self._installed_workload_container_version
            ):
                # Rollback to original charm code & workload container version; skip checks
                self._refresh_started = True
                hashes: typing.MutableSequence[str] = self._relation.my_unit.setdefault(
                    "refresh_started_if_app_controller_revision_hash_in", tuple()
                )
                if self._unit_controller_revision not in hashes:
                    hashes.append(self._unit_controller_revision)
                self._refresh_started_local_state.touch()
        if self._refresh_started:
            if force_start:
                force_start.fail("refresh already started")  # TODO UX
            return

        # Run checks TODO comment
        charm_revision = original_versions.charm_revision.split("-")[
            -1
        ]  # TODO improve docstring/naming on _OriginalVersions
        rollback_command = f"juju refresh {charm.app} --revision {charm_revision} --resource {self._charm_specific.oci_resource_name}={self._installed_workload_image_name}@{original_versions.workload_container}"
        if force_start and not force_start.check_workload_container:
            force_start.log(
                f"Skipping check that refresh is to {self._charm_specific.workload_name} container version that has been validated to work with the charm revision"
            )
        else:
            # Check workload container
            if (
                self._installed_workload_container_version
                == self._pinned_workload_container_version
            ):
                if force_start:
                    force_start.log(
                        f"Checked that refresh is to {self._charm_specific.workload_name} container version that has been validated to work with the charm revision"
                    )
            else:
                self._unit_status_higher_priority = charm.BlockedStatus(
                    "`juju refresh` was run with missing/incorrect OCI resource. Rollback with instructions in docs or see `juju debug-log`"
                )
                logger.error(
                    f"`juju refresh` was run with missing or incorrect OCI resource. Rollback by running `{rollback_command}`. If you are intentionally attempting to refresh to a {self._charm_specific.workload_name} container version that is not validated with this release, you may experience data loss and/or downtime as a result of refreshing. The refresh can be forced to continue with the `force-refresh-start` action and the `check-workload-container` parameter. Run `juju show-action {charm.app} force-refresh-start` for more information"
                )
                if force_start:
                    force_start.fail(
                        f"Refresh is to {self._charm_specific.workload_name} container version that has not been validated to work with the charm revision. Rollback by running `{rollback_command}`"
                    )
                return
        if force_start and not force_start.check_compatibility:
            force_start.log(
                f"Skipping check for compatibility with previous {self._charm_specific.workload_name} version and charm revision"
            )
        else:
            # Check compatibility
            if self._charm_specific.is_compatible(
                old_charm_version=original_versions.charm,
                new_charm_version=self._installed_charm_version,
                old_workload_version=original_versions.workload,
                # TODO what to do if workload image not pinned
                new_workload_version=self._pinned_workload_version,
            ):
                if force_start:
                    force_start.log(
                        f"Checked that refresh from previous {self._charm_specific.workload_name} version and charm revision to current versions is compatible"
                    )
            else:
                self._unit_status_higher_priority = charm.BlockedStatus(
                    "Refresh incompatible. Rollback with instructions in Charmhub docs or see `juju debug-log`"
                )
                logger.info(
                    f"Refresh incompatible. Rollback by running `{rollback_command}`. Continuing this refresh may cause data loss and/or downtime. The refresh can be forced to continue with the `force-refresh-start` action and the `check-compatibility` parameter. Run `juju show-action {charm.app} force-refresh-start` for more information"
                )
                if force_start:
                    force_start.fail(
                        f"Refresh incompatible. Rollback by running `{rollback_command}`"
                    )
                return
        if force_start and not force_start.run_pre_refresh_checks:
            force_start.log("Skipping pre-refresh checks")
        else:
            # Run pre-refresh checks
            if force_start:
                force_start.log("Running pre-refresh checks")
            try:
                self._charm_specific.run_pre_refresh_checks_after_1_unit_refreshed()
            except PrecheckFailed as exception:
                self._unit_status_higher_priority = charm.BlockedStatus(
                    f"Rollback with `juju refresh`. Pre-refresh check failed: {exception.message}"
                )
                logger.error(
                    f"Pre-refresh check failed: {exception.message}. Rollback by running `{rollback_command}`. Continuing this refresh may cause data loss and/or downtime. The refresh can be forced to continue with the `force-refresh-start` action and the `run-pre-refresh-checks` parameter. Run `juju show-action {charm.app} force-refresh-start` for more information"
                )
                if force_start:
                    force_start.fail(
                        f"Pre-refresh check failed: {exception.message}. Rollback by running `{rollback_command}`"
                    )
                return
            if force_start:
                force_start.log("Pre-refresh checks successful")
        # All checks that ran succeeded
        self._refresh_started = True
        hashes: typing.MutableSequence[str] = self._relation.my_unit.setdefault(
            "refresh_started_if_app_controller_revision_hash_in", tuple()
        )
        if self._unit_controller_revision not in hashes:
            hashes.append(self._unit_controller_revision)
        self._refresh_started_local_state.touch()
        if force_start:
            force_start.result = {
                "result": f"{self._charm_specific.workload_name} refreshed on unit {charm.unit.number}. Starting {self._charm_specific.workload_name} on unit {charm.unit.number}"
            }

    def _set_partition_and_app_status(self, *, handle_action: bool):
        """Lower StatefulSet partition and set `self._app_status_higher_priority` & app status

        Handles resume-refresh action if `handle_action`

        App status only set if `self._app_status_higher_priority` (app status is not cleared if
        `self._app_status_higher_priority` is `None`—that is the responsibility of the charm)
        """
        # `handle_action` parameter needed to prevent duplicate action logs if this method is
        # called twice in one Juju event

        self._app_status_higher_priority: typing.Optional[charm.Status] = None

        class _ResumeRefreshAction(charm.ActionEvent):
            def __init__(self, event: charm.ActionEvent, /):
                super().__init__()
                assert event.action == "resume-refresh"
                self.check_health_of_refreshed_units: bool = event.parameters[
                    "check-health-of-refreshed-units"
                ]

        action: typing.Optional[_ResumeRefreshAction] = None
        if isinstance(charm.event, charm.ActionEvent) and charm.event.action == "resume-refresh":
            action = _ResumeRefreshAction(charm.event)
        if not charm.is_leader:
            if handle_action and action:
                action.fail(
                    f"Must run action on leader unit. (e.g. `juju run {charm.app}/leader resume-refresh`)"
                )
            return
        if self._pause_after is _PauseAfter.UNKNOWN:
            self._app_status_higher_priority = charm.BlockedStatus(
                'pause_after_unit_refresh config must be set to "all", "first", or "none"'
            )
        if not self._in_progress:
            self._set_partition(0)
            if handle_action and action:
                action.fail("No refresh in progress")
            # TODO log
            if self._app_status_higher_priority:
                charm.app_status = self._app_status_higher_priority
            return
        if (
            handle_action
            and action
            and self._pause_after is _PauseAfter.NONE
            and action.check_health_of_refreshed_units
        ):
            action.fail(
                "`pause_after_unit_refresh` config is set to `none`. This action is not applicable."
            )
            # TODO comment
            action = None

        for index, unit in enumerate(self._units):
            if unit.controller_revision != self._app_controller_revision:
                break
        next_unit_to_refresh = unit
        next_unit_to_refresh_index = index

        # Determine if `next_unit_to_refresh` is allowed to refresh
        if action and not action.check_health_of_refreshed_units:
            allow_next_unit_to_refresh = True
            if handle_action:
                action.log("Ignoring health of refreshed units")
                action.result = {
                    "result": f"Attempting to refresh unit {next_unit_to_refresh.number}"
                }
        elif not self._refresh_started:
            allow_next_unit_to_refresh = False
            if handle_action and action:
                assert action.check_health_of_refreshed_units
                # TODO: change message to refresh not started? (for scale up case)
                action.fail(f"Unit {self._units[0].number} is unhealthy. Refresh will not resume.")
        else:
            # Check if up-to-date units have allowed the next unit to refresh
            up_to_date_units = self._units[:next_unit_to_refresh_index]
            for unit in up_to_date_units:
                if (
                    # During scale up, `unit` may be missing from relation
                    self._relation.get(unit, {}).get(
                        "next_unit_allowed_to_refresh_if_app_controller_revision_hash_equals"
                    )
                    != self._app_controller_revision
                ):
                    # `unit` has not allowed the next unit to refresh
                    allow_next_unit_to_refresh = False
                    if handle_action and action:
                        action.fail(f"Unit {unit.number} is unhealthy. Refresh will not resume.")
                    break
            else:
                # All up-to-date units have allowed the next unit to refresh
                if (
                    action
                    or self._pause_after is _PauseAfter.NONE
                    or (self._pause_after is _PauseAfter.FIRST and next_unit_to_refresh_index >= 2)
                ):
                    allow_next_unit_to_refresh = True
                    if handle_action and action:
                        assert self._pause_after is not _PauseAfter.NONE
                        if self._pause_after is _PauseAfter.FIRST:
                            action.result = {
                                "result": f"Refresh resumed. Unit {next_unit_to_refresh.number} is refreshing next"
                            }
                        else:
                            assert (
                                self._pause_after is _PauseAfter.ALL
                                or self._pause_after is _PauseAfter.UNKNOWN
                            )
                            action.result = {
                                "result": f"Unit {next_unit_to_refresh.number} is refreshing next"
                            }
                else:
                    # User must run resume-refresh action to refresh `next_unit_to_refresh`
                    allow_next_unit_to_refresh = False

        if allow_next_unit_to_refresh:
            target_partition = next_unit_to_refresh.number
        else:
            # Use unit before `next_unit_to_refresh`, if it exists, to determine `target_partition`
            target_partition = self._units[max(next_unit_to_refresh_index - 1, 0)].number
        # Only lower the partition—do not raise it
        # If the partition is lowered and then quickly raised, the unit that is refreshing will not
        # be able to start. This is a Juju bug: https://bugs.launchpad.net/juju/+bug/2073473
        # (If this method is called during the resume-refresh action and then called in another
        # Juju event a few seconds later, `target_partition` can be higher than it was during the
        # resume-refresh action.)
        partition = self._get_partition()
        if target_partition < partition:
            self._set_partition(target_partition)
            partition = target_partition
        # TODO log
        if self._pause_after is _PauseAfter.ALL or (
            self._pause_after is _PauseAfter.FIRST
            # Whether only the first unit is allowed to refresh
            and partition >= self._units[0].number
        ):
            self._app_status_higher_priority = charm.BlockedStatus(
                f"Refreshing. Check units >={partition} are healthy & run `resume-refresh` on leader. To rollback, see docs or `juju debug-log`"
            )
        else:
            self._app_status_higher_priority = charm.MaintenanceStatus(
                f"Refreshing. To pause refresh, run `juju config {charm.app} pause_after_unit_refresh=all`"
            )
        assert self._app_status_higher_priority is not None
        charm.app_status = self._app_status_higher_priority

    def __init__(self, charm_specific: CharmSpecific, /):
        assert charm_specific.cloud is Cloud.KUBERNETES
        self._charm_specific = charm_specific

        _LOCAL_STATE.mkdir(exist_ok=True)
        # Save state if unit is tearing down.
        # Used in future Juju event (stop event) to determine whether to set StatefulSet partition
        tearing_down = _LOCAL_STATE / "kubernetes_unit_tearing_down"
        if (
            isinstance(charm.event, charm.RelationDepartedEvent)
            and charm.event.departing_unit == charm.unit
        ):
            # Unit is tearing down and 1+ other units are not tearing down
            tearing_down.touch(exist_ok=True)
        # Check if Juju app was deployed with `--trust` (needed to patch StatefulSet partition)
        if not (
            lightkube.Client()
            .create(
                lightkube.resources.authorization_v1.SelfSubjectAccessReview(
                    spec=lightkube.models.authorization_v1.SelfSubjectAccessReviewSpec(
                        resourceAttributes=lightkube.models.authorization_v1.ResourceAttributes(
                            name=charm.app,
                            namespace=charm.model,
                            resource="statefulset",
                            verb="patch",
                        )
                    )
                )
            )
            .status.allowed
        ):
            logger.warning(
                f"Run `juju trust {charm.app} --scope=cluster`. Needed for in-place refreshes"
            )
            if charm.is_leader:
                charm.app_status = charm.BlockedStatus(
                    f"Run `juju trust {charm.app} --scope=cluster`. Needed for in-place refreshes"
                )
            raise KubernetesJujuAppNotTrusted
        if (
            isinstance(charm.event, charm.StopEvent)
            # If `tearing_down.exists()`, this unit is being removed for scale down.
            # Therefore, we should not raise the partition—so that the partition never exceeds the
            # highest unit number (which would cause `juju refresh` to not trigger any Juju
            # events).
            and not tearing_down.exists()
        ):
            # This unit could be refreshing or just restarting.
            # Raise StatefulSet partition to prevent other units from refreshing in case a refresh
            # is in progress.
            # If a refresh is not in progress, the leader unit will reset the partition to 0.
            if self._get_partition() < charm.unit.number:
                # Raise partition
                self._set_partition(charm.unit.number)
                logger.info(f"Set StatefulSet partition to {charm.unit.number} during stop event")

        self._relation = charm_json.PeerRelation.from_endpoint("refresh-v-three")
        if not self._relation:
            raise PeerRelationMissing

        # TODO comment
        self._relation.my_unit["pause_after_unit_refresh_config"] = charm.config[
            "pause_after_unit_refresh"
        ]

        # Get app & unit controller revisions from Kubernetes API
        # Each `juju refresh` updates the app's StatefulSet which creates a new controller revision
        # https://kubernetes.io/docs/reference/kubernetes-api/workload-resources/controller-revision-v1/
        # Controller revisions are used by Kubernetes for StatefulSet rolling updates
        stateful_set = lightkube.Client().get(lightkube.resources.apps_v1.StatefulSet, charm.app)
        self._app_controller_revision: str = stateful_set.status.updateRevision
        """This app's controller revision"""
        assert self._app_controller_revision is not None
        pods = lightkube.Client().list(
            lightkube.resources.core_v1.Pod, labels={"app.kubernetes.io/name": charm.app}
        )
        unsorted_units = []
        for pod in pods:
            unit = _KubernetesUnit.from_pod(pod)
            unsorted_units.append(unit)
            if unit == charm.unit:
                this_pod = pod
        assert this_pod
        self._units = sorted(unsorted_units, reverse=True)
        """Sorted from highest to lowest unit number (refresh order)"""
        self._unit_controller_revision = next(
            unit for unit in self._units if unit == charm.unit
        ).controller_revision
        """This unit's controller revision"""

        # Get installed charm revision
        dot_juju_charm = pathlib.Path(".juju-charm")
        self._installed_charm_revision_raw = dot_juju_charm.read_text().strip()
        """Contents of this unit's .juju-charm file (e.g. "ch:amd64/jammy/postgresql-k8s-381")"""

        # Get versions from refresh_versions.toml
        refresh_versions = _RefreshVersions.from_file()
        self._installed_charm_version = refresh_versions.charm
        """This unit's charm version"""
        self._pinned_workload_version = refresh_versions.workload
        """Upstream workload version (e.g. 14.11) pinned by this unit's charm code"""
        # TODO improve docstring

        # Get installed & pinned workload container digest
        metadata_yaml = yaml.safe_load(pathlib.Path("metadata.yaml").read_text())
        upstream_source = (
            metadata_yaml.get("resources", {})
            .get(self._charm_specific.oci_resource_name)
            .get("upstream-source")
        )
        if not isinstance(upstream_source, str):
            raise ValueError(
                f"Unable to find `upstream-source` for {self._charm_specific.oci_resource_name=} resource in metadata.yaml `resources`"
            )
        try:
            _, digest = upstream_source.split("@")
            if not digest.startswith("sha256:"):
                raise ValueError
        except ValueError:
            raise ValueError(
                f"OCI image in `upstream-source` must be pinned to a digest (e.g. ends with '@sha256:76ef26c7d11a524bcac206d5cb042ebc3c8c8ead73fa0cd69d21921552db03b6'): {repr(upstream_source)}"
            )
        else:
            self._pinned_workload_container_version = digest
            """Workload image digest pinned by this unit's charm code

            (e.g. "sha256:76ef26c7d11a524bcac206d5cb042ebc3c8c8ead73fa0cd69d21921552db03b6)"
            """
        workload_containers: typing.List[str] = [
            key
            for key, value in metadata_yaml.get("containers", {}).items()
            if value.get("resource") == self._charm_specific.oci_resource_name
        ]
        if len(workload_containers) == 0:
            raise ValueError(
                f"Unable to find workload container with {self._charm_specific.oci_resource_name=} in metadata.yaml `containers`"
            )
        elif len(workload_containers) > 1:
            raise ValueError(
                f"Expected 1 container. Found {len(workload_containers)} workload containers with {self._charm_specific.oci_resource_name=} in metadata.yaml `containers`: {repr(workload_containers)}"
            )
        else:
            workload_container = workload_containers[0]
        # TODO race condition on startup?
        workload_container_statuses = [
            status
            for status in this_pod.status.containerStatuses
            if status.name == workload_container
        ]
        if len(workload_container_statuses) == 0:
            raise ValueError(f"Unable to find {workload_container} container for this unit's pod")
        if len(workload_container_statuses) > 1:
            raise ValueError(
                f"Found multiple {workload_container} containers for this unit's pod. Expected 1 container"
            )
        # Example: "registry.jujucharms.com/charm/kotcfrohea62xreenq1q75n1lyspke0qkurhk/postgresql-image@sha256:76ef26c7d11a524bcac206d5cb042ebc3c8c8ead73fa0cd69d21921552db03b6"
        image_id = workload_container_statuses[0].imageID
        image_name, image_digest = image_id.split("@")
        self._installed_workload_image_name = image_name
        """This unit's workload image name
        
        Includes registry and path
        
        (e.g. "registry.jujucharms.com/charm/kotcfrohea62xreenq1q75n1lyspke0qkurhk/postgresql-image")
        """
        self._installed_workload_container_version = image_digest
        """This unit's workload image digest
        
        (e.g. "sha256:76ef26c7d11a524bcac206d5cb042ebc3c8c8ead73fa0cd69d21921552db03b6)"
        """

        self._refresh_started_local_state = _LOCAL_STATE / "kubernetes_refresh_started"
        # TODO comment
        if self._refresh_started_local_state.exists():
            hashes: typing.MutableSequence[str] = self._relation.my_unit.setdefault(
                "refresh_started_if_app_controller_revision_hash_in", tuple()
            )
            if self._unit_controller_revision not in hashes:
                hashes.append(self._unit_controller_revision)

        # Determine `self._in_progress`
        for unit in self._units:
            if unit.controller_revision != self._app_controller_revision:
                self._in_progress = True
                break
        else:
            self._in_progress = False

        # Determine `self._pause_after`
        # TODO comment
        # It's possible that no units are up-to-date—if the first unit to refresh is stopping
        # before it's refreshed. In that case, units with the same controller revision as the first
        # unit to refresh are the closest to up-to-date.
        most_up_to_date_units = (
            unit
            for unit in self._units
            if unit.controller_revision == self._units[0].controller_revision
        )
        pause_after_values = (
            # During scale up or initial install, unit or "pause_after_unit_refresh_config" key may
            # be missing from relation
            self._relation.get(unit, {}).get("pause_after_unit_refresh_config")
            for unit in most_up_to_date_units
        )
        # Exclude `None` values (for scale up or initial install) to avoid displaying app status
        # that says pause_after_unit_refresh is set to invalid value
        pause_after_values = (value for value in pause_after_values if value is not None)
        self._pause_after = max(_PauseAfter(value) for value in pause_after_values)

        if not self._in_progress:
            # Clean up state that is no longer in use
            self._relation.my_unit.pop("refresh_started_if_app_controller_revision_hash_in", None)
        if not self._in_progress and isinstance(
            # Whether this unit is leader
            self._relation.my_app,
            collections.abc.MutableMapping,
        ):
            # TODO: add note about doing this before compat check in case force refresh?
            # Save versions in app databag for next refresh
            self._original_versions = _OriginalVersions(
                workload=self._pinned_workload_version,
                workload_container=self._installed_workload_container_version,
                charm=self._installed_charm_version,
                charm_revision=self._installed_charm_revision_raw,
            )
            self._original_versions.write_to_app_databag(self._relation.my_app)

        # pre-refresh-check action
        if isinstance(charm.event, charm.ActionEvent) and charm.event.action == "pre-refresh-check":
            if self._in_progress:
                charm.event.fail("Refresh already in progress")
            elif charm.is_leader:
                assert self._original_versions
                try:
                    self._charm_specific.run_pre_refresh_checks_before_any_units_refreshed()
                except PrecheckFailed as exception:
                    charm.event.fail(
                        f"Charm is not ready for refresh. Pre-refresh check failed: {exception.message}"
                    )
                else:
                    charm.event.result = {
                        "result": f"Charm is ready for refresh. For refresh instructions, see {self._charm_specific.refresh_user_docs_url}\n"
                        "After the refresh has started, use this command to rollback (copy this down in case you need it later):\n"
                        f"`juju refresh {charm.app} --revision {self._original_versions.charm_revision} --resource {self._charm_specific.oci_resource_name}={self._installed_workload_image_name}@{self._original_versions.workload_container}`"
                    }
            else:
                charm.event.fail(
                    f"Must run action on leader unit. (e.g. `juju run {charm.app}/leader pre-refresh-check`)"
                )

        self._start_refresh()

        self._set_partition_and_app_status(handle_action=True)
