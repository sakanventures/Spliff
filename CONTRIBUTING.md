# Contributing to Spliff

Install Python and FFmpeg as described in the [setup guide](README.md). From the project folder, install Spliff in a virtual environment and run the tests.

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m unittest discover -s tests -v
```

Use generated audio or media you have permission to share in bug reports and tests. Remove private tags, library paths, peer names, and catalog data from shared logs. Include the Spliff, Python, and FFmpeg versions and the steps to reproduce the issue.

Keep music reads separate from catalog writes. Test new behavior with temporary directories and check that original audio stays intact. See the [integration plan](INTEGRATIONS.md) before changing provider or analysis interfaces.
