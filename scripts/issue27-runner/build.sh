#!/usr/bin/env bash
# Build the #27 generated-Python runner image and print its local image digest.
#
# Build-time network access is needed only for the pinned pip packages.  The
# resulting image runs offline under --network none.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
image="${PRAJNA_ISSUE27_IMAGE:-prajnatrade-issue27-runner:local}"
docker_bin="${DOCKER:-docker}"

"$docker_bin" build --tag "$image" "$here"
"$docker_bin" image inspect --format '{{.Id}}' "$image"
