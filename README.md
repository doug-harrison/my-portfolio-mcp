# my-portfolio-mcp

A FastMCP server packaged so it installs into a Dataiku code env's `site-packages`,
which is both readable by the impersonated run-as user and on `PYTHONPATH`.

## Layout

```
my_portfolio_mcp/
├── pyproject.toml
├── README.md
└── mcpsrv/
    ├── __init__.py
    └── mcp_portfolio_server.py
```

## Install into the Dataiku code env

1. Push this folder to a git repo your Dataiku instance can reach.
2. In Dataiku: **Administration → Code envs → DJH_Local_MCP → Packages to install**,
   add one line:

   ```
   git+https://YOUR-GIT-HOST/you/my-portfolio-mcp.git
   ```

   (Or build a wheel — `python -m build` — and reference that instead.)
3. **Update / rebuild** the code env.

## Configure the Dataiku agent tool

- **command:** `python`
- **args (two separate entries):**
  - `-m`
  - `mcpsrv.mcp_portfolio_server`

That's it. Because the package is now in `site-packages`, `-m` resolves it and the
file is readable — clearing both the `ModuleNotFoundError` and the `Errno 13`
permission error.

## Local sanity check (optional)

```
pip install -e .
python -m mcpsrv.mcp_portfolio_server   # should start and wait on stdin; Ctrl-C to exit
```

## Adding more tools

Add more `@mcp.tool()`-decorated functions in `mcp_portfolio_server.py`. Use
absolute paths or package-relative imports inside them — the tool subprocess runs
from a throwaway temp working directory, so bare relative paths will break.
