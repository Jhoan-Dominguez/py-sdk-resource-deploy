"""Central inventory of deployed resources, stored in a DynamoDB table.

Table: named by the required INVENTORY_TABLE env var. By default the tool creates it on
first use (`inventory init --apply`, or implicitly on the first `deploy --apply`). With
INVENTORY_TABLE_MODE=existing the tool never creates it and only uses a table that already
exists (it fails if it's missing).

Existing tables only need a composite primary key of two string attributes; their names
are read from the table itself, so a shared single-table design (e.g. `PK`/`SK`) works.
Items written by this tool are marked with record_type=RECORD_TYPE so they can be told
apart from anything else stored in a shared table.

Item layout (key attribute names come from the table):
    <partition key> = "<project>#<service>#<environment>"
    <sort key>      = resource id (ARN, or a synthetic id for attachments)
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from boto3.dynamodb.types import TypeDeserializer

from .resources import (
    ACTION_AUDIT,
    ACTION_IMPORT,
    TAG_MANAGED_BY,
    TAG_PROJECT,
    TOOL_NAME,
    DeployRun,
    ResourceRecord,
    format_timestamp,
)

RECORD_TYPE = "deploy-inventory"
STATUS_ACTIVE = "active"
STATUS_DELETED = "deleted"

# Cleared when a deleted resource is recorded again (redeployed/reimported).
_DELETION_ATTRS = (
    "deleted_at",
    "deleted_by",
    "deleted_deployment_id",
    "deleted_note",
    "imported_at",
    "imported_by",
)

MODE_CREATE = "create"
MODE_EXISTING = "existing"
MODES = (MODE_CREATE, MODE_EXISTING)

# Key attribute names used only when the tool creates the table itself.
_DEFAULT_PARTITION_KEY = "pk"
_DEFAULT_SORT_KEY = "sk"

_deserializer = TypeDeserializer()


class InventoryError(RuntimeError):
    pass


def _now() -> str:
    return format_timestamp(datetime.now(UTC))


def partition_key(project_name: str, service: str, environment: str) -> str:
    return f"{project_name}#{service}#{environment}"


class InventoryStore:
    def __init__(self, dynamodb_client, table_name: str, mode: str, project_name: str):
        if mode not in MODES:
            raise InventoryError(
                f"INVENTORY_TABLE_MODE must be one of {', '.join(MODES)} (got '{mode}')."
            )
        self._ddb = dynamodb_client
        self._table = table_name
        self._mode = mode
        self._project_name = project_name
        self._pk: str | None = None
        self._sk: str | None = None

    @property
    def table_name(self) -> str:
        return self._table

    @property
    def ready(self) -> bool:
        """True once the table is known to exist and its key names are resolved."""
        return self._pk is not None

    def ensure_table(self, apply: bool) -> str:
        """Validate (or, in create mode with apply, create) the table. Returns a status line."""
        try:
            table = self._ddb.describe_table(TableName=self._table)["Table"]
        except self._ddb.exceptions.ResourceNotFoundException:
            if self._mode == MODE_EXISTING:
                raise InventoryError(
                    f"Inventory table '{self._table}' does not exist and "
                    "INVENTORY_TABLE_MODE=existing never creates it."
                ) from None
            if not apply:
                return "does not exist, would be created"
            self._create_table()
            self._pk, self._sk = _DEFAULT_PARTITION_KEY, _DEFAULT_SORT_KEY
            return "created"

        self._pk, self._sk = _validate_key_schema(table)
        return f"exists (keys: {self._pk}, {self._sk})"

    def _create_table(self) -> None:
        self._ddb.create_table(
            TableName=self._table,
            AttributeDefinitions=[
                {"AttributeName": _DEFAULT_PARTITION_KEY, "AttributeType": "S"},
                {"AttributeName": _DEFAULT_SORT_KEY, "AttributeType": "S"},
            ],
            KeySchema=[
                {"AttributeName": _DEFAULT_PARTITION_KEY, "KeyType": "HASH"},
                {"AttributeName": _DEFAULT_SORT_KEY, "KeyType": "RANGE"},
            ],
            BillingMode="PAY_PER_REQUEST",
            # The inventory is what undeploy relies on; don't let it disappear by accident.
            DeletionProtectionEnabled=True,
            Tags=[
                {"Key": TAG_MANAGED_BY, "Value": TOOL_NAME},
                {"Key": TAG_PROJECT, "Value": self._project_name},
            ],
        )
        self._ddb.get_waiter("table_exists").wait(TableName=self._table)
        self._ddb.update_continuous_backups(
            TableName=self._table,
            PointInTimeRecoverySpecification={"PointInTimeRecoveryEnabled": True},
        )

    def _require_ready(self) -> None:
        if not self.ready:
            raise InventoryError("ensure_table() must succeed before using the inventory.")

    def put(self, run: DeployRun, record: ResourceRecord) -> None:
        """Upsert one resource. created_*/imported_* fields are only set the first time."""
        self._require_ready()
        now = _now()
        always = {
            "record_type": {"S": RECORD_TYPE},
            "project": {"S": run.project_name},
            "service": {"S": run.service},
            "environment": {"S": run.environment},
            "account_id": {"S": run.aws_account_id},
            "resource_type": {"S": record.resource_type},
            "resource_name": {"S": record.name},
            "origin": {"S": record.origin},
            "status": {"S": STATUS_ACTIVE},
            "depends_on": {"L": [{"S": d} for d in record.depends_on]},
            "updated_at": {"S": now},
            "updated_by": {"S": run.caller_arn},
            "last_deployment_id": {"S": run.deployment_id},
            "last_action": {"S": run.action},
        }
        if run.expires_at:
            always["expires_at"] = {"S": run.expires_at}
        imported = run.action == ACTION_IMPORT
        first_time_only = {
            "created_at": {"S": record.created_at or now},
            # An import or audit only finds the resource; who created it is unknown.
            "created_by": {
                "S": "unknown" if run.action in (ACTION_IMPORT, ACTION_AUDIT) else run.caller_arn
            },
            "created_deployment_id": {"S": run.deployment_id},
        }
        if imported:
            first_time_only["imported_at"] = {"S": now}
            first_time_only["imported_by"] = {"S": run.caller_arn}
        attrs = always | first_time_only
        key = self._key(run.project_name, run.service, run.environment, record.resource_id)
        values = {f":{a}": v for a, v in attrs.items()}
        try:
            # Every attribute goes through a #placeholder: several of these (e.g. `status`)
            # are DynamoDB reserved words.
            self._ddb.update_item(
                TableName=self._table,
                Key=key,
                UpdateExpression="SET "
                + ", ".join(
                    [f"#{a} = :{a}" for a in always]
                    + [f"#{a} = if_not_exists(#{a}, :{a})" for a in first_time_only]
                )
                + " REMOVE #last_error, #last_error_at",
                ConditionExpression="attribute_not_exists(#status) OR #status <> :deleted",
                ExpressionAttributeNames=_names(*attrs, "last_error", "last_error_at"),
                ExpressionAttributeValues=values | {":deleted": {"S": STATUS_DELETED}},
            )
        except self._ddb.exceptions.ConditionalCheckFailedException:
            # The resource was undeployed and now exists again: start a fresh lifecycle
            # instead of keeping the old creation/deletion data.
            removed = [
                a
                for a in (*_DELETION_ATTRS, "last_error", "last_error_at")
                if a not in first_time_only
            ]
            self._ddb.update_item(
                TableName=self._table,
                Key=key,
                UpdateExpression="SET "
                + ", ".join(f"#{a} = :{a}" for a in attrs)
                + " REMOVE "
                + ", ".join(f"#{a}" for a in removed),
                ExpressionAttributeNames=_names(*attrs, *removed),
                ExpressionAttributeValues=values,
            )

    def _key(self, project: str, service: str, environment: str, resource_id: str) -> dict:
        return {
            self._pk: {"S": partition_key(project, service, environment)},
            self._sk: {"S": resource_id},
        }

    def _item_key(self, item: dict[str, Any]) -> dict:
        return self._key(item["project"], item["service"], item["environment"], item["resource_id"])

    def _update(self, item: dict[str, Any], set_attrs: dict[str, Any], remove: tuple = ()) -> None:
        """SET/REMOVE attributes on an existing item (never creates a new one)."""
        expression = "SET " + ", ".join(f"#{a} = :{a}" for a in set_attrs)
        if remove:
            expression += " REMOVE " + ", ".join(f"#{a}" for a in remove)
        self._ddb.update_item(
            TableName=self._table,
            Key=self._item_key(item),
            UpdateExpression=expression,
            ConditionExpression="attribute_exists(#record_type)",
            ExpressionAttributeNames=_names(*set_attrs, *remove, "record_type"),
            ExpressionAttributeValues={f":{a}": {"S": v} for a, v in set_attrs.items()},
        )

    def mark_deleted(self, run: DeployRun, item: dict[str, Any], note: str | None = None) -> None:
        """Mark an item deleted (by undeploy, or by an audit that found it gone from AWS)."""
        self._require_ready()
        now = _now()
        attrs = {
            "status": STATUS_DELETED,
            "deleted_at": now,
            "deleted_by": run.caller_arn,
            "deleted_deployment_id": run.deployment_id,
            "updated_at": now,
            "updated_by": run.caller_arn,
            "last_deployment_id": run.deployment_id,
            "last_action": run.action,
        }
        if note:
            attrs["deleted_note"] = note
        self._update(
            item,
            attrs,
            remove=(
                "last_error",
                "last_error_at",
                "keep",
                "kept_at",
                "kept_by",
                "keep_reason",
                "expires_at",
            ),
        )

    def mark_error(self, run: DeployRun, item: dict[str, Any], error: str) -> None:
        """The resource still exists: record why undeploy couldn't remove it."""
        self._require_ready()
        self._update(
            item,
            {
                "last_error": error,
                "last_error_at": _now(),
                "last_action": run.action,
                "last_deployment_id": run.deployment_id,
            },
        )

    def set_expiry(self, item: dict[str, Any], expires_at: str | None, caller_arn: str) -> None:
        """Set (or, with None, clear) when this resource is due for removal."""
        self._require_ready()
        attrs = {"updated_at": _now(), "updated_by": caller_arn}
        if expires_at:
            self._update(item, attrs | {"expires_at": expires_at})
        else:
            self._update(item, attrs, remove=("expires_at",))

    def set_keep(
        self, item: dict[str, Any], keep: bool, caller_arn: str, reason: str | None = None
    ) -> None:
        self._require_ready()
        if not keep:
            self._update(
                item,
                {"updated_at": _now(), "updated_by": caller_arn},
                remove=("keep", "kept_at", "kept_by", "keep_reason"),
            )
            return
        attrs = {"keep": "true", "kept_at": _now(), "kept_by": caller_arn}
        if reason:
            attrs["keep_reason"] = reason
        self._update(item, attrs)

    def list(
        self,
        environment: str | None = None,
        service: str | None = None,
        status: str | None = None,
        deployment_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """This project's inventory items, optionally filtered.

        `deployment_id` matches the deployment that *created* the resource. Every item gets a
        `resource_id` key (the sort key value, whatever the table calls it).
        """
        self._require_ready()
        names = {"#record_type": "record_type", "#project": "project"}
        values: dict[str, Any] = {
            ":record_type": {"S": RECORD_TYPE},
            ":project": {"S": self._project_name},
        }
        filters = ["#record_type = :record_type", "#project = :project"]
        for attr, value in (
            ("environment", environment),
            ("service", service),
            ("status", status),
            ("created_deployment_id", deployment_id),
        ):
            if value:
                names[f"#{attr}"] = attr
                values[f":{attr}"] = {"S": value}
                filters.append(f"#{attr} = :{attr}")

        params: dict[str, Any] = {
            "TableName": self._table,
            "FilterExpression": " AND ".join(filters),
            "ExpressionAttributeNames": names,
            "ExpressionAttributeValues": values,
        }
        if environment and service:
            # Both parts of the partition key are known: query instead of scanning the table.
            names["#pk"] = self._pk
            values[":pk"] = {"S": partition_key(self._project_name, service, environment)}
            params["KeyConditionExpression"] = "#pk = :pk"
            pages = self._ddb.get_paginator("query").paginate(**params)
        else:
            pages = self._ddb.get_paginator("scan").paginate(**params)

        items = [
            {k: _deserializer.deserialize(v) for k, v in item.items()}
            for page in pages
            for item in page["Items"]
        ]
        for item in items:
            item["resource_id"] = item[self._sk]
        return sorted(
            items,
            key=lambda i: (i["environment"], i["service"], i["resource_type"], i["resource_name"]),
        )


def _names(*attrs: str) -> dict[str, str]:
    """#placeholder -> attribute name, for exactly the attributes an expression uses."""
    return {f"#{a}": a for a in attrs}


def _validate_key_schema(table: dict[str, Any]) -> tuple[str, str]:
    keys = {k["KeyType"]: k["AttributeName"] for k in table["KeySchema"]}
    types = {a["AttributeName"]: a["AttributeType"] for a in table["AttributeDefinitions"]}
    pk, sk = keys.get("HASH"), keys.get("RANGE")
    if not pk or not sk or types.get(pk) != "S" or types.get(sk) != "S":
        raise InventoryError(
            f"Inventory table '{table['TableName']}' must have a string partition key and a "
            f"string sort key (found: {table['KeySchema']})."
        )
    return pk, sk
