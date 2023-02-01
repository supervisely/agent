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

RUN pip install --no-cache-dir \
    docker==5.0.3 \
    psutil==5.4.5 \
    requests==2.24.0 \
    hurry.filesize==0.9 \
    scandir==1.10.0 \
    grpcio==1.47.0 \
    grpcio-tools==1.47.0 \
    py3exiv2==0.9.3

RUN pip install requests-toolbelt
RUN pip install packaging

RUN apt-get update \
    && apt-get install -y tree \
    && apt-get -qq -y autoremove \
    && apt-get autoclean \
    && rm -rf /var/lib/apt/lists/* /var/log/dpkg.log

RUN pip install docker --upgrade
RUN pip install version-parser==1.0.1
RUN pip install python-slugify==6.1.2

############### copy code ###############
#COPY supervisely_lib /workdir/supervisely_lib
RUN pip install httpx
RUN pip install supervisely==6.69.21

COPY . /workdir

#ENV PYTHONPATH /workdir:/workdir/src:/workdir/supervisely_lib/worker_proto:$PYTHONPATH
WORKDIR /workdir/agent

ENTRYPOINT ["sh", "-c", "python -u /workdir/agent/main.py"]

