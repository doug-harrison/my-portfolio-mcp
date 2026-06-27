"""Portfolio MCP server.

Run modes (all start the same stdio server):
  python -m mcpsrv.mcp_portfolio_server      # what the Dataiku tool uses
  mcp-portfolio-server                       # console-script entry point
"""

from fastmcp import FastMCP

mcp = FastMCP("portfolio")


@mcp.tool()
def get_portfolio_summary(account_id: str) -> dict:
    """Return a summary for the given account."""
    # ... your logic ...
    return {"account_id": account_id, "value": 0}


# add your other @mcp.tool() functions here


def main() -> None:
    """Entry point. stdio is the default transport; DSS talks over stdin/stdout."""
    mcp.run()


if __name__ == "__main__":
    main()
