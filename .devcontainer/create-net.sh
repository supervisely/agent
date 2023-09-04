
set -o pipefail

WHITE='\033[1;37m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

command_exists() {
  command -v "$@" > /dev/null 2>&1
}

sudo_cmd=()

if command_exists sudo; then
  sudo_cmd=(sudo -E bash -c)
elif command_exists su; then
  sudo_cmd=(su -p -s bash -c)
fi

docker ps > /dev/null 2>&1

access_test_code=$?

if [[ ${access_test_code} -ne 0 && ${EUID} -ne 0 && ${#sudo_cmd} -ne 0 ]]; then
  cur_fd="$(printf %q "$BASH_SOURCE")$((($#)) && printf ' %q' "$@")"
  cur_script=$(cat "${cur_fd}")
  ${sudo_cmd[*]} "${cur_script}"

  exit 0
fi

export SUPERVISELY_AGENT_IMAGE='supervisely/agent:dev'
# same as in devcontainer.json and debug.env
export AGENT_HOST_DIR="/home/fedor_lisin/agent_debug_dir/agent"

# from secret.env ↓
export ACCESS_TOKEN=''
export SERVER_ADDRESS=''
export DOCKER_REGISTRY=''
export DOCKER_LOGIN=''
export DOCKER_PASSWORD=''
# from secret.env ↑

secrets=("${ACCESS_TOKEN}" "${SERVER_ADDRESS}" "${DOCKER_REGISTRY}" "${DOCKER_LOGIN}" "${DOCKER_PASSWORD}")

for value in "${secrets[@]}"
do
  if [ -z $value ];
  then
    echo "${RED}One of the required secrets is not defined${NC}"
    exit 1
  fi
done


export DELETE_TASK_DIR_ON_FINISH='true'
export DELETE_TASK_DIR_ON_FAILURE='true'
export PULL_POLICY='ifnotpresent'
# same as in devcontainer.json and debug.env
export SUPERVISELY_AGENT_FILES=$(echo -n "/home/fedor_lisin/agent_debug_dir/files")


echo 'Supervisely Net is enabled, starting client...'
docker pull supervisely/sly-net-client:latest
docker network create "supervisely-net-${ACCESS_TOKEN}" 2> /dev/null
echo 'Remove existing Net Client container if any...'
docker rm -fv $(docker ps -aq -f name="supervisely-net-client-${ACCESS_TOKEN}") 2> /dev/null
docker run -it -d --name="supervisely-net-client-${ACCESS_TOKEN}" \
      -e "SLY_NET_CLIENT_PING_INTERVAL=60" \
      --privileged \
      --network "supervisely-net-${ACCESS_TOKEN}" \
      --restart=unless-stopped \
      --log-driver=local \
      --log-opt max-size=1m \
      --log-opt max-file=1 \
      --log-opt compress=false \
      --cap-add NET_ADMIN \
      --device /dev/net/tun:/dev/net/tun \
       \
       \
       \
       \
       \
       \
      -v /var/run/docker.sock:/tmp/docker.sock:ro \
      -v "${AGENT_HOST_DIR}:/app/sly" \
       \
      -v "/home/fedor_lisin/agent_debug_dir/files:/app/sly-files" \
      "supervisely/sly-net-client:latest" \
      "${ACCESS_TOKEN}" \
      "https://dev.supervisely.com/net/" \
      "dev.supervisely.com:51822"

retVal=$?
if [ $retVal -ne 0 ]; then
    echo -e "
${RED}Couldn't start Supervisely Net. Agent is running fine. Please, contact support and attach the log above${NC}"
fi

echo -e "${WHITE}============ You can close this terminal safely now ============${NC}"