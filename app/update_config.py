import json
import os
import sys

this_dir = os.path.dirname(os.path.abspath(__file__))
config = json.load(open(f"{this_dir}/config.json", "r"))
docker_image = sys.argv[1]
parts = docker_image.split("/", maxsplit=1)
if len(parts) == 1:
    parts = ["", parts[0]]
org, rest = parts
if not org:
    org = "supervisely"
parts = rest.split(":", maxsplit=1)
if len(parts) == 1:
    parts = ["", parts[0]]
name, rest = parts
if not name:
    name = "agent"
tag = parts[1]
config["docker_image"] = f"{org}/{name}:{tag}"
json.dump(config, open(f"{this_dir}/config.json", "w"), indent=4)
print(f"Updated config.json with docker_image={config['docker_image']}")
