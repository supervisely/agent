docker build -t supervisely/agent:dev \
  --build-arg LABEL_VERSION=agent:6.999.0 \
  . && \
docker push supervisely/agent:dev