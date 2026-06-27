import os
import json
import uuid
import time
import logging
from typing import Any, Dict, List, Optional

import requests
from fastmcp import FastMCP

# ----------------------------
# Basic config
# ----------------------------
BASE_URL = os.getenv("SURVEYMONKEY_BASE_URL", "https://api.surveymonkey.com/v3")
ACCESS_TOKEN = os.getenv("SURVEYMONKEY_ACCESS_TOKEN")
REQUIRE_EXPLICIT_CONFIRMATION = (
    os.getenv("REQUIRE_EXPLICIT_CONFIRMATION", "true").lower() == "true"
)
# Optional: directory for persisting drafts so they survive process restarts /
# multiple workers (e.g. a Dataiku managed folder path). If unset, drafts are
# kept in memory only.
DRAFT_DIR = os.getenv("SURVEYMONKEY_DRAFT_DIR")

if not ACCESS_TOKEN:
    raise RuntimeError("Missing SURVEYMONKEY_ACCESS_TOKEN")

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("surveymonkey-mcp")

mcp = FastMCP("surveymonkey-response-toolkit")


# ----------------------------
# Draft store
# In-memory by default; optionally persisted to JSON files on disk so that
# a draft_id created in one process/worker is usable in another.
# ----------------------------
class DraftStore:
    def __init__(self, directory: Optional[str] = None):
        self.directory = directory
        self._mem: Dict[str, Dict[str, Any]] = {}
        if self.directory:
            os.makedirs(self.directory, exist_ok=True)

    def _path(self, draft_id: str) -> str:
        return os.path.join(self.directory, f"{draft_id}.json")

    def __contains__(self, draft_id: str) -> bool:
        if draft_id in self._mem:
            return True
        if self.directory:
            return os.path.exists(self._path(draft_id))
        return False

    def get(self, draft_id: str) -> Dict[str, Any]:
        if draft_id in self._mem:
            return self._mem[draft_id]
        if self.directory and os.path.exists(self._path(draft_id)):
            with open(self._path(draft_id), "r", encoding="utf-8") as fh:
                draft = json.load(fh)
            self._mem[draft_id] = draft
            return draft
        raise ValueError(f"Unknown draft_id: {draft_id}")

    def save(self, draft: Dict[str, Any]) -> None:
        draft_id = draft["draft_id"]
        self._mem[draft_id] = draft
        if self.directory:
            tmp = self._path(draft_id) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(draft, fh)
            os.replace(tmp, self._path(draft_id))


DRAFTS = DraftStore(DRAFT_DIR)


