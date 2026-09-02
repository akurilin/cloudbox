# Infrastructure

Use the wrappers from the repository root. They select configuration, local
backend state, and Terraform working data together. Do not switch accounts with
raw `terraform apply` or workspace selection.

```sh
uv run python scripts/check_resources.py --env test --require-clean
uv run python scripts/setup.py --env test
uv run python scripts/teardown.py --env test --force-delete-secret
uv run python scripts/check_resources.py --env test --require-clean
```

Setup prepares infrastructure only. The separate `scripts/e2e_cloud.py` command
defaults to test, resets it without a prompt, then tests setup, a real job, and
teardown. Account and ownership checks remain. See [usage](../README.md).

## Ownership and state

Each environment reuses these Terraform roots:

- `bootstrap/`: SSO administrator; provisioner role and permission boundaries.
- This directory: restricted provisioner; storage, logs, secret metadata, workers.
- `modules/policy/`: shared names, permissions, and resource inventory contract.

`test` and `prod` use separate local backend paths and
`TF_DATA_DIR` directories under `.cloudbox/environments/<env>/`. Inputs live in
`infra/environments/<env>.tfvars.json`. Copy
`infra/environments/deployment.tfvars.example.json` and replace its placeholders.
The real files are Git-ignored. Keep inputs and state
backups private. The wrappers check account identity and reject state from
another account or region. There is no shared state or deployment-output cache.

Generated directories link shared Terraform source files; they never link
another environment's variable or state files.

Terraform owns no secret value. The setup helper writes the OpenRouter key
directly to Secrets Manager. The image script owns MicroVM images and source
archives: the pinned AWS provider lacks the hook, memory, and logging fields
needed by this worker. Setup selects an exact successful image version.

## Optional GitHub access

Add all three fields under `deployment` in the selected input file. Replace
these sample IDs with the App, installation, and allowed repository IDs:

```json
"github_app_id": 12345,
"github_installation_id": 67890,
"github_repository_ids": [24680]
```

Use 1-500 unique repository IDs. Omit all three fields to disable GitHub access.
Terraform creates separate App key metadata and grants key reads only to the
provisioner. Worker roles and boundaries cannot read this key.

For first setup, supply the PEM file:

```sh
uv run python scripts/setup.py --env test --github-key-file /path/to/private-key.pem
```

Later setup runs reuse the saved key if this argument is absent. To replace it:

```sh
uv run python scripts/set_github_secret.py --env test --key-file /path/to/private-key.pem
```

Keep the PEM file outside the repository. No key value enters Terraform state.
Resource checks and teardown cover both secrets. `--force-delete-secret`
removes recovery for both. Run teardown before removing the GitHub input fields;
checks reject an existing GitHub secret without matching configuration.

## Resource checks

Terraform state records managed objects; it is not an account inventory.
The shared checker evaluates the Terraform inventory contract and calls AWS
to check the project resources, including script-owned compute and bucket data.
Setup refuses untracked conflicts. Teardown refuses unknown deletion targets.
The full test requires clean checks before setup and after teardown.

When adding infrastructure, update its Terraform inventory entry and any new
AWS checker adapter. Coverage checks must pass for configuration, state, and
plans. Child settings, policies, log streams, and image versions disappear with
their checked parent resources.

Checks cover the configured project's supported resource types in the selected
region, plus IAM. They do not inventory every AWS service or erase AWS defaults,
SSO access, or retained VM history. Teardown keeps the bucket's `force_destroy`
disabled and explicitly empties it only after compute stops. Normal secret
deletion retains seven-day recovery; the full test explicitly removes recovery.

## Access limits

Worker permissions remain unchanged. The runtime role has no S3 or AssumeRole
grant. Each run gets a separate, prefix-scoped S3 session. Chained credentials
last at most one hour, so the spike limits runs to 3,300 seconds.

MicroVM role passing lacks usable `iam:PassedToService` context; only the exact
build/runtime roles can be passed. Connector passing and initial image tags need
region-limited wildcard grants. These grants are not given to workers.

Pi and the supervisor share a VM. The runtime can read the model key and stop
sibling runs of the same image. Internal hardening remains deferred. No VPC,
NAT gateway, public inbound endpoint, or private ECR is created.

References: [local backends](https://developer.hashicorp.com/terraform/language/backend/local),
[Terraform working data](https://developer.hashicorp.com/terraform/cli/config/environment-variables#tf_data_dir),
[destroy scope](https://developer.hashicorp.com/terraform/cli/commands/destroy),
[provider fields](https://github.com/hashicorp/terraform-provider-aws/blob/v6.62.0/internal/service/lambdamicrovms/image.go).
