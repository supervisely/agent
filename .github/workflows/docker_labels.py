import os
import base64
import datetime

env_file = os.getenv('GITHUB_ENV')

## VERSION
# version_dir = "my_app"
# with open(os.path.join(version_dir, 'VERSION')) as f:
#     label_version = f.read()
label_version = "dev"

## PLUGIN INFO
info_path = "./plugin_info.json"
with open(info_path, 'rb') as fp:
    data = fp.read()
    label_info = base64.b64encode(data).decode("utf-8")

## MODES
label_modes = "main"

## README
readme_path = "./README.md"
with open(readme_path, 'rb') as fp:
    data = fp.read()
    label_readme = base64.b64encode(data).decode("utf-8")


## BUILT_AT
label_built_at = str(datetime.datetime.now())


## Create ENV variables
with open(env_file, "a") as workflow:
    workflow.write(f"LABEL_VERSION={label_version}\n")
    workflow.write(f"LABEL_INFO={label_info}\n")
    workflow.write(f"LABEL_MODES={label_modes}\n")
    workflow.write(f"LABEL_README={label_readme}\n")
    workflow.write(f"LABEL_BUILT_AT={label_built_at}\n")
