# Cloudbox spike

Run Pi with GLM 5.3 (`z-ai/glm-5.3`) through OpenRouter in an AWS Lambda
MicroVM. This is a cloud-only spike, not a production agent platform.

```text
CLI -> MicroVM -> Pi -> OpenRouter
          |
          +-> S3 result + CloudWatch events -> stop VM
```

## Environments

Commands require `--env test` or `--env prod`. Use `test` for disposable resources
and `prod` for a persistent deployment. The full cloud test defaults to `test`
and rejects other environments.

Set your account ID, region, and AWS profile in
`infra/environments/<env>.tfvars.json`. These local files are Git-ignored.
Each environment has separate bootstrap and main state under
`.cloudbox/environments/<env>/`. Back up inputs and state. Never change the
account or region of existing state. The wrappers check the selected account;
the CLI reads Terraform outputs in memory.

`--env legacy` is removed. Its old local files remain ignored. Existing AWS
resources remain; Cloudbox no longer manages that deployment.

## Setup

Requirements: Python 3.12+, uv, Terraform, AWS CLI, and SSO administrator access
in your own accounts. Copy the shared example:

```sh
cp infra/environments/deployment.tfvars.example.json infra/environments/test.tfvars.json
```

Replace the placeholder account ID and AWS profile; check the other settings.
Keep the OpenRouter key in `.env.test` as `OPENROUTER_API_KEY=...`.
For `prod`, copy the example to `infra/environments/prod.tfvars.json` and use
`.env.prod`. Both key files are Git-ignored.

```sh
aws sso login --profile YOUR-PROFILE
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
Keep `test` deployed during implementation. Run setup again to apply changes and
select a new worker image. Use teardown when requested or when testing cleanup.

## GitHub tools

GitHub is optional. When configured, each run receives Git, `gh`, and temporary
access to the configured repositories. The agent reads the prompt and chooses
its actions. Cloudbox does not parse issue URLs or control GitHub workflows.
Python, Node, and `uv` are also available in the worker image.

Create a GitHub App with Contents, Issues, and Pull requests read/write, and
Metadata read. Install it on selected repositories. Add these fields to the
`deployment` object in the selected environment's ignored input file:

```json
{
  "github_app_id": 123456,
  "github_installation_id": 234567,
  "github_repository_ids": [345678]
}
```

Use the App ID, installation ID, and numeric repository IDs from GitHub.
Keep the downloaded App private key outside the repository. For first setup:

```sh
uv run python scripts/setup.py --env test --github-key-file /absolute/path/to/key.pem
```

Setup creates a separate secret and loads the key outside Terraform state.
Later setup runs can reuse its stored value. To replace the stored key:

```sh
uv run python scripts/set_github_secret.py --env test --key-file /absolute/path/to/key.pem
```

The trusted CLI reads that key and creates a repository-restricted token for
each run. The worker receives only the temporary token. Git uses `gh` as its
credential helper. The agent receives tool and access information, with no
token in its prompt. If required access or tools are unavailable through
authorized means, it must return a blocked result with the missing requirement.

Use normal prompts for GitHub tasks. For example, replace the URL below:

```sh
uv run cloudbox --env test submit --timeout 20m \
  'Address https://github.com/OWNER/REPO/issues/NUMBER. Run relevant checks, open a draft PR, and post its link on the issue.'
```

Cloudbox records the agent's completion report. Review external changes
independently. No automatic task retry is safe
after a lost response: a GitHub action may already have completed.

## Before a commit

Install the native tools once (macOS):

```sh
brew install terraform-linters/tap/tflint hadolint gitleaks
uv sync --dev
```

Terraform and uv are required above. On Linux, install TFLint, Hadolint, and
Gitleaks from their release packages. [pre-commit](https://pre-commit.com/)
installs and caches the remaining tools on the first run.

Run only when the changes are ready to commit:

```sh
uv run pre-commit run --all-files
```

The checks apply style fixes. Review and stage changed files, then rerun until
all checks pass. New files must be staged to be included. Do not run this suite
after each edit or during routine reviews.

| Tool                     | Check                                                      |
| ------------------------ | ---------------------------------------------------------- |
| Ruff                     | Python errors, imports, common bugs, and formatting        |
| Terraform fmt and TFLint | Terraform formatting and language rules                    |
| ShellCheck and shfmt     | Shell errors and formatting                                |
| Prettier                 | Markdown, JSON, YAML, and JavaScript formatting            |
| Hadolint                 | Dockerfile errors and shell commands                       |
| Gitleaks                 | Secrets in the selected files; findings are redacted       |
| pre-commit-hooks         | File syntax, merge conflicts, private keys, and whitespace |

These checks use local files. They do not initialize Terraform or use AWS.
Gitleaks checks file contents; it does not scan Git history.

To run the checks automatically on staged files at each commit:

```sh
uv run pre-commit install
```

## Local tests

```sh
uv run python -m unittest discover -s tests
node --test tests/test_finish.mjs tests/test_publish.mjs
```

These tests cover result handling, credentials, and cleanup. Cloud calls are
simulated. Use the cloud smoke check to verify the deployed agent loop.

## Job test: existing deployment

```sh
uv run python scripts/smoke_cloud.py --env test
```

Run this test for CLI and worker changes. It uses the selected worker image and
existing infrastructure. Missing configuration or access fails the test.
The job deadline is 20 minutes; override it with `--timeout`.

```text
exec -> agent creates and publishes files -> response + live logs
     -> job VM stops -> download printed links -> check file contents
