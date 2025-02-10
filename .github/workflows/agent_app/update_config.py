import json
import os


this_dir = os.path.dirname(os.path.abspath(__file__))
config = json.load(open(f"{this_dir}/config.json", "r"))
config["docker_image"] = os.getenv("DOCKER_IMAGE")
json.dump(config, open(f"{this_dir}/config.json", "w"))
