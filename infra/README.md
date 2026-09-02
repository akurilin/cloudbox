# Infrastructure

One input file: `cloudbox.auto.tfvars.json`. Copy the example and keep the real
file out of Git. Never put credentials or the OpenRouter key in it.

The secret setup helper uses the provisioner to write the key directly to
Secrets Manager. The provisioner cannot read it. Terraform stores metadata only.

Two Terraform roots keep the provisioner from changing its own access:

- `bootstrap/`: approved SSO administrator setup; owns provisioner and boundaries.
- This directory: restricted provisioner; owns bucket, logs, secret metadata,
  and worker roles. Each root has separate local state.

Run from the repository root, after SSO login. Plans are read-only. Review each
plan and obtain approval before its apply command.

```sh
terraform -chdir=infra/bootstrap init
terraform -chdir=infra/bootstrap plan -var-file=../cloudbox.auto.tfvars.json -out=bootstrap.tfplan
terraform -chdir=infra/bootstrap apply bootstrap.tfplan
terraform -chdir=infra init
terraform -chdir=infra plan -out=cloudbox.tfplan
terraform -chdir=infra apply cloudbox.tfplan
```

Do not use either state with another account or region. Make a separate
deployment and state. `allowed_account_ids` rejects the wrong account.

The image build script owns the MicroVM image and archives. Provider 6.62.0 has
an image resource but lacks hook, memory, and logging fields. It cannot enable
the required `/run` hook. Do not split ownership of one image between Terraform
and the script. [Provider schema](https://github.com/hashicorp/terraform-provider-aws/blob/v6.62.0/internal/service/lambdamicrovms/image.go).

After a cloud build, set `deployment.image_version` to its reported version.
Review and apply the output change before submission. Never select `latest`.
`base_image_version: null` lets AWS choose the managed base during a build;
the resulting worker image version is still selected explicitly.

The runtime role has no S3 or AssumeRole grant. The CLI passes a separate data
session restricted to the run prefix. Chained STS sessions last at most one hour;
the spike limits runs to 3,300 seconds to leave launch margin.

MicroVM calls do not supply usable `iam:PassedToService` context. The provisioner
can pass only the exact build/runtime roles, without that condition. AWS also
requires wildcard resource scope for `lambda:PassNetworkConnector`; this grant
is limited to the configured region and is not given to workers. The CLI still
sets `NO_INGRESS` explicitly.

Initial image creation also needs `lambda:TagResource` on `*`. That grant requires
the configured region and exact Project/ManagedBy tags. It can tag other Lambda
resources with those values; workers do not receive it.

IAM boundaries do not protect the worker from its own agent. The runtime role
can read the OpenRouter key and terminate other runs of the same project image.

Run objects and logs expire after 30 days. The new bucket never enables
versioning. No VPC, NAT gateway, inbound endpoint access, or private ECR is added.

For approved teardown: stop runs, delete the script-owned image, then remove
main infrastructure and bootstrap last. The bucket refuses deletion while it
has objects; deletion of saved data needs a separate decision. Secret deletion
uses the seven-day recovery window. Never use `force_destroy` for convenience.
