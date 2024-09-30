import abc
import dataclasses
import enum
import functools
import logging
import pathlib
import typing

import charm
import lightkube
import lightkube.resources.apps_v1
import lightkube.resources.core_v1
import ops
import packaging.version

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
            raise ValueError(
                f"{type(self).__name__} message must be longer than 0 characters"
            )
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
            raise ValueError(
                "`refresh_snap` can only be called if `cloud` is `Cloud.MACHINES`"
            )

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
        if not cls._is_charm_version_compatible(
            old=old_charm_version, new=new_charm_version
        ):
            return False


class PeerRelationMissing(Exception):
    """Refresh peer relation is not yet available"""


class Refresh:
    # TODO: add note about putting at end of charm __init__

    @property
    def in_progress(self) -> bool:
        """Whether a refresh is currently in progress"""

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

    @next_unit_allowed_to_refresh.setter
    def next_unit_allowed_to_refresh(self, value: typing.Literal[True]):
        if value is not True:
            raise ValueError("`next_unit_allowed_to_refresh` can only be set to `True`")

    def update_snap_revision(self):
        """Must be called immediately after the workload snap is refreshed

        Only applicable if cloud is `Cloud.MACHINES`

        If the charm code raises an uncaught exception in the same Juju event where this method is
        called, this method does not need to be called again. (That situation will be automatically
        handled.)

        When the snap is refreshed, `next_unit_allowed_to_refresh` is automatically reset to
        `False`.
        """
        if self.charm_specific.cloud is not Cloud.MACHINES:
            raise ValueError(
                "`update_snap_revision` can only be called if cloud is `Cloud.MACHINES`"
            )

    @property
    def pinned_snap_revision(self) -> str:
        """Workload snap revision pinned by this unit's current charm code

        This attribute should only be read during initial snap installation and should not be read
        during a refresh.

        During a refresh, the snap revision should be read from the `refresh_snap` method's
        `snap_revision` parameter.
        """
        if self.charm_specific.cloud is not Cloud.MACHINES:
            raise ValueError(
                "`pinned_snap_revision` can only be accessed if cloud is `Cloud.MACHINES`"
            )
        # TODO: raise exception if accessed while self.in_progress—but scale up case

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
        if self.charm_specific.cloud is not Cloud.KUBERNETES:
            return True

    @property
    def app_status_higher_priority(self) -> typing.Optional[ops.StatusBase]:
        """App status with higher priority than any other app status in the charm

        Charm code should ensure that this status is not overriden
        """

    @property
    def unit_status_higher_priority(self) -> typing.Optional[ops.StatusBase]:
        """Unit status with higher priority than any other unit status in the charm

        Charm code should ensure that this status is not overriden
        """

    @property
    def unit_status_lower_priority(self) -> typing.Optional[ops.StatusBase]:
        """Unit status with lower priority than any other unit status with a message in the charm

        This status will not be automatically set. It should be set by the charm code if there is
        no other unit status with a message to display.
        """

    def __init__(self, charm_specific: CharmSpecific, /):
        self.charm_specific = charm_specific
        if self.charm_specific.cloud is Cloud.KUBERNETES:
            # Save state if unit is tearing down.
            # Used in future Juju event (stop event) to determine whether to set Kubernetes
            # partition
            tearing_down = pathlib.Path(".charm_refresh_v3/unit_tearing_down")
            if (
                isinstance(charm.event, charm.RelationDepartedEvent)
                and charm.event.departing_unit == charm.unit
            ):
                # Unit is tearing down and 1+ other units are not tearing down
                tearing_down.parent.mkdir(exist_ok=True)
                tearing_down.touch(exist_ok=True)

            # TODO check deployed with trust
            client = lightkube.Client()
        if (
            self.charm_specific.cloud is Cloud.KUBERNETES
            and isinstance(charm.event, charm.StopEvent)
            # If `tearing_down.exists()`, this unit is being removed for scale down.
            # Therefore, we should not raise the partition—so that the partition never exceeds
            # the highest unit number (which would cause `juju refresh` to not trigger any Juju
            # events).
            and not tearing_down.exists()
        ):
            # This unit could be refreshing or just restarting.
            # Raise StatefulSet partition to prevent other units from refreshing in case a refresh
            # is in progress.
            # If a refresh is not in progress, the leader unit will reset the partition to 0.
            stateful_set = client.get(
                lightkube.resources.apps_v1.StatefulSet, charm.app
            )
            partition = stateful_set.spec.updateStrategy.rollingUpdate.partition
            assert partition is not None
            if partition < charm.unit.number:
                # Raise partition
                client.patch(
                    lightkube.resources.apps_v1.StatefulSet,
                    charm.app,
                    {
                        "spec": {
                            "updateStrategy": {
                                "rollingUpdate": {"partition": charm.unit.number}
                            }
                        }
                    },
                )
                logger.info(f"Set StatefulSet partition to {charm.unit.number} during stop event")
        relation = charm.Endpoint("refresh-v-three").relation
        if not relation:
            raise PeerRelationMissing
        # TODO comment
        relation.my_unit["pause_after_unit_refresh_config"] = charm.config[
            "pause_after_unit_refresh"
        ]
        # TODO update snap revision in databag
        # Check if refresh in progress
        if self.charm_specific.cloud is Cloud.KUBERNETES:
            stateful_set = client.get(
                lightkube.resources.apps_v1.StatefulSet, charm.app
            )
            app_controller_revision = stateful_set.status.updateRevision
            assert app_controller_revision is not None
            pods = client.list(
                lightkube.resources.core_v1.Pod,
                labels={"app.kubernetes.io/name": charm.app},
            )

            def get_unit(pod_name: str):
                # Example `pod_name`: "postgresql-k8s-0"
                *app_name, unit_number = pod_name.split("-")
                # Example: "postgresql-k8s/0"
                unit_name = f'{"-".join(app_name)}/{unit_number}'
                return charm.Unit(unit_name)

            unit_controller_revisions = {
                get_unit(pod.metadata.name): pod.metadata.labels[
                    "controller-revision-hash"
                ]
                for pod in pods
            }
            self._in_progress = any(
                revision != app_controller_revision
                for revision in unit_controller_revisions.values()
            )
        else:
            pass
