import os
import json
import uuid
import time
import logging
from typing import Any, Dict, List, Optional, Literal

import requests
from fastmcp import FastMCP
from pydantic import BaseModel, Field


# ----------------------------
# Basic config
# ----------------------------

BASE_URL = os.getenv("SURVEYMONKEY_BASE_URL", "https://api.surveymonkey.com/v3")
ACCESS_TOKEN = os.getenv("SURVEYMONKEY_ACCESS_TOKEN")
REQUIRE_EXPLICIT_CONFIRMATION = os.getenv("REQUIRE_EXPLICIT_CONFIRMATION", "true").lower() == "true"

if not ACCESS_TOKEN:
    raise RuntimeError("Missing SURVEYMONKEY_ACCESS_TOKEN")

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("surveymonkey-mcp")

mcp = FastMCP("surveymonkey-response-toolkit")


# ----------------------------
# In-memory state
# For production: replace with Redis, Dataiku dataset, or managed folder JSON.
# ----------------------------

DRAFTS: Dict[str, Dict[str, Any]] = {}


# ----------------------------
# SurveyMonkey API client
# ----------------------------

class SurveyMonkeyClient:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        })

    def request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        url = f"{self.base_url}/{path.lstrip('/')}"
        response = self.session.request(
            method=method,
            url=url,
            params=params,
            json=json_body,
            timeout=60,
        )

        if response.status_code == 429:
            retry_after = int(response.headers.get("Retry-After", "2"))
            time.sleep(retry_after)
            response = self.session.request(
                method=method,
                url=url,
                params=params,
                json=json_body,
                timeout=60,
            )

        if not response.ok:
            raise RuntimeError(
                f"SurveyMonkey API error {response.status_code}: {response.text}"
            )

        return response.json() if response.text else {}


client = SurveyMonkeyClient(BASE_URL, ACCESS_TOKEN)


# ----------------------------
# Models
# ----------------------------

class SaveAnswerInput(BaseModel):
    draft_id: str
    question_id: str
    answer_text: Optional[str] = None
    choice_ids: Optional[List[str]] = None
    row_answers: Optional[List[Dict[str, str]]] = None


# ----------------------------
# Helpers
# ----------------------------

def normalize_survey(details: Dict[str, Any]) -> Dict[str, Any]:
    pages = []

    for page in details.get("pages", []):
        norm_page = {
            "id": page.get("id"),
            "title": page.get("title"),
            "questions": []
        }

        for q in page.get("questions", []):
            answers = q.get("answers", {}) or {}

            norm_q = {
                "id": q.get("id"),
                "heading": (q.get("headings") or [{}])[0].get("heading"),
                "family": q.get("family"),
                "subtype": q.get("subtype"),
                "required": q.get("required", False),
                "choices": [
                    {
                        "id": c.get("id"),
                        "text": c.get("text"),
                        "position": c.get("position")
                    }
                    for c in answers.get("choices", [])
                ],
                "rows": [
                    {
                        "id": r.get("id"),
                        "text": r.get("text"),
                        "position": r.get("position")
                    }
                    for r in answers.get("rows", [])
                ],
                "cols": [
                    {
                        "id": c.get("id"),
                        "text": c.get("text"),
                        "position": c.get("position")
                    }
                    for c in answers.get("cols", [])
                ],
            }

            norm_page["questions"].append(norm_q)

        pages.append(norm_page)

    return {
        "survey_id": details.get("id"),
        "title": details.get("title"),
        "pages": pages
    }


def find_question(draft: Dict[str, Any], question_id: str) -> Dict[str, Any]:
    for page in draft["survey"]["pages"]:
        for q in page["questions"]:
            if q["id"] == question_id:
                return q
    raise ValueError(f"Question ID not found: {question_id}")


def find_page_for_question(draft: Dict[str, Any], question_id: str) -> str:
    for page in draft["survey"]["pages"]:
        for q in page["questions"]:
            if q["id"] == question_id:
                return page["id"]
    raise ValueError(f"Page not found for question ID: {question_id}")


def validate_choice_ids(question: Dict[str, Any], choice_ids: List[str]) -> None:
    valid = {c["id"] for c in question.get("choices", [])}
    invalid = [c for c in choice_ids if c not in valid]
    if invalid:
        raise ValueError(f"Invalid choice_id(s) for question {question['id']}: {invalid}")


