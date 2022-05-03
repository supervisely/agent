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

Symlink supervisely SDK sources:
for example

```sh
# ln -s ~/work/sly_public/supervisely ./supervisely
# ln -s ~/work/sly_public/supervisely_lib ./agent/supervisely_lib
ln -s ~/work/sly_public/supervisely_lib ./supervisely_lib

```


Build dockerimage
```sh
docker build -t supervisely/agent:build --label "LABEL_VERSION=agent:6.999.0" --label "LABEL_INFO=1" --label "LABEL_README=1" --label "LABEL_BUILT_AT=1" .
```