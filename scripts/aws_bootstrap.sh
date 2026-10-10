#!/usr/bin/env bash
set -euo pipefail

REGION="${AWS_REGION:-us-east-2}"
PROFILE="${AWS_PROFILE:-default}"
STACK_NAME="${STACK_NAME:-watchparty}"
KEY_NAME="${KEY_NAME:-watchparty-ohio}"

AWS_BIN="${AWS_BIN:-$(command -v aws || true)}"
[[ -n "$AWS_BIN" ]] || [[ ! -x "$HOME/.local/bin/aws" ]] || AWS_BIN="$HOME/.local/bin/aws"
[[ -n "$AWS_BIN" ]] || { echo "Missing required command: aws" >&2; exit 1; }
command -v curl >/dev/null || { echo "Missing required command: curl" >&2; exit 1; }

"$AWS_BIN" sts get-caller-identity --profile "$PROFILE" >/dev/null
if ! "$AWS_BIN" ec2 describe-key-pairs --region "$REGION" --profile "$PROFILE" --key-names "$KEY_NAME" >/dev/null 2>&1; then
  echo "EC2 key pair '$KEY_NAME' does not exist in $REGION." >&2
  echo "Create it first using the README command so its private key remains only on your laptop." >&2
  exit 1
fi

PUBLIC_IP="$(curl -fsS https://checkip.amazonaws.com | tr -d '[:space:]')"
SSH_CIDR="${SSH_CIDR:-${PUBLIC_IP}/32}"

"$AWS_BIN" cloudformation deploy \
  --region "$REGION" \
  --profile "$PROFILE" \
  --stack-name "$STACK_NAME" \
  --template-file infra/aws-stack.yaml \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides "KeyName=$KEY_NAME" "SSHLocation=$SSH_CIDR" "InstanceType=t3.micro" \
  --no-fail-on-empty-changeset

output() {
  "$AWS_BIN" cloudformation describe-stacks --region "$REGION" --profile "$PROFILE" \
    --stack-name "$STACK_NAME" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue | [0]" --output text
}

EC2_HOST_VALUE="$(output PublicIp)"
DEFAULT_APP_DOMAIN="${EC2_HOST_VALUE//./-}.sslip.io"
cat > .aws-stack.env <<EOF
AWS_REGION=$REGION
AWS_PROFILE=$PROFILE
STACK_NAME=$STACK_NAME
EC2_HOST=$EC2_HOST_VALUE
HLS_BUCKET=$(output HlsBucketName)
CLOUDFRONT_DOMAIN=$(output CloudFrontDomain)
CLOUDFRONT_DISTRIBUTION_ID=$(output CloudFrontDistributionId)
DEFAULT_APP_DOMAIN=$DEFAULT_APP_DOMAIN
EOF
chmod 600 .aws-stack.env

echo "Infrastructure is ready. Outputs were written to .aws-stack.env (no AWS credentials)."
echo "Free HTTPS hostname: $DEFAULT_APP_DOMAIN"
echo "Run scripts/redeploy_aws.sh, or set APP_DOMAIN first to use a custom DNS name."
