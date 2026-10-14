#!/usr/bin/env bash
# ------------------------------------------------------------------------------
# Deploy ONE CloudFormation stack, always through a reviewed change set.
#
#   scripts/deploy.sh <env> <stack> [image-tag]
#
#   env     dev | prod
#   stack   data | security | app | observability   (deploy in this order)
#   image-tag  required for "app": the tag pushed to ECR (CI uses the git SHA)
#
# What it does:
#   1. lints the template (cfn-lint) if available,
#   2. reads parameter values from infra/cloudformation/params/<env>.json,
#   3. creates a CHANGE SET (nothing changes yet) and prints what it will do,
#   4. asks you to confirm, then executes it.
# Set AUTO_APPROVE=1 to skip the question (CI does this for dev only).
# ------------------------------------------------------------------------------
set -euo pipefail

ENV_NAME="${1:?usage: deploy.sh <dev|prod> <data|security|app|observability> [image-tag]}"
STACK="${2:?usage: deploy.sh <dev|prod> <data|security|app|observability> [image-tag]}"
IMAGE_TAG="${3:-}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TEMPLATE="$ROOT/infra/cloudformation/$STACK.yaml"
PARAMS="$ROOT/infra/cloudformation/params/$ENV_NAME.json"
STACK_NAME="slm-$ENV_NAME-$STACK"

[[ -f "$TEMPLATE" ]] || { echo "No template: $TEMPLATE" >&2; exit 1; }
[[ -f "$PARAMS" ]]   || { echo "No parameter file: $PARAMS" >&2; exit 1; }
if [[ "$STACK" == "app" && -z "$IMAGE_TAG" ]]; then
  echo "The app stack needs an image tag (the one you pushed to ECR)." >&2; exit 1
fi

# 1. Lint (catches most template mistakes before AWS sees them).
if command -v cfn-lint >/dev/null; then cfn-lint "$TEMPLATE"; else echo "(cfn-lint not installed: skipping lint)"; fi

# 2. Turn {"Key": "Value"} for this stack into "Key=Value" arguments.
mapfile -t OVERRIDES < <(python3 -c '
import json, sys
block = json.load(open(sys.argv[1]))[sys.argv[2]]
for k, v in block.items():
    print(f"{k}={v}")
' "$PARAMS" "$STACK")
[[ -n "$IMAGE_TAG" ]] && OVERRIDES+=("ImageTag=$IMAGE_TAG")

# 3. Create the change set only (--no-execute-changeset).
aws cloudformation deploy \
  --stack-name "$STACK_NAME" \
  --template-file "$TEMPLATE" \
  --parameter-overrides "${OVERRIDES[@]}" \
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

# 4. Execute after confirmation. "Replacement = True" means a resource is DELETED and recreated.
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
