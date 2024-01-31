FROM supervisely/base-py:6.1.7

ARG LABEL_VERSION
ARG LABEL_INFO
ARG LABEL_MODES
ARG LABEL_README
ARG LABEL_BUILT_AT

LABEL VERSION=$LABEL_VERSION
LABEL INFO=$LABEL_INFO
LABEL MODES=$LABEL_MODES
LABEL README=$LABEL_README
LABEL BUILT_AT=$LABEL_BUILT_AT
LABEL CONFIGS=""

##############################################################################
# Additional project libraries
##############################################################################
RUN rm /etc/apt/sources.list.d/cuda.list
RUN rm /etc/apt/sources.list.d/nvidia-ml.list

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
    lshw=02.18.85-0.3ubuntu2 \
    aha=0.5-1 \
    html2text=1.3.2a-25 \
    htop=2.2.0-2build1 \
    && apt-get -qq -y autoremove \
    && apt-get autoclean \
    && rm -rf /var/lib/apt/lists/* /var/log/dpkg.log

RUN apt-get update \
    && apt-get install -y tree \
    && apt-get -qq -y autoremove \
    && apt-get autoclean \
    && rm -rf /var/lib/apt/lists/* /var/log/dpkg.log


############### copy code ###############
#COPY supervisely_lib /workdir/supervisely_lib
COPY . /workdir

RUN pip install --no-cache-dir -r /workdir/requirements.txt

#ENV PYTHONPATH /workdir:/workdir/src:/workdir/supervisely_lib/worker_proto:$PYTHONPATH
WORKDIR /workdir/agent

ENTRYPOINT ["sh", "-c", "python -u /workdir/agent/main.py"]

