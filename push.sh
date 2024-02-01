#!/bin/bash

set -o pipefail -e

VERSION="${1}"
TAG="${2}"

source ./build.sh "${VERSION}"

docker push "supervisely/agent:${TAG}"
images=()

if [[ -f './push_images.txt' ]]; then
  images=($(cat ./push_images.txt))
fi

if [[ "${TAG}" != "dev" ]]; then
  for img in "${images[@]}"; do
    docker tag "supervisely/agent:${TAG}" "${img}:${TAG}"
    docker push "${img}:${TAG}"
  done
fi
