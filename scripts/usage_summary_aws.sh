#!/usr/bin/env bash
set -euo pipefail

[[ -f .aws-stack.env ]] && source .aws-stack.env
: "${EC2_HOST:?Set EC2_HOST or run scripts/aws_bootstrap.sh}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/watchparty-ohio.pem}"
SSH_USER="${SSH_USER:-ec2-user}"
[[ -f "$SSH_KEY" ]] || { echo "SSH key not found: $SSH_KEY" >&2; exit 1; }

ssh -o StrictHostKeyChecking=accept-new -i "$SSH_KEY" "$SSH_USER@$EC2_HOST" \
  docker exec watchparty-app python /app/tools/usage_summary.py "$@"
