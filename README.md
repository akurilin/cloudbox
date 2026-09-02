# Cloudbox spike

Run Pi with GLM 5.3 (`z-ai/glm-5.3`) through OpenRouter in an AWS Lambda
MicroVM. This is a cloud-only spike, not a production agent platform.

```text
CLI -> MicroVM -> Pi -> OpenRouter
          |
          +-> S3 result + CloudWatch events -> stop VM
```

## Environments

Use one repository with separate credentials, configuration, and Terraform state.
Commands require `--env`, except the full test, which defaults to `test` and
cannot use another environment.

| Environment | AWS account | SSO profile |
| --- | --- | --- |
| `test` | `783951396681` | `cloudbox-test` |
| `prod` | `968438785594` | `cloudbox-prod` |
| `legacy` | `618170664907` | `AdministratorAccess-618170664907` |

Resources use `us-east-1`. The existing SSO session, `my-sso`, uses `us-west-1`.
The `legacy` option retains access to the old management-account deployment.
It does not migrate its data or state.

Non-secret inputs live in `infra/environments/<env>.tfvars.json`. Each environment
has separate bootstrap and main state under `.cloudbox/environments/<env>/`.
Keep these local files out of Git and back them up. Never change the account or
region of an existing state. The CLI reads selected Terraform outputs in memory.
Generated working directories link to the shared Terraform source; they do not
copy configuration or state from another environment.

## Setup

Requirements: Python 3.12+, uv, Terraform, AWS CLI, and SSO administrator access.
For a new checkout, copy the selected `infra/environments/*.tfvars.example.json`
to the corresponding `*.tfvars.json` path. Keep the OpenRouter key in
`.env.test` or `.env.prod` as `OPENROUTER_API_KEY=...`; these files are ignored.
Use `--env-file .env` to select the existing shared key explicitly.

```sh
aws sso login --profile cloudbox-test
uv run python scripts/setup.py --env test
```

For `prod`, use its profile and `--env prod`. Setup asks for one approval;
`--yes` skips the prompt. It checks the account, state, resources, and key, then:

1. Applies IAM bootstrap and infrastructure.
2. Loads the key into Secrets Manager, outside Terraform state.
3. Builds or reuses a matching worker image and waits for completion.
4. Saves the exact image version in the selected input file and Terraform output.

Setup reports `ready: true`. It does not submit jobs or call a model. AWS charges
apply. Setup refuses resource deletion, replacement, and untracked resource
conflicts. After a failed stage, correct the error and run setup again.

## Full cloud test

```sh
uv run python scripts/e2e_cloud.py --env-file .env
```

The test runs without approval prompts. The test deployment is disposable:

```text
reset test -> check clean -> setup -> math job -> validate -> teardown -> check clean
```

The test clears an existing Cloudbox test deployment before setup. It refuses
`prod`, `legacy`, shared account IDs, and unknown deletion targets. It checks
`(12345 * 6789) + 98765 == 83908970`, the downloaded JSON,
run status, logs, listing, and VM termination. It tries teardown even if setup or
the job fails. Cleanup failure fails the test. AWS and OpenRouter charges apply.

Reset and cleanup permanently delete the test secret, results, logs, and image.
Local reports and downloaded results stay under `.cloudbox/`. Do not run other setup,
teardown, or job commands against `test` during this test. If cleanup fails or the
process is killed, run teardown again with the same configuration and state.

To test an existing deployment without deleting it:

```sh
uv run python scripts/smoke_cloud.py --env test
```

The worker's `output/result.json` is the answer. The run's top-level `result.json`
is the supervisor's status record. They are separate files.

## Check or delete resources

```sh
uv run python scripts/check_resources.py --env test
uv run python scripts/check_resources.py --env test --require-clean
uv run python scripts/teardown.py --env test --plan
uv run python scripts/teardown.py --env test --force-delete-secret
```

Teardown asks for approval; `--yes` skips the prompt. It stops VMs, deletes the
image and bucket contents, then destroys main infrastructure and IAM bootstrap.
It verifies absence. Local keys, inputs, state files, and downloads stay.

Without `--force-delete-secret`, the secret keeps seven-day recovery and the
checker does not report a clean deployment. Keep state until deletion completes.
For the old deployment, use `--env legacy`; its files remain in their old paths.

Terraform deletes tracked resources, not all resources in an account. The shared
checker uses a Terraform resource manifest plus AWS checks for images, active
VMs, stored data, and project resources missing from state. New resource types
need checker support before setup can proceed. See [infrastructure notes](infra/README.md).

“Clean” means no Cloudbox resources in the checked scope and no populated
Cloudbox state. AWS account defaults, SSO roles, and retained service history
are not deleted. This is not an account-wide erase command.

## Jobs

Commands return JSON. Check `task_status` and `compute_state`; a successful CLI
command does not mean that the remote task succeeded.

```sh
uv run cloudbox --env prod submit 'Calculate 12 * 13 and write {"answer": 156} to output/result.json.'
uv run cloudbox --env prod list
uv run cloudbox --env prod status RUN_ID
uv run cloudbox --env prod logs RUN_ID --follow
uv run cloudbox --env prod download RUN_ID
uv run cloudbox --env prod cancel RUN_ID
```

Submission accepts stdin (`submit -`) or `--spec job.json`, with `--model` and
`--timeout` overrides. A job accepts `schema_version`, `prompt`, `model`, and
`timeout_seconds`. Only the prompt is required. No automatic model substitution.
Downloads refuse to overwrite files. Saved partial output remains available.

## Limits

- Pi and its supervisor share a VM without an internal security boundary.
- Outbound access is open; inbound access is disabled.
- A compromised worker can disclose credentials it can read or stop sibling VMs
  that use the same image. Workers cannot access other runs' S3 files.
- Default deadline: 600 seconds, including 30 seconds for cleanup; maximum 3,300.
- Prompt limit: 128,000 characters. Result limit: 1 MiB.
- Run data and logs expire after 30 days; deletion is not immediate at that age.
- Cancellation can leave an `unknown` outcome. Recovery is not yet tested.
- No uploads, resume, local worker simulation, or per-run environment changes.

The prior cloud math test passed on 2026-09-02 in the legacy account. The new
multi-account lifecycle test has not yet been run against AWS.
See [PLAN.md](PLAN.md) for decisions and verification records.
