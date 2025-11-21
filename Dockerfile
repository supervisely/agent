# FROM ubuntu:24.04 # No GPU support
FROM nvidia/cuda:12.8.1-cudnn-runtime-ubuntu24.04

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

ENV \
    DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    PATH=/root/.local/bin:$PATH

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
    build-essential \
    python3.12 \
    python3.12-venv \
    python3.12-dev \
    python3-pip \
    python3-grpcio \
    libexiv2-27 \
    libexiv2-dev \
    libboost-all-dev \
    libgeos-dev \
    libsm6 \
    libxext6 \
    libxrender-dev \
    libgl1-mesa-dev \
    libglu1-mesa-dev \
    libglib2.0-0 \
    libgtk-3-0 \
    libavcodec-dev \
    libavformat-dev \
    libswscale-dev \
    libv4l-dev \
    libxvidcore-dev \
    libx264-dev \
    libjpeg-dev \
    libpng-dev \
    libtiff-dev \
    libatlas-base-dev \
    gfortran \
    pkg-config \
    ca-certificates \
    curl \
    lshw \
    aha \
    html2text \
    htop \
    tree \
    && ln -sf /usr/bin/python3.12 /usr/bin/python \
    && ln -sf /usr/bin/pip3 /usr/bin/pip \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /var/log/dpkg.log

RUN python -m pip install --ignore-installed --upgrade pip setuptools wheel

RUN python -m pip install torch==2.9.1 torchvision==0.24.1 --index-url https://download.pytorch.org/whl/cu128

# Install runtime dependencies that must stay
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    libmagic-dev \
    openssh-server \
    ffmpeg \
    fonts-noto \
    && mkdir -p /var/run/sshd \
    && apt-get -qq -y autoremove \
    && apt-get autoclean \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /workdir/requirements.txt
RUN python -m pip install --no-cache-dir -r /workdir/requirements.txt

RUN apt-get purge -y --auto-remove \
    build-essential \
    python3.12-dev \
    libexiv2-dev \
    libboost-all-dev \
    libgeos-dev \
    libxrender-dev \
    libgl1-mesa-dev \
    libglu1-mesa-dev \
    libavcodec-dev \
    libavformat-dev \
    libswscale-dev \
    libv4l-dev \
    libxvidcore-dev \
    libx264-dev \
    libjpeg-dev \
    libpng-dev \
    libtiff-dev \
    libatlas-base-dev \
    gfortran \
    pkg-config \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /var/log/dpkg.log

COPY agent /workdir/agent

WORKDIR /workdir/agent

ENTRYPOINT ["python", "-u", "/workdir/agent/main.py"]
