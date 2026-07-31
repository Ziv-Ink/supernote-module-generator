# Publishing to PyPI

Publish only from a clean checkout after the release version and documentation
are final.

## Prepare the environment

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -e '.[dev]'
```

## Validate the release

```bash
python3 -m pytest -q
python3 -m build
python3 -m twine check dist/*
```

Inspect both archives before uploading. They must include the MIT license,
PyPI README, source documentation, Python package, templates, and tests. Install
the wheel into a fresh virtual environment and verify:

```bash
supernote-module --version
supernote-module --help
```

## TestPyPI first

Create API tokens through the PyPI and TestPyPI account interfaces. Do not put a
token in this repository.

```bash
python3 -m twine upload --repository testpypi dist/*
```

Install the exact uploaded version into a new environment:

```bash
python3 -m pip install --index-url https://test.pypi.org/simple/ \
  --no-deps supernote-module-generator==1.0.0
supernote-module --version
supernote-module --help
```

## Publish the verified files

Upload the same files that passed TestPyPI verification:

```bash
python3 -m twine upload dist/*
```

PyPI does not permit replacing a file or reusing a published version. If an
artifact is wrong, increment the version, rebuild from a clean checkout, and
upload the new release.
