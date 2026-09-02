"""Check Terraform targets and project resources without changing AWS."""

import json
import re

from botocore.exceptions import ClientError

from cloudbox.common import MICROVM_SERVICE, SDK_CONFIG, CloudboxError, operator_session

TERRAFORM_OWNER = "Terraform"
IMAGE_OWNER = "CloudboxImageScript"
VM_STOPPED = "TERMINATED"
S3_MISSING = {"404", "NoSuchBucket", "NotFound"}
S3_TAGS_MISSING = {"NoSuchTagSet"}
RESOURCE_MISSING = {"ResourceNotFoundException"}
IAM_MISSING = {"NoSuchEntity"}
SECRET_ADDRESS = "aws_secretsmanager_secret.openrouter"
GITHUB_SECRET_ADDRESS = "aws_secretsmanager_secret.github[0]"
GITHUB_SECRET_DECLARATION = GITHUB_SECRET_ADDRESS.split("[", 1)[0]
RESOURCE_HEADER = re.compile(
    r'^\s*resource\s+"([^"\n]+)"\s+"([^"\n]+)"\s*\{', re.MULTILINE
)
MODULE_HEADER = re.compile(r'^\s*module\s+"([^"\n]+)"\s*\{', re.MULTILINE)
ARN_PATTERN = re.compile(r"arn:aws:([a-z0-9-]+):([^:\s]*):([^:\s]*):")
REGION_PATTERN = re.compile(r"[a-z]{2}-[a-z]+-[0-9]+")
KIND_BY_TYPE = {
    "aws_s3_bucket": "bucket",
    "aws_s3_bucket_public_access_block": "bucket_setting",
    "aws_s3_bucket_ownership_controls": "bucket_setting",
    "aws_s3_bucket_server_side_encryption_configuration": "bucket_setting",
    "aws_s3_bucket_policy": "bucket_setting",
    "aws_s3_bucket_lifecycle_configuration": "bucket_setting",
    "aws_cloudwatch_log_group": "log_group",
    "aws_secretsmanager_secret": "secret",
    "aws_iam_role": "role",
    "aws_iam_policy": "policy",
    "aws_iam_role_policy": "role_policy",
    "aws_iam_role_policy_attachment": "role_attachment",
}
SINGLETON_ADDRESSES = {
    "bucket": {"aws_s3_bucket.data"},
    "bucket_setting": {
        "aws_s3_bucket_public_access_block.data",
        "aws_s3_bucket_ownership_controls.data",
        "aws_s3_bucket_server_side_encryption_configuration.data",
        "aws_s3_bucket_policy.tls",
        "aws_s3_bucket_lifecycle_configuration.runs",
    },
    "log_group": {"aws_cloudwatch_log_group.worker"},
}
SCOPE = {
    "services": ["IAM", "S3", "Secrets Manager", "CloudWatch Logs", "Lambda MicroVMs"],
    "discovery": "Terraform targets and project-named or project-tagged resources",
    "regions": "Configured region; IAM and S3 discovery are account-wide",
    "excluded": [
        "Unrelated resources",
        "AWS account baseline",
        "Terminated VM history",
    ],
}


def optional_call(function, missing, **arguments):
    # A denied request does not prove absence.
    try:
        return function(**arguments)
    except ClientError as error:
        if error.response["Error"]["Code"] in missing:
            return None
        raise


def tag_values(tags):
    return (
        tags if isinstance(tags, dict) else {tag["Key"]: tag["Value"] for tag in tags}
    )


def check_tags(tags, config, *, owner=TERRAFORM_OWNER):
    values = tag_values(tags)
    if (
        values.get("Project") != config["project_name"]
        or values.get("ManagedBy") != owner
    ):
        raise CloudboxError(
            "owner_mismatch", "An AWS target lacks this deployment's ownership tags."
        )


