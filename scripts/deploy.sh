#!/usr/bin/env bash
# ------------------------------------------------------------------------------
# Deploy ONE CloudFormation stack, always through a reviewed change set.
#
#   scripts/deploy.sh <env> <stack>
#
#   env     dev | prod
#   stack   data | security | app | observability   (deploy in this order)
#
# What it does:
#   1. lints the template (cfn-lint) if available,
#   2. reads parameter values from infra/cloudformation/params/<env>.json,
#   3. for "app": builds the Lambda zip (scripts/build_lambda.py) and uploads it to
#      the code bucket from the data stack, unless that exact package is already there,
#   4. creates a CHANGE SET (nothing changes yet) and prints what it will do,
#   5. asks you to confirm, then executes it.
# For a throw-away proof of concept in ONE stack, use scripts/poc.py instead.
# Set AUTO_APPROVE=1 to skip the question (CI does this for dev only).
# ------------------------------------------------------------------------------
set -euo pipefail

ENV_NAME="${1:?usage: deploy.sh <dev|prod> <data|security|app|observability>}"
STACK="${2:?usage: deploy.sh <dev|prod> <data|security|app|observability>}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TEMPLATE="$ROOT/infra/cloudformation/$STACK.yaml"
PARAMS="$ROOT/infra/cloudformation/params/$ENV_NAME.json"
STACK_NAME="slm-$ENV_NAME-$STACK"

[[ -f "$TEMPLATE" ]] || { echo "No template: $TEMPLATE" >&2; exit 1; }
[[ -f "$PARAMS" ]]   || { echo "No parameter file: $PARAMS" >&2; exit 1; }

# 1. Lint (catches most template mistakes before AWS sees them).
if command -v cfn-lint >/dev/null; then cfn-lint "$TEMPLATE"; else echo "(cfn-lint not installed: skipping lint)"; fi

# 2. Turn {"Key": "Value"} for this stack into "Key=Value" arguments.
mapfile -t OVERRIDES < <(python3 -c '
import json, sys
block = json.load(open(sys.argv[1]))[sys.argv[2]]
for k, v in block.items():
    print(f"{k}={v}")
' "$PARAMS" "$STACK")
# The code bucket (data stack) also holds templates larger than CloudFormation's
# 51 KB inline limit (app.yaml is), so every stack after `data` is uploaded through it.
CODE_BUCKET=$(aws cloudformation list-exports \
  --query "Exports[?Name=='slm-$ENV_NAME-CodeBucketName'].Value" --output text 2>/dev/null || true)
S3_ARGS=()
[[ -n "$CODE_BUCKET" && "$CODE_BUCKET" != "None" ]] && S3_ARGS=(--s3-bucket "$CODE_BUCKET" --s3-prefix templates)

# 3. The app stack runs the Lambda zip: build it and upload it once (name = content hash).
if [[ "$STACK" == "app" ]]; then
  [[ ${#S3_ARGS[@]} -gt 0 ]] || { echo "Deploy the data stack first (it creates the code bucket)." >&2; exit 1; }
  ZIP=$(cd "$ROOT" && uv run python scripts/build_lambda.py | sed -n 's/^Built \([^ ]*\).*/\1/p')
  KEY="code/$(basename "$ZIP")"
  aws s3api head-object --bucket "$CODE_BUCKET" --key "$KEY" >/dev/null 2>&1 \
    || aws s3 cp "$ROOT/$ZIP" "s3://$CODE_BUCKET/$KEY"
  OVERRIDES+=("CodeKey=$KEY")
fi

# 4. Create the change set only (--no-execute-changeset).
aws cloudformation deploy \
  --stack-name "$STACK_NAME" \
  --template-file "$TEMPLATE" \
  --parameter-overrides "${OVERRIDES[@]}" \
  "${S3_ARGS[@]}" \
  --capabilities CAPABILITY_NAMED_IAM \
  --tags app=slm env="$ENV_NAME" \
  --no-execute-changeset \
  --no-fail-on-empty-changeset

CHANGE_SET=$(aws cloudformation list-change-sets --stack-name "$STACK_NAME" \
  --query "sort_by(Summaries[?ExecutionStatus=='AVAILABLE'], &CreationTime)[-1].ChangeSetName" --output text)
if [[ -z "$CHANGE_SET" || "$CHANGE_SET" == "None" ]]; then
  echo "No changes for $STACK_NAME."; exit 0
fi

echo; echo "Planned changes for $STACK_NAME:"
aws cloudformation describe-change-set --stack-name "$STACK_NAME" --change-set-name "$CHANGE_SET" \
  --query "Changes[].ResourceChange.[Action, LogicalResourceId, ResourceType, Replacement]" --output table

# 5. Execute after confirmation. "Replacement = True" means a resource is DELETED and recreated.
if [[ "${AUTO_APPROVE:-0}" != "1" ]]; then
  read -r -p "Execute this change set? [y/N] " answer
  [[ "$answer" == "y" || "$answer" == "Y" ]] || { echo "Not executed. Change set left for review: $CHANGE_SET"; exit 0; }
fi
# A brand-new stack sits in REVIEW_IN_PROGRESS until its first change set runs.
STATUS=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" --query "Stacks[0].StackStatus" --output text)
aws cloudformation execute-change-set --stack-name "$STACK_NAME" --change-set-name "$CHANGE_SET"
echo "Waiting for $STACK_NAME ..."
if [[ "$STATUS" == "REVIEW_IN_PROGRESS" ]]; then
  aws cloudformation wait stack-create-complete --stack-name "$STACK_NAME"
else
  aws cloudformation wait stack-update-complete --stack-name "$STACK_NAME"
fi
echo "Done: $STACK_NAME"
