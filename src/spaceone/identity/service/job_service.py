import logging
import random
from datetime import datetime, timedelta
from typing import Union, List, Tuple

from spaceone.core.service import *
from spaceone.core.service.utils import *

from spaceone.identity.conf.global_conf import WORKSPACE_COLORS_NAME
from spaceone.identity.error.error_job import *
from spaceone.identity.manager.account_collector_plugin_manager import (
    AccountCollectorPluginManager,
)
from spaceone.identity.manager.job_manager import JobManager
from spaceone.identity.manager.project_manager import ProjectManager
from spaceone.identity.manager.project_group_manager import ProjectGroupManager
from spaceone.identity.manager.provider_manager import ProviderManager
from spaceone.identity.manager.schema_manager import SchemaManager
from spaceone.identity.manager.service_account_manager import ServiceAccountManager
from spaceone.identity.manager.secret_manager import SecretManager
from spaceone.identity.manager.trusted_account_manager import TrustedAccountManager
from spaceone.identity.manager.workspace_manager import WorkspaceManager
from spaceone.identity.model.project.database import Project
from spaceone.identity.model.project_group.database import ProjectGroup
from spaceone.identity.model.provider.database import Provider
from spaceone.identity.model.service_account.database import ServiceAccount
from spaceone.identity.model.trusted_account.database import TrustedAccount
from spaceone.identity.model.job.database import Job
from spaceone.identity.model.job.request import *
from spaceone.identity.model.job.response import *
from spaceone.identity.model.workspace.database import Workspace

_LOGGER = logging.getLogger(__name__)