```

The agent creates a CSV of integers 1–10 and their squares, a 32×32 blue PNG,
and a ZIP containing both. The test checks the sum (385), a unique test ID,
the saved response, separate live agent/supervisor logs, and all three URLs.
It downloads after VM termination, checks CSV values and PNG pixels, and
compares the archived files with the separate downloads.

Evidence stays under `.cloudbox/smoke/test/<test-id>/`: `response.txt`,
`debug.log`, the result, downloaded files, and `verification.json`.
On failure, the test stops only its job VM. Shared infrastructure and stored
results remain. Image updates are a separate deployment step.

## Full infrastructure lifecycle test

```sh
uv run python scripts/e2e_cloud.py
```

The test runs without approval prompts. The test deployment is disposable:

```text
reset test -> check clean -> setup -> job test -> teardown -> check clean
```

If GitHub is configured, also pass `--github-key-file /absolute/path/to/key.pem`.
The test checks this file before reset and reloads it during setup. The job
creates files in S3; it does not create GitHub content.

The test clears an existing Cloudbox test deployment before setup. It refuses
unknown deletion targets, invalid production inputs, and a production account
that matches the test account. Production inputs can be absent only when
production state is empty. It runs the
same job test described above. It tries teardown even if setup or
the job fails. Cleanup failure fails the test. AWS and OpenRouter charges apply.

Reset and cleanup permanently delete the test secrets, results, logs, and image.
Local reports and downloaded results stay under `.cloudbox/`. Do not run other setup,
teardown, or job commands against `test` during this test. If cleanup fails or the
process is killed, run teardown again with the same configuration and state.

## Agent completion and files

The agent calls `finish` with `status` (`completed` or `blocked`), a short
`summary`, either `response` text or a `response_file` path, and optional JSON
object `result`. The tool reads a response file under `output/` into `response`
without edits. It validates the report and ends the agent run. Invalid calls
return an error for correction. Call `finish` alone after other tools finish. The response
must contain text; files are optional.

If the agent returns text without `finish`, the harness sends one completion
reminder. A missing report after that reminder still fails the run.

The supervisor saves these fields under `report` in the run's `result.json`,
with runtime status, timing, and usage. Crashes and timeouts remain failures even
if a report claims completion. A blocked report also ends the run. The supervisor
still revokes credentials and stops the VM. Reports are limited to 1 MiB of JSON
and 128 nesting levels. Top-level fields are fixed; `result` has arbitrary fields
whose values can contain any JSON data.

The agent publishes selected files with `publish_file(path)` before completion.
Files must be regular files under the workspace's `output/` directory. The tool
checks paths and limits, uploads to this run's private S3 prefix, and returns a
download URL and a local JSON receipt. The agent can use the receipt to copy
exact URLs into its response file. Upload errors remain correctable tool errors.
The supervisor saves the response and published file metadata; it does not
select files or rewrite the response.

Signed links expire with their signing credentials, at most one hour. Use
`links RUN_ID` for fresh URLs or `download RUN_ID` for authenticated downloads.
Stored object keys remain available until normal retention removes the files.

New runs use internal schema 5. Update the worker image before submitting them.
User input specifications remain schema 1. Historical result files still download.

## Check or delete resources

```sh
uv run python scripts/check_resources.py --env test
uv run python scripts/check_resources.py --env test --require-clean
uv run python scripts/teardown.py --env test --plan
uv run python scripts/teardown.py --env test --force-delete-secret
```

Teardown asks for approval; `--yes` skips the prompt. It stops VMs, deletes the
image and bucket contents, then destroys main infrastructure and IAM bootstrap.
It waits for active image builds before deletion and verifies absence. Local
keys, inputs, state files, and downloads stay.

Without `--force-delete-secret`, secrets keep seven-day recovery and the
checker does not report a clean deployment. Keep state until deletion completes.

Terraform deletes tracked resources, not all resources in an account. The shared
checker uses a Terraform resource manifest plus AWS checks for images, active
VMs, stored data, and project resources missing from state. New resource types
need checker support before setup can proceed. See [infrastructure notes](infra/README.md).

“Clean” means no Cloudbox resources in the checked scope and no populated
Cloudbox state. AWS account defaults, SSO roles, and retained service history
are not deleted. This is not an account-wide erase command.

## Jobs

`exec` submits, waits for the saved result and VM termination, then prints the
agent's final response. It returns nonzero for failed, blocked, timed-out,
cancelled, or unknown outcomes. `wait` does the same for an existing run.

```sh
uv run cloudbox --env test exec 'Calculate 12 * 13.'
uv run cloudbox --env test exec 'Create and publish a CSV of square numbers.' --debug-agent --debug-supervisor
printf 'Explain this topic.\n' | uv run cloudbox --env test exec -
uv run cloudbox --env test wait RUN_ID
```

The final response goes to stdout. The run ID, errors, and optional debug events
go to stderr. `--json` returns the full result instead of response text. Debug
flags are independent and can be combined. They show message and tool events;
token deltas and incremental shell output are not included. Ctrl-C stops local
waiting and leaves the cloud job running. Use `wait RUN_ID` to reconnect or
`cancel RUN_ID` to stop it. A lost launch response must not trigger resubmission.

The commands below return JSON. `submit` returns at launch. Their exit codes
describe the CLI operation; inspect `task_status` and `compute_state` for the job.

```sh
uv run cloudbox --env prod submit 'Calculate 12 * 13. Set finish.result to a JSON object with an integer answer field.'
uv run cloudbox --env prod list
uv run cloudbox --env prod status RUN_ID
uv run cloudbox --env prod logs RUN_ID --follow
uv run cloudbox --env prod download RUN_ID
uv run cloudbox --env prod links RUN_ID
uv run cloudbox --env prod cancel RUN_ID
```

Both `submit` and `exec` accept stdin (`-`) or `--spec job.json`, with `--model` and
`--timeout` overrides. A job accepts `schema_version`, `prompt`, `model`, and
`timeout_seconds`. Only the prompt is required. No automatic model substitution.
Downloads refuse to overwrite files. Saved partial output remains available.

CloudWatch traces include visible assistant messages, tool arguments, tool output,
errors, timing, and usage. Private reasoning and binary content are excluded.
Known runtime credentials and common secret formats are redacted before logging.
Text values are limited to 8 KiB and records to 16 KiB; truncation is marked.
Use commands and their results to assess behavior; verify task outcomes separately.
Logs can contain repository content, and redaction cannot detect every arbitrary
secret.

Terraform sets CloudWatch retention to 30 days. Use `cloudbox logs RUN_ID` to read
the trace, or `cloudbox logs RUN_ID --follow` while the agent runs.

## Limits

- Pi and its supervisor share a VM without an internal security boundary.
- Outbound access is open; inbound access is disabled.
- A compromised worker can disclose credentials it can read or stop sibling VMs
  that use the same image. Workers cannot access other runs' S3 files.
- GitHub tokens permit writes across configured repositories, including merges
  where branch rules allow them. They are readable inside the VM. The App private
  key is not available to the worker. Normal cleanup revokes each temporary token;
  forced termination can leave it valid until its one-hour expiry.
- Default deadline: 600 seconds, including 30 seconds for cleanup; maximum 3,300.
- Prompt limit: 128,000 characters. Finish report limit: 1 MiB.
- Run data and logs expire after 30 days; deletion is not immediate at that age.
- Cancellation can leave an `unknown` outcome. Recovery is not yet tested.
- No input attachments, agent resume, local worker simulation, or per-run
  environment changes.
- Published files: at most 32 files, 32 MiB per file, and 128 MiB total per run.
  Uploads must finish before the agent deadline.

## Future work

Goal: unattended tasks with durable results. Ideas for later:

- Input attachments and finer progress streaming.
- Longer runs with credential renewal.
- Recovery for unknown results and cancellation.
- Task validation commands, shared-resource locks, resume, and stronger security
  boundaries.
