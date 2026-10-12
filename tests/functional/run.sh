#!/bin/bash

# Functional tests: install the checkout into a throwaway systemd container and
# exercise the running service end to end.
#
# Usage: tests/functional/run.sh [--from REV]
#
#   --from REV   install REV first and upgrade from it to the checkout
#
# The checkout is installed as it is on disk, uncommitted changes included.
# Needs Docker. The container is privileged, because systemd and the service's
# sandboxing need cgroups and mount namespaces; it is removed on exit.

set -euo pipefail

FROM_REV=""
while [ $# -gt 0 ]; do
    case "$1" in
        --from) FROM_REV=${2:?--from needs a revision}; shift 2 ;;
        *) echo "Usage: $0 [--from REV]" >&2; exit 2 ;;
    esac
done

HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(git -C "$HERE" rev-parse --show-toplevel)
IMAGE=child-minder-functional:debian
CONTAINER=child-minder-functional-$$

if [ -n "$FROM_REV" ]; then
    git -C "$REPO" rev-parse --verify --quiet "$FROM_REV^{commit}" >/dev/null \
        || { echo "Unknown revision: $FROM_REV" >&2; exit 2; }
fi

echo "Building test image..."
docker build -q -t "$IMAGE" "$HERE" >/dev/null

trap 'docker rm -f "$CONTAINER" >/dev/null 2>&1 || true' EXIT
docker run -d --name "$CONTAINER" --privileged --cgroupns=private \
    --tmpfs /run --tmpfs /run/lock "$IMAGE" >/dev/null

# is-system-running reports "degraded" in a container, since some units have
# no hardware to manage; any answer means boot has finished.
docker exec "$CONTAINER" systemctl is-system-running --wait >/dev/null 2>&1 || true

docker exec "$CONTAINER" mkdir -p /root/new /root/old
(cd "$REPO" && git ls-files -z --cached --others --exclude-standard \
    | tar --null -T - -cf -) | docker exec -i "$CONTAINER" tar -xf - -C /root/new
if [ -n "$FROM_REV" ]; then
    git -C "$REPO" archive "$FROM_REV" | docker exec -i "$CONTAINER" tar -xf - -C /root/old
fi

docker exec -e FROM_REV="$FROM_REV" "$CONTAINER" bash /root/new/tests/functional/scenarios.sh
