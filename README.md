# Cloudbox spike

Run Pi with GLM 5.3 (`z-ai/glm-5.3`) on OpenRouter in a short-lived AWS Lambda
MicroVM. This is a cloud-only spike, not a production agent platform. There is
no local worker simulation or Claude Code integration.

```text
CLI -> new MicroVM -> Pi -> OpenRouter
             |        |
             |        +-> output/result.json
             +-> S3 results + CloudWatch metadata -> stop VM
```

The one end-to-end test passed on 2026-09-02 with image `2.0` and Pi `0.84.4`.
It submitted `(12345 * 6789) + 98765`, downloaded `{"answer": 83908970}` after
termination, and checked the answer. CloudWatch received the run metadata.
Run ID: `c9be3807-0acc-4fac-adfb-8974592e4b57`.

## Configuration

Use `infra/cloudbox.auto.tfvars.json` for the account, region, SSO profile, image
version, and shared settings. This file is ignored by Git. The checked-in example
shows its shape. Terraform state stays local and is also ignored.

The selected account is `618170664907` in `us-east-1`. A different account needs
separate state; do not reuse an old deployment's state with new credentials.
The CLI reads Terraform outputs in memory. It does not keep a configuration cache.

Keep the OpenRouter key in the ignored `.env` file until secret setup. Do not put
it in Terraform variables. Worker images contain no runtime keys or task prompts.

## Run the spike

Requirements: Python 3.12 or later, uv, Terraform, AWS CLI, and an active SSO login.

```sh
uv sync
aws sso login --profile AdministratorAccess-618170664907
```

Follow [infra/README.md](infra/README.md) to plan and apply the two Terraform roots.
Review changes before apply. The bootstrap creates the restricted provisioner;
the main root creates storage, logs, secret metadata, and worker roles.

After approved infrastructure deployment:

```sh
uv run python scripts/set_secret.py --env-file .env
uv run python scripts/build_image.py create --wait
```

Set `deployment.image_version` in the ignored Terraform input file to the reported
`imageVersion`. Review and apply this output change. The build does not select
itself. Use `update --wait` for later builds, not another `create`.

After deployment and image selection:

```sh
uv run python scripts/smoke_cloud.py
```

The test's expected answer is computed independently in its code. A pass requires
the downloaded answer to match, a successful run record, and a terminated VM.

Downloaded smoke results stay under `.cloudbox/smoke/<run-id>/`. The worker's
`output/result.json` is the deliverable. The run's top-level `result.json` is the
supervisor's status record; it is not the same file.

## CLI

Commands return JSON. A command exit code describes the CLI operation, not the
remote task outcome. Inspect `task_status` and `compute_state` separately.

```sh
uv run cloudbox submit 'Calculate 12 * 13 and write {"answer": 156} to output/result.json.'
uv run cloudbox list
uv run cloudbox status RUN_ID
uv run cloudbox logs RUN_ID --follow
uv run cloudbox download RUN_ID
uv run cloudbox cancel RUN_ID
```

Submission also accepts stdin (`submit -`) or `--spec job.json`, with `--model`
and `--timeout` overrides. The default is GLM 5.3; there is no automatic model
fallback. A JSON job accepts `schema_version`, `prompt`, `model`, and
`timeout_seconds`. Only the prompt is required. Downloads refuse to overwrite
an existing destination. Saved partial outputs remain available after failure.

## Limits

- Pi and its supervisor share a VM without an internal security boundary.
- Outbound internet access is open; inbound access is disabled.
- Runtime AWS access is scoped, but a compromised worker can stop sibling VMs
  that use the same image. It can also disclose credentials it can read.
- The default deadline is 600 seconds, including 30 seconds for cleanup. The
  spike maximum is 3,300 seconds because run-file credentials last one hour.
- The prompt limit is 128,000 characters; the result limit is 1 MiB.
- Run data and logs expire after 30 days. Deletion is not immediate at that age.
- Cancellation can leave an `unknown` outcome if AWS stops the VM after the
  CLI's immediate state check. Cancellation recovery is not yet tested.
- No uploads, resume, local simulation, custom environments, or extra test suite.

S3, logs, secret metadata, and IAM use Terraform. MicroVM image hooks are not yet
supported by the selected Terraform resource, so the image uses a small script.
Individual runs use Cloudbox records, not Terraform state. Keep the old image
and retained run data until deletion is explicitly approved.

The spike CLI reuses the restricted provisioner for trusted operator commands.
Workers receive neither its credentials nor Terraform state. The final run record
reports the Pi version from the selected image; it does not assume the current
source tree describes an older image.

See [PLAN.md](PLAN.md) for the decisions and current spike scope.