def build_question_answers(question: Dict[str, Any], answer: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Handles common SurveyMonkey answer patterns:
    - Open text / essay
    - Single choice
    - Multiple choice
    - Matrix/rating-style row + choice answers

    For unusual survey types, add handlers here.
    """
    family = question.get("family")
    subtype = question.get("subtype")

    if answer.get("answer_text") is not None:
        return [{"text": answer["answer_text"]}]

    if answer.get("choice_ids"):
        validate_choice_ids(question, answer["choice_ids"])
        return [{"choice_id": cid} for cid in answer["choice_ids"]]

    if answer.get("row_answers"):
        valid_rows = {r["id"] for r in question.get("rows", [])}
        valid_choices = {c["id"] for c in question.get("choices", [])} | {c["id"] for c in question.get("cols", [])}

        built = []
        for row_answer in answer["row_answers"]:
            row_id = row_answer.get("row_id")
            choice_id = row_answer.get("choice_id")

            if row_id not in valid_rows:
                raise ValueError(f"Invalid row_id for question {question['id']}: {row_id}")

            if choice_id not in valid_choices:
                raise ValueError(f"Invalid choice_id/col_id for question {question['id']}: {choice_id}")

            built.append({
                "row_id": row_id,
                "choice_id": choice_id
            })

        return built

    raise ValueError(
        f"No valid answer supplied for question {question['id']} "
        f"family={family}, subtype={subtype}"
    )


def build_submission_pages(draft: Dict[str, Any]) -> List[Dict[str, Any]]:
    pages_by_id: Dict[str, Dict[str, Any]] = {}

    for question_id, answer in draft["answers"].items():
        page_id = find_page_for_question(draft, question_id)
        question = find_question(draft, question_id)

        if page_id not in pages_by_id:
            pages_by_id[page_id] = {
                "id": page_id,
                "questions": []
            }

        pages_by_id[page_id]["questions"].append({
            "id": question_id,
            "answers": build_question_answers(question, answer)
        })

    return list(pages_by_id.values())


def get_missing_required_questions(draft: Dict[str, Any]) -> List[Dict[str, str]]:
    missing = []

    for page in draft["survey"]["pages"]:
        for q in page["questions"]:
            if q.get("required") and q["id"] not in draft["answers"]:
                missing.append({
                    "question_id": q["id"],
                    "heading": q.get("heading", "")
                })

    return missing


# ----------------------------
# MCP tools
# ----------------------------

@mcp.tool()
def list_surveys(page: int = 1, per_page: int = 50) -> Dict[str, Any]:
    """List surveys visible to the authenticated SurveyMonkey account."""
    return client.request(
        "GET",
        "/surveys",
        params={"page": page, "per_page": per_page},
    )


@mcp.tool()
def list_collectors(survey_id: str, page: int = 1, per_page: int = 50) -> Dict[str, Any]:
    """List collectors for a survey."""
    return client.request(
        "GET",
        f"/surveys/{survey_id}/collectors",
        params={"page": page, "per_page": per_page},
    )


@mcp.tool()
def load_survey(survey_id: str, collector_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Load and normalize a SurveyMonkey survey.
    Returns question IDs, choices, rows, and columns needed for answering.
    """
    details = client.request("GET", f"/surveys/{survey_id}/details")
    survey = normalize_survey(details)

    return {
        "survey": survey,
        "collector_id": collector_id,
        "agent_instruction": (
            "Use the returned page/question/choice/row/col IDs. "
            "Ask the user each question conversationally. "
            "Call start_response before saving answers."
        )
    }


@mcp.tool()
def start_response(
    survey_id: str,
    collector_id: str,
    respondent_label: Optional[str] = None,
    custom_variables: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """
    Start a local draft response.

    This does not submit anything to SurveyMonkey.
    """
    details = client.request("GET", f"/surveys/{survey_id}/details")
    survey = normalize_survey(details)

    draft_id = str(uuid.uuid4())

    DRAFTS[draft_id] = {
        "draft_id": draft_id,
        "survey_id": survey_id,
        "collector_id": collector_id,
        "respondent_label": respondent_label,
        "custom_variables": custom_variables or {},
        "survey": survey,
        "answers": {},
        "confirmed": False,
        "submitted": False,
        "response_id": None,
    }

    return {
        "draft_id": draft_id,
        "survey_title": survey["title"],
        "question_count": sum(len(p["questions"]) for p in survey["pages"]),
        "message": "Draft response started. Use save_answer for each question."
    }


@mcp.tool()
def save_answer(
    draft_id: str,
    question_id: str,
    answer_text: Optional[str] = None,
    choice_ids: Optional[List[str]] = None,
    row_answers: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, Any]:
    """
    Save one answer to the local draft.

    Use answer_text for free text.
    Use choice_ids for single/multiple choice.
    Use row_answers for matrix questions:
    [{"row_id": "...", "choice_id": "..."}]
    """
    if draft_id not in DRAFTS:
        raise ValueError(f"Unknown draft_id: {draft_id}")

    draft = DRAFTS[draft_id]
    question = find_question(draft, question_id)

    answer = {
        "answer_text": answer_text,
        "choice_ids": choice_ids,
        "row_answers": row_answers,
    }

    # Validate now, before storing.
    build_question_answers(question, answer)

    draft["answers"][question_id] = answer
    draft["confirmed"] = False

    return {
        "draft_id": draft_id,
        "question_id": question_id,
        "saved": True,
        "message": "Answer saved. Review response before submission."
    }


@mcp.tool()
def review_response(draft_id: str) -> Dict[str, Any]:
    """
    Review saved answers and identify missing required questions.
    """
    if draft_id not in DRAFTS:
        raise ValueError(f"Unknown draft_id: {draft_id}")

    draft = DRAFTS[draft_id]
    missing = get_missing_required_questions(draft)

    readable_answers = []

    for page in draft["survey"]["pages"]:
        for q in page["questions"]:
            qid = q["id"]
            if qid in draft["answers"]:
                readable_answers.append({
                    "question_id": qid,
                    "heading": q.get("heading"),
                    "answer": draft["answers"][qid],
                })

    return {
        "draft_id": draft_id,
        "survey_title": draft["survey"]["title"],
        "answers": readable_answers,
        "missing_required_questions": missing,
        "ready_to_submit": len(missing) == 0,
        "instruction": (
            "Show this review to the user. "
            "Only call submit_response if the user explicitly confirms."
        )
    }


@mcp.tool()
def submit_response(
    draft_id: str,
    user_confirmed: bool,
) -> Dict[str, Any]:
    """
    Submit the draft to SurveyMonkey.

    The agent must pass user_confirmed=True only after explicit user approval.
    """
    if draft_id not in DRAFTS:
        raise ValueError(f"Unknown draft_id: {draft_id}")

    if REQUIRE_EXPLICIT_CONFIRMATION and not user_confirmed:
        raise PermissionError("Explicit user confirmation is required before submission.")

    draft = DRAFTS[draft_id]

    if draft["submitted"]:
        return {
            "draft_id": draft_id,
            "submitted": True,
            "response_id": draft["response_id"],
            "message": "This draft was already submitted."
        }

    missing = get_missing_required_questions(draft)
    if missing:
        raise ValueError(f"Cannot submit. Missing required questions: {missing}")

    pages = build_submission_pages(draft)

    # Create an empty response.
    # Validate this endpoint against your collector type and SurveyMonkey plan.
    created = client.request(
        "POST",
        f"/collectors/{draft['collector_id']}/responses",
        json_body={
            "custom_variables": draft.get("custom_variables", {})
        }
    )

    response_id = created.get("id")
    if not response_id:
        raise RuntimeError(f"SurveyMonkey did not return a response id: {created}")

    payload = {
        "pages": pages,
        "custom_variables": draft.get("custom_variables", {})
    }

    # Submit response details.
    # SurveyMonkey documents response detail retrieval under:
    # /collectors/{collector_id}/responses/{response_id}/details
    submitted = client.request(
        "PUT",
        f"/collectors/{draft['collector_id']}/responses/{response_id}/details",
        json_body=payload,
    )

    draft["submitted"] = True
    draft["response_id"] = response_id

    return {
        "draft_id": draft_id,
        "submitted": True,
        "response_id": response_id,
        "survey_id": draft["survey_id"],
        "collector_id": draft["collector_id"],
        "submission_result": submitted,
    }


@mcp.tool()
def get_submitted_response(collector_id: str, response_id: str) -> Dict[str, Any]:
    """
    Retrieve submitted response details for audit/confirmation.
    """
    return client.request(
        "GET",
        f"/collectors/{collector_id}/responses/{response_id}/details",
    )


if __name__ == "__main__":
    mcp.run()
