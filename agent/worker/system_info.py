# coding: utf-8

import json
import os.path as osp
import platform
import subprocess

import psutil
import pynvml as smi
import docker
import socket
import warnings

warnings.filterwarnings(action="ignore", category=UserWarning)

import supervisely_lib as sly

from worker import constants


# Some functions below perform dirty parsing of corresponding utils output.
# @TODO: They shall be replaced with more portable implementations.

# echo q | htop -C | aha --line-fix | html2text -width 999 | grep -v "F1Help" | grep -v "xml version=" > file.txt
# echo q | nvidia-smi | aha --line-fix | html2text -width 999 | grep -v "F1Help" | grep -v "xml version=" > fnvsmi.txt


def _proc_run(in_args, strip=True, timeout=2):
    compl = subprocess.run(in_args, stdout=subprocess.PIPE, check=True, timeout=timeout)
    rows = compl.stdout.decode("utf-8").split("\n")
    if strip:
        rows = [x.strip() for x in rows]
    return rows


def _val_unit(s):
    s = s.strip()
    for i, c in enumerate(s):
        if not c.isdigit():
            return int(s[:i]), s[i:]
    return None


def parse_cpuinfo():
    rows = _proc_run(["cat", "/proc/cpuinfo"])
    logical_count = sum((1 for r in rows if r.startswith("processor")))
    physical_count = len(set((r.split()[-1] for r in rows if r.startswith("physical id"))))
    models = list(set((r.split(":")[-1] for r in rows if r.startswith("model name"))))
    res = {
        "models": models,
        "count": {
            "logical_cores": logical_count,
            "physical_cpus": physical_count,
        },
    }
    return res


def parse_meminfo():
    rows = _proc_run(["cat", "/proc/meminfo"])
    spl_colon = [x.split(":") for x in rows]
    dct = {z[0]: z[1] for z in spl_colon if len(z) > 1}

    def to_bytes(name):
        val, unit = dct[name].split()
        val = int(val)
        if unit == "B":
            return val
        elif unit == "kB":
            return val * 2**10
        elif unit == "mB":
            return val * 2**20
        elif unit == "gB":
            return val * 2**30
        raise RuntimeError("Unknown unit.")

    res = {
        "memory_B": {
            "physical": to_bytes("MemTotal"),
            "swap": to_bytes("SwapTotal"),
        },
    }
    return res


def print_nvsmi_devlist():
    name_rows = _proc_run(["nvidia-smi", "-L"])
    return name_rows


def print_nvsmi():
    res_rows = _proc_run(["nvidia-smi"])
    return res_rows


def cpu_freq_MHZ():
    res = psutil.cpu_freq(percpu=False).max
    return res


def get_disk_usage():
    root = psutil.disk_usage("/")
    agent_data = psutil.disk_usage(constants.AGENT_ROOT_DIR())  # root dir mounted to host
    apps_data = psutil.disk_usage(constants.SUPERVISELY_AGENT_FILES_CONTAINER())
    res = {
        "root": {
            "total": root.total,
            "used": root.used,
            "free": root.free,
        },
        "agent_data": {
            "total": agent_data.total,
            "used": agent_data.used,
            "free": agent_data.free,
        },
        "apps_data": {
            "total": apps_data.total,
            "used": apps_data.used,
            "free": apps_data.free,
        },
    }
    return res


def get_hw_info():
    res = {
        "psutil": {
            "cpu": {
                "count": {
                    "logical_cores": psutil.cpu_count(logical=True),
                    "physical_cores": psutil.cpu_count(logical=False),
                },
            },
            "memory_B": {
                "physical": psutil.virtual_memory()[0],
                "swap": psutil.swap_memory()[0],
            },
        },
        "platform": {
            "uname": platform.uname(),
        },
        "cpuinfo": sly.catch_silently(parse_cpuinfo),
        "meminfo": sly.catch_silently(parse_meminfo),
        "nvidia-smi": sly.catch_silently(print_nvsmi_devlist),
        "disk_usage": get_disk_usage(),
    }
    return res


def get_load_info():
    vmem = psutil.virtual_memory()
    res = {
        "nvidia-smi": sly.catch_silently(print_nvsmi),
        "cpu_percent": psutil.cpu_percent(
            interval=0.1, percpu=True
        ),  # @TODO: does the blocking call hit performance?
        "memory_B": {
            "total": vmem[0],
            "available": vmem[1],
        },
        "disk_usage": get_disk_usage(),
    }
    return res


