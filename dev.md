Create venv:
```sh
python3 -m venv venv
```

install requirements:
```sh
. venv/bin/activate
pip install -r requirements.txt
deactivate
```



Minimum env variables for development:
-e ACCESS_TOKEN="abc" \
-e SERVER_ADDRESS=supervisely.private:38585/ \
-e AGENT_HOST_DIR=/home/ds/work/agent_temp_data \
-e DOCKER_REGISTRY=docker.deepsystems.io \
-e DOCKER_LOGIN=\
-e DOCKER_PASSWORD=