#!/usr/bin/env bash
# Build the El Verificador verification sandbox image.
#
# The image the acceptance checks run inside. It exists so the network can be
# off at run time: the toolchain is baked in, so nothing has to be fetched
# while untrusted code is on the machine.
#
# Usage:
#   ./scripts/build_sandbox_image.sh [tag]
#
# The default tag is el-verificador/sandbox:local, which is what
# `docker_image` defaults to in VerificationConfig.
set -euo pipefail

TAG="${1:-el-verificador/sandbox:local}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DOCKERFILE="$ROOT/docker/verification/Dockerfile"

if ! command -v docker >/dev/null 2>&1; then
  echo "error: docker is not on PATH" >&2
  exit 127
fi

if ! docker info >/dev/null 2>&1; then
  echo "error: the Docker daemon is not reachable" >&2
  echo "       start Docker and run this again" >&2
  exit 1
fi

echo "building $TAG from $DOCKERFILE"
docker build --file "$DOCKERFILE" --tag "$TAG" "$ROOT/docker/verification"

echo
echo "built $TAG"
echo
echo "record the digest for a release:"
echo "  docker image inspect $TAG --format '{{index .RepoDigests 0}}'"
echo "  docker image inspect $TAG --format '{{.Id}}'"