def parse_du_hs(dir_path, timeout):
    du_res = _proc_run(["du", "-sb", dir_path], timeout=timeout)
    byte_str = du_res[0].split()[0].strip()
    byte_sz = int(byte_str)
    return byte_sz


def get_directory_size_bytes(dir_path, timeout=10):
    if not dir_path or not osp.isdir(dir_path):
        return 0
    res = sly.catch_silently(parse_du_hs, dir_path, timeout)
    if res is None:
        return -1  # ok, to indicate error
    return res


def _get_self_container_idx():
    docker_short_id = socket.gethostname()
    return docker_short_id

    # need cgroupns argument
    # # root@37523:~# docker run --rm -it --cgroupns host --entrypoint=""  supervisely/agent bash

    # with open("/proc/self/cgroup") as fin:
    #     for line in fin:
    #         docker_split = line.strip().split(":/docker/")
    #         if len(docker_split) == 2:
    #             return docker_split[1]
    # return ""


def get_container_info():
    docker_inspect_cmd = (
        "curl -s --unix-socket /var/run/docker.sock http://localhost/containers/$(hostname)/json"
    )
    docker_img_info = subprocess.Popen(
        [docker_inspect_cmd], shell=True, executable="/bin/bash", stdout=subprocess.PIPE
    ).communicate()[0]
    return json.loads(docker_img_info)


def _get_self_docker_image_digest():
    container_idx = _get_self_container_idx()
    dc = docker.from_env()
    self_cont = dc.containers.get(container_idx)
    self_img = self_cont.image
    self_img_digests = list(self_img.attrs["RepoDigests"])
    common_digests = set(
        x.split("@")[1] for x in self_img_digests
    )  # "registry.blah-blah.com@sha256:value"
    if len(common_digests) > 1:
        raise RuntimeError("Unable to determine unique image digest.")
    elif len(common_digests) == 0:
        return None
    else:
        res = common_digests.pop()
        return res


def get_self_docker_image_digest():
    return sly.catch_silently(_get_self_docker_image_digest)


def get_gpu_info(logger):
    """
    Collect GPU information using NVML.
    """

    gpu_info = {
        "is_available": False,
        "device_count": 0,
        "device_names": [],
        "device_memory": [],
        "device_capability": [],
    }

    try:
        smi.nvmlInit()
    except Exception as e:  # pylint: disable=broad-except
        logger.warning("Failed to initialize NVML: %s", repr(e))
        return gpu_info

    try:
        try:
            device_count = smi.nvmlDeviceGetCount()
        except Exception as e:  # pylint: disable=broad-except
            logger.warning("Failed to get GPU count via NVML: %s", repr(e))
            return gpu_info

        gpu_info["device_count"] = device_count
        gpu_info["is_available"] = device_count > 0

        if not gpu_info["is_available"]:
            return gpu_info

        for idx in range(device_count):
            handle = smi.nvmlDeviceGetHandleByIndex(idx)
            capability = smi.nvmlDeviceGetCudaComputeCapability(handle)
            capability_str = "{major}.{minor}".format(major=capability[0], minor=capability[1])
            gpu_info["device_names"].append(smi.nvmlDeviceGetName(handle))
            gpu_info["device_capability"].append(
                {
                    "device": f"GPU {idx}",
                    "compute_capability": capability_str,
                }
            )

            mem = {}
            try:
                device_props = smi.nvmlDeviceGetMemoryInfo(handle)
                mem = {
                    "total": device_props.total,
                    "reserved": device_props.used,
                    "available": device_props.free,
                }
            except Exception as e:  # pylint: disable=broad-except
                logger.debug("Failed to collect GPU memory info: %s", repr(e))
            finally:
                gpu_info["device_memory"].append(mem)

        try:
            gpu_info["driver_version"] = smi.nvmlSystemGetDriverVersion()
        except Exception as e:  # pylint: disable=broad-except
            logger.debug("Failed to get NVIDIA driver version: %s", repr(e))

        try:
            gpu_info["cuda_version"] = smi.nvmlSystemGetCudaDriverVersion()
        except Exception as e:  # pylint: disable=broad-except
            logger.debug("Failed to get CUDA driver version: %s", repr(e))

    except Exception as e:  # pylint: disable=broad-except
        logger.warning("Failed to collect GPU info via NVML: %s", repr(e))
    finally:
        try:
            smi.nvmlShutdown()
        except Exception:  # pylint: disable=broad-except
            # Ignore shutdown errors
            pass

    return gpu_info