@authentication_handler
@authorization_handler
@mutation_handler
@event_handler
class JobService(BaseService):
    resource = "Job"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.job_mgr = JobManager()
        self.trusted_account_mgr = TrustedAccountManager()
        self.provider_mgr = ProviderManager()
        self.account_collector_plugin_mgr = AccountCollectorPluginManager()
        self.workspace_mgr = WorkspaceManager()
        self.service_account_mgr = ServiceAccountManager()
        self.project_mgr = ProjectManager()
        self.project_group_mgr = ProjectGroupManager()

    @transaction(exclude=["authentication", "authorization", "mutation"])
    def create_jobs_by_trusted_account(self, params: dict):
        """Create jobs by trusted account
        Args:
            params (dict): {
                'params':
            }
        Returns:
            None:
        """

        current_hour = params.get("current_hour", datetime.utcnow().hour)

        for trusted_account_vo in self._get_all_schedule_enabled_trusted_accounts(
            current_hour
        ):
            try:
                self.create_service_account_job(trusted_account_vo, {})
            except Exception as e:
                _LOGGER.error(
                    f"[create_jobs_by_trusted_account] sync error: {e}", exc_info=True
                )

    @transaction(
        permission="identity:Job.write", role_types=["DOMAIN_ADMIN", "WORKSPACE_OWNER"]
    )
    @convert_model
    def delete(self, params: JobDeleteRequest) -> None:
        """Delete job
        Args:
            params (JobDeleteRequest): {
                'job_id': 'str',            # required
                'workspace_id': 'str',      # injected from auth (required)
                'domain_id': 'str'          # injected from auth (required)
            }
        Returns:
            None:
        """

        job_vo = self.job_mgr.get_job(
            params.domain_id,
            params.job_id,
            params.workspace_id,
        )

        self.job_mgr.delete_job_by_vo(job_vo)

    @transaction(
        permission="identity:Job.read",
        role_types=["DOMAIN_ADMIN", "WORKSPACE_OWNER", "WORKSPACE_MEMBER"],
    )
    @change_value_by_rule("APPEND", "workspace_id", "*")
    @convert_model
    def get(self, params: JobGetRequest) -> JobResponse:
        """Get job
        Args:
            params (JobGetRequest): {
                'job_id': 'str',            # required
                'workspace_id': 'str',      # injected from auth (required)
                'domain_id': 'str'          # injected from auth (required)
            }
        Returns:
            JobInfo:
        """

        job_id = params.job_id
        workspace_id = params.workspace_id
        domain_id = params.domain_id

        job_vo = self.job_mgr.get_job(domain_id, job_id, workspace_id)

        return JobResponse(**job_vo.to_dict())

    @transaction(
        permission="identity:Job.read",
        role_types=["DOMAIN_ADMIN", "WORKSPACE_OWNER", "WORKSPACE_MEMBER"],
    )
    @change_value_by_rule("APPEND", "workspace_id", "*")
    @append_query_filter(
        [
            "job_id",
            "status",
            "trusted_account_id",
            "plugin_id",
            "workspace_id",
            "domain_id",
        ]
    )
    @append_keyword_filter(["job_id", "status"])
    @convert_model
    def list(self, params: JobSearchQueryRequest) -> Union[JobsResponse, dict]:
        """List jobs
        Args:
            params (JobSearchQueryRequest): {
                'query': 'dict',
                'job_id': 'str',
                'status': 'str',
                'trusted_account_id': 'str',
                'plugin_id': 'str',
                'workspace_id': 'str',
                'domain_id': 'str'          # injected from auth (required)
            }
        Returns:
            JobsResponse:
        """

        query = params.query or {}

        job_vos, total_count = self.job_mgr.list_jobs(query)
        jobs_info = [job_vo.to_dict() for job_vo in job_vos]

        return JobsResponse(results=jobs_info, total_count=total_count)

    @transaction(
        permission="identity:Job.read",
        role_types=["DOMAIN_ADMIN", "WORKSPACE_OWNER", "WORKSPACE_MEMBER"],
    )
    @append_query_filter(["workspace_id", "domain_id"])
    @append_keyword_filter(["job_id"])
    @convert_model
    def stat(self, params: JobStatQueryRequest) -> dict:
        """Stat jobs
        Args:
            params (JobStatQueryRequest): {
                'domain_id': 'str' # injected from auth (required)
            }
        Returns:
            dict:
        """

        query = params.query or {}

        return self.job_mgr.stat_jobs(query)

    @transaction(exclude=["authentication", "authorization", "mutation"])
    def sync_service_accounts(self, params: dict) -> None:
        """Sync account data
        Args:
            params (dict): {
                    'job_id': 'str',
                    'trusted_account_id': 'str',
                    'trusted_secret_id': 'str',
                    'secret_data': 'dict',
                    'workspace_id': 'str',
                    'domain_id': 'str'
                    'options': 'dict'
            }
        Returns:
            None:
        """

        trusted_secret_id = params["trusted_secret_id"]
        secret_data = params["secret_data"]
        trusted_account_id = params["trusted_account_id"]
        job_id = params["job_id"]
        workspace_id = params.get("workspace_id")
        domain_id = params["domain_id"]

        schema_mgr = SchemaManager()

        trusted_account_vo: TrustedAccount = (
            self.trusted_account_mgr.get_trusted_account(
                trusted_account_id, domain_id, workspace_id
            )
        )
        job_vo: Job = self.job_mgr.get_job(domain_id, job_id, workspace_id)
        schema_mgr.get_schema(trusted_account_vo.secret_schema_id, domain_id)

        provider_vo: Provider = self.provider_mgr.get_provider(
            trusted_account_vo.provider, domain_id
        )
        plugin_info = provider_vo.plugin_info

        provider = provider_vo.provider
        sync_options = trusted_account_vo.sync_options or {}
        plugin_options = trusted_account_vo.plugin_options or {}

        if self._is_job_failed(job_id, domain_id, job_vo.workspace_id):
            self.job_mgr.change_canceled_status(job_vo)
        else:
            self.job_mgr.change_in_progress_status(job_vo)

            try:
                # Merge plugin options and trusted_account plugin options
                options = plugin_info.get("options", {})
                options.update(plugin_options)
                schema_id = plugin_info.get("schema_id")

                (
                    endpoint,
                    updated_version,
                ) = self.account_collector_plugin_mgr.get_account_collector_plugin_endpoint(
                    plugin_info, domain_id
                )

                self.account_collector_plugin_mgr.initialize(endpoint)
                start_dt = datetime.utcnow()

                is_canceled = False

                response = self.account_collector_plugin_mgr.sync(
                    endpoint, options, secret_data, domain_id, schema_id
                )

                for result in response.get("results", []):
                    location: List[dict] = self._get_location(
                        result, trusted_account_vo.resource_group, sync_options
                    )

                    if trusted_account_vo.resource_group == "DOMAIN":
                        if sync_options.get("single_workspace_id"):
                            workspace_vo = self.workspace_mgr.get_workspace(
                                sync_options.get("single_workspace_id"), domain_id
                            )
                        elif location:
                            location_info = location.pop(0)
                            workspace_vo = self._create_workspace(
                                domain_id, trusted_account_id, location_info
                            )
                        else:
                            _LOGGER.debug(
                                f"[sync_service_accounts] location is empty => SKIP"
                            )
                            continue

                        sync_workspace_id = workspace_vo.workspace_id

                    else:
                        sync_workspace_id = workspace_id

                    parent_group_id = None
                    for location_info in location:
                        project_group_vo = self._create_project_group(
                            domain_id,
                            sync_workspace_id,
                            trusted_account_id,
                            location_info,
                            parent_group_id,
                        )
                        parent_group_id = project_group_vo.project_group_id

                    project_vo = self._create_project(
                        result,
                        domain_id,
                        sync_workspace_id,
                        trusted_account_id,
                        project_group_id=parent_group_id,
                        sync_options=sync_options,
                    )
                    self._create_service_account(
                        result,
                        project_vo,
                        trusted_account_id,
                        trusted_secret_id,
                        provider,
                        sync_options,
                    )

                if self._is_job_failed(job_id, domain_id, job_vo.workspace_id):
                    self.job_mgr.change_canceled_status(job_vo)
                    is_canceled = True

                if not is_canceled:
                    end_dt = datetime.utcnow()
                    _LOGGER.debug(
                        f"[sync_service_accounts] end job ({job_vo.job_id}): {end_dt}"
                    )
                    _LOGGER.debug(
                        f"[sync_service_accounts] total job time ({job_vo.job_id}): {end_dt - start_dt}"
                    )
                    self.job_mgr.change_success_status(job_vo)

            except Exception as e:
                self.job_mgr.change_error_status(job_vo, e)
                _LOGGER.error(f"[sync_service_accounts] sync error: {e}", exc_info=True)

        self._close_job(
            job_id,
            domain_id,
            job_vo.workspace_id,
        )

    def create_service_account_job(
        self, trusted_account_vo: TrustedAccount, job_options: dict
    ) -> Union[Job, dict]:
        resource_group = trusted_account_vo.resource_group
        provider = trusted_account_vo.provider
        trusted_account_id = trusted_account_vo.trusted_account_id

        if resource_group == "DOMAIN":
            workspace_id = "*"
        else:
            workspace_id = trusted_account_vo.workspace_id

        domain_id = trusted_account_vo.domain_id

        provider_vo = self.provider_mgr.get_provider(provider, domain_id)
        plugin_id = provider_vo.plugin_info["plugin_id"]
        ac_plugin_mgr = AccountCollectorPluginManager()
        endpoint = ac_plugin_mgr.get_account_collector_plugin_endpoint_by_vo(
            provider_vo
        )

        options = provider_vo.plugin_info.get("options")
        schema_id = trusted_account_vo.secret_schema_id

        ac_plugin_mgr.initialize(endpoint)

        try:
            trusted_secret_data = self._get_trusted_secret_data(
                trusted_account_vo.trusted_secret_id, domain_id
            )

            if trusted_secret_data:
                schema_mgr = SchemaManager()
                # Check secret_data by schema
                schema_mgr.validate_secret_data_by_schema_id(
                    schema_id, domain_id, trusted_secret_data, "SECRET"
                )
        except Exception as e:
            trusted_secret_data = {}
            _LOGGER.error(
                f"[created_trusted_account_job] get trusted secret error: {e}",
                exc_info=True,
            )

        # Add Job Options
        job_vo = self.job_mgr.create_job(
            resource_group,
            domain_id,
            workspace_id,
            trusted_account_id,
            plugin_id,
            job_options,
        )

        if self._check_duplicate_job(domain_id, trusted_account_id, job_vo):
            self.job_mgr.change_error_status(
                job_vo, ERROR_DUPLICATE_JOB(trusted_account_id=trusted_account_id)
            )
        else:
            try:
                self.job_mgr.push_job(
                    {
                        "job_id": job_vo.job_id,
                        "trusted_account_id": trusted_account_id,
                        "trusted_secret_id": trusted_account_vo.trusted_secret_id,
                        "secret_data": trusted_secret_data,
                        "workspace_id": trusted_account_vo.workspace_id,
                        "domain_id": domain_id,
                        "options": job_options,
                    }
                )
            except Exception as e:
                self.job_mgr.change_error_status(job_vo, e)

        return job_vo

    def _get_all_schedule_enabled_trusted_accounts(self, current_hour: int) -> list:
        query = {
            "filter": [
                {"k": "schedule.state", "v": "ENABLED", "o": "eq"},
                {"k": "schedule.hours", "v": [current_hour], "o": "in"},
            ]
        }
        (
            trusted_account_vos,
            total_count,
        ) = self.trusted_account_mgr.list_trusted_accounts(query)
        _LOGGER.debug(
            f"[_get_all_schedule_enabled_trusted_accounts] scheduled trusted accounts count (UTC {current_hour}: {total_count}"
        )
        return trusted_account_vos

    def _get_trusted_secret_data(self, trusted_secret_id: str, domain_id: str) -> dict:
        secret_mgr: SecretManager = self.locator.get_manager("SecretManager")
        if trusted_secret_id:
            secret_data = secret_mgr.get_trusted_secret_data(
                trusted_secret_id, domain_id
            )
        else:
            secret_data = {}

        return secret_data

    def _check_duplicate_job(
        self,
        domain_id: str,
        trusted_account_id: str,
        this_job_vo: Job,
    ) -> bool:
        query = {
            "filter": [
                {"k": "trusted_account_id", "v": trusted_account_id, "o": "eq"},
                {"k": "workspace_id", "v": this_job_vo.workspace_id, "o": "eq"},
                {"k": "domain_id", "v": domain_id, "o": "eq"},
                {"k": "status", "v": "IN_PROGRESS", "o": "eq"},
                {"k": "job_id", "v": this_job_vo.job_id, "o": "not"},
            ]
        }

        job_vos, total_count = self.job_mgr.list_jobs(query)

        duplicate_job_time = datetime.utcnow() - timedelta(minutes=10)

        for job_vo in job_vos:
            if job_vo.created_at > duplicate_job_time:
                return True
            else:
                self.job_mgr.change_canceled_by_vo(job_vo)
        return False

    def _is_job_failed(
        self, job_id: str, domain_id: str, workspace_id: str = None
    ) -> bool:
        job_vo: Job = self.job_mgr.get_job(domain_id, job_id, workspace_id)

        if job_vo.status in ["CANCELED", "FAILURE"]:
            return True
        else:
            return False

    def _close_job(
        self,
        job_id: str,
        domain_id: str,
        workspace_id: str = None,
    ):
        job_vo: Job = self.job_mgr.get_job(domain_id, job_id, workspace_id)
        if job_vo.status == "IN_PROGRESS":
            self.job_mgr.change_success_status(job_vo)
        elif job_vo.status == "FAILURE":
            self.job_mgr.update_job_by_vo({"finished_at": datetime.utcnow()}, job_vo)

    def _create_workspace(
        self, domain_id: str, trusted_account_id: str, location_info: dict
    ) -> Workspace:
        name = location_info.get("name")
        reference_id = location_info.get("resource_id")

        workspace_vos = self.workspace_mgr.filter_workspaces(
            domain_id=domain_id, name=name
        )

        _LOGGER.debug(
            f"[_create_workspace] {name} 'domain_id': {domain_id}, count: {len(workspace_vos)}"
        )

        params = {"trusted_account_id": trusted_account_id, "is_managed": True}
        if workspace_vos:
            workspace_vo = workspace_vos[0]

            if workspace_vo.name != name:
                params.update({"name": name})
            if workspace_vo.references and reference_id not in workspace_vo.references:
                params.update({"references": workspace_vo.references + [reference_id]})

            params.update({"last_synced_at": datetime.utcnow()})
            workspace_vo = self.workspace_mgr.update_workspace_by_vo(
                params, workspace_vo
            )

            self._remove_old_reference_id_from_workspace(domain_id, reference_id)
        else:
            params.update(
                {
                    "name": name,
                    "tags": self._set_workspace_theme(),
                    "domain_id": domain_id,
                    "last_synced_at": datetime.utcnow(),
                    "references": [reference_id],
                }
            )
            workspace_vo = self.workspace_mgr.create_workspace(params)
        return workspace_vo

    def _create_project_group(
        self,
        domain_id: str,
        workspace_id: str,
        trusted_account_id: str,
        location_info: dict,
        parent_group_id: str = None,
    ) -> ProjectGroup:
        name = location_info["name"]
        reference_id = location_info["resource_id"]

        filter_params = {
            "is_managed": True,
            "reference_id": reference_id,
            "domain_id": domain_id,
            "workspace_id": workspace_id,
        }

        project_group_vos = self.project_group_mgr.filter_project_groups(
            **filter_params
        )

        params = {
            "trusted_account_id": trusted_account_id,
        }
        if parent_group_id:
            params.update({"parent_group_id": parent_group_id})

        if project_group_vos:
            project_group_vo = project_group_vos[0]
            if project_group_vo.name != name:
                params.update({"name": name})

            params.update({"last_synced_at": datetime.utcnow()})
            project_group_vo = self.project_group_mgr.update_project_group_by_vo(
                params, project_group_vo
            )

        else:
            params.update(
                {
                    "name": name,
                    "reference_id": reference_id,
                    "is_managed": True,
                    "domain_id": domain_id,
                    "workspace_id": workspace_id,
                    "last_synced_at": datetime.utcnow(),
                }
            )
            if parent_group_id:
                params["parent_group_id"] = parent_group_id
            project_group_vo = self.project_group_mgr.create_project_group(params)

        return project_group_vo

    def _create_project(
        self,
        result: dict,
        domain_id: str,
        workspace_id: str,
        trusted_account_id: str,
        project_group_id: str = None,
        sync_options: dict = None,
        project_type: str = "PRIVATE",
    ) -> Project:
        name = result["name"]
        reference_id = result["resource_id"]

        params = {
            "domain_id": domain_id,
            "workspace_id": workspace_id,
            "project_type": project_type,
            "reference_id": reference_id,
            "is_managed": True,
        }

        project_vos = self.project_mgr.filter_projects(**params)
        _LOGGER.debug(
            f"[_create_service_account] project_vos: {name} {params} count: {len(project_vos)}"
        )

        if project_group_id:
            params["project_group_id"] = project_group_id

        if project_vos:
            project_vo = project_vos[0]
            if project_vo.name != name:
                params.update({"name": name})

            params.update(
                {
                    "trusted_account_id": trusted_account_id,
                    "last_synced_at": datetime.utcnow(),
                }
            )
            project_vo = self.project_mgr.update_project_by_vo(params, project_vo)
        else:
            params.update({"name": name, "last_synced_at": datetime.utcnow()})
            project_vo = self.project_mgr.create_project(params)
        return project_vo

    def _create_service_account(
        self,
        result: dict,
        project_vo: Project,
        trusted_account_id: str,
        trusted_secret_id: str,
        provider: str,
        sync_options: dict = None,
    ) -> Union[ServiceAccount, None]:
        domain_id = project_vo.domain_id
        workspace_id = project_vo.workspace_id
        project_id = project_vo.project_id
        name = result["name"]
        reference_id = result["resource_id"]
        secret_data = result.get("secret_data", {})
        data = result.get("data", {})
        secret_schema_id = result.get("secret_schema_id")
        tags = result.get("tags", {})

        params = {
            "provider": provider,
            "reference_id": reference_id,
            "is_managed": True,
            "domain_id": domain_id,
            "workspace_id": workspace_id,
            "project_id": project_id,
        }

        service_account_vos = self.service_account_mgr.filter_service_accounts(**params)
        _LOGGER.debug(
            f"[_create_service_account] service_account_vos: {name} {params} count: {len(service_account_vos)}"
        )

        if service_account_vos:
            service_account_vo = service_account_vos[0]
            update_params = {}
            if service_account_vo.name != result["name"]:
                update_params.update({"name": name})

            update_params = {
                "trusted_account_id": trusted_account_id,
                "last_synced_at": datetime.utcnow(),
            }

            service_account_vo = self.service_account_mgr.update_service_account_by_vo(
                update_params, service_account_vo
            )
        else:
            params.update(
                {
                    "name": name,
                    "data": data,
                    "trusted_account_id": trusted_account_id,
                    "tags": tags,
                }
            )
            if secret_schema_id:
                params["schema_id"] = secret_schema_id

            service_account_vo = self.service_account_mgr.create_service_account(params)

        if secret_data:
            secret_mgr: SecretManager = self.locator.get_manager("SecretManager")
            if service_account_vo.secret_id:
                secret_mgr.delete_secret(service_account_vo.secret_id, domain_id)

            # Check secret_data by schema
            schema_mgr = SchemaManager()
            schema_mgr.validate_secret_data_by_schema_id(
                secret_schema_id,
                service_account_vo.domain_id,
                secret_data,
                "TRUSTING_SECRET",
            )

            create_secret_params = {
                "name": f"{service_account_vo.service_account_id}-secret",
                "data": secret_data,
                "resource_group": "PROJECT",
                "workspace_id": workspace_id,
                "project_id": project_id,
                "service_account_id": service_account_vo.service_account_id,
                "trusted_secret_id": trusted_secret_id,
                "schema_id": secret_schema_id,
            }
            secret_info = secret_mgr.create_secret(create_secret_params, domain_id)
            # Update secret_id in service_account_vo
            service_account_vo = self.service_account_mgr.update_service_account_by_vo(
                {"secret_id": secret_info["secret_id"]}, service_account_vo
            )
        return service_account_vo

    def _remove_old_reference_id_from_workspace(
        self, domain_id: str, reference_id: str
    ) -> None:
        workspace_vos = self.workspace_mgr.filter_workspaces(
            domain_id=domain_id, references=[reference_id]
        )
        for workspace_vo in workspace_vos:
            references = workspace_vo.references
            references.remove(reference_id)
            self.workspace_mgr.update_workspace_by_vo(
                {"references": references}, workspace_vo
            )

    @staticmethod
    def _get_location(result: dict, resource_group: str, sync_options: dict) -> list:
        location = result.get("location", [])
        skip_project_group_option = sync_options.get("skip_project_group")

        if skip_project_group_option:
            if resource_group == "DOMAIN":
                if location:
                    location = [location[0]]
            else:
                location = []

        else:
            if resource_group == "DOMAIN" and not location:
                _LOGGER.debug(
                    f"[_get_location] location is empty: {result} {sync_options} => SKIP"
                )
                # raise ERROR_REQUIRED_PARAMETER(
                #     key="location", reason="location is required"
                # )

        return location

    @staticmethod
    def _set_workspace_theme(tags: dict = None) -> dict:
        theme = random.choice(WORKSPACE_COLORS_NAME)
        if tags:
            tags.update({"theme": theme})
        else:
            tags = {"theme": theme}

        return tags
