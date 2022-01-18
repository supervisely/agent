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
