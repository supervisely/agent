FROM supervisely/agent:latest

# use same name in devcontainer.json
ARG USERNAME=fedor
# execute `echo $UID` on host
ARG USER_UID=1003
ARG USER_GID=$USER_UID
# execute `getent group docker` on host
ARG DOCKER_UID=999

RUN groupadd --gid $USER_GID $USERNAME \
    && useradd --uid $USER_UID --gid $USER_GID -m $USERNAME \
    #
    # [Optional] Add sudo support. Omit if you don't need to install software after connecting.
    && apt-get update \
    && apt-get install -y sudo \
    && echo $USERNAME ALL=\(root\) NOPASSWD:ALL > /etc/sudoers.d/$USERNAME \
    && chmod 0440 /etc/sudoers.d/$USERNAME

RUN groupadd -g $DOCKER_UID docker \
    && usermod -aG docker $USERNAME

RUN mkdir /sly_agent
RUN mkdir -p /app/sly-files
# uncomment and uninstall to debug sdk
# RUN pip3 uninstall -y supervisely

# if no User defined all files changed in devcontainer will have root as owner
USER $USERNAME
ENTRYPOINT ["/bin/bash"]