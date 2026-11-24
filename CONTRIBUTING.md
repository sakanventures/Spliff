# Contributing to Spliff

Install Python and FFmpeg as described in the [setup guide](README.md). From the project folder, install Spliff in a virtual environment and run the tests.

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m unittest discover -s tests -v
```

The local dashboard builds its shadcn Button and Card markup and Tailwind styles into `spliff/static`. Edit the component sections in `dashboard-ui/sections.tsx`, then rebuild and check the generated sections from the project root.

```sh
npm --prefix dashboard-ui ci
npm --prefix dashboard-ui run build
npm --prefix dashboard-ui run check
```

Run `spliff serve` to review dashboard interactions. Opening `spliff/static/index.html` directly shows the design preview. The Python service serves the generated files at runtime.

Use generated audio or media you have permission to share in bug reports and tests. Remove private tags, library paths, peer names, and catalog data from shared logs. Include the Spliff, Python, and FFmpeg versions and the steps to reproduce the issue.

Keep music reads separate from catalog writes. Test new behavior with temporary directories and check that original audio stays intact. See the [integration plan](INTEGRATIONS.md) before changing provider or analysis interfaces.
