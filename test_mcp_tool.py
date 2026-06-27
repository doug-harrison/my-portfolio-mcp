#!/usr/bin/env python
"""
Smoke test for the SurveyMonkey MCP tool.

Launches the server the same way Dataiku does (``python -m mcpsrv.mcp_portfolio_server``
over stdio, via a FastMCP client), then:

  1. lists the registered tools (proves the server starts and the MCP plumbing works),
  2. calls ``list_surveys`` (proves the access token / API connectivity works),
  3. calls ``load_survey`` for one survey and prints its questions, choices,
     rows and columns (the "list questions" validation you asked for).

Run it from the repository root, using the SAME code env Python that Dataiku uses,
so that ``mcpsrv`` resolves on the path:

    SURVEYMONKEY_ACCESS_TOKEN=xxxxx python test_mcp_tool.py
    SURVEYMONKEY_ACCESS_TOKEN=xxxxx python test_mcp_tool.py <survey_id>

If you omit <survey_id>, the first survey returned by list_surveys is used.

Exit code is 0 on success, non-zero on any failure (handy for CI / a Dataiku
scenario step).
"""

import os
import sys
import json
import asyncio
from typing import Any, Dict, List, Optional

from fastmcp import Client
from fastmcp.client.transports import StdioTransport


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def _payload(result: Any) -> Any:
    """
    Normalize a FastMCP call_tool result into plain Python data, tolerating
    differences across FastMCP versions (.data / .structured_content / text).
    """
    data = getattr(result, "data", None)
    if data is not None:
        return data
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        return structured
    content = getattr(result, "content", None)
    if content:
        block = content[0]
        text = getattr(block, "text", None)
        if text is not None:
            try:
                return json.loads(text)
            except (ValueError, TypeError):
                return text
    return result


def _print_questions(survey: Dict[str, Any]) -> int:
    total = 0
    print(f"\nSurvey: {survey.get('title')!r}  (id={survey.get('survey_id')})")
    for page in survey.get("pages", []):
        print(f"\n  Page {page.get('id')}: {page.get('title')!r}")
        for q in page.get("questions", []):
            total += 1
            req = " [required]" if q.get("required") else ""
            print(
                f"    - Q {q.get('id')}{req}  "
                f"({q.get('family')}/{q.get('subtype')})\n"
                f"      {q.get('heading')!r}"
            )
            for c in q.get("choices", []):
                print(f"        choice {c.get('id')}: {c.get('text')!r}")
            for r in q.get("rows", []):
                print(f"        row    {r.get('id')}: {r.get('text')!r}")
            for c in q.get("cols", []):
                print(f"        col    {c.get('id')}: {c.get('text')!r}")
            other = q.get("other")
            if other and other.get("id"):
                print(f"        other  {other.get('id')}: {other.get('text')!r}")
    print(f"\n  -> {total} question(s) total.")
    return total


# ----------------------------------------------------------------------
# Main test
# ----------------------------------------------------------------------
async def run(survey_id: Optional[str]) -> int:
    if not os.getenv("SURVEYMONKEY_ACCESS_TOKEN"):
        print("ERROR: SURVEYMONKEY_ACCESS_TOKEN is not set.", file=sys.stderr)
        return 2

    # Launch the server exactly like Dataiku: python -m mcpsrv.mcp_portfolio_server.
    # Pass the current environment through so the token / base URL reach the child.
    transport = StdioTransport(
        command=sys.executable,
        args=["-m", "mcpsrv.mcp_portfolio_server"],
        env=dict(os.environ),
    )

    async with Client(transport) as client:
        # 1) Tools registered?
        tools = await client.list_tools()
        names = sorted(t.name for t in tools)
        print(f"[1/3] Connected. {len(names)} tool(s) registered:")
        print("      " + ", ".join(names))

        expected = {
            "list_surveys", "list_collectors", "load_survey", "start_response",
            "save_answer", "review_response", "submit_response",
            "get_submitted_response",
        }
        missing = expected - set(names)
        if missing:
            print(f"ERROR: expected tools missing: {sorted(missing)}", file=sys.stderr)
            return 3

        # 2) Can we reach the SurveyMonkey API?
        print("\n[2/3] Calling list_surveys ...")
        surveys_result = _payload(await client.call_tool("list_surveys", {"per_page": 50}))
        data: List[Dict[str, Any]] = (surveys_result or {}).get("data", [])
        print(f"      Found {len(data)} survey(s).")
        for s in data[:10]:
            print(f"        - {s.get('id')}: {s.get('title')!r}")
        if not data and not survey_id:
            print("ERROR: no surveys returned and no survey_id supplied.", file=sys.stderr)
            return 4

        target = survey_id or data[0]["id"]

        # 3) Load that survey and list its questions.
        print(f"\n[3/3] Calling load_survey for survey_id={target} ...")
        loaded = _payload(await client.call_tool("load_survey", {"survey_id": target}))
        survey = (loaded or {}).get("survey")
        if not survey:
            print(f"ERROR: load_survey returned no survey: {loaded}", file=sys.stderr)
            return 5

        count = _print_questions(survey)
        if count == 0:
            print("WARNING: survey loaded but contains no questions.", file=sys.stderr)

    print("\nSUCCESS: MCP tool is responding correctly.")
    return 0


def main() -> None:
    survey_id = sys.argv[1] if len(sys.argv) > 1 else None
    sys.exit(asyncio.run(run(survey_id)))


if __name__ == "__main__":
    main()
