#!/bin/bash

set -o pipefail -e

plugin_info=$(cat plugin_info.json | base64)
readme=$(cat README.md | base64)

built_at=$(date -u +"%Y-%m-%d %H:%M:%SZ")

VERSION="${1}"
TAG="${VERSION}"

if [[ -z "${VERSION}" ]] || [[ "${VERSION}" == "dev" ]]; then
  VERSION='6.999.0'
  TAG='dev'
fi

docker build . -t supervisely/agent:${TAG} \
--label "VERSION=agent:${VERSION}" \
--label "INFO=${plugin_info}" \
--label "MODES=main" \
--label "README=${readme}" \
--label "BUILT_AT=${built_at}" \
--progress plain
