#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "Usage: $0 /path/to/video.mp4 media-id" >&2
  exit 2
fi

INPUT="$1"
MEDIA_ID="$2"
[[ -f "$INPUT" ]] || { echo "Input video not found: $INPUT" >&2; exit 1; }
[[ "$MEDIA_ID" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]] || {
  echo "Media ID may contain only letters, numbers, dots, dashes, and underscores." >&2
  exit 2
}
[[ -f .aws-stack.env ]] && source .aws-stack.env
: "${HLS_BUCKET:?Set HLS_BUCKET or run scripts/aws_bootstrap.sh}"
: "${CLOUDFRONT_DOMAIN:?Set CLOUDFRONT_DOMAIN or run scripts/aws_bootstrap.sh}"

REGION="${AWS_REGION:-us-east-2}"
PROFILE="${AWS_PROFILE:-default}"
ENCODER="${FFMPEG_VIDEO_ENCODER:-libx264}"
command -v ffmpeg >/dev/null || { echo "Missing required command: ffmpeg" >&2; exit 1; }
AWS_BIN="${AWS_BIN:-$(command -v aws || true)}"
[[ -n "$AWS_BIN" ]] || [[ ! -x "$HOME/.local/bin/aws" ]] || AWS_BIN="$HOME/.local/bin/aws"
[[ -n "$AWS_BIN" ]] || { echo "Missing required command: aws" >&2; exit 1; }

WORK_DIR="$(mktemp -d -t watchparty-hls.XXXXXX)"
trap 'rm -rf "$WORK_DIR"' EXIT
mkdir -p "$WORK_DIR"/{360p,720p,1080p}

ffmpeg -hide_banner -y -i "$INPUT" \
  -filter_complex "[0:v]split=3[v0][v1][v2];[v0]scale=640:360:force_original_aspect_ratio=decrease,pad=640:360:(ow-iw)/2:(oh-ih)/2[v0out];[v1]scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2[v1out];[v2]scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2[v2out]" \
  -map '[v0out]' -map '0:a:0?' -map '[v1out]' -map '0:a:0?' -map '[v2out]' -map '0:a:0?' \
  -c:v "$ENCODER" -preset "${FFMPEG_PRESET:-medium}" -pix_fmt yuv420p \
  -b:v:0 800k -maxrate:v:0 800k -bufsize:v:0 1600k \
  -b:v:1 2800k -maxrate:v:1 2800k -bufsize:v:1 5600k \
  -b:v:2 5000k -maxrate:v:2 5000k -bufsize:v:2 10000k \
  -c:a aac -b:a 128k -ac 2 -sn -force_key_frames 'expr:gte(t,n_forced*3)' \
  -f hls -hls_time 3 -hls_list_size 0 -hls_flags independent_segments \
  -hls_segment_filename "$WORK_DIR/%v/seg%05d.ts" \
  -master_pl_name master.m3u8 \
  -var_stream_map 'v:0,a:0,name:360p v:1,a:1,name:720p v:2,a:2,name:1080p' \
  "$WORK_DIR/%v/index.m3u8"

DESTINATION="s3://$HLS_BUCKET/media/$MEDIA_ID"
AWS_ARGS=(--region "$REGION" --profile "$PROFILE")
"$AWS_BIN" s3 sync "$WORK_DIR" "$DESTINATION" "${AWS_ARGS[@]}" \
  --exclude '*' --include '*.ts' --content-type video/mp2t \
  --cache-control 'public,max-age=31536000,immutable' --only-show-errors
"$AWS_BIN" s3 sync "$WORK_DIR" "$DESTINATION" "${AWS_ARGS[@]}" \
  --exclude '*' --include '*.m3u8' --content-type application/vnd.apple.mpegurl \
  --cache-control 'public,max-age=60,s-maxage=300' --only-show-errors

if [[ -n "${CLOUDFRONT_DISTRIBUTION_ID:-}" ]]; then
  "$AWS_BIN" cloudfront create-invalidation --profile "$PROFILE" \
    --distribution-id "$CLOUDFRONT_DISTRIBUTION_ID" \
    --paths "/media/$MEDIA_ID/*.m3u8" >/dev/null
fi

echo "Uploaded media ID: $MEDIA_ID"
echo "HLS URL: https://$CLOUDFRONT_DOMAIN/media/$MEDIA_ID/master.m3u8"