def state_strings(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from state_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from state_strings(item)
    elif isinstance(value, str):
        yield value


def check_identity(address, values, expected, config, *, allow_unknown=False):
    identity = expected.get(address, {}).get("identity")
    if identity is None or not isinstance(values, dict):
        raise CloudboxError(
            "target_mismatch",
            "A Terraform resource has no inventory rule.",
            resource=address,
        )
    if any(
        values.get(key) != value and not (allow_unknown and values.get(key) is None)
        for key, value in identity.items()
    ):
        raise CloudboxError(
            "target_mismatch",
            "A Terraform resource is outside this deployment.",
            resource=address,
        )
    for value in state_strings(values):
        for _, region, account in ARN_PATTERN.findall(value):
            if (account.isdigit() and account != config["aws_account_id"]) or (
                REGION_PATTERN.fullmatch(region) and region != config["aws_region"]
            ):
                raise CloudboxError(
                    "state_mismatch", "A resource belongs to another account or region."
                )


def secret_targets(names):
    # Preserve the OpenRouter target and add the optional submitter-only App key.
    targets = {SECRET_ADDRESS: names["secret_name"]}
    if names.get("github_secret_name"):
        targets[GITHUB_SECRET_ADDRESS] = names["github_secret_name"]
    return targets


def covered_declarations(resources, names, *, main):
    covered = {address.split("[", 1)[0] for address in resources}
    if main and not names.get("github_secret_name"):
        # A disabled count resource has a declaration but no live instance.
        covered.add(GITHUB_SECRET_DECLARATION)
    return covered


def check_declarations(environment, manifest, names):
    # This spike supports flat HCL roots and a resource-free policy module.
    for label, directory in (
        ("bootstrap", environment.bootstrap_root),
        ("main", environment.main_root),
    ):
        if list(directory.glob("*.tf.json")):
            raise CloudboxError(
                "inventory_coverage",
                "Add JSON configuration support to the resource checker first.",
            )
        source = "\n".join(path.read_text() for path in directory.glob("*.tf"))
        declared = {f"{kind}.{name}" for kind, name in RESOURCE_HEADER.findall(source)}
        covered = covered_declarations(manifest[label], names, main=label == "main")
        if declared != covered or set(MODULE_HEADER.findall(source)) != {"policy"}:
            raise CloudboxError(
                "inventory_coverage",
                "Update the resource manifest and checker with Terraform changes.",
                root=label,
                uncovered=sorted(declared - covered),
                stale=sorted(covered - declared),
            )
    modules = environment.main_root / "modules"
    if list(modules.rglob("*.tf.json")):
        raise CloudboxError(
            "inventory_coverage", "Add JSON module checks before deployment."
        )
    for path in modules.rglob("*.tf"):
        source = path.read_text()
        if RESOURCE_HEADER.search(source) or MODULE_HEADER.search(source):
            raise CloudboxError(
                "inventory_coverage",
                "Add module resource support to the checker before deployment.",
            )
    for resources in manifest.values():
        for address, resource in resources.items():
            kind = KIND_BY_TYPE.get(address.split(".", 1)[0])
            if (
                kind is None
                or resource.get("kind") != kind
                or not resource.get("identity")
            ):
                raise CloudboxError(
                    "inventory_coverage",
                    "Add an inventory check for this resource type.",
                    resource=address,
                )
            if (
                kind == "bucket_setting"
                and resource.get("covered_by") != "aws_s3_bucket.data"
            ):
                raise CloudboxError(
                    "inventory_coverage",
                    "A bucket setting has no parent absence check.",
                )
    # These service adapters each support one target; extra instances need code changes.
    all_resources = {
        address: resource
        for root in manifest.values()
        for address, resource in root.items()
    }
    singleton_identities = {
        "bucket": {"bucket": names["bucket_name"]},
        "bucket_setting": {"bucket": names["bucket_name"]},
        "log_group": {"name": names["log_group_name"]},
    }
    for kind, supported in SINGLETON_ADDRESSES.items():
        selected = {
            address: item
            for address, item in all_resources.items()
            if item["kind"] == kind
        }
        if set(selected) != supported or any(
            item["identity"] != singleton_identities[kind] for item in selected.values()
        ):
            raise CloudboxError(
                "inventory_coverage",
                "Extend the service adapter before adding a target.",
                kind=kind,
            )
    secrets = {
        address: item["identity"]
        for address, item in all_resources.items()
        if item["kind"] == "secret"
    }
    if secrets != {
        address: {"name": name} for address, name in secret_targets(names).items()
    }:
        raise CloudboxError(
            "inventory_coverage",
            "The secret inventory does not match the configured targets.",
        )
    roles = {
        item["identity"].get("name") or item["identity"]["arn"].rsplit("/", 1)[1]
        for item in all_resources.values()
        if item["kind"] == "role"
    }
    policies = {
        item["identity"]["arn"]
        for item in all_resources.values()
        if item["kind"] == "policy"
    }
    for item in all_resources.values():
        if (
            item["kind"] in {"role_policy", "role_attachment"}
            and item["identity"]["role"] not in roles
        ):
            raise CloudboxError(
                "inventory_coverage", "A role policy has no parent ownership check."
            )
        if (
            item["kind"] == "role_attachment"
            and item["identity"]["policy_arn"] not in policies
        ):
            raise CloudboxError(
                "inventory_coverage", "A role attachment has no policy ownership check."
            )


def terraform_manifest(environment):
    # Console resolves names even when there are no saved outputs or AWS resources.
    result = environment.terraform(
        environment.bootstrap_root,
        "console",
        "-no-color",
        capture=True,
        input_text="jsonencode({names=module.policy.names, resources=module.policy.resource_manifest})\n",
    )
    document = json.loads(json.loads(result))
    manifest = document["resources"]
    if set(manifest) != {"bootstrap", "main"}:
        raise CloudboxError(
            "inventory_coverage",
            "The resource manifest must define both Terraform roots.",
        )
    check_declarations(environment, manifest, document["names"])
    expected = {
        environment.bootstrap_root: manifest["bootstrap"],
        environment.main_root: manifest["main"],
    }
    return document["names"], expected


def saved_resources(environment, directory, expected, config):
    path = environment.state_path(directory)
    state = json.loads(path.read_bytes()) if path.exists() else {}
    resources = {}
    for resource in state.get("resources", []):
        address = f"{resource['type']}.{resource['name']}"
        if resource.get("module") or resource.get("mode") != "managed":
            raise CloudboxError(
                "unexpected_state",
                "Review the unexpected Terraform state before proceeding.",
            )
        for instance in resource["instances"]:
            indexed = address + (
                f"[{json.dumps(instance['index_key'])}]"
                if "index_key" in instance
                else ""
            )
            values = instance["attributes"]
            check_identity(indexed, values, expected, config)
            resources[indexed] = values
    return resources


def state_remains(environment, directory):
    path = environment.state_path(directory)
    state = json.loads(path.read_bytes()) if path.exists() else {}
    return bool(state.get("resources") or state.get("outputs"))


def check_plan_coverage(directory, plan, environment):
    from scripts.setup import read_config

    config = read_config(environment)[0]["deployment"]
    names, expected = terraform_manifest(environment)
    targets = expected[directory]
    # Terraform's parsed configuration also catches module and JSON additions.
    root = plan.get("configuration", {}).get("root_module", {})
    resources = root.get("resources", [])
    declared = {item["address"] for item in resources if item.get("mode") == "managed"}
    covered = covered_declarations(targets, names, main=directory == environment.main_root)
    if declared != covered:
        raise CloudboxError(
            "inventory_coverage",
            "The plan contains resources outside the inventory manifest.",
        )
    for module in root.get("module_calls", {}).values():
        if module.get("module", {}).get("resources") or module.get("module", {}).get(
            "module_calls"
        ):
            raise CloudboxError(
                "inventory_coverage",
                "Add module resource checks before applying this plan.",
            )
    for change in plan.get("resource_changes", []):
        if change.get("mode") != "managed":
            continue
        for field in ("before", "after"):
            values = change.get("change", {}).get(field)
            if values is not None:
                check_identity(
                    change["address"],
                    values,
                    targets,
                    config,
                    allow_unknown=field == "after",
                )


def list_vms(compute, names):
    items, request = [], {"imageIdentifier": names["image_arn"]}
    while True:
        page = optional_call(compute.list_microvms, RESOURCE_MISSING, **request)
        if page is None:
            return items
        items.extend(page.get("items", []))
        if not page.get("nextToken"):
            return items
        request["nextToken"] = page["nextToken"]


def inventory(session, names, config, expected):
    compute = session.client(MICROVM_SERVICE, config=SDK_CONFIG)
    image = optional_call(
        compute.get_microvm_image, RESOURCE_MISSING, imageIdentifier=names["image_arn"]
    )
    if image and image.get("state") == "DELETED":
        image = None
    if image:
        check_tags(image.get("tags", {}), config, owner=IMAGE_OWNER)
        if image.get("imageArn") != names["image_arn"]:
            raise CloudboxError(
                "image_mismatch", "The image ARN does not match this deployment."
            )
    present, objects, uploads = set(), [], []
    s3 = session.client("s3", config=SDK_CONFIG)
    bucket = {
        "Bucket": names["bucket_name"],
        "ExpectedBucketOwner": config["aws_account_id"],
    }
    if optional_call(s3.head_bucket, S3_MISSING, **bucket) is not None:
        present.add("aws_s3_bucket.data")
        check_tags(s3.get_bucket_tagging(**bucket)["TagSet"], config)
        if s3.get_bucket_versioning(**bucket).get("Status"):
            raise CloudboxError(
                "versioned_bucket", "Review versioned bucket cleanup separately."
            )
        for page in s3.get_paginator("list_objects_v2").paginate(**bucket):
            objects.extend({"Key": item["Key"]} for item in page.get("Contents", []))
        for page in s3.get_paginator("list_multipart_uploads").paginate(**bucket):
            uploads.extend(page.get("Uploads", []))
    secrets = {}
    for address, name in secret_targets(names).items():
        secret = optional_call(
            session.client("secretsmanager", config=SDK_CONFIG).describe_secret,
            RESOURCE_MISSING,
            SecretId=name,
        )
        secrets[address] = secret
        if secret:
            check_tags(secret.get("Tags", []), config)
            prefix = (
                f"arn:aws:secretsmanager:{config['aws_region']}:"
                f"{config['aws_account_id']}:secret:{name}-"
            )
            if secret["Name"] != name or not secret["ARN"].startswith(prefix):
                raise CloudboxError(
                    "secret_mismatch",
                    "The secret identity does not match this deployment.",
                )
            if not secret.get("DeletedDate"):
                present.add(address)
    iam = session.client("iam", config=SDK_CONFIG)
    targets = {
        address: resource
        for root in expected.values()
        for address, resource in root.items()
    }
    roles = {}
    for address, resource in targets.items():
        fields, kind = resource["identity"], resource["kind"]
        if kind == "role":
            name = fields.get("name") or fields["arn"].rsplit("/", 1)[1]
            response = optional_call(iam.get_role, IAM_MISSING, RoleName=name)
            if response:
                role = response["Role"]
                check_tags(role.get("Tags", []), config)
                if (
                    role["Arn"]
                    != f"arn:aws:iam::{config['aws_account_id']}:role/{name}"
                ):
                    raise CloudboxError(
                        "role_mismatch", "The role ARN does not match this deployment."
                    )
                roles[name] = role
                present.add(address)
        elif kind == "policy":
            response = optional_call(
                iam.get_policy, IAM_MISSING, PolicyArn=fields["arn"]
            )
            if response:
                check_tags(
                    iam.list_policy_tags(PolicyArn=fields["arn"])["Tags"], config
                )
                present.add(address)
    for address, resource in targets.items():
        fields, kind = resource["identity"], resource["kind"]
        if kind == "role_policy" and fields["role"] in roles:
            if (
                optional_call(
                    iam.get_role_policy,
                    IAM_MISSING,
                    RoleName=fields["role"],
                    PolicyName=fields["name"],
                )
                is not None
            ):
                present.add(address)
        elif kind == "role_attachment" and fields["role"] in roles:
            for page in iam.get_paginator("list_attached_role_policies").paginate(
                RoleName=fields["role"]
            ):
                if any(
                    policy["PolicyArn"] == fields["policy_arn"]
                    for policy in page["AttachedPolicies"]
                ):
                    present.add(address)
    logs = session.client("logs", config=SDK_CONFIG)
    for page in logs.get_paginator("describe_log_groups").paginate(
        logGroupNamePrefix=names["log_group_name"]
    ):
        for group in page.get("logGroups", []):
            if group["logGroupName"] == names["log_group_name"]:
                arn = group.get("logGroupArn") or group["arn"].removesuffix(":*")
                check_tags(
                    logs.list_tags_for_resource(resourceArn=arn).get("tags", {}), config
                )
                present.add("aws_cloudwatch_log_group.worker")
    return {
        "present": present,
        "image": image,
        "vms": list_vms(compute, names),
        "objects": objects,
        "uploads": uploads,
        "secret": secrets[SECRET_ADDRESS],
        "secrets": secrets,
        "orphans": orphan_resources(session, names, config, targets, roles),
    }


def orphan_resources(session, names, config, targets, owned_roles):
    # Discover drift outside state, but never delete these extra targets automatically.
    project = config["project_name"]
    orphans = set()

    def belongs(name, tags):
        return (
            name == project
            or name.startswith((f"{project}-", f"{project}/"))
            or tag_values(tags).get("Project") == project
        )

    role_names = {
        item["identity"].get("name") or item["identity"]["arn"].rsplit("/", 1)[1]
        for item in targets.values()
        if item["kind"] == "role"
    }
    policy_arns = {
        item["identity"]["arn"] for item in targets.values() if item["kind"] == "policy"
    }
    iam = session.client("iam", config=SDK_CONFIG)
    for page in iam.get_paginator("list_roles").paginate():
        for role in page["Roles"]:
            if role["RoleName"] in role_names:
                continue
            tags = iam.list_role_tags(RoleName=role["RoleName"])["Tags"]
            if belongs(role["RoleName"], tags):
                orphans.add(role["Arn"])
    for page in iam.get_paginator("list_policies").paginate(Scope="Local"):
        for policy in page["Policies"]:
            if policy["Arn"] in policy_arns:
                continue
            tags = iam.list_policy_tags(PolicyArn=policy["Arn"])["Tags"]
            if belongs(policy["PolicyName"], tags):
                orphans.add(policy["Arn"])
    for role in owned_roles:
        inline = {
            item["identity"]["name"]
            for item in targets.values()
            if item["kind"] == "role_policy" and item["identity"]["role"] == role
        }
        attached = {
            item["identity"]["policy_arn"]
            for item in targets.values()
            if item["kind"] == "role_attachment" and item["identity"]["role"] == role
        }
        for page in iam.get_paginator("list_role_policies").paginate(RoleName=role):
            orphans.update(
                f"role/{role}/inline/{name}"
                for name in page["PolicyNames"]
                if name not in inline
            )
        for page in iam.get_paginator("list_attached_role_policies").paginate(
            RoleName=role
        ):
            orphans.update(
                f"role/{role}/attached/{item['PolicyArn']}"
                for item in page["AttachedPolicies"]
                if item["PolicyArn"] not in attached
            )
    s3 = session.client("s3", config=SDK_CONFIG)
    for page in s3.get_paginator("list_buckets").paginate():
        for bucket in page["Buckets"]:
            if bucket["Name"] == names["bucket_name"]:
                continue
            response = optional_call(
                s3.get_bucket_tagging,
                S3_TAGS_MISSING,
                Bucket=bucket["Name"],
                ExpectedBucketOwner=config["aws_account_id"],
            )
            if belongs(bucket["Name"], (response or {}).get("TagSet", [])):
                orphans.add(f"arn:aws:s3:::{bucket['Name']}")
    secrets = session.client("secretsmanager", config=SDK_CONFIG)
    secret_names = set(secret_targets(names).values())
    for page in secrets.get_paginator("list_secrets").paginate(
        IncludePlannedDeletion=True
    ):
        for secret in page["SecretList"]:
            if secret["Name"] not in secret_names and belongs(
                secret["Name"], secret.get("Tags", [])
            ):
                orphans.add(secret["ARN"])
    logs = session.client("logs", config=SDK_CONFIG)
    for page in logs.get_paginator("describe_log_groups").paginate():
        for group in page.get("logGroups", []):
            if group["logGroupName"] == names["log_group_name"]:
                continue
            arn = group.get("logGroupArn") or group["arn"].removesuffix(":*")
            tags = logs.list_tags_for_resource(resourceArn=arn).get("tags", {})
            if belongs(group["logGroupName"].rsplit("/", 1)[-1], tags):
                orphans.add(arn)
    compute = session.client(MICROVM_SERVICE, config=SDK_CONFIG)
    request = {}
    while True:
        page = compute.list_microvm_images(**request)
        for item in page.get("items", []):
            if item["imageArn"] == names["image_arn"] or item["state"] == "DELETED":
                continue
            image = optional_call(
                compute.get_microvm_image,
                RESOURCE_MISSING,
                imageIdentifier=item["imageArn"],
            )
            if image and belongs(image["name"], image.get("tags", {})):
                orphans.add(item["imageArn"])
        if not page.get("nextToken"):
            break
        request["nextToken"] = page["nextToken"]
    return sorted(orphans)


def resource_context(environment):
    from scripts.setup import check_local_state, read_config

    document, original = read_config(environment)
    config = document["deployment"]
    check_local_state(config, environment)
    admin = operator_session(config, provisioner=False)
    for directory in environment.roots:
        environment.terraform(directory, "init", "-input=false", "-no-color")
        environment.terraform(directory, "validate", "-no-color")
    names, expected = terraform_manifest(environment)
    saved = {
        directory: saved_resources(environment, directory, targets, config)
        for directory, targets in expected.items()
    }
    found = inventory(admin, names, config, expected)
    for address, secret in found["secrets"].items():
        state_secret = saved[environment.main_root].get(address)
        if state_secret and secret and state_secret["arn"] != secret["ARN"]:
            raise CloudboxError(
                "secret_mismatch", "The saved and live secret ARNs differ."
            )
    return config, original, admin, names, expected, saved, found


def resource_report(environment, config, names, saved, found):
    tracked = set().union(*saved.values())
    active = [vm["microvmId"] for vm in found["vms"] if vm["state"] != VM_STOPPED]
    empty_state = not any(
        state_remains(environment, directory) for directory in environment.roots
    )
    clean = not (
        found["present"]
        or found["image"]
        or active
        or any(found["secrets"].values())
        or found["orphans"]
        or not empty_state
    )
    secrets = {
        name: "pending_deletion"
        if found["secrets"][address] and found["secrets"][address].get("DeletedDate")
        else "active"
        if found["secrets"][address]
        else "absent"
        for address, name in secret_targets(names).items()
    }
    return {
        "ok": True,
        "environment": environment.name,
        "account_id": config["aws_account_id"],
        "region": config["aws_region"],
        "project": config["project_name"],
        "clean": clean,
        "local_state_empty": empty_state,
        "tracked_resources": len(tracked),
        "present_resources": sorted(found["present"]),
        "untracked_resources": sorted(found["present"] - tracked),
        "orphan_resources": found["orphans"],
        "image": names["image_arn"] if found["image"] else None,
        "active_vms": active,
        "bucket": names["bucket_name"],
        "objects": len(found["objects"]),
        "multipart_uploads": len(found["uploads"]),
        "secret": secrets[names["secret_name"]],
        "secrets": secrets,
        "scope": SCOPE,
    }


def check_resources(environment, require_clean=False):
    config, _, _, names, _, saved, found = resource_context(environment)
    report = resource_report(environment, config, names, saved, found)
    if require_clean and not report["clean"]:
        raise CloudboxError(
            "resources_remain",
            "This Cloudbox environment is not empty.",
            inventory=report,
        )
    return report
