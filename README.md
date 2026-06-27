# my-portfolio-mcp

A FastMCP server (SurveyMonkey response toolkit) packaged so it installs into a
Dataiku code env's `site-packages`, which is both readable by the impersonated
run-as user and on `PYTHONPATH`.

## Layout

```
my-portfolio-mcp/
├── pyproject.toml
├── README.md
└── mcpsrv/
    ├── __init__.py
    └── mcp_portfolio_server.py    # the one and only server module
```

> Note: there is a single server module. (A previous version shipped two
> divergent copies — `mcp_portfolio_server.py` and `surveymonkey_mcp.py` — which
> caused Dataiku to run the older, buggy copy. They have been consolidated.)

## Required environment variables

| Variable | Required | Purpose |
| --- | --- | --- |
| `SURVEYMONKEY_ACCESS_TOKEN` | yes | Bearer token; the server refuses to start without it. |
| `SURVEYMONKEY_BASE_URL` | no | Defaults to `https://api.surveymonkey.com/v3`. |
| `REQUIRE_EXPLICIT_CONFIRMATION` | no | Defaults to `true`; `submit_response` then requires `user_confirmed=True`. |
| `SURVEYMONKEY_DRAFT_DIR` | no | Directory (e.g. a Dataiku managed-folder path) for persisting drafts to JSON so a `draft_id` survives process restarts / multiple workers. If unset, drafts are in-memory only. |
| `LOG_LEVEL` | no | Defaults to `INFO`. |

Set these on the Dataiku code env / agent tool, not in the repo.

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

Because the package is now in `site-packages`, `-m` resolves it and the file is
readable — clearing both the `ModuleNotFoundError` and the `Errno 13` permission
error.

Equivalently, the installed console script `mcp-portfolio-server` runs the same
`main()` entry point.

## Local sanity check (optional)

```
pip install -e .
SURVEYMONKEY_ACCESS_TOKEN=xxx python -m mcpsrv.mcp_portfolio_server
# should start and wait on stdin; Ctrl-C to exit
```

## Tools exposed

`list_surveys`, `list_collectors`, `load_survey`, `start_response`,
`save_answer`, `review_response`, `submit_response`, `get_submitted_response`.

The flow is draft-based: `start_response` → `save_answer` (per question) →
`review_response` → `submit_response(user_confirmed=True)`. Submission is a single
`POST /collectors/{id}/responses` with the full `pages` payload and
`response_status="completed"`.

## Adding more tools

Add more `@mcp.tool()`-decorated functions in `mcp_portfolio_server.py`. Use
absolute paths or package-relative imports inside them — the tool subprocess runs
from a throwaway temp working directory, so bare relative paths will break.
