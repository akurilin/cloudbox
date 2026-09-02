# Cloudbox plan

Archived on 2026-09-02. This file preserves earlier decisions and test records.
Its instructions and status entries are historical; it is no longer maintained.

Status: finish tool implemented and verified; test environment remains deployed.
Last reviewed: 2026-09-02.

The user approved implementation, Git initialization, incremental commits, and
the disposable test lifecycle. GitHub tools and CloudWatch traces are complete.
The live agent created draft PR 2 and an issue comment, passed local checks,
saved its result, revoked its token, and stopped its VM. The test environment
was later restored for finish validation and will remain deployed. AWS login
alone does not authorize new changes.

## Current spike scope

This section overrides the earlier v1 sequence and test requirements below.

### Blocking CLI and output files: code review

Requested on 2026-09-02. Assessment only; implementation and deployment are
not approved by this request. Reviewed the code at `da21c53`, including local
changes present before that commit. No application code or AWS resources changed.

The code already submits prompts, stdin, and job specifications to a separate
MicroVM. Pi runs unattended. The supervisor saves the accepted finish report,
revokes the GitHub token, and requests VM termination. `submit` returns at launch.
All CLI output is JSON; command success does not imply task success.

`logs --follow` polls the shared agent/supervisor CloudWatch stream. It records
completed assistant messages, tool starts/results, and lifecycle events. It has
no source filters, text deltas, or incremental tool output. Private reasoning is
excluded. `finish` has a short `summary` and arbitrary `result` object; it has no
defined full-answer field. `download` reads fixed JSON filenames only. There is
no general file uploader or signed-link command.

Proposed implementation:

- Add `exec PROMPT` with stdin/spec support. Reuse submission and add bounded
  waiting for the saved result and VM stop. Add `wait RUN_ID` for an existing
  run. Preserve asynchronous `submit` and existing JSON commands.
- Print the final answer and file links to stdout; send run ID, progress, and
  errors to stderr. Support `--json`. Return nonzero for blocked, failed,
  timed-out, cancelled, or unknown outcomes. Ctrl-C stops local waiting and
  prints the run ID; explicit `cancel` stops the cloud job. Never resubmit after
  an uncertain launch. Stop waiting within a deadline even if records are absent.
- Add optional `finish.response` for the full text answer, with `summary` as
  fallback. Keep structured task data in `result`. Print the saved response
  directly. Update both validators and the internal run schema together.
- Add independent `--debug-agent` and `--debug-supervisor` filters over one
  log reader. Preserve credential redaction and truncation markers. Start with
  existing message/tool events. Text and tool-output deltas need a separate
  worker change if finer streaming is required. Log delays must not determine
  task completion or prevent retrieval of a saved result.
- Extend `finish` with an explicit file list under a dedicated output directory.
  Validate paths and limits before accepting finish; recheck before upload.
  Reject links, paths outside that directory, and non-regular files. The agent
  creates any requested ZIP. The supervisor uploads declared files before the
  terminal record and cleanup, and records keys, names, sizes, and media types.
  An upload failure must prevent success and preserve any uploaded files.
- Store files under `runs/<run-id>/artifacts/`. Current S3 grants already allow
  reads and writes within the run prefix. Keep report and binary size limits
  separate; bound file count, total bytes, and upload time within the deadline.
  Extend downloads to use the recorded file list and stream bytes to disk.