# ----------------------------
# SurveyMonkey API client
# ----------------------------
class SurveyMonkeyClient:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )

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
            logger.warning("Rate limited by SurveyMonkey; retrying in %ss", retry_after)
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
# Helpers
# ----------------------------
def normalize_survey(details: Dict[str, Any]) -> Dict[str, Any]:
    pages = []
    for page in details.get("pages", []):
        norm_page = {
            "id": page.get("id"),
            "title": page.get("title"),
            "questions": [],
        }
        for q in page.get("questions", []):
            answers = q.get("answers", {}) or {}
            norm_q = {
                "id": q.get("id"),
                "heading": (q.get("headings") or [{}])[0].get("heading"),
                "family": q.get("family"),
                "subtype": q.get("subtype"),
                "required": bool(q.get("required", False)),
                "choices": [
                    {"id": c.get("id"), "text": c.get("text"), "position": c.get("position")}
                    for c in answers.get("choices", [])
                ],
                "other": (
                    {"id": answers["other"].get("id"), "text": answers["other"].get("text")}
                    if isinstance(answers.get("other"), dict)
                    else None
                ),
                "rows": [
                    {"id": r.get("id"), "text": r.get("text"), "position": r.get("position")}
                    for r in answers.get("rows", [])
                ],
                "cols": [
                    {"id": c.get("id"), "text": c.get("text"), "position": c.get("position")}
                    for c in answers.get("cols", [])
                ],
            }
            norm_page["questions"].append(norm_q)
        pages.append(norm_page)
    return {
        "survey_id": details.get("id"),
        "title": details.get("title"),
        "pages": pages,
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
    other = question.get("other")
    if other and other.get("id"):
        valid.add(other["id"])
    invalid = [c for c in choice_ids if c not in valid]
    if invalid:
        raise ValueError(
            f"Invalid choice_id(s) for question {question['id']}: {invalid}"
        )


def _resolve(
    items: List[Dict[str, Any]],
    value: str,
    kind: str,
    question_id: str,
    extra: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Resolve a value (which may be an ID *or* the human-readable text/label of a
    choice/row/col) to its ID. Matching is: exact ID first, then case-insensitive
    text. ``extra`` allows the "other" choice to be matched too.

    Raises a helpful error listing the valid options if there is no unique match,
    so the calling agent can self-correct.
    """
    valid_ids = {i["id"] for i in items}
    if extra and extra.get("id"):
        valid_ids.add(extra["id"])
    if value in valid_ids:
        return value

    norm = str(value).strip().lower()
    matches = [i["id"] for i in items if (i.get("text") or "").strip().lower() == norm]
    if extra and extra.get("id"):
        if (extra.get("text") or "").strip().lower() == norm or norm in (
            "other",
            "other (please specify)",
        ):
            matches.append(extra["id"])

    options = ", ".join(f"{(i.get('text') or '')!r}={i['id']}" for i in items)
    if extra and extra.get("id"):
        options += f", 'other'={extra['id']}"
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ValueError(
            f"Could not match {kind} {value!r} for question {question_id}. "
            f"Valid options: {options}"
        )
    raise ValueError(
        f"Ambiguous {kind} {value!r} for question {question_id} (matched several). "
        f"Pass the exact ID instead. Options: {options}"
    )


def normalize_answer(question: Dict[str, Any], raw: Dict[str, Any]) -> Dict[str, Any]:
    """
    Turn a "raw" answer (which may use IDs *or* human-readable labels) into the
    canonical, ID-based answer dict that ``build_question_answers`` consumes.

    Accepted label fields (in addition to the ID fields):
      - choice_labels: list of choice texts (resolved alongside choice_ids).
      - row_answers items may use row / col / choice (texts) as well as
        row_id / col_id / choice_id.
    """
    qid = question["id"]
    other = question.get("other")

    choice_ids = list(raw.get("choice_ids") or [])
    for label in raw.get("choice_labels") or []:
        choice_ids.append(_resolve(question.get("choices", []), label, "choice", qid, other))

    rows_out: List[Dict[str, Any]] = []
    for ra in raw.get("row_answers") or []:
        item: Dict[str, Any] = {}
        rv = ra.get("row_id") if ra.get("row_id") is not None else ra.get("row")
        if rv is not None:
            item["row_id"] = _resolve(question.get("rows", []), rv, "row", qid)
        cv = ra.get("col_id") if ra.get("col_id") is not None else ra.get("col")
        if cv is not None:
            item["col_id"] = _resolve(question.get("cols", []), cv, "col", qid)
        chv = ra.get("choice_id") if ra.get("choice_id") is not None else ra.get("choice")
        if chv is not None:
            item["choice_id"] = _resolve(question.get("choices", []), chv, "choice", qid)
        if ra.get("text") is not None:
            item["text"] = ra["text"]
        rows_out.append(item)

    return {
        "answer_text": raw.get("answer_text"),
        "choice_ids": choice_ids or None,
        "row_answers": rows_out or None,
        "other_id": raw.get("other_id"),
        "other_text": raw.get("other_text"),
    }


def _store_answer(draft: Dict[str, Any], raw: Dict[str, Any]) -> Dict[str, Any]:
    """
    Resolve + validate + store one answer on the draft (in memory only; the
    caller is responsible for persisting via DRAFTS.save). Returns the canonical
    answer that was stored.
    """
    qid = raw.get("question_id")
    if not qid:
        raise ValueError("Each answer must include a 'question_id'.")
    question = find_question(draft, qid)
    normalized = normalize_answer(question, raw)
    build_question_answers(question, normalized)  # validate before storing
    draft["answers"][qid] = normalized
    draft["confirmed"] = False
    return normalized


def build_question_answers(
    question: Dict[str, Any], answer: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """
    Build the SurveyMonkey ``answers`` array for one question.

    Supported answer shapes (matching the SurveyMonkey POST /responses schema):
      - Open text / essay / slider:           answer_text
      - Single / multiple choice:             choice_ids  (may include the "other" id)
      - "Other" write-in text:                other_id + other_text
      - Matrix single / rating / ranking:     row_answers = [{"row_id", "choice_id"}]
      - Matrix menu:                          row_answers = [{"row_id", "col_id", "choice_id"}]
      - Matrix open-ended (per-row text):     row_answers = [{"row_id", "text"}]

    row_answers entries may freely combine row_id / col_id / choice_id / text;
    each present field is validated against the survey definition.
    """
    family = question.get("family")
    subtype = question.get("subtype")

    answers: List[Dict[str, Any]] = []

    # Plain open-ended text.
    if answer.get("answer_text") is not None:
        answers.append({"text": answer["answer_text"]})

    # Single / multiple choice selections.
    if answer.get("choice_ids"):
        validate_choice_ids(question, answer["choice_ids"])
        answers.extend({"choice_id": cid} for cid in answer["choice_ids"])

    # "Other" write-in (e.g. an "Other (please specify)" choice with free text).
    if answer.get("other_text") is not None:
        other = question.get("other")
        other_id = answer.get("other_id") or (other.get("id") if other else None)
        if not other_id:
            raise ValueError(
                f"other_text supplied but question {question['id']} has no 'other' choice"
            )
        answers.append({"other_id": other_id, "text": answer["other_text"]})

    # Matrix / per-row answers.
    if answer.get("row_answers"):
        valid_rows = {r["id"] for r in question.get("rows", [])}
        valid_cols = {c["id"] for c in question.get("cols", [])}
        valid_choices = {c["id"] for c in question.get("choices", [])}
        for row_answer in answer["row_answers"]:
            row_id = row_answer.get("row_id")
            col_id = row_answer.get("col_id")
            choice_id = row_answer.get("choice_id")
            text = row_answer.get("text")

            if row_id is None or row_id not in valid_rows:
                raise ValueError(
                    f"Invalid/missing row_id for question {question['id']}: {row_id}"
                )
            if col_id is not None and col_id not in valid_cols:
                raise ValueError(
                    f"Invalid col_id for question {question['id']}: {col_id}"
                )
            if choice_id is not None and choice_id not in valid_choices:
                raise ValueError(
                    f"Invalid choice_id for question {question['id']}: {choice_id}"
                )

            built: Dict[str, Any] = {"row_id": row_id}
            if col_id is not None:
                built["col_id"] = col_id
            if choice_id is not None:
                built["choice_id"] = choice_id
            if text is not None:
                built["text"] = text
            answers.append(built)

    if not answers:
        raise ValueError(
            f"No valid answer supplied for question {question['id']} "
            f"family={family}, subtype={subtype}"
        )
    return answers


def build_submission_pages(draft: Dict[str, Any]) -> List[Dict[str, Any]]:
    pages_by_id: Dict[str, Dict[str, Any]] = {}
    for question_id, answer in draft["answers"].items():
        page_id = find_page_for_question(draft, question_id)
        question = find_question(draft, question_id)
        if page_id not in pages_by_id:
            pages_by_id[page_id] = {"id": page_id, "questions": []}
        pages_by_id[page_id]["questions"].append(
            {"id": question_id, "answers": build_question_answers(question, answer)}
        )
    return list(pages_by_id.values())


def get_missing_required_questions(draft: Dict[str, Any]) -> List[Dict[str, str]]:
    missing = []
    for page in draft["survey"]["pages"]:
        for q in page["questions"]:
            if q.get("required") and q["id"] not in draft["answers"]:
                missing.append(
                    {"question_id": q["id"], "heading": q.get("heading", "")}
                )
    return missing


# ----------------------------
# MCP tools
# ----------------------------
@mcp.tool()
def list_surveys(page: int = 1, per_page: int = 50) -> Dict[str, Any]:
    """List surveys visible to the authenticated SurveyMonkey account."""
    return client.request(
        "GET", "/surveys", params={"page": page, "per_page": per_page}
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
    Returns question IDs, choices, rows, columns, and any "other" choice IDs
    needed for answering.
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
        ),
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
    draft = {
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
    DRAFTS.save(draft)

    return {
        "draft_id": draft_id,
        "survey_title": survey["title"],
        "question_count": sum(len(p["questions"]) for p in survey["pages"]),
        "message": "Draft response started. Use save_answer for each question.",
    }


@mcp.tool()
def save_answer(
    draft_id: str,
    question_id: str,
    answer_text: Optional[str] = None,
    choice_ids: Optional[List[str]] = None,
    choice_labels: Optional[List[str]] = None,
    row_answers: Optional[List[Dict[str, str]]] = None,
    other_id: Optional[str] = None,
    other_text: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Save ONE answer to the local draft.

    You may answer using IDs or the human-readable text/labels from load_survey
    (the server resolves labels to IDs for you):

    - answer_text: free text (open ended / essay / slider / numerical / date).
    - choice_ids: single/multiple choice selections by ID.
    - choice_labels: single/multiple choice selections by their text (e.g.
      ["Strongly agree"]) — resolved to IDs automatically.
    - other_text (+ optional other_id): the "Other (please specify)" write-in.
    - row_answers: matrix questions. Each item may use IDs or labels:
        matrix single/rating: [{"row": "Speed", "choice": "Good"}]
                           or [{"row_id": "...", "choice_id": "..."}]
        matrix menu:          [{"row": "...", "col": "...", "choice": "..."}]
        matrix open-ended:    [{"row": "...", "text": "..."}]

    To save many answers at once, prefer save_answers.
    """
    draft = DRAFTS.get(draft_id)
    _store_answer(
        draft,
        {
            "question_id": question_id,
            "answer_text": answer_text,
            "choice_ids": choice_ids,
            "choice_labels": choice_labels,
            "row_answers": row_answers,
            "other_id": other_id,
            "other_text": other_text,
        },
    )
    DRAFTS.save(draft)

    return {
        "draft_id": draft_id,
        "question_id": question_id,
        "saved": True,
        "message": "Answer saved. Review response before submission.",
    }


@mcp.tool()
def save_answers(draft_id: str, answers: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Save MANY answers in one call (preferred over repeated save_answer).

    Each item in ``answers`` is an object with a ``question_id`` plus the same
    fields save_answer accepts (answer_text / choice_ids / choice_labels /
    row_answers / other_id / other_text). IDs or human-readable labels both work.

    Example:
        save_answers(draft_id, answers=[
            {"question_id": "111", "answer_text": "Jane Doe"},
            {"question_id": "222", "choice_labels": ["Yes"]},
            {"question_id": "333", "row_answers": [
                {"row": "Support", "choice": "Excellent"},
                {"row": "Price",   "choice": "Fair"}
            ]}
        ])

    Each answer is validated independently: valid ones are saved even if others
    fail, and the result lists per-question success/error plus which required
    questions are still missing — so you can fix and re-send only the failures.
    """
    draft = DRAFTS.get(draft_id)

    results: List[Dict[str, Any]] = []
    saved = 0
    for raw in answers or []:
        qid = raw.get("question_id")
        try:
            _store_answer(draft, raw)
            saved += 1
            results.append({"question_id": qid, "saved": True})
        except Exception as exc:  # surface a fixable error per question
            results.append({"question_id": qid, "saved": False, "error": str(exc)})

    DRAFTS.save(draft)
    missing = get_missing_required_questions(draft)

    return {
        "draft_id": draft_id,
        "saved_count": saved,
        "total": len(answers or []),
        "results": results,
        "missing_required_questions": missing,
        "ready_to_submit": len(missing) == 0,
        "message": (
            "Saved answers. Review failures (if any) and re-send only those, "
            "then call review_response before submitting."
        ),
    }


@mcp.tool()
def review_response(draft_id: str) -> Dict[str, Any]:
    """Review saved answers and identify missing required questions."""
    draft = DRAFTS.get(draft_id)
    missing = get_missing_required_questions(draft)

    readable_answers = []
    for page in draft["survey"]["pages"]:
        for q in page["questions"]:
            qid = q["id"]
            if qid in draft["answers"]:
                readable_answers.append(
                    {
                        "question_id": qid,
                        "heading": q.get("heading"),
                        "answer": draft["answers"][qid],
                    }
                )

    return {
        "draft_id": draft_id,
        "survey_title": draft["survey"]["title"],
        "answers": readable_answers,
        "missing_required_questions": missing,
        "ready_to_submit": len(missing) == 0,
        "instruction": (
            "Show this review to the user. "
            "Only call submit_response if the user explicitly confirms."
        ),
    }


@mcp.tool()
def submit_response(draft_id: str, user_confirmed: bool) -> Dict[str, Any]:
    """
    Submit the draft to SurveyMonkey in a single POST.
    The agent must pass user_confirmed=True only after explicit user approval.
    """
    if REQUIRE_EXPLICIT_CONFIRMATION and not user_confirmed:
        raise PermissionError(
            "Explicit user confirmation is required before submission."
        )

    draft = DRAFTS.get(draft_id)

    if draft["submitted"]:
        return {
            "draft_id": draft_id,
            "submitted": True,
            "response_id": draft["response_id"],
            "message": "This draft was already submitted.",
        }

    missing = get_missing_required_questions(draft)
    if missing:
        raise ValueError(f"Cannot submit. Missing required questions: {missing}")

    pages = build_submission_pages(draft)

    # SurveyMonkey accepts the full response (pages + status) in a single POST to
    # the collector. There is no PUT on /responses/{id}/details (GET only), so we
    # do NOT split this into create-then-update.
    payload = {
        "response_status": "completed",
        "pages": pages,
        "custom_variables": draft.get("custom_variables", {}),
    }
    created = client.request(
        "POST",
        f"/collectors/{draft['collector_id']}/responses",
        json_body=payload,
    )

    response_id = created.get("id")
    if not response_id:
        raise RuntimeError(
            f"SurveyMonkey did not return a response id: {created}"
        )

    draft["submitted"] = True
    draft["response_id"] = response_id
    DRAFTS.save(draft)

    return {
        "draft_id": draft_id,
        "submitted": True,
        "response_id": response_id,
        "survey_id": draft["survey_id"],
        "collector_id": draft["collector_id"],
        "submission_result": created,
    }


@mcp.tool()
def get_submitted_response(collector_id: str, response_id: str) -> Dict[str, Any]:
    """Retrieve submitted response details for audit/confirmation."""
    return client.request(
        "GET",
        f"/collectors/{collector_id}/responses/{response_id}/details",
    )


def main() -> None:
    """Console-script / ``python -m`` entry point."""
    mcp.run()


if __name__ == "__main__":
    main()
