import os
import yaml
from pathlib import Path
from django.core.management.base import BaseCommand
from ddpui.models.org import (
    Org,
    OrgPrefectBlock,
    OrgPrefectBlockv1,
    OrgDataFlow,
    OrgDataFlowv1,
    OrgWarehouse,
)
from ddpui.models.orgjobs import DataflowBlock
from ddpui.utils import secretsmanager
from ddpui.ddpprefect.schema import PrefectDataFlowUpdateSchema3
from ddpui.ddpprefect.prefect_service import get_deployment, update_dataflow_v1
from ddpui.models.tasks import OrgTask, Task, DataflowOrgTask
from ddpui.utils.constants import (
    TASK_AIRBYTESYNC,
    AIRBYTE_SYNC_TIMEOUT,
    TASK_DBTRUN,
    TASK_GITPULL,
)
from ddpui.ddpprefect import (
    AIRBYTESERVER,
    AIRBYTECONNECTION,
    DBTCORE,
    DBTCLIPROFILE,
    SECRET,
    SHELLOPERATION,
)
from ddpui.ddpprefect.schema import PrefectSecretBlockCreate
from ddpui.ddpprefect import prefect_service
from ddpui.ddpairbyte import airbyte_service

import logging

logger = logging.getLogger("migration")


class Command(BaseCommand):
    """migrate from old blocks to new tasks"""

    help = "Process Commands in tasks-architecture folder"

    def __init__(self):
        self.failures = []
        self.successes = []

    def add_arguments(self, parser):
        pass

    def migrate_airbyte_server_blocks(self, org: Org):
        """Create/update new server block"""
        old_block = OrgPrefectBlock.objects.filter(
            org=org, block_type=AIRBYTESERVER
        ).first()
        if not old_block:
            self.failures.append(f"Server block not found for the org '{org.slug}'")
            return

        new_block = OrgPrefectBlockv1.objects.filter(
            org=org, block_type=AIRBYTESERVER
        ).first()

        if not new_block:  # create
            logger.debug(
                f"Creating new server block with id '{old_block.block_id}' in orgprefectblockv1"
            )
            OrgPrefectBlockv1.objects.create(
                org=org,
                block_id=old_block.block_id,
                block_name=old_block.block_name,
                block_type=AIRBYTESERVER,
            )
        else:  # update
            logger.debug(
                f"Updating the newly created server block with id '{old_block.block_id}' in orgprefectblockv1"
            )
            new_block.block_id = old_block.block_id
            new_block.block_name = old_block.block_name
            new_block.save()

        # assert server block creation
        cnt = OrgPrefectBlockv1.objects.filter(
            org=org, block_type=AIRBYTESERVER
        ).count()
        if cnt == 0:
            self.failures.append(
                f"found 0 server blocks for org {org.slug} in orgprefectblockv1"
            )
        else:
            self.successes.append(
                f"found {cnt} server block(s) for org {org.slug} in orgprefectblockv1"
            )

        return new_block

    def migrate_manual_sync_conn_deployments(self, org: Org):
        """
        Create/update airbyte connection's manual deployments
        """

        # check if the server block exists or not
        server_block = OrgPrefectBlockv1.objects.filter(
            org=org, block_type=AIRBYTESERVER
        ).first()
        if not server_block:
            # self.failures.append(f"Server block not found for {org.slug}")
            return

        logger.debug("Found airbyte server block")

        airbyte_sync_task = Task.objects.filter(slug=TASK_AIRBYTESYNC).first()
        if not airbyte_sync_task:
            self.failures.append("run the tasks migration to populate the master table")
            return

        for old_dataflow in OrgDataFlow.objects.filter(
            org=org, dataflow_type="manual", deployment_name__startswith="manual-sync"
        ).all():
            new_dataflow = OrgDataFlowv1.objects.filter(
                org=org,
                dataflow_type="manual",
                deployment_id=old_dataflow.deployment_id,
            ).first()

            org_task = OrgTask.objects.filter(
                org=org,
                task=airbyte_sync_task,
                connection_id=old_dataflow.connection_id,
            ).first()

            if not org_task:
                org_task = OrgTask.objects.create(
                    org=org,
                    task=airbyte_sync_task,
                    connection_id=old_dataflow.connection_id,
                )

            # assert creation of orgtask
            cnt = OrgTask.objects.filter(
                org=org,
                task=airbyte_sync_task,
                connection_id=old_dataflow.connection_id,
            ).count()
            if cnt == 0:
                self.failures.append(f"found 0 orgtasks in {org.slug}")
                return
            else:
                self.successes.append(f"found {cnt} orgtasks in {org.slug}")

            if not new_dataflow:  # create
                logger.info(
                    f"Creating new dataflow with id '{old_dataflow.deployment_id}' in orgdataflowv1"
                )
                new_dataflow = OrgDataFlowv1.objects.create(
                    org=org,
                    name=old_dataflow.name,
                    deployment_name=old_dataflow.deployment_name,
                    deployment_id=old_dataflow.deployment_id,
                    cron=old_dataflow.cron,
                    dataflow_type="manual",
                )

                DataflowOrgTask.objects.create(dataflow=new_dataflow, orgtask=org_task)

            # assert orgdataflowv1 creation
            cnt = OrgDataFlowv1.objects.filter(
                org=org,
                dataflow_type="manual",
                deployment_id=old_dataflow.deployment_id,
            ).count()
            if cnt == 0:
                self.failures.append(
                    f"found 0 dataflowv1 in {org.slug} with deployment_id {old_dataflow.deployment_id}"
                )
            else:
                self.successes.append(
                    f"found {cnt} dataflowv1 in {org.slug} with deployment_id {old_dataflow.deployment_id}"
                )
            cnt = DataflowOrgTask.objects.filter(
                dataflow=new_dataflow, orgtask=org_task
            ).count()
            if cnt == 0:
                self.failures.append(
                    f"found 0 datafloworgtask in {org.slug} with deployment_id {old_dataflow.deployment_id}"
                )
            else:
                self.successes.append(
                    f"found {cnt} datafloworgtask in {org.slug} with deployment_id {old_dataflow.deployment_id}"
                )

            # update deployment params
            deployment = None
            try:
                deployment = get_deployment(new_dataflow.deployment_id)
            except Exception as error:
                logger.info(
                    f"Something went wrong in fetching the deployment with id '{new_dataflow.deployment_id}'"
                )
                logger.exception(error)
                logger.info("skipping to next loop")
                continue

            params = deployment["parameters"]
            task_config = {
                "slug": airbyte_sync_task.slug,
                "type": AIRBYTECONNECTION,
                "seq": 1,
                "airbyte_server_block": server_block.block_name,
                "connection_id": org_task.connection_id,
                "timeout": AIRBYTE_SYNC_TIMEOUT,
            }
            params["config"] = {"tasks": [task_config]}
            logger.info(f"PARAMS {new_dataflow.deployment_id}")
            try:
                payload = PrefectDataFlowUpdateSchema3(
                    name=new_dataflow.name,  # wont be updated
                    connections=[],  # wont be updated
                    dbtTransform="ignore",  # wont be updated
                    cron=new_dataflow.cron if new_dataflow.cron else "",
                    deployment_params=params,
                )
                update_dataflow_v1(new_dataflow.deployment_id, payload)
                logger.info(
                    f"updated deployment params for the deployment with id {new_dataflow.deployment_id}"
                )
            except Exception as error:
                logger.info(
                    f"Something went wrong in updating the deployment params with id '{new_dataflow.deployment_id}'"
                )
                logger.exception(error)
                logger.info("skipping to next loop")
                continue

            # assert deployment params updation
            try:
                deployment = get_deployment(new_dataflow.deployment_id)
                if "config" not in deployment["parameters"]:
                    self.failures.append(
                        f"Missing 'config' key in the deployment parameters for {org.slug} {new_dataflow.deployment_id}"
                    )
                else:
                    self.successes.append(
                        f"Found correct deployment params for for {org.slug} {new_dataflow.deployment_id}"
                    )
            except Exception as error:
                self.failures.append(
                    f"Failed to fetch deployment with id '{new_dataflow.deployment_id}' for {org.slug}"
                )
                logger.exception(error)
                logger.info("skipping to next loop")
                continue

    def migrate_transformation_blocks(self, org: Org):
        """
        Migrate Dbt Core Operation & Shell Operation blocks to tasks
        """

        # fetch warehouse and credentials
        warehouse = OrgWarehouse.objects.filter(org=org).first()
        if warehouse is None:
            self.failures.append("SKIPPING: org does not have a warehouse")
            return
        try:
            credentials = secretsmanager.retrieve_warehouse_credentials(warehouse)
        except:
            self.failures.append("SKIPPING: couldnt retrieve the warehouse creds")
            return

        # get the dataset location if warehouse type is bigquery
        bqlocation = None
        if warehouse.wtype == "bigquery":
            try:
                destination = airbyte_service.get_destination(
                    org.airbyte_workspace_id, warehouse.airbyte_destination_id
                )
            except:
                self.failures.append("SKIPPING: couldnt bigquery warehouse location")
                return
            if destination.get("connectionConfiguration"):
                bqlocation = destination["connectionConfiguration"]["dataset_location"]

        dbt_env_dir = Path(org.dbt.dbt_venv)
        if not dbt_env_dir.exists():
            self.failures.append("SKIPPING: couldnt find the dbt venv")
            return
        dbt_binary = str(dbt_env_dir / "venv/bin/dbt")
        dbtrepodir = Path(os.getenv("CLIENTDBT_ROOT")) / org.slug / "dbtrepo"
        project_dir = str(dbtrepodir)
        dbt_project_filename = str(dbtrepodir / "dbt_project.yml")
        if not os.path.exists(dbt_project_filename):
            self.failures.append(f"{dbt_project_filename} is missing")
            return

        with open(dbt_project_filename, "r", encoding="utf-8") as dbt_project_file:
            dbt_project = yaml.safe_load(dbt_project_file)
            if "profile" not in dbt_project:
                self.failures.append(
                    "SKIPPING: could not find 'profile:' in dbt_project.yml"
                )
                return

        profile_name = dbt_project["profile"]
        target = org.dbt.default_schema

        # create the dbt cli profile block
        cli_profile_block = OrgPrefectBlockv1.objects.filter(
            org=org, block_type=DBTCLIPROFILE
        ).first()
        if cli_profile_block is None:
            try:
                cli_block_name = f"{org.slug}-{profile_name}"

                cli_block_response = prefect_service.create_dbt_cli_profile_block(
                    cli_block_name,
                    profile_name,
                    target,
                    warehouse.wtype,
                    bqlocation,
                    credentials,
                )

                # save the cli profile block in django db
                cli_profile_block = OrgPrefectBlockv1.objects.create(
                    org=org,
                    block_type=DBTCLIPROFILE,
                    block_id=cli_block_response["block_id"],
                    block_name=cli_block_response["block_name"],
                )
            except Exception as error:
                self.failures.append("FAILED to create the dbt cli profile block")
                logger.exception(error)
                return

            self.successes.append("Created the dbt cli profile block for the org")

        self.successes.append(
            f"Using the dbt cli profile block {cli_profile_block.block_name}"
        )
        cnt = OrgPrefectBlockv1.objects.filter(
            org=org, block_type=DBTCLIPROFILE
        ).count()
        self.successes.append(f"ASSERT: Found {cnt} dbt cli profile block")

        # create the secret block for git token url if needed
        secret_git_url_block = OrgPrefectBlockv1.objects.filter(
            org=org, block_type=SECRET
        ).first()
        if secret_git_url_block is None:
            gitrepo_access_token = secretsmanager.retrieve_github_token(org.dbt)
            gitrepo_url = org.dbt.gitrepo_url

            if gitrepo_access_token is not None and gitrepo_access_token != "":
                gitrepo_url = gitrepo_url.replace(
                    "github.com", "oauth2:" + gitrepo_access_token + "@github.com"
                )
                secret_block = PrefectSecretBlockCreate(
                    block_name=f"{org.slug}-git-pull-url",
                    secret=gitrepo_url,
                )
                try:
                    block_response = prefect_service.create_secret_block(secret_block)
                except:
                    self.failures.append(
                        "FAILED to create the secret git pull url block"
                    )
                    logger.exception(error)
                    return

                secret_git_url_block = OrgPrefectBlockv1.objects.create(
                    org=org,
                    block_type=SECRET,
                    block_id=block_response["block_id"],
                    block_name=block_response["block_name"],
                )
                self.successes.append("Created the secret git url block")

        self.successes.append(
            f"Org has a {'private repo' if secret_git_url_block else 'public repo'}"
        )
        cnt = OrgPrefectBlockv1.objects.filter(org=org, block_type=SECRET).count()
        self.successes.append(f"ASSERT: Found {cnt} secret block")

        # migrate dbt blocks ->  dbt tasks
        dbt_blocks = OrgPrefectBlock.objects.filter(
            org=org, block_type__in=[DBTCORE, SHELLOPERATION]
        ).all()
        for old_block in dbt_blocks:
            # its a git pull block
            task = None
            if old_block.block_name.endswith("git-pull"):
                task = Task.objects.filter(slug=TASK_GITPULL).first()
            else:  # its one of the dbt core block
                old_cmd = old_block.block_name.split(f"{old_block.dbt_target_schema}-")[
                    -1
                ]
                task = Task.objects.filter(slug__endswith=old_cmd).first()

            if not task:
                self.failures.append(f"Couldnt find the task {old_cmd}")
                self.failures.append(f"SKIPPING: migration of {old_block.block_name}")
                continue

            self.successes.append(f"Found corresponding task {task.slug}")

            org_task = OrgTask.objects.filter(org=org, task=task).first()
            if not org_task:
                self.successes.append(f"Creating orgtask for task {task.slug}")
                org_task = OrgTask.objects.create(task=task, org=org)
                self.successes.append(f"Created orgtask for task {task.slug}")

            cnt = OrgTask.objects.filter(org=org, task=task).count()
            self.successes.append(f"ASSERT: Found {cnt} orgtask for {task.slug}")

    def handle(self, *args, **options):
        for org in Org.objects.all():
            self.migrate_airbyte_server_blocks(org)
            self.migrate_manual_sync_conn_deployments(org)
            self.migrate_transformation_blocks(org)

        # show summary
        print("=" * 80)
        print("SUCCESSES")
        print("=" * 80)
        for success in self.successes:
            print("SUCCESS " + success)
        print("=" * 80)
        print("FAILURES")
        print("=" * 80)
        for failure in self.failures:
            print("FAILURE " + failure)
