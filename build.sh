plugin_info=$(cat plugin_info.json | base64)
readme=$(cat README.md | base64)

built_at=$(date -u +"%Y-%m-%d %H:%M:%SZ")

if [[ -z "${VERSION}" ]]; then
  VERSION='6.999.0'
fi

docker build . -t supervisely/agent:dev \
--label "VERSION=agent:${VERSION}" \
--label "INFO=${plugin_info}" \
--label "MODES=main" \
--label "README=${readme}" \
--label "BUILT_AT=${built_at}"