- Generate private S3 HTTPS download links when displaying results. Save object
  keys, not expiring links. Report link expiry and let `wait RUN_ID` create fresh
  links later. Refresh signer credentials when needed: current assumed sessions
  last one hour, and a long run can consume most of that period. AWS confirms
  that [signed links expire with their signing credentials](https://docs.aws.amazon.com/AmazonS3/latest/userguide/using-presigned-url.html).

The first version can reuse the current AWS resources and run-scoped S3 access.
It needs CLI, finish-tool, supervisor, image, and focused regression changes.
Core checks: saved-result waiting, remote failure exit codes, interrupted waits,
log filtering/redaction, file path and size limits, upload failure, and binary
round-trip retrieval. Cloud validation needs a later approved run; no cloud
test was run for this review. Keep the existing test environment deployed.

The CLI still requires configured AWS access and local Terraform state. The
current sandbox has open outbound access; Pi and its supervisor share a VM
without an internal security boundary. This proposal does not change that model.

### Test scope

The user approved keeping only core regression tests on 2026-09-02.
Reduction complete: 111 tests became 45 (39 Python, 6 Node); test code fell
from 1,715 to 886 lines across eight files. Worker edits and supervisor review
are complete. Both local suites and the diff whitespace check pass.

Keep tests for core behavior or reproduced failures. Do not add tests solely
for AWS field forwarding, display formatting, literal schemas, prompt wording,
or speculative input variants. Removed the infrastructure helper/report and
smoke orchestration suites, legacy client cases, and repeated input matrices.
One large-report test now covers supervisor events, storage, reads, and download.

Retained finish correction and failure handling, result storage, credential
scope and cleanup, real Git setup, log redaction, pre-reset key checks, and
image cleanup failures. README now gives both local test commands; Python
discovery requires `-s tests`. Local tests simulate cloud and Pi boundaries.
Use the cloud smoke check when requested; run the full infrastructure lifecycle
test only on explicit request. No application code or cloud resources changed.

### Result reporting

The user approved implementation of a simpler completion contract:
one generic `finish` tool with `status` (`completed` or `blocked`), a short
`summary`, and an optional JSON object `result` with arbitrary fields. Replace the
mandatory output file and exact final-message JSON. Validate tool arguments so the agent
can correct errors. The supervisor stores the report and runtime facts, and still
owns failure, timeout, cancellation, upload, and cleanup. Return needed data in
the report or use granted external tools; general file collection is separate.
Completion remains an agent claim, not proof of
task quality. Pi supports custom tools with argument schemas; verify the pinned
version when implementing. Use internal run schema 3 so old workers reject the
new contract. User prompt input stays schema 1. The saved run record contains
`report` with the accepted tool parameters; runtime status remains separate.
Preserve access to historical downloads. Replace the math check's fixed output
file with `report.result.answer`.

The user approved keeping the test environment deployed and updating it as needed.
Do not reset or tear it down after each change. Completed job VMs must still stop.
The previous test environment was restored for the finish check and remains
deployed. The full lifecycle test was not run.

Implementation uses Pi 0.84.4's native `terminate: true` tool result. The explicit
trusted extension loads while automatic extension discovery stays disabled.
`finish` must be the only tool in its message; invalid calls remain correctable.
Accepted completion cancels later automatic compaction. The supervisor reads the
full tool result independently of log redaction and truncation. Reports allow
1 MiB of stored JSON, with a depth limit of 128. Runtime metadata has a separate
16 KiB read allowance. JSON encoding uses UTF-8 throughout.

Local implementation and review passed: 94 Python tests and 17 isolated tool
tests. Regressions cover the removed final-reply requirement, blocked reports,
runtime failure precedence, save failure, historical downloads, and encoding
differences between Node and Python. No local agent or image run was used.
The test environment was restored with worker image `1.0`, then updated to `2.0`.
Three cloud runs accepted the finish report, saved it, revoked the token, and
stopped the VM. The math check failed because `result` contained JSON text instead
of an object, even with an explicit object request and explicit union schema.
Pi forwards the schema unchanged; it did not convert the result.

The final contract requires an object at `result`, with arbitrary fields and
unrestricted nested JSON values. This keeps task data generic while allowing
stringified objects to fail validation before the run ends. No automatic parsing
changes result values. Python regression tests failed before this correction,
then passed. The extension review passed.

The final cloud check passed on image `3.0`: run
`3b54bb09-a302-486c-bff4-f3cc8763135c` returned `{"answer":83908970}` as an object.
One finish call ended the agent with no later model call. The supervisor saved
the report, revoked the token, and stopped the VM. Listing and 18 CloudWatch
events passed; log retention is 30 days. The deployed source matches
`cd52ad58c555019a6c50ff5940bf73276b80f0a4aa402dcc7dfe3e13fd7b2bd9`.
Records are under `.cloudbox/finish-validation/2026-09-02/`, including
`verification.json`. The test environment remains deployed. Prod and legacy
were not changed.

### GitHub tools

Requested and approved for implementation on 2026-09-02. The user clarified
that Cloudbox must remain a general agent sandbox. Provide tools and access;
the agent selects its actions from the prompt. Do not add issue-specific job
modes, URL parsing, checkout, test, commit, PR, or comment orchestration.
This replaces the earlier issue-to-PR implementation proposal.

Local implementation and worker/verifier review are complete. All 68 unit tests
pass. The user resumed work after the pause. The user then approved a live test
that creates a draft PR and issue comment. Use a short task prompt and the generic
tools. Verify the result, then clean up the disposable AWS resources.

The user created `cloudbox-agent` and supplied its IDs and local key path.
Authenticated checks passed on 2026-09-02:

- App ID: `4809321`; installation ID: `158588142`; account: `akurilin`.
- Installation: `https://github.com/settings/installations/158588142`.
- Key: `/Users/alex/Downloads/cloudbox-agent.2026-09-02.private-key.pem`.
  Its signature matched the App. File permissions were changed to owner-only
  read/write (`0600`). No key or token value was printed or saved in the project.
- Permissions: Contents, Issues, and Pull requests write; Metadata read.
  No event subscriptions. Webhook activation was not checked.
- Repository: `akurilin/cloudbox`, ID `1355081064`, default branch `main`.
  A temporary token restricted to this repository and read access could read
  the repository and issue 1. The token was then revoked.
- Installation scope was **all repositories**. The suggested installation scope
  is **Only select repositories**, with `cloudbox` selected. Each run token must
  still be restricted to the configured repository IDs and permissions.

No repository content or AWS resources changed during access checks. The test
input file now contains the App, installation, and repository IDs. The cloud
lifecycle will load the key into its separate secret and remove it at teardown.

#### Access and tools

Keep the App private key in a separate Secrets Manager secret. Terraform owns
its metadata; a setup command loads the value outside Terraform state. Only
trusted submitter access can read this key. Worker, build, and run-data roles
must not gain key access. App ID, installation ID, allowed repository IDs, and
the derived secret reference are non-secret deployment settings.

When GitHub is configured, the CLI creates an installation token restricted to
the configured repositories. Pass it through the transient run-hook payload,
separate from the saved specification. Record available repositories and granted
permissions in the specification so the agent knows its access. Do not inspect
the task text to choose a workflow or repository.

Use `GH_TOKEN` and the `gh` Git credential helper at runtime. Never put the token
in clone URLs, saved Git credentials, model prompts, result files, or normal logs.
The image includes Git, pinned ARM64 `gh`, and `uv`, with the existing Python,
Node, Pi, and OpenRouter tools. No MCP server is required. Pi uses its shell tool.

Give the agent concise tool and access documentation. It decides whether to
read an issue, clone a repository, run tests, publish a PR, post a comment, or
perform another task. Keep the current JSON output and completed/blocked reply
contract. If a required tool or permission is unavailable through authorized
means, return blocked with the missing requirement. Do not discover credentials,
exploit systems, or bypass access controls. Public dependency installation inside
the sandbox remains allowed. Ordinary task actions follow the user's prompt.

GitHub installation tokens expire after one hour. Check returned expiry against
the full run deadline and a margin. Keep the existing 3,300-second maximum and
4,096-byte hook payload cap; current tokens have variable length. Reject unsafe
expiry or oversize payloads before launch. Revoke on known prelaunch failure or
normal cleanup; retain tokens after an uncertain launch until outcome is known
or expiry. Longer jobs need a trusted cloud token service, not the App key in VM.

The token is readable by Pi and repository scripts in the current shared VM.
Write permissions apply to all allowed repositories, not one branch or issue.
Contents write can permit merging. Instructions are not access controls. Branch
rules can enforce review without an App bypass. Strict operation restrictions
need a credential service outside the VM; that service remains deferred.

#### Implementation and checks

- Keep normal prompt/model/timeout inputs. Add generic GitHub capability metadata
  and transient credentials. Use a newer run schema when GitHub is enabled so an
  old worker cannot silently ignore the new capability. Existing jobs still work.
- Add App authentication with a maintained JWT library and bounded API requests.
  Enforce exact repository/permission scope and prevent credentials in errors.
- Add optional secret/configuration to Terraform, setup, teardown, and the
  resource manifest/checker. Preserve OpenRouter-only deployments. Private keys
  remain outside Terraform state, source, and worker images.
- Add generic runtime tool setup and documentation, then revoke tokens during
  cleanup. The supervisor continues to own lifecycle, output validation, logs,
  and termination. It does not decide or perform GitHub task actions.
- Test token scope, expiry, payload size, rejected access, tool environment,
  blocked results, and existing artifact jobs. Use the required worker-agent
  implementation and supervisor/verifier review split.

Implemented checks cover exact token scope, bot commit identity, transient
credential transport, expiry, hook size, known and uncertain launch failures,
secret isolation, optional configuration, cleanup, and blocked agent results.
The shared GitHub client bounds responses, refuses redirects, hides remote error
text, and does not retry writes. Regression tests failed before corrections for
partial HTTP responses and ambiguous AWS launch responses after SDK retries.
Python compilation, whitespace checks, and both Terraform roots pass.

The first cloud build failed because Git 2.55 enabled Rust and `cargo` was not
installed. Its documented `NO_RUST=YesPlease` option now selects the supported
C implementation for build and install. The corrected AWS image built successfully.
The failed attempt removed all 21 Terraform items, image, source archive, logs,
and both secrets. The final resource/state check was clean. Its report and
captured build error are in
`.cloudbox/e2e/test/d830fdcb-395e-4a6a-83a5-9587ddc981db/`.

#### Resumed validation

The retry was interrupted at the user's request before an agent job ran.
Report: `.cloudbox/e2e/test/b058625f-cf95-4cd1-a70c-9c9c02b19d09/report.json`.
Initial cleanup failed: AWS returned `ValidationException` from
`DeleteMicrovmImage` while the build was active. The test launched no agent VM.
The cleanup-only wait later failed on DNS before teardown. On resume, the
resource check found all 21 tracked resources, image/archive, and both secrets;
no active VM, orphan, or untracked resource was present.

The resumed image `1.0` built successfully. Its source digest matched local code:
`8ad79781bed4852f7249e6c3b04d827c9b518c1324f0991ffb328332e96f0a1a`.
It was reused for the remaining math/token checks. The first resumed
submission failed with AWS 502 before a run record or VM was created.
The checked retry passed: run `b9403d68-7666-4be6-b6a7-522999b8642b` returned
`{"answer": 83908970}`, recorded schema 2 and confirmed token revocation, then
terminated. Listing and 21 CloudWatch events passed. Downloads are in `resumed-run/`
under the interrupted test's report directory.

A normal read-only prompt then asked the agent to assess issue 1 from a local
checkout. Run `c8e9e636-38ee-498f-bda7-dcd247bac41f` succeeded, revoked its token,
and terminated. Its report identifies the issue and checkout commit
`8a2affae454a7424d2f5b358d3de26b2ed324f5d`; the commit and title match the local Git
object. Downloads are in `github-readonly/` under the same report directory.
The agent report is evidence to review, not proof that each claim is correct.
The cleanup fix waits for parent and version builds, retries transient reads
within a deadline, and uses one SDK attempt for each delete request. Seven
regression tests pass; independent review found no remaining blocker.
A live prompt that creates a GitHub PR/comment was approved after this check.

#### Agent execution trace

The user requested CloudWatch traces for task evaluation and 30-day log retention.
Log visible assistant progress/final text, tool arguments, tool results, errors,
timing, and existing usage metadata. Exclude private/provider reasoning blocks.
Redact runtime credentials and common secret formats before logging. Bound event
content and show truncation so logs cannot grow without useful limits. Keep all
task decisions with the agent; tracing adds no workflow logic.

The deployed `/aws/lambda-microvms/cloudbox-worker` log group already has 30-day
retention, matching Terraform. Confirmed by AWS on 2026-09-02. Trace implementation
and ten new tests are complete; the full 68-test suite passes. Text values are
limited to 8 KiB and trace records to 16 KiB. Setup reused the infrastructure and
built image `2.0` with the trace changes; it did not reset the environment.
Source digest: `1f976282d761e81a73adf99e49a53ea344dd58d93b1b0bd02eafd247705191a4`.

Trace run `00faa8a3-6763-4af9-84ba-0a8aff65a580` read issue 1, cloned the repo,
and saved valid JSON. It failed `invalid_completion_signal`: the final message
contained prose before the required status object. This is an agent contract
failure; do not change the parser to count the run as successful. Token revocation
and VM termination passed. The saved trace contains 80 events, including all
23 tool calls and their results. The largest event is 4,841 bytes. A credential
printed by an agent shell command was redacted in the trace. Logs and downloads
are in `trace.jsonl` and `traced-run/` under the interrupted test report directory.

The user approved real, disposable PRs and issue comments. Full task run
`05aa34a3-a77a-46c8-a589-6aa157ce5bae` uses image `2.0` and a short prompt:
address issue 1, run relevant local checks, open a draft PR, post its link on the
issue, and save the result as JSON. No workflow or system-prompt change was made
for this test. Keep prompts minimal.

The full task passed. The agent read the issue, cloned the repo, added
`scripts/hello.py`, ran it with Python 3.12 through `uv`, and passed Ruff lint and
format checks. It pushed commit `e1402539c59f252975d646a6b21b70edab8ef60e`, opened
[draft PR 2](https://github.com/akurilin/cloudbox/pull/2), and posted the
[issue comment](https://github.com/akurilin/cloudbox/issues/1#issuecomment-5516735271)
as `cloudbox-agent[bot]`. GitHub confirms the PR contains only the requested script.
The final JSON status was valid; the result was complete, the token revoked, and
the VM terminated. The trace contains 56 events for 14 tool calls; one long read
was marked truncated. Downloads and logs are in `pr-run/` and `pr-trace.jsonl`.
The PR remains open for review. AWS cleanup is separate from GitHub publication.
Independent review confirmed the bot authorship, pinned script output, PR scope,
and comment link. Focused checks passed. Repository-wide lint found existing
errors; the agent's PR claims only the focused checks. Review evidence is in
`pr-verification/summary.json` under the same test directory.

Standard teardown passed. It removed the image, 18 S3 objects, all 21 Terraform
items, logs, and both secrets without recovery. The final checker reports empty
local state, no active VM, and no tracked, untracked, or orphan Cloudbox resource.
Local logs and results remain. The original interrupted report remains unchanged;
`resume-verification.json` records the resumed checks and final cleanup.

Acceptance remains a normal prompt containing
`https://github.com/akurilin/cloudbox/issues/1`, asking for its change, a PR, and
an issue comment. The issue asks for a script that prints `hello world`.
The agent must discover and use GitHub/Git itself. Independently verify the
script check, PR, comment, saved result, and VM termination. No special job type
or production workflow code may be needed for this example. If given a task
outside its access, the agent must return blocked and identify missing access.

References checked on 2026-09-02:

- [GitHub App access](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/differences-between-github-apps-and-oauth-apps)
- [Installation token scope and expiry](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app)
- [GitHub CLI environment](https://cli.github.com/manual/gh_help_environment)
- [Git credential helper](https://cli.github.com/manual/gh_auth_setup-git)

### Latest full cloud test

On 2026-09-02, `uv run python scripts/e2e_cloud.py --env-file .env` passed
without approval prompts in test account `783951396681`, `us-east-1`.

- Initial check: no Cloudbox resources; both Terraform states empty.
- Setup: created 20 Terraform items, loaded the key, built and selected image
  `cloudbox-worker:1.0`. Setup itself submitted no job.
- Math: Pi `0.84.4` with OpenRouter `z-ai/glm-5.3` returned
  `{"answer": 83908970}` for `(12345 * 6789) + 98765`.
- Run: `309332b8-e36a-4b6b-9a76-82e9aed752a7`; status `succeeded`;
  VM `TERMINATED` before download. Listing and 20 CloudWatch events verified.
- Teardown: removed all 20 Terraform items, image, five S3 objects, logs, and
  secret without recovery. Final checker found no Cloudbox resources, active
  VMs, orphan resources, or populated state in its scope.
- No job or cleanup failures. Prod and legacy were not changed; both legacy
  state hashes match their pre-test values.
- Report and downloaded files remain under
  `.cloudbox/e2e/test/4c701c9a-0628-487c-86d8-4089fae25df1/`.

The account started clean, so this run did not test removal of an existing
deployment before setup. Failure recovery and hostile-agent isolation remain
unverified. Earlier checkpoint records below describe prior states.

### Git secret audit

On 2026-09-02, Gitleaks 8.30.1 found no secrets in all 23 local commits,
including reflogs, or all 106 file objects, including unreachable objects.
Targeted credential checks also found no matches. The local OpenRouter key
and its base64, URL-encoded, and hex forms were absent. `.env` is ignored and
untracked; no credential files or Terraform state appeared in commit paths.
The repository is not shallow and has no remote. This check cannot cover
external copies or objects already removed by Git garbage collection.
Results: `.cloudbox/security-audit/2026-09-02/summary.json`.
No Git history or AWS resources changed.

### One-command setup requirement

The user now wants one entry point from no Cloudbox infrastructure to a deployment
ready for jobs. Internal Terraform stages are acceptable; no manual image build,
secret upload, or version selection between stages. Keep the separate IAM
bootstrap because the main root uses its restricted role.

Use `uv run python scripts/setup.py --env test`: check the Terraform input file, AWS
access, and `.env` key; apply bootstrap and main infrastructure; load the key;
build or reuse the matching image and wait; select its exact successful version.
Report ready after these stages succeed. Do not submit an agent job or run the
cloud math test during setup. Run `uv run python scripts/smoke_cloud.py --env test` separately
when an end-to-end check is wanted; that command incurs AWS and OpenRouter usage.
Tools, AWS account/SSO configuration, and the two private input files remain
prerequisites. The entry point must not install tools, create credentials, or
delete an existing deployment. One confirmation or `--yes` covers its stages.

The setup command may update `image_version` in the existing Terraform-owned
input file after build success. Do not create a second configuration cache.
This replaces Q19's manual selection step, not its exact-version pinning rule.
Repeated setup should reuse a matching successful image. Interrupted setup must
not require the user to copy intermediate values between commands.

This replaces the earlier requirement to run the math test within setup. Help,
syntax, and a static no-job check pass; the revised setup has not been applied.
The current deployment and saved results are unchanged.

The requested infrastructure test removes only inventoried Cloudbox resources,
checks their absence, then runs this entry point. Existing S3 data and logs would
be lost; downloaded files and `.env` stay local. Immediate removal of the old
secret needs explicit approval because normal deletion has a seven-day recovery
window. The user approved deletion of the inventoried 20 Terraform items, one
image, eight S3 objects, three log streams, and the secret without recovery,
followed by rebuild and the math check. The image, eight objects, 14 main items,
secret without recovery, and six bootstrap items were deleted. Exact-resource
absence checks passed: both Terraform states were empty; the bucket, four roles,
four policies, log group, secret by ARN and name, and image were absent. All 13
AWS checks passed. The first `uv run python scripts/setup.py --yes` attempt
created six bootstrap items, then stopped: AWS rejected AssumeRole before the
new role was ready. Main infrastructure was not created. A later read-only STS
check succeeded without any policy change. Setup now retries only this role
access check for up to 120 seconds. The six bootstrap items were removed again;
both states and all IAM targets were confirmed empty. The second one-command
attempt created all 20 items, loaded the key, and built image `1.0`. Its final
selection plan stopped on a transient DNS error during a CloudWatch refresh.
Selection now uses `-refresh=false`: it changes only local outputs using the
preceding infrastructure state. This attempt's image, archive, 20 Terraform
items, and secret were removed. Both states and all 13 AWS absence checks passed
again. The third clean setup invocation completed with exit code zero and
`ready: true`. No agent job ran in either failed attempt.
Local inputs, state files, and the earlier downloaded result remain.
Do not claim that AWS-managed account resources or retained service history
are removed.

### Prior clean setup result, with the math test

- Entry point: `uv run python scripts/setup.py --yes`.
- Start: no Cloudbox resources; both Terraform states empty.
- Finish: 20 Terraform items, key loaded outside state, image built and selected,
  and the existing cloud math check passed. No manual command between stages.
- Image: `cloudbox-worker:1.0`; Pi `0.84.4`; OpenRouter `z-ai/glm-5.3`.
- Run: `e32a9407-5b57-44a6-8794-8465491bb9db`.
- Result: `{"answer": 83908970}`; status `succeeded`; VM `TERMINATED`.
- CloudWatch events received; no active Cloudbox VMs remain.
- Local file: `.cloudbox/smoke/e32a9407-5b57-44a6-8794-8465491bb9db/output/result.json`.

Image versions restarted after full image deletion. The earlier `2.0` result
below describes the removed deployment. The current `1.0` contains the corrected
worker. Infrastructure remains deployed and can accept new CLI submissions.

### Standard teardown

Use `uv run python scripts/teardown.py --env test`; `--plan` makes no AWS changes. One
confirmation or `--yes` approves stopping VMs, deleting the image and bucket
contents, and destroying main infrastructure before bootstrap. Check exact
Terraform-derived targets, account, ownership, and absence. Permit another
attempt after partial deletion; do not bypass missing state or provisioner access.
Keep local keys, inputs, state files, and downloaded results.

Normal secret deletion keeps seven-day recovery and reports pending deletion.
`--force-delete-secret` explicitly removes recovery for a clean rebuild. It also
works for a scheduled secret after Terraform has finished. Do not claim that
AWS-managed history or unrelated account resources are removed. See README for
the command. Adding this command does not authorize another live teardown.
Python syntax, command help, and the live read-only `--plan` check passed. The
preview found 20 Terraform items, one image, five S3 objects, and no active VMs.
An output-only destroy plan also passed without cloud resource changes. The
new deletion path has not been run. The current deployment is unchanged.

### Deployment environments

Use `prod` and `test`; the user selected `prod`, not `persistent`.
Approved implementation: one repository and shared Terraform roots, with separate
environment inputs, local backend paths, and Terraform working data. Require
`--env test`, `--env prod`, or `--env legacy`, except for the full test, which
defaults to test and rejects other environments. Legacy keeps the
existing management-account deployment and its original state paths.
Never migrate that state into either new account.

Inputs: ignored `infra/environments/<env>.tfvars.json`, with checked-in examples.
States and backend data: `.cloudbox/environments/<env>/{bootstrap,main}/`.
Terraform executes in each stage's `source/` directory, separate from state.
Keys default to `.env.<env>`; `--env-file .env` explicitly selects the shared key.
Setup still submits no jobs. Each CLI and helper uses the selected environment.

Full test: `uv run python scripts/e2e_cloud.py --env-file .env`.
The user permits test resources to be removed at any time. No approval prompt
or `--yes` is needed for the full test. Keep account, state, and ownership checks;
this changes human approval, not IAM permissions or deletion scope.
Reset existing Cloudbox test resources with the standard teardown, including
permanent secret deletion. Require a clean check before setup and a test account
distinct from prod and legacy. Run setup, submit the math job, validate its
downloaded JSON and VM termination, check list/log operations, then tear down.
Try cleanup after setup or job failure. Permanently delete the test secret and
require a final clean check. A failed job or failed cleanup fails the test.
Keep local reports and downloads. Do not run other commands against test during
the full test. Unknown targets or missing state still fail; do not erase unrelated
account resources. Standalone setup/teardown confirmation behavior is unchanged.

A shared checker uses a Terraform-owned resource manifest, state and plan
coverage checks, and AWS inventory of supported project resources. It also checks
script-owned images, active VMs, bucket data, and pending secret deletion.
New infrastructure needs corresponding checker coverage. Clean means no
Cloudbox resources in that scope; AWS defaults, SSO access, unrelated resources,
and retained service history remain. Terraform destroy alone is not an orphan
resource checker.

The Cloudbox test lifecycle has standing user approval, including AWS/OpenRouter
charges and permanent test-data deletion. Prod and legacy operations do not.
Default selection, automatic reset, no-prompt execution, and prod rejection
passed local control-flow checks with AWS calls replaced. No live run was made
for this change. The old `--yes` flag remains an optional compatibility no-op.

Implementation checks found that `TF_DATA_DIR` alone did not isolate Terraform
from the old root's implicit state. Use separate execution directories with links
to shared source for all three environments. During the legacy compatibility
check, same-path backend initialization emptied both original state files.
Terraform had saved each prior state as a backup. Both were restored after JSON,
resource-count, and account checks; restored files match those backups byte for
byte. They contain the original 6 bootstrap and 14 main resource instances.
AWS resources were not changed. Verify state hashes during further init checks.

Review also found that valid partial state can lack account or region ARNs.
Early checks now reject conflicting known identities; exact manifest matching
and live AWS account/ownership checks cover partial state before any mutation.

Verification completed on 2026-09-02:

- Python compilation, all command help, Terraform formatting, and all six
  environment/root validation checks passed.
- Init preserves state bytes, including the restored legacy state. Each source
  directory is separate from its state; unexpected implicit state is rejected.
- Read-only checks report test and prod clean: no Cloudbox resources or state.
- Legacy inventory and destroy preview found the expected 20 tracked items,
  one image, five objects, no active VMs, and no untracked or orphan resources.
  Normal plans for both legacy roots contain zero resource changes.
- Partial-state and conflicting-identity checks passed. The lifecycle test
  rejects prod. Declining approval returns JSON and starts no cloud stages.
- Review found and fixed approval prompts entering JSON stdout. The declined
  test then passed; report: `.cloudbox/e2e/test/12ed13c8-53d7-4b12-87af-1adb89fbcaaf/report.json`.

No infrastructure apply, agent job, or teardown ran in this pass. The new full
lifecycle path is not yet cloud-verified. The user later removed the full test's
approval requirement and permitted automatic test reset, as recorded above.
The legacy deployment remains in AWS.

Initial read-only Organizations checks on 2026-09-02 found that account
`618170664907` is the management account of `o-4vt9cqww4f`, then its only account.
The user subsequently created these member accounts; both were verified ACTIVE:

- `prod`: `cloudbox-prod`, account `968438785594`.
- `test`: `cloudbox-test`, account `783951396681`.

The existing IAM Identity Center instance is in `us-west-1`, using local SSO
session `my-sso`. Read-only checks confirmed that `alex.kurilin` has a direct
`AdministratorAccess` assignment in both accounts, with the AWS-managed
`AdministratorAccess` policy. Local profiles `cloudbox-prod` and `cloudbox-test`
now reuse `my-sso`; STS verified each expected member account. Worker
permissions stay restricted. Keep Cloudbox resources in `us-east-1`.
AWS recommends keeping workloads out of the management account and using
temporary credentials through federation.
See [account guidance](https://docs.aws.amazon.com/organizations/latest/userguide/orgs_best-practices_mgmt-acct.html)
and [access guidance](https://docs.aws.amazon.com/IAM/latest/UserGuide/best-practices.html).
No Cloudbox resources were deployed in the new accounts and no access grants
were changed during verification. Deployment, migration, and removal of the
current deployment need separate approval. Never repoint its existing state
at a new account. Local and read-only checks passed; no cloud job has run in
either member account.

### Cloud execution scope

- Build and run the worker only in AWS. No local simulation or storage adapter.
- Use one end-to-end test: submit a math prompt, let Pi write
  `output/result.json`, download it, validate the answer, and confirm compute stops.
  Keep `runs/<id>/result.json` separate for supervisor bookkeeping.
- No other test suite in this pass. Formatting, syntax checks, and review remain
  useful, but do not build the earlier failure-case test matrix now.
- Keep the code small and disposable. Do not build a general agent framework.
- Preserve the agreed AWS IAM limits, own-run S3 access, secret handling,
  metadata logs, and hard deadline. Internal security hardening stays deferred.
- Use one-hour scoped STS sessions for run files. Limit spike run deadlines to
  3,300 seconds and check credential lifetime before launch. The default remains
  600 seconds with 30 seconds for cleanup. Longer jobs are later work.
- The manager coordinates infrastructure, worker, and CLI agents, then reviews
  their integration. No agent may deploy without the reviewed cloud changes.

Git is initialized on `main`. AWS SSO identity was verified for account
`618170664907`. The user supplied the OpenRouter key in the ignored `.env` file.
Never put either credential in source, chat, saved job records, or Terraform state.
The user confirmed Pi and `z-ai/glm-5.3` through OpenRouter; no Claude Code or
Anthropic inference integration is part of this spike.

Implementation files now cover Terraform, the Pi worker, the CLI, image builds,
secret setup, and one cloud smoke test. Terraform validation, Python compilation,
and shell syntax checks passed. The real cloud test passed with
`uv run python scripts/smoke_cloud.py`.

### First cloud result, before rebuild

- Run: `c9be3807-0acc-4fac-adfb-8974592e4b57`.
- Image: `cloudbox-worker:2.0`; Pi `0.84.4`; OpenRouter `z-ai/glm-5.3`.
- Shared allocation: ARM64, 1,024 MiB memory; AWS manages other allocation.
- Prompt: calculate `(12345 * 6789) + 98765` and write JSON.
- Downloaded output: `{"answer": 83908970}`; independent calculation matched.
- Supervisor status: `succeeded`; Pi exit code: `0`; artifact upload confirmed.
- AWS compute state: `TERMINATED`, before the 600-second deadline.
- Image inventory: both test VMs terminated; no active Cloudbox VMs remain.
- CloudWatch received model/tool and supervisor events, including shutdown.
- Local result: `.cloudbox/smoke/c9be3807-0acc-4fac-adfb-8974592e4b57/output/result.json`.

The test downloads only after compute stops. The result therefore outlives its
VM. One tool call failed; Pi recovered and completed the task. The logs contain
metadata, not tool content. This pass does not prove hostile-agent isolation or
all CLI failure paths. Review found that cancellation can remain `unknown` if
AWS stops after the CLI's immediate state check; defer recovery to later work.

The runtime role reads the OpenRouter secret and can terminate project VMs; it
has no S3 or AssumeRole access. The CLI supplies a separate one-hour STS session
restricted to this run's S3 prefix. The cloud test verified normal access, not
cross-run denial. The trusted CLI reuses the restricted provisioner role.
The image script owns image builds; Terraform owns the remaining infrastructure.

Cloud test run `97bb7c8b-867e-4a05-8ccc-62394174756e` ended with no result record.
AWS reported a run-hook HTTP 400 and terminated the VM. The listener assumed an
`mvm-` ID prefix; AWS returned `microvm-...`. The listener now accepts opaque
IDs. The same cloud test passed after selecting corrected image version `2.0`.

Image creation returned `AccessDeniedException`. Read-only checks found two
MicroVM IAM constraints. A saved bootstrap plan updates only the provisioner
policy: remove `iam:PassedToService` from the two exact role grants, and use
region-limited `Resource: "*"` for `lambda:PassNetworkConnector`, which has no
resource-level scope. The user approved this one-policy update and it was applied.
Worker policies are unchanged. See the AWS sample and permission reference
in Sources. The CLI still explicitly uses `NO_INGRESS`.

A later build denial identified `lambda:TagResource` on `*` during image
creation. A second saved bootstrap plan adds only that initial-tag grant, limited
to `us-east-1` and the exact Project/ManagedBy tags. It can tag other Lambda
resources with those values; worker access is unchanged. The user approved this
change with "proceed" and it was applied. Image creation then succeeded. Version
`1.0` was explicitly selected before the first cloud test.

Design review checkpoint: Q1-Q42 are answered. V1 design choices are settled;
technical checks remain. Implementation is now approved; deployment review remains.
Q19 requires explicit image-version selection. Q20 now defers supervisor/agent
isolation and log-tamper protection; keep the shared-VM implementation simple.
Stream logs during execution, not only
at shutdown. Q21 uses a 128,000-character prompt cap and a 1 MiB result limit;
no tokenizer integration in v1. These answers replace the earlier Q20 protection
requirements and Q21 token-counting proposal.
Q22 and Q24-Q27 specify own-run S3 access, concurrent independent runs,
explicit unknown status, an agent-friendly CLI, and automatic OpenRouter provider
selection. Q23 accepts a final JSON completion signal; the supervisor owns all
bookkeeping. Q28 accepts image-scoped termination permission and its cross-run
risk for v1. Q29 defers named locks. Concurrency supersedes the single-active-run
assumption. Q30 selects run IDs only, with no separate task/session identity or
follow-up work in v1. Q31 selects one standard environment. Q32 selects one small
resource allocation with no per-run overrides; exact sizes need verification.
Q34 adds shared startup and teardown scripts, initially allowed to do nothing.
Q33 selects shared environment-variable defaults and supervisor-set runtime
values, with no user overrides. Q35 selects committed source only for future
code tasks. Q36 excludes input uploads. Q37 selects simple deliverables without
a separate code diff. Q38 permits success without code changes. Q39 requires
supplied validation to pass when that later feature is supported. Q40 selects
Lambda MicroVMs. Q41 selects account `618170664907`, with configurable deployment
inputs and project resource tracking. Q42 selects `us-east-1`.
Record agreed answers here as the discussion proceeds; distinguish them from
proposals. These records do not approve implementation or AWS deployment.

## Original goal

Build a simple, understandable, extensible AWS foundation for personal agent
experiments. Submit instructions, code, datasets, or images. Let agents work
unattended, potentially for hours. Retrieve durable results. Isolate execution,
control access, and stop compute when work ends. The local computer can be off.

AWS is the starting cloud because it is familiar to the user. Use current
industry research to guide the design and Terraform to reproduce infrastructure.
Keep agent tools and execution backends replaceable. The choices below are
derived from this goal; they are not the goal itself.

Example tasks:

- Change or refactor code; return a patch and test results.
- Review a pull request; return a report.
- Process a dataset or image; return output files.
- Research topics; return a document or SQLite database.
- Perform a coding task on a github repo, submit changes in a PR

## Decisions and preferences

| Item | Current position |
| --- | --- |
| CLI and supervisor | Python |
| Terraform state | Local; exclude from Git and worker access |
| Agent harness | Pi is the user's preferred default, not a fixed dependency |
| Model provider | OpenRouter |
| Sandbox model | GLM 5.3 (`z-ai/glm-5.3`); allow explicit per-run overrides |
| Deployment | Account `618170664907`, region `us-east-1`; configurable, not hard-coded in implementation |
| Compute | Lambda MicroVMs |
| Bootstrap profile | `AdministratorAccess-618170664907`; temporary SSO, approved bootstrap only |
| First task | Write a poem to a new, non-empty `result.txt`; download it after completion |
| Initial use | One user, concurrent independent runs; no sensitive data needed for the poem test |
| User interface | Agent-friendly local CLI with JSON output, listing, filtering, logs, status, download, submit, and cancel |
| Run identity | New unique ID for each intentional submission; reuse identity only for retries of that launch |
| Task/session identity | No separate task/session IDs or task versions in v1; save the task specification with each run |
| S3 isolation | Worker can access only the files authorized for its own run |
| Outcome bookkeeping | Agent returns completed/blocked JSON; supervisor owns validation, records, metadata logs, and cleanup |
| Shutdown permission | Direct termination of the current VM; image-scoped permission and cross-run risk accepted for v1 |
| Named locks | Deferred until shared-resource tasks |
| Unknown outcome | Report missing evidence as unknown; do not block unrelated submissions |
| Provider routing | OpenRouter selects providers for the chosen model; no automatic model substitution |
| Submission | Prompt or stdin plus flags, or a JSON job file; one validated run schema |
| Input uploads | Not supported in v1; no file, directory, or repository uploads |
| Deliverables | `output/result.txt` in v1; no separate code-diff collection |
| Code changes | Not required for success; questions and exploratory tasks are valid |
| Invalid input | Fail before submission; do not start work with invalid settings |
| AWS configuration | Read named Terraform outputs in memory; require initialized local state; no deployment cache |
| Deployment inputs | Terraform-owned `infra/cloudbox.auto.tfvars.json`; no separate CLI input copy |
| Image updates | Explicitly select the version; building a new image does not change later runs |
| Environment | One standard configuration for all runs; no environment selector |
| Environment variables | Shared defaults and supervisor-set runtime values; no user overrides in v1 |
| Resources | One small CPU, memory, and disk allocation for all runs; no per-run overrides; exact sizes remain open |
| Lifecycle scripts | Shared startup and teardown scripts run by the supervisor; default no-op; per-run scripts deferred |
| Run deadline | Ten minutes by default; configurable per run |
| Cleanup allowance | Final thirty seconds within the run deadline; no guaranteed final upload |
| Agent control | Simple supervisor and Pi in one VM; internal isolation and log-tamper protection deferred |
| Text limits | Prompt: 128,000 characters; result file: 1 MiB; no tokenizer integration |
| Blocked task | Fail with a reason, then terminate; no waiting for user input |
| Retention | Thirty days for run data and logs; no S3 version history |
| Partial output | Download if saved; mark incomplete and retain failure status; expire normally |
| Logging | Stream Pi and supervisor metadata to CloudWatch during execution; no full transcripts in v1 |
| Runtime Internet access | Open outbound access in v1; restrictions deferred |
| Customer VPC | None in v1; managed networking with `NO_INGRESS` |
| Threat model | Treat the agent and its tool code as potentially hostile |
| Credential risk | User accepts runtime credential exposure; still restrict permissions |
| Spending controls | User configures the OpenRouter key limit and AWS billing alert |

Keep the provisioner role, secrets handling, and per-run S3 checks. The external
review's broad IAM simplifications were not adopted. Q20 separately defers
in-VM agent/supervisor protection; it does not remove existing AWS IAM limits
or the independent AWS run deadline.

The user prioritizes credential protection for this prototype; no broader
customer-data policy was selected. Avoid sending credentials as model content.
Credentialed tools may use them locally, but this is not a guarantee against a
hostile agent disclosing them.

## Proposed first version

First prove an agent can write a text file, save it outside the worker, and stop.
Code changes, PRs, datasets, images, research, and other output formats come later.
Selected architecture:

```text
Local cloudbox CLI
  |
  +--> S3: task specification (file inputs come later)
  |
  +--> RunMicrovm
         |
         v
      One MicroVM per run
        supervisor -> Pi -> OpenRouter
        (outbound Internet access is open in v1)
         |
         +--> S3: outputs and result
         +--> CloudWatch: logs
         +--> TerminateMicrovm: current VM ID

AWS maximum run duration stops a failed or stuck worker.
The local CLI does not need to remain online. Runs do not block other runs.
```

Lambda MicroVMs are not standard Lambda functions. Documented limits include
ARM64 only and an eight-hour lifetime, including suspended time. Verify before
implementation.

Batch or Fargate is a later alternative for incompatible tools or longer jobs.
Neither is part of the first version.

## AWS resources

| Resource | Purpose |
| --- | --- |
| One MicroVM image | Pinned hook listener, supervisor, Pi, and dependencies |
| One private S3 bucket | Image archive, run specifications, outputs, and result files |
| IAM roles and policies | Separate deployment, build, and runtime access |
| CloudWatch log groups | Build and runtime logs with thirty-day retention |
| One Secrets Manager secret | OpenRouter API key, fetched at runtime |

Do not add DynamoDB, SQS, Step Functions, API Gateway, EventBridge, a private ECR
repository, or customer-managed KMS key yet. Do not add a VPC, private endpoints,
firewall, or proxy solely for outbound restrictions in v1. Reassess these when
network restrictions return to scope.

Q41 clarification: keep AWS-managed networking with `NO_INGRESS` and default
public outbound access. No customer VPC, NAT gateway, load balancer, or web
hosting is needed for the current task. OpenRouter and public AWS API endpoints
use outbound access. Consider a VPC only when private resources or network
controls become requirements; a VPC template does not remove those extra parts.

Q28 accepts image-scoped worker termination permission for v1, including the
risk that a compromised worker can stop sibling runs. Use direct self-termination;
do not add a separate stop controller. Own-run S3 access remains required.

Use S3 JSON files for run records. Keep local Terraform state outside worker
access.

### Resource ownership and tracking

Use project-scoped Terraform configuration and state for persistent infrastructure.
Derive names from the configured project and add a common project tag where
supported. Tags and state identify resources; they do not enforce security
isolation. IAM policies and the MicroVM boundary provide access and execution
controls, subject to the accepted v1 risks. A VPC would not contain IAM, S3, or
Secrets Manager resources as a project folder.

Track temporary MicroVMs and run objects through Cloudbox run records, not one
Terraform resource per task. Keep the image script fallback if native Terraform
support is absent; document its ownership and cleanup explicitly. Do not claim
that Terraform state includes script-created images or individual runs. No
separate AWS resource-group service is needed for v1.

## AWS access and deployment

Q15 requirement: configure the default deployment once, then use it without a
repeated `--config` argument. Q18 selects `infra/cloudbox.auto.tfvars.json` for
non-secret inputs that belong to Terraform. The operator supplies choices such
as region once; Terraform derives resource IDs. Do not hand-copy generated IDs
back into this input file or keep a separate CLI copy. Q15 accepts direct in-memory
reads of named Terraform outputs for each submission, with Terraform and its
initialized local state required on the submitting computer. No generated
deployment file or disk cache. Keep secret values out of deployment inputs
and outputs. Q19 requires explicit image-version selection; do not use an
automatically changing latest version.

Keep the real `infra/cloudbox.auto.tfvars.json` out of Git. Check in
`infra/cloudbox.auto.tfvars.example.json` with placeholders and documented fields;
its `.example.json` suffix prevents automatic loading. The local file holds the
expected account, region, SSO profile, project name, selected image version, and
shared resource settings. Do not duplicate these values in CLI configuration or
hard-code the selected account in implementation. Per-run prompts, model
overrides, and deadlines remain in the existing run contract.

Set the Terraform AWS provider's region explicitly and use
`allowed_account_ids = [var.aws_account_id]`. The guard rejects credentials for
another account; it does not select credentials or log in. The CLI must also
check the expected account before writes. Derive both checks from the same
deployment input rather than duplicate account constants.

Use one deployment and local state initially. A future account or region change
creates a separate deployment with separate state; it is not a migration caused
by editing the account value against the old state. Preserve the old deployment's
configuration and state until its resources are deliberately retained or removed.

Use temporary credentials through an AWS CLI SSO profile. Do not create an IAM
user or a long-lived access key for this project. Do not paste credentials into
chat or commit them.

Read-only check on 2026-09-02: AWS CLI 2.36.13 is installed and recognizes
`RunMicrovm`. Profiles `default` and `AdministratorAccess-618170664907` both
configure account `618170664907`, role `AdministratorAccess`, and `us-west-1`.
Both STS checks failed because the SSO token expired. These are configured
identities, not verified current access. Q41 subsequently selected the named
administrator profile for approved bootstrap; Q42 selected `us-east-1` instead
of the profile's configured region.
Terraform was installed at an earlier check. Recheck tools and identity before
cloud work; no login or AWS write was performed during this review.

Local tool update on 2026-09-02: upgraded AWS CLI from 2.36.13 to 2.36.37
through Homebrew. Verified `/opt/homebrew/bin/aws` with `aws --version`;
Homebrew reports the current release. No AWS login or deployment occurred.

```sh
aws configure list-profiles
aws sso login --profile AdministratorAccess-618170664907
aws sts get-caller-identity --profile AdministratorAccess-618170664907
```

Q41 selects account `618170664907`. The user reports that it is empty; no resource
inventory has verified this. Q40 selects Lambda MicroVMs and Q42 selects
`us-east-1`. Set the project region explicitly; do not change the global AWS
profile region. Recheck authenticated identity before approved cloud writes.

Use administrator access only for an approved bootstrap. Create a restricted
`cloudbox-provisioner` role, then use it for Terraform. Scope access to project
resources. Restrict `iam:PassRole` to the exact build and runtime role ARNs. The
MicroVM service does not supply usable `iam:PassedToService` context; the approved
spike correction removes that condition. Role trust still names Lambda. Review
a permissions boundary: a name prefix alone does not prevent
IAM privilege escalation. The provisioner must not change its own access or
the boundary that limits it.

Use native Terraform support for MicroVM images if available. Otherwise use
Terraform for the bucket, roles, and logs, and a small AWS CLI script for image
creation, updates, and cleanup. Do not add Cloud Control only to manage an image.
Assign one owner to each resource; delete script-owned images before their
Terraform-owned dependencies during approved teardown.

Show the Terraform plan and obtain approval before AWS writes. Do not change
unrelated resources. Confirm the account and region before every apply.

## IAM boundaries

| Identity | Allowed access |
| --- | --- |
| Human operator | Submit, inspect, download, cancel; pass approved roles |
| Provisioner | Manage only approved Cloudbox infrastructure |
| Build role | Read image archive; write build logs |
| Runtime role | Read approved inputs; write outputs and logs; use selected model access |

The build and runtime roles use the Lambda service trust policy. AWS currently
requires `sts:AssumeRole` and `sts:TagSession` in that policy.

Workers must not change IAM, create compute resources, delete S3 data, read
unrelated secrets, or access production resources in v1. Future tasks may need
more account access. Approve that access per use case; accepting credential
exposure is not approval for unrestricted permissions.

Two limits need explicit treatment:

- Q22 requires own-run S3 access in v1, including the poem test. A shared role
  with access to every run does not meet this requirement. Select and test
  exact-object signed URLs or run-specific credentials. Deny access to other
  runs and image archives; permit only required reads/writes within the run.
- AWS currently scopes `TerminateMicrovm` access to the image, not one running
  VM. Calling it with the current VM ID works for normal self-termination, but
  the permission can also stop sibling VMs. Q28 explicitly accepts this risk
  for concurrent v1 runs. Scope the grant to the project image, not all images.
  Do not describe it as an IAM-enforced own-VM-only permission.

Do not infer either boundary from Firecracker isolation.

### Supervisor and agent boundary

Q20 revised decision: use a simple Python supervisor that starts Pi as a child
process in the same VM. The supervisor is ordinary code, not another model.
Pi calls the model and executes tools. Use normal process management for startup,
timeouts, and cleanup, not a separate security subsystem.

Defer separate-user isolation, protected control endpoints, resource isolation,
supervisor-only credential access, and log-tamper protection to a later security
pass. These are not v1 acceptance gates. Q28 selects direct self-termination;
do not add another controller service in v1.
This supersedes the earlier requirement to prove those controls before Pi runs.

Agent code can interfere with the supervisor, its files, runtime credentials,
or log collection. Logs are diagnostic, not an audit guarantee. The user accepts
this risk for the first test. Keep existing AWS permission limits; deferral is
not a reason to add administrator or log-deletion permissions.

AWS enforces the maximum duration outside the VM. Keep this independent stop
even if the supervisor fails. The thirty-second cleanup allowance and final
uploads remain best effort; streaming logs does not guarantee final output.

## Model and secrets

Prefer Pi with OpenRouter and GLM 5.3 for sandbox agents. This default applies to
sandbox workers, not the assistant developing this project. Allow per-run model
overrides. Keep the harness and provider replaceable; do not add their public
configuration options before supporting another implementation. Verify the
model ID and pin the Pi release before integration.
Do not substitute Claude Code or Bedrock as defaults.

Q27 accepts OpenRouter's automatic provider selection for the chosen model,
including provider fallback for that same model. Use existing account privacy
settings; do not add provider-selection options in v1. Do not configure automatic
fallback to another model. Fail if no eligible provider serves the chosen model.
Default provider routing does not guarantee zero retention. Reassess data policy
before sensitive tasks; account privacy settings have not been inspected.

For the OpenRouter key:

- Store the key in Secrets Manager and fetch it after startup.
- Resolve the secret reference from deployment configuration, not user job
  input. Internal worker configuration may carry the reference, never its value.
- Set the secret value outside Terraform so it does not enter Terraform state.
- Trusted code must not put secret values in images, build snapshots, stored
  configuration, model messages, logs, output files, or Git.
- If the CLI requires an environment variable, set it only for that runtime
  process. Treat it as readable by code inside the VM.

The OpenRouter key is sent to OpenRouter for authentication, not as model
content. Tool results can still expose credentials to upstream model providers.
Secrets Manager cannot protect a key after the worker reads it. Pi tools use
the Pi process permissions; its default bash tool receives its environment.
Removing bash or setting a working directory does not establish a secret or
filesystem boundary. Treat runtime AWS permissions as available to hostile code
in v1. Q20 explicitly defers isolation between Pi and the supervisor.

The user accepts this risk in v1. Use a dedicated, spending-limited OpenRouter
key and narrowly scoped AWS runtime access. Do not supply operator or production
credentials for the poem test. Open outbound access permits disclosure of
readable credentials. Instructions and log redaction cannot guarantee prevention.
An external credential service remains a later option, not a v1 requirement.

Future code tasks use committed source only. The user expects code work to live
in GitHub; do not build a separate code-diff collector. Repository uploads are
not part of v1. GitHub access and writes need a separate decision; this direction
does not authorize credentials, pushes, merges, or review posts.

## Network and image rules

- Use `NO_INGRESS`; no public shell or application endpoint is needed.
- Allow outbound Internet access in v1, including OpenRouter and required AWS
  APIs. The user explicitly deferred the earlier OpenRouter-only restriction.
- Set per-run data and credentials after snapshot restore, never during build.
- Pin the image version, agent version, and dependencies for each run.
- Use the explicitly selected image version. A new build does not select itself.
- Check ARM64 compatibility for all required binaries.

Deferred network research: MicroVM default egress is unrestricted. Future
OpenRouter-only enforcement needs controls outside the worker, with separate
paths for required AWS access. Domain filtering alone is not a complete boundary
against hostile clients. Assess a trusted relay or VPC controls and their idle
costs before selection. Test direct IP, alternate ports, IPv6, DNS, and redirects.

Allow required build-time downloads separately. Preinstall runtime dependencies.
Disable Pi startup updates, telemetry, and unapproved package or extension
loading. Verify the pinned release's controls with a network test.

Open egress does not block web research or package downloads. Those task types
remain outside the first deliverable. Preinstallation still makes runs repeatable.

Disable automatic idle suspension for autonomous background jobs. AWS detects
idle state from inbound endpoint traffic, not from CPU work. A busy agent can
otherwise appear idle.

### Standard environment and resources

Q31: use one standard environment for all runs in v1. Each run still has its own
VM and workspace. No named-environment selection is required at submission.
Keep common configuration in the image and deployment settings.

Q32: use the same CPU, memory, and disk allocation for all runs. Choose a small
supported allocation that passes image and worker checks. Verify exact sizes
after selecting the backend; do not guess service limits. Keep shared sizing in
deployment configuration and record resolved values with each run. Users cannot
override sizing at submission. The operator can change the shared configuration
for later runs. This does not change the accepted per-run deadline setting.

Q33: set non-secret environment-variable defaults in the image or supervisor.
The supervisor may set run-specific values, such as workspace paths. Do not
accept user-supplied variable maps or overrides through submission in v1.
Do not copy the submitter's shell environment into the worker. Keep credential
values in the existing runtime secrets mechanism, outside saved configuration.

## Run contract

### Submission interface

Accepted Q11 contract: prompt and common flags for direct use; optional JSON
input for repeatable jobs. Both routes create the same validated run specification.
These commands describe the planned interface; no CLI exists yet.

```sh
cloudbox submit "Write a poem and save it to result.txt."
cloudbox submit - --timeout 10m < task.md
cloudbox submit --spec job.json
cloudbox submit --spec job.json --timeout 20m
```

Example `job.json`:

```json
{
  "schema_version": 1,
  "prompt": "Write a poem and save it to result.txt.",
  "model": "z-ai/glm-5.3",
  "timeout_seconds": 600
}
```

- Only `prompt` is required. Omitted settings use the selected defaults.
- Apply defaults, then JSON file values, then explicit CLI overrides.
- `--model` overrides the model. `--timeout` sets the run deadline.
- `-` reads prompt text from stdin. `--spec` reads the JSON job file.
- These are submission inputs, not file attachments. Do not support file,
  directory, or repository uploads in v1.
- Both routes use one validator. Reject unknown fields, unsupported schema
  versions, invalid limits, and competing prompt sources before AWS submission.
  Fail as early as possible; do not launch work or guess corrections.
- `--json` controls CLI output for scripts, not task input or model output.
- Save resolved settings and pinned image/agent versions before launch. Do not
  change the submitted specification during a run.
- Keep AWS account, region, IAM, secret references, image selection, and shared
  resource sizing in deployment configuration, outside the user job file.
  Terraform owns its inputs.
- No per-run environment selector, resource overrides, user-supplied environment
  variables, or custom lifecycle scripts in v1.
- Keep the required output fixed at `result.txt` in v1. Add inputs, skills, and
  other output types when supported. Do not pass arbitrary Pi flags through.

Q21 accepted: reject a submitted prompt longer than 128,000 characters before
AWS submission. Count decoded text with Python string length (Unicode code
points), not UTF-8 bytes or JSON escape characters. Use the same check for flags,
stdin, and JSON input. Do not truncate. The threshold is deliberately simple,
not an equivalent of 128,000 tokens or a promise that a model will accept it.

Limit `result.txt` to 1 MiB (1,048,576 bytes) in v1. Fail rather than truncate an
oversized result. This file limit does not cap agent disk use or log volume.

Do not integrate tokenizers or add custom context-budget accounting in v1.
Provider limits still apply to the full request, including instructions, tools,
history, and reply allowance. A terminal context-limit rejection is a failed run;
do not silently switch models. Exact counting and model-specific validation are
deferred. This replaces the 64 KiB and 128,000-token prompt proposals.

Evidence checked on 2026-09-02: Codex and Claude cloud CLIs use task text plus
execution context. Their local CLIs distinguish configuration from JSON output.
AWS Batch accepts JSON submission input with flag overrides. This supports the
hybrid pattern; Cloudbox's schema is our design, not a shared vendor standard.
The sources below describe public interfaces, not private cloud implementations.

### Stored records and results

Q30: automatically generate run IDs. Do not add separate Cloudbox task/session
IDs, task versions, or follow-up commands in v1. The task is the prompt and
execution inputs saved in the run specification, not a separate stored entity.

Q24: give each intentional submission a new unique run ID, including repeated
use of the same prompt or job file. Use that ID for its records and artifact
paths, with one independent VM/workspace and log stream per run. Do not identify
runs by prompt content or a shared output filename outside the run prefix.
Retries of the same launch reuse its run ID and idempotency token; a deliberately
new run gets new values. Concurrency is normal, not a duplicate-submission error.

Prompt matching can help find repeated wording. It does not prove equivalent
work: model and environment settings can differ, as can source revisions and
file inputs when supported. Different prompts can also describe the same work.
Even matching specifications do not guarantee matching results. Do not merge
runs or reuse their IDs because prompts match.

V1 bucket layout; no task input uploads:

```text
images/<image-version>/source.zip
runs/<run-id>/spec.json
runs/<run-id>/launch.json
runs/<run-id>/output/result.txt
runs/<run-id>/result.json
```

`spec.json` records the resolved task, schema version, image and agent versions,
provider, model, CPU/memory/disk allocation, output requirement, and maximum
duration. Add input references when uploads are supported. It contains no secret
values.

`launch.json` records the MicroVM ID, pinned image version, and CloudWatch log
location. The CLI needs this mapping for status, logs, and cancellation.

`result.json` records the final status, timestamps, exit code, summary, artifact
paths, and usage when available. Use explicit success, failure, cancellation,
and timeout states. A blocked agent produces failure with a reason and ends;
there is no `needs_input` state or waiting session in v1. The user can change
future tasks. Missing results after termination mean interruption or an unknown
outcome, never success.

Q25: a lost launch response means AWS may have accepted the request before the
CLI lost its connection or stopped. The task can be running, finished, or failed.
Show missing outcome evidence as `unknown` in status/list, with known compute
state shown separately. Preserve the run identity and reconcile available records
on later reads. Do not automatically launch a replacement. An unknown run does
not block unrelated submissions; operator-requested new runs remain possible.

### Completion ownership: Q23

Accepted responsibility: the supervisor, not the model, manages all bookkeeping.
It observes Pi's process and events, enforces the deadline, validates output,
uploads artifacts, writes the run result, streams metadata, and initiates cleanup.
The agent does not write AWS status records, collect usage, or manage run IDs.

Q23 accepted: the agent's final reply is only
`{"status":"completed"}` or `{"status":"blocked","reason":"..."}`. The
supervisor reads that reply from Pi's native event stream. It maps blocked work
to failure and still checks exit status, terminal errors, file validity, and
upload confirmation before recording success. Missing or malformed completion
data fails. This adds no model logging tool, extra status file, or model call.

Pi's process exit describes harness execution, not task meaning. In JSON mode,
even a terminal model error can exit zero; a clarification can end normally.
Running `exit 1` in Pi's shell tool stops that shell, not the Pi harness. A wrapper
could translate the final declaration to an exit code, but it still needs that
declaration. The declaration is not proof of task quality or agent honesty.

For the first task, success requires a new, non-empty `result.txt`, successful
agent completion, and confirmed upload. Check both exit code and terminal events;
Pi JSON mode can exit zero after a model error. Do not grade the poem's quality.
Keep diagnostic logs separate. Questions and exploratory tasks can succeed
without code changes; the normal completion and output checks still apply.
Later deliverables may include reports, datasets, and SQLite. Do not separately
collect code diffs.

Q16: saved partial output remains downloadable after failure or timeout, clearly
marked incomplete. Do not delete results just because the task failed. Let the
agreed retention policy expire them. Forced termination may prevent an upload;
recovery is not guaranteed. This does not change the compute-termination rule.

### Retention

Use private S3 access, encryption, public-access blocking, and no version history.
Apply one thirty-day lifecycle rule to `runs/`, excluding `images/`. Age is per
object from upload, not from run completion. S3 deletion is asynchronous, not an
exact-time TTL. Without version history, overwritten files cannot be recovered
through S3 versions. Set CloudWatch log-group retention to thirty days too.

No per-run retention setting in v1. S3 has no arbitrary deletion TTL field on
each upload. Later, object tags can select predefined retention classes such as
seven, thirty, or ninety days. Replace the broad rule before adding longer classes.
CloudWatch retention is per log group, not per run. Downloaded copies are unaffected.

## Logging

Q17 accepted: use Pi's native JSON event stream and a small supervisor adapter,
not a new logging tool, custom Pi extension, or separate observability service.
Emit one JSON log record per selected event to CloudWatch; retain thirty days.
The model does not need to remember to log.

- Include run ID, timestamp, event type, tool call ID/name, observed duration,
  outcome, model, stop reason, reported token usage, and cost estimates when present.
- Add supervisor events for launch, lifecycle-script start/end/failure, deadline,
  exit, validation, and upload.
- Use an explicit field allowlist. Omit full prompts, model text, tool arguments,
  raw results, token deltas, and raw stderr by default. Classify errors instead
  of blindly forwarding free-form error strings. Filtering is not a security boundary.
- Disable Pi session-file persistence for this mode; its default session JSONL
  includes full conversation content. Defer optional full transcripts/debug mode.
- Count usage once per completed assistant message, not on every stream update
  or repeated end event. Treat model cost as an estimate, not a provider invoice.
- Stream during execution, flush each selected JSON line, and drain child output
  continuously. Also flush remaining application buffers before normal shutdown;
  do not defer all logging until the final S3 upload. Abrupt termination can lose
  final events. Logs from a hostile worker are not a tamper-proof audit.

Capture Pi stdout and stderr in the supervisor. Forward only selected metadata
to application stdout for managed CloudWatch delivery, not raw child output.
MicroVM forwarding requires an execution role with CloudWatch Logs permissions.
The CLI can follow that stream while the VM is running. Delivery is near-real-time,
not an exact-latency guarantee. No final S3 upload is needed to make logs visible.
Flushing stdout does not confirm CloudWatch storage. Keep the S3 result record
and AWS compute state separate; do not infer completion from the last log line.
Log-tamper protection is deferred under Q20; retain Q17's metadata-only defaults.

Pin and test the Pi event contract. Current JSON mode can exit zero after a model
error, and an `agent_end` can precede a retry. Check terminal state after retries
as well as exit code and output validity. A recovered tool error alone is not a
failed task. Verify these cases before integrating the chosen Pi release.

Research checked on 2026-09-02. OpenTelemetry and Claude Code describe automatic
model/tool timing, outcomes, and usage, with message content separately enabled.
Langfuse instruments runtime operations; it does not require model logging calls.

## Four components to build

### Infrastructure

Terraform defines the bucket, build and runtime roles, provisioner controls,
log groups, and secret metadata. The secret value stays outside Terraform.
Do not add outbound network enforcement or billing-alert resources in v1.

### Image and build script

An image is the reusable, versioned worker environment, not a generated picture
or a model's weights. It contains the listener, supervisor, Pi, language runtimes,
and tools. AWS builds and snapshots that environment; each task starts a fresh
VM from the selected snapshot. Supply the task and secrets after startup.

Build it initially and when installed software, supervisor code, or OS patches
change. A different poem, prompt, or remotely served model does not by itself
require a rebuild. Q19 requires explicit selection before future tasks use a new
image version. Building an image and submitting a task are separate operations.

Package a Dockerfile, Python hook listener and worker, pinned Node.js and Pi,
certificates, shared startup/teardown scripts, and required task tools. For the
MicroVM backend, use the AWS managed base image and a compatible container base.

The script uploads the ZIP to S3 and calls `CreateMicrovmImage` or the update
API. AWS runs the Dockerfile, starts the listener, waits for `/ready`, and
captures a snapshot. The script waits for `CREATED` and reports the image ARN
and version. Resolve script-owned image selection without a local deployment
cache; use the explicitly selected version. Include update and cleanup commands.
No agent task or runtime secret may enter the build snapshot.

### Python worker and supervisor

Use one worker with local-file and AWS adapters. It loads the specification,
runs lifecycle scripts and the agent child process, filters events for CloudWatch,
checks outputs, saves results, and requests termination.
The hook listener starts the worker and remains available during execution.

Local mode accepts a specification file and workspace directory. Test a plain
command before Pi. Also test the image in Docker. Cloud tests use runtime-scoped
credentials, never mounted administrator credentials. Docker does not prove AWS
isolation or lifecycle behavior.

#### Shared lifecycle scripts

Q34: provide startup and teardown script slots called by the supervisor. Both
default to successful no-ops. Keep them in the pinned image so all runs use the
same versions. Bake fixed dependencies into the image; use startup for work that
needs a running VM, such as checking tools and workspace write access.

- Run startup after loading the run specification and runtime credentials, before
  starting the agent. A startup failure or timeout fails the run; do not start Pi.
- Attempt teardown when the supervisor can clean up, including after startup or
  agent failure. Stop the agent and save available outputs before teardown.
  Teardown can stop temporary services or remove disposable local files.
- Keep output validation, uploads, result records, logs, and VM termination in
  the supervisor. Teardown failure is a cleanup error; preserve the task outcome
  and still attempt result recording and termination.
- Limit both scripts to the existing deadline. Startup consumes the work budget;
  teardown shares the thirty-second cleanup allowance with uploads and shutdown.
  Stop a hanging script rather than extend the AWS deadline. A forced VM stop or
  supervisor failure can prevent teardown entirely.
- Stream selected script metadata through supervisor logging. Apply the existing
  content filters; do not forward raw script output by default.

Defer per-run script paths, commands, and script uploads. These scripts prepare
and clean up a worker; they do not deploy or destroy shared AWS infrastructure.

### Python CLI

Q26: design the CLI for use by another agent as well as a person. Reuse one
validated run schema. Support structured JSON output, stable error/exit behavior,
noninteractive operation, pagination, and status filters without a separate UI.
Keep machine-readable stdout separate from diagnostics. CLI operation status
and remote task outcome must remain distinct. Exact flags and exit codes are
implementation details to document and test, not new services.

| Command | Behavior |
| --- | --- |
| `submit` | Validate and save a new run; start its VM; return its unique ID without blocking other runs |
| `list` | List past and current runs with IDs, times, recorded outcomes, and result references; support filtering and pagination |
| `status` | Read the result and VM state; show task outcome and compute state separately |
| `logs` | Follow the run's CloudWatch stream during execution or read saved events after termination |
| `download` | Download saved results, including partial output marked incomplete after failure or timeout |
| `cancel` | Request VM termination and check its state; do not overwrite a completed result |

Missing evidence remains an unknown outcome. A successful task with a running
VM is incomplete cleanup, not a fully completed run.
No web UI or completion notifications in v1. Do not require interactive prompts
for ordinary run operations; deployment approval remains a separate requirement.

## Worker lifecycle

1. The CLI resolves settings, creates a run ID, and uploads the specification.
   Input uploads are a later extension.
2. The CLI starts a pinned image with a stable `clientToken`, run reference,
   and hard deadline. It saves the returned MicroVM ID in `launch.json`.
3. The `/run` hook starts a supervisor and returns; it does not block for the
   full task duration.
4. The supervisor loads the task and runtime credentials, then runs startup.
   Stream selected script, Pi, and supervisor metadata throughout execution.
   Fetch file inputs before startup when that feature is supported.
5. After successful startup, the supervisor starts the agent and enforces the
   local deadline. A blocked agent fails and stops.
6. The supervisor stops the agent, uploads available outputs, attempts teardown,
   writes the final result, and flushes logs within the remaining deadline.
7. The supervisor calls `TerminateMicrovm` with its current VM ID. Q28 accepts
   the wider image-scoped permission; normal code must target only its own VM.

Pass the run ID and S3 specification reference as JSON encoded in
`RunMicrovm.runHookPayload`. AWS supplies that string and `microvmId` to
`POST /aws/lambda-microvms/runtime/v1/run`. The listener parses the payload,
starts the worker, and returns without waiting for task completion.

Set AWS `maximumDurationInSeconds` for every run. It is the independent backstop
if the agent, supervisor, or termination request fails. Do not rely only on an
agent deciding that it is done.

Default to a ten-minute run limit, configurable per run. Q20 reserves the final
thirty seconds for saving output and termination. Stop agent work by 9:30 for
the default limit, or earlier on completion. Startup time is inside the run
budget; do not start a new full timer when Pi begins. Reject limits that leave
no agent work time. Never extend the AWS deadline for cleanup. This allowance
does not guarantee that uploads finish or that a failed supervisor can run.

Termination hooks and final uploads are best effort. Save partial output during
long jobs where useful. Check AWS state when the final result is missing.

Do not automatically retry whole agent tasks. Retries can repeat external
actions or model charges. Limit initial automatic retries to safe API calls.
Use launch idempotency and a stable run ID to prevent duplicate submissions.
This applies to retries of one submission, not deliberate new submissions of the
same task. Do not impose a global active-run or unknown-run admission lock.
The user configures the OpenRouter key spending limit and the AWS billing alert
outside this project, with an intended email alert above about USD 10 per month.
No alert has been verified or created here. Billing alerts are not hard spending
limits and do not replace the run deadline.

### Future shared-resource locks

The user raised optional named locks for tasks that touch a shared resource,
such as one database migration target. Unrelated runs must still proceed. If a
named lock is held, a competing run should fail or cancel rather than wait.
Q29 explicitly defers this work; do not implement named locks in v1.

A lock prevents concurrent access, not repeated execution after release. Once-only
side effects also require a task-specific idempotency record or protection in the
target system. Do not claim that unique run IDs or launch tokens solve that.
Lease expiry, ownership, crash recovery, and stale-worker protection would need
design before implementation. No lock service or migration support is selected.

## Implementation sequence

### Milestone zero: cloud execution test

After deployment approval, build a small hook handler that boots, writes one
S3 file, and terminates. Use minimum resources under the existing IAM rules;
do not wait for the full CLI, supervisor, or agent. Internal security tests are
deferred under Q20. Test process-exit behavior, but retain explicit termination
and the hard deadline unless AWS proves otherwise.

### Milestone one: local worker

Define the accepted run schema and Python worker with local storage. Test success,
failure, blocked tasks, missing or empty output, and deadlines with plain commands,
then Docker. No AWS required.

### Milestone two: unattended cloud run

Connect AWS adapters and implement the CLI. Test detached work, durable results,
concurrent runs, retry identity, cancellation, termination, unknown outcomes,
listing/filtering, and equivalent flag/JSON input.

### Milestone three: Pi and OpenRouter

Pin Pi and verify the preferred GLM 5.3 model. Verify OpenRouter and required AWS
access with open outbound rules. Run the poem task and download `result.txt` after
the VM stops. Test failure, blocked work, and timeout. Document operation and cleanup.

Keep any future execution backend behind a small interface. Do not build a
general workflow framework, web UI, queue, or fleet manager in the first version.
Independent concurrent runs do not require those components.

## Acceptance checks

- A submitted task continues after the local CLI exits.
- Prompt/flags and JSON input use the same schema; explicit flags override file
  settings. The saved specification contains resolved settings and runtime versions.
- Invalid or conflicting submission input fails before AWS submission.
- All runs use the standard environment and shared resource allocation. Reject
  per-run environment selection, sizing overrides, user-supplied environment
  variables, and custom lifecycle scripts.
- Read deployment outputs into memory without creating a deployment cache.
  Missing or invalid outputs fail before submission; never apply or refresh
  infrastructure as part of submission.
- Keep deployment inputs and state out of Git; check in an example input file.
  Wrong-account credentials fail before writes. Use the configured project region
  without changing the user's global AWS profile. Track persistent resources and
  temporary runs through their defined owners; tags alone are not isolation.
- Outputs and logs remain available after the VM terminates.
- Selected Pi and supervisor events reach CloudWatch while the task is still
  running, without model logging calls or waiting for a final upload.
  Normal logs exclude prompts, raw tool content, stderr, and secret values.
- Saved partial output stays downloadable with its failure status until expiry.
- Success, failure, cancellation, and missing results are distinguishable.
- A blocked task fails with a reason and terminates without waiting for input.
- The supervisor consumes the final completed/blocked JSON declaration. Missing
  or invalid declarations fail; blocked work fails even if partial output exists.
  The agent does not write run-status records or perform AWS bookkeeping.
- Normal completion terminates compute; a stuck worker reaches the AWS deadline.
- Reserve thirty seconds within that deadline, including startup in the budget.
- Shared lifecycle scripts work as no-ops. Startup failure prevents agent launch;
  teardown failure or timeout does not skip result recording or termination.
  Scripts cannot extend the run deadline; forced termination can skip teardown.
- Internal supervisor protection and log-tamper tests are deferred. Test normal
  timeout handling and the AWS hard stop without claiming hostile-agent isolation.
- Accept prompts at the 128,000-character bound; reject longer prompts before
  submission. Count decoded characters consistently across all input routes.
- Reject a `result.txt` larger than 1 MiB; do not silently truncate it.
- Intentional concurrent submissions, including identical tasks, get distinct
  run IDs, records, files, and VM mappings. Cancelling one targets only that VM.
- Normal worker shutdown uses the current VM ID. Document the accepted ability
  of a compromised worker to terminate sibling runs under image-scoped IAM.
- Retries of one launch reuse its identity; verify service idempotency behavior.
  Do not silently retry an uncertain launch as a new run.
- Missing outcome evidence is listed as unknown without blocking other runs.
- Agents can use submit, list/filter, status, logs, download, and cancel without
  interactive input; JSON output distinguishes command errors from task failures.
- The worker cannot read unrelated S3 objects or secrets, delete data, or change IAM.
- The worker cannot obtain the provisioner credentials or Terraform state.
- Trusted code does not store secret values in the image, snapshot, job records,
  model messages, or Terraform state. Check normal logs and artifacts for leaks;
  do not claim this prevents deliberate disclosure by hostile code.
- Success requires a new, non-empty `result.txt`, successful agent completion,
  and confirmed upload. Check exit code and terminal events; an exit alone is
  not success. Test a terminal model error with a zero Pi exit code.
- Pi can reach OpenRouter and required AWS calls work. Outbound restrictions
  are explicitly deferred, not an acceptance gate.
- The same worker logic runs locally, in Docker, and in the cloud.
- Thirty-day retention applies to run objects and log groups, not image archives.
  S3 versioning is disabled; expiry is not an exact deletion-time guarantee.
- Cleanup affects only Cloudbox resources and preserves outputs unless deletion
  was explicitly approved. The agreed thirty-day expiration is approved policy.

## Remaining implementation checks

- Renew SSO and verify selected account access before approved cloud work.
- Pinned Pi release and model runtime settings; GLM 5.3 is the preferred default.
- Runtime key delivery details. Exposure risk and automatic provider routing are
  accepted; an external credential service is not required for v1.
- Per-run S3 permission mechanism.
- Native Terraform image support or the image-script path.
- Remaining schema bounds and exact values for the shared small CPU, memory,
  and disk allocation. Per-run sizing overrides are excluded. Prompt length,
  result size, explicit image selection, ten-minute default runtime, thirty-second
  cleanup allowance, and thirty-day retention are settled.

Deferred: outbound restrictions, per-run retention classes, broader task inputs
and tools, alternate harness/provider options, and user interaction
during a run. Also defer supervisor/agent security isolation, log-tamper protection,
advanced log-volume controls, named locks, and tokenizer/context-budget integration. Revisit
these after the poem test, not as hidden v1 requirements.
Additional environments, per-run resource sizing, user-supplied environment
variables, and custom lifecycle scripts are also deferred.
File/directory uploads, repository integration, and user-supplied validation
commands are later features. Q35-Q39 settle their direction, not their inclusion
in v1.

### Review record: Q22-Q27

- Q22 accepted: own-run S3 files only. Resolve and test the permission mechanism;
  no exemption for the poem test.
- Q23 accepted: final JSON reports completed or blocked, with a reason for blocked
  work. The supervisor owns all bookkeeping and validation. Missing or invalid
  completion data fails; no extra agent status file, logging tool, or API is needed.
- Q24 accepted: allow concurrent independent runs, including repeated tasks.
  Give each intentional submission a new identity. Reject the earlier global
  single-run gate. Optional named locks were raised for future shared-resource
  tasks; do not infer a global lock or claim once-only execution from a lock.
- Q25 accepted: show unknown status when outcome evidence is missing. A request
  might have reached AWS even if the reply did not reach the CLI. Keep unknown
  separate from running, success, and failure; do not block unrelated work.
- Q26 accepted: an agent-friendly CLI with submit, list/filter, status, logs,
  download, and cancel. Include results in listings, with structured output and
  no need for an agent to interpret a human-only display.
- Q27 accepted: let OpenRouter select providers for the chosen model. Keep the
  no-model-substitution rule. Account privacy settings were not inspected.

### Review record: Q28-Q29

- Q28 accepted: keep direct self-termination in v1. The user accepts that a
  compromised worker can stop sibling runs because the permission covers the
  image. No external stop controller. Retain the independent AWS deadline and
  required own-run S3 permissions; this acceptance does not broaden other access.
- Q29 accepted: defer named locks until tasks access shared systems.
  The poem test needs unique run storage, not resource locks.
  Later, a busy lock should reject its contender rather than queue it. Once-only
  side effects remain separate from mutual exclusion.

Deferred security option, not selected for v1: an S3 completion notification invokes
a small trusted Lambda. It reads a run-to-VM mapping that workers cannot alter
and terminates that VM, ignoring any worker-supplied target ID. Handle duplicate
notifications and completion before the mapping exists. This removes termination
permission from workers but adds a component. The hard deadline remains required.

Q40-Q42 settle compute, account, bootstrap profile, and region. No further v1
feature questions remain. Checks can expose a blocker; report it rather than
silently changing the accepted design.

Technical checks owned by the implementer, not questions for the user:

- Verify native Terraform image support; otherwise follow the agreed script path.
- Pin and test Pi, model availability, runtime key loading, and one small shared
  VM allocation that supports the image, supervisor, agent, and lifecycle scripts.
  Close Pi stdin after supplying a prompt; piped input waits for EOF.
- Resolve S3 credential lifetime and run-hook payload limits before choosing the
  per-run access mechanism. Do not put bearer credentials into stored job records.
- Verify launch idempotency and recovery. AWS documents `clientToken`, but not
  its retention window or an explicit replay-response guarantee. Save an explicit
  token before launch; the SDK can otherwise generate a new one. A matching VM
  image/start time is not proof that it belongs to a particular run.
- Evaluate worker-written launch evidence: the run hook receives the VM ID and
  payload. This can help after a lost CLI response, but not if the hook never runs.
- Define terminal-record ownership and test cancel/completion races. Preserve a
  completed result; never claim cancellation from the request alone.
- Use bounded API retries; do not retry whole agent tasks. Verify that retries
  do not extend the configured run deadline.
- Define download paths and refuse to overwrite local files by default. Check
  that outputs come from the specified workspace path before upload.
- Specify backup/recovery instructions for local Terraform state and approved
  teardown behavior. Keep retained results available unless deletion is approved.

### Ontology review: Q30-Q34

- Q30 accepted: use automatically generated run IDs only in v1. No separate
  task/session IDs, task versions, or follow-up work. Keep each run's specification,
  status, logs, and results. Prompt equality is not execution identity.
- Q31 accepted: one standard environment for all runs; no environment selector.
  Each run still receives a separate VM and workspace.
- Q32 accepted: one small shared CPU, memory, and disk allocation; no per-run
  overrides. Select exact supported sizes during implementation checks.
- Q33 accepted: shared variable defaults in the image or supervisor; no user
  overrides. The supervisor may set run-specific values, such as workspace paths.
  Secret delivery remains separate from saved non-secret configuration.
- Q34 accepted: shared startup and teardown script slots, called by the supervisor
  and versioned with the image. No-op defaults are sufficient initially. Use them
  for runtime preparation and cleanup; defer custom scripts for individual runs.

### Ontology review: Q35-Q39

- Q35 accepted: committed source only when code tasks are supported; exclude
  uncommitted local changes. This does not add repository uploads to v1.
- Q36 revised and accepted: no file or directory uploads yet. Prompt stdin and
  `--spec` remain supported submission routes, not attachment features.
- Q37 revised and accepted: simple deliverables in the output directory; no
  separately collected code diff. Keep v1 fixed to `output/result.txt` and its
  existing size limit. Future code work belongs in GitHub; access remains open.
- Q38 accepted: no code change is required for success. Questions and exploratory
  tasks are valid; retain the normal completion and output checks.
- Q39 accepted for later validation support: a supplied validation command must
  pass. Failure or timeout fails the run; saved partial output remains available.
  Validation must fit the existing deadline. Do not add this command field to
  the v1 job schema yet.

These answers replace the repository-archive and separate-patch proposals.
They do not expand v1 into file or code integration or approve implementation.
Q40-Q42 settle the AWS account/profile, region, and compute backend.
The implementer checks versions, service support, sizing, S3 permissions, and
recovery behavior. Raise new choices only if these checks expose a blocker.
Deployment still needs separate approval.

### Final deployment choices: Q40-Q42

- Q40 accepted: Lambda MicroVMs for v1. Do not silently select another backend
  if checks fail.
- Q41 accepted: account `618170664907`, reported empty by the user. Use
  `AdministratorAccess-618170664907` for approved bootstrap, then the restricted
  provisioner role for normal provisioning. Track project infrastructure with
  Terraform and identify project resources consistently. Keep the account and
  other deployment settings configurable in the Terraform-owned local input
  file, with a checked-in example. The user permits a VPC if needed but asks for
  minimum infrastructure; this does not require adding one.
- Q42 accepted: `us-east-1`. Override the profile's region for this deployment;
  do not change the global profile setting.

The design review is complete. Expired SSO needs renewal before cloud checks,
not before local implementation. Technical checks belong to the implementer;
raise a new choice only if a check exposes a blocker. These answers do not
authorize implementation or AWS deployment.

### Review record: Q14-Q17

- Q14 accepted: fail early on invalid input. Do not run until submission is valid.
- Q15 accepted: one stable default AWS configuration coordinated with Terraform.
  Read named outputs in memory. Require Terraform and initialized local state
  on the submitting computer. No repeated `--config` or deployment cache.
- Q16 accepted: keep saved partial results available until normal expiration.
- Q17 accepted: automatic metadata events in CloudWatch, as specified in Logging.

### Q15 accepted: read deployment outputs in memory

Keep profile, region, and expected account in the Terraform-owned input file
`infra/cloudbox.auto.tfvars.json`, accepted in Q18. Derive resource values from
Terraform; do not copy them into a second input source.
Expose required non-secret deployment values in one root output named `cloudbox`.
For each submission, the CLI runs this read-only command and parses stdout in
memory. The directory and output names below describe the proposed interface.

```sh
terraform -chdir=infra output -json cloudbox
```

- No `.cloudbox/deployment.json`, temporary export, shell `eval`, or fallback cache.
  Read once per command invocation so its operations use the same settings.
- The submitting machine needs Terraform and access to the deployment's local
  state. This removes the extra copy, not Terraform state itself. Workers receive
  neither Terraform state nor provisioner credentials.
  Keep the Terraform working directory initialized; backend or provider setup
  can prevent output reads even though reading local state needs no AWS access.
- Read saved output values, not freshly evaluated input files or live AWS state.
  Do not run `plan`, `apply`, or `refresh` as part of submission. Infrastructure
  changes still need a separate approved apply.
- Fail before submission if the command fails or required values are missing or
  invalid. Do not assume a zero command exit means the output is valid.
- Keep operator and provisioner access separate. Use existing SSO credentials
  and check the expected AWS account before writes. Output secret references,
  never secret values; named JSON output does not redact sensitive values.
- If the image is script-owned, resolve its identity/version from its owner
  without a local build-record cache or hand-copied ARN. AWS supports discovery
  with `ListMicrovmImages`: paginate and require an exact name match because its
  name filter is a substring match. Check the selected version with
  `GetMicrovmImageVersion` and pin it in the launch request. Keep the stable image
  name in the shared configuration. Q19 selects a specified release, not latest.
  Image ownership remains open.
- Still save each resolved run specification and pinned versions in S3. Those
  records explain what ran; they are not a deployment configuration cache.

This replaces the earlier generated-file proposal. Q18 settles the input-file
format; Q19 settles explicit image-version selection.

### Review record: Q18-Q21

- Q18 accepted: use `infra/cloudbox.auto.tfvars.json` for Terraform-owned,
  non-secret inputs. Derive outputs from Terraform; no separate `.env` copy.
- Q19 accepted: select a specific image release in deployment settings.
  Building a new image must not change the version used by later runs until
  the operator selects it. Each run still records its resolved image version.
- Q20 revised and accepted: reserve thirty seconds inside the run limit. Keep
  Pi and the supervisor simple and co-located; defer internal security and stored
  log protection. Stream CloudWatch logs during the run, not only at shutdown.
- Q21 revised and accepted: cap the submitted prompt at 128,000 characters,
  with no tokenizer integration. Keep the 1 MiB result limit. Fail rather than
  truncate. The character count does not guarantee provider context acceptance.

### Per-run S3 research: no decision yet

`RunMicrovm` has no session-policy or session-tag argument. The trust policy's
`sts:TagSession` requirement does not establish a run-specific principal tag.
Feasible options are exact-object signed URLs or one static S3 role with scoped
STS sessions issued by the trusted submitter. Neither needs an IAM role per run.
The execution role must not offer broader S3 access that bypasses either option.

Signed URLs fit the three fixed objects: GET the spec, PUT the output, PUT the
result record. They are reusable bearer credentials and expire no later than
their signer credentials. Conditional writes can prevent overwrites, but cannot
prove the first result is truthful. Scoped STS sessions suit variable output names;
role chaining can limit later long jobs. Verify credential lifetime before launch.

Before implementation, resolve conflicting documented `runHookPayload` limits:
16,384 bytes in prose versus 4,096 characters in the schema. Multiple signed URLs
may reach this limit. Do not select this mechanism without checking payload size.

## Sources

Primary documentation; recheck before deployment.

- [Lambda MicroVMs overview](https://docs.aws.amazon.com/lambda/latest/dg/lambda-microvms-guide.html)
- [Core concepts and snapshot behavior](https://docs.aws.amazon.com/lambda/latest/dg/microvms-how-it-works.html)
- [Runtime controls and lifecycle hooks](https://docs.aws.amazon.com/lambda/latest/dg/microvms-launching.html)
- [Security and roles](https://docs.aws.amazon.com/lambda/latest/dg/microvms-security.html)
- [IAM action and resource scope](https://docs.aws.amazon.com/service-authorization/latest/reference/list_lambda.html)
- [AWS sample: MicroVM role-passing constraints](https://aws-samples.github.io/sample-autonomous-cloud-coding-agents/decisions/adr-021-lambda-microvms-compute-backend/)
- [Networking](https://docs.aws.amazon.com/lambda/latest/dg/microvms-networking.html)
- [Image build process](https://docs.aws.amazon.com/lambda/latest/dg/microvms-images.html)
- [Run payload and idempotency](https://docs.aws.amazon.com/lambda/latest/microvm-api/API_RunMicrovm.html)
- [MicroVM discovery filters](https://docs.aws.amazon.com/lambda/latest/microvm-api/API_ListMicrovms.html)
- [MicroVM state consistency](https://docs.aws.amazon.com/lambda/latest/microvm-api/API_GetMicrovm.html)
- [MicroVM termination behavior](https://docs.aws.amazon.com/lambda/latest/microvm-api/API_TerminateMicrovm.html)
- [S3 completion notifications, unselected controller option](https://docs.aws.amazon.com/AmazonS3/latest/userguide/EventNotifications.html)
- [Network Firewall domain rules and limitations](https://docs.aws.amazon.com/network-firewall/latest/developerguide/stateful-rule-groups-domain-names.html)
- [Pi usage and startup network controls](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/README.md)
- [Pi provider configuration](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/providers.md)
- [OpenRouter API](https://openrouter.ai/docs/quickstart)
- [OpenRouter provider selection and fallback](https://openrouter.ai/docs/guides/routing/provider-selection)
- [OpenRouter model fallback](https://openrouter.ai/docs/guides/routing/model-fallbacks)
- [Model-host data policies](https://openrouter.ai/docs/guides/privacy/provider-logging)
- [GLM 5.3 on OpenRouter](https://openrouter.ai/z-ai/glm-5.3)
- [GLM 5.3 model documentation](https://docs.z.ai/guides/llm/glm-5.3)
- [GLM 5.3 tokenizer files, checked revision](https://huggingface.co/zai-org/GLM-5.3/tree/187fb9fff6319062325ff825627ef6db084d9bc6)
- [OpenRouter GLM 5.3 endpoint capacities](https://openrouter.ai/api/v1/models/z-ai/glm-5.3/endpoints)
- [Pi context estimates](https://github.com/earendil-works/pi/blob/main/packages/ai/src/utils/estimate.ts)
- [Quotas](https://docs.aws.amazon.com/lambda/latest/dg/gettingstarted-limits.html)
- [AWS CLI SSO setup](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-sso.html)
- [Region expansion, August 2026](https://aws.amazon.com/about-aws/whats-new/2026/08/lambda-microvms-5-additional-regions/)
- [Initial MicroVM regions, June 2026](https://aws.amazon.com/about-aws/whats-new/2026/06/aws-lambda-microvms/)
- [Pi tool permissions and sandbox limits](https://pi.dev/docs/latest/security)
- [Pi provider credentials](https://pi.dev/docs/latest/providers)
- [S3 lifecycle filters and object age](https://docs.aws.amazon.com/AmazonS3/latest/userguide/intro-lifecycle-rules.html)
- [S3 expiration behavior](https://docs.aws.amazon.com/AmazonS3/latest/userguide/lifecycle-expire-general-considerations.html)
- [CloudWatch log-group retention](https://docs.aws.amazon.com/AmazonCloudWatchLogs/latest/APIReference/API_PutRetentionPolicy.html)
- [Codex cloud and local CLI commands](https://learn.chatgpt.com/docs/developer-commands?surface=cli)
- [Codex configuration precedence](https://learn.chatgpt.com/docs/config-file/config-basic)
- [Claude Code cloud submission](https://code.claude.com/docs/en/claude-code-on-the-web)
- [Claude Code CLI settings and input/output formats](https://code.claude.com/docs/en/cli-reference)
- [AWS Batch JSON submission and flag overrides](https://docs.aws.amazon.com/cli/latest/reference/batch/submit-job.html)
- [S3 signed URL access and expiry](https://docs.aws.amazon.com/AmazonS3/latest/userguide/using-presigned-url.html)
- [S3 conditional writes](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html)
- [Enforce conditional writes](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes-enforce.html)
- [STS session policies and duration](https://docs.aws.amazon.com/STS/latest/APIReference/API_AssumeRole.html)
- [Terraform input files](https://developer.hashicorp.com/terraform/language/values/variables)
- [AWS provider account guard, region precedence, and tags](https://github.com/hashicorp/terraform-provider-aws/blob/main/website/docs/index.html.markdown)
- [Terraform state purpose and resource mapping](https://developer.hashicorp.com/terraform/language/state/purpose)
- [Terraform named JSON outputs and sensitive values](https://developer.hashicorp.com/terraform/cli/commands/output)
- [MicroVM image discovery](https://docs.aws.amazon.com/lambda/latest/microvm-api/API_ListMicrovmImages.html)
- [MicroVM image version checks](https://docs.aws.amazon.com/lambda/latest/microvm-api/API_GetMicrovmImageVersion.html)
- [Linux privilege-gain prevention](https://docs.kernel.org/userspace-api/no_new_privs.html)
- [Linux process and resource controls](https://docs.kernel.org/admin-guide/cgroup-v2.html)
- [OpenTelemetry GenAI logging patterns, May 2026](https://opentelemetry.io/blog/2026/genai-observability/)
- [Claude Code automatic monitoring and content controls](https://code.claude.com/docs/en/monitoring-usage)
- [Langfuse instrumentation and shutdown flushing](https://langfuse.com/docs/observability/data-model)
- [MicroVM stdout/stderr forwarding](https://docs.aws.amazon.com/lambda/latest/dg/microvms-monitoring.html)
- [Follow CloudWatch logs with the AWS CLI](https://docs.aws.amazon.com/cli/latest/reference/logs/tail.html)
- [Pi JSON event stream](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/json.md)
- [Pi session content](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/session-format.md)
- [Pi print-mode terminal status, checked source](https://github.com/earendil-works/pi/blob/b8b873b9872db04a938fb4357b5e8e824ddc051c/packages/coding-agent/src/modes/print-mode.ts)
- [Pi stop reasons, Q23 review](https://github.com/earendil-works/pi/blob/e266507b606b9552fa277252644054afd4384b11/packages/ai/src/types.ts)
- [Pi noninteractive completion, Q23 review](https://github.com/earendil-works/pi/blob/e266507b606b9552fa277252644054afd4384b11/packages/coding-agent/src/modes/print-mode.ts)
- [Pi shell tool process behavior](https://github.com/earendil-works/pi/blob/e266507b606b9552fa277252644054afd4384b11/packages/coding-agent/src/core/tools/bash.ts)
