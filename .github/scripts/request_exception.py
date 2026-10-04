"""
request_exception.py

Triggered by a PR comment like:

    /request-exception We need this in prod for the Q4 launch, will fix by Nov 15

Flow:
  1. Parse the reason out of the comment.
  2. Find open findings for this repo (POST /api/findings).
  3. Look for a reusable exception (POST /api/risk-register/assignable).
     If none, create one (POST /api/risk-register).
  4. Attach the findings to the exception
     (PUT /user/findings/v2/bulk/change-basic-details, APPEND mode, async job).
  5. Post a confirmation comment on the PR.

All endpoints and body shapes below come from the ArmorCode Swagger/OpenAPI
document (not from code searches). Items marked VERIFY are the parts the
Swagger does not show a worked example for.
"""

import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

ARMORCODE_BASE_URL = os.environ["ARMORCODE_BASE_URL"].rstrip("/")
ARMORCODE_API_TOKEN = os.environ["ARMORCODE_API_TOKEN"]
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
PR_NUMBER = os.environ["PR_NUMBER"]
REPO = os.environ["REPO"]  # e.g. "wayfair/ph-inbound-order-terraform"
COMMENT_BODY = os.environ["COMMENT_BODY"]
COMMENT_AUTHOR = os.environ["COMMENT_AUTHOR"]

# Optional tuning via workflow env / repo variables
EXCEPTION_DAYS = int(os.environ.get("EXCEPTION_DAYS", "30"))
EXCEPTION_ENVIRONMENT = os.environ.get("EXCEPTION_ENVIRONMENT", "")  # e.g. "Production"

REPO_NAME = REPO.split("/")[-1]

armorcode_headers = {
    "Authorization": f"Bearer {ARMORCODE_API_TOKEN}",
    "Content-Type": "application/json",
}
github_headers = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
}


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_reason(comment_body: str) -> str:
    match = re.match(r"^/request-exception\s+(.*)$", comment_body.strip(), re.DOTALL)
    if not match or not match.group(1).strip():
        return "No reason provided by requester; follow up before approving."
    return match.group(1).strip()


def product_id_of(finding: dict):
    p = finding.get("product")
    if isinstance(p, dict) and p.get("id"):
        return p["id"]
    return finding.get("apId") or finding.get("productId")


def subproduct_id_of(finding: dict):
    sp = finding.get("subProduct")
    if isinstance(sp, dict) and sp.get("id"):
        return sp["id"]
    return finding.get("aspId")


def find_findings_for_repo(repo_name: str) -> list[dict]:
    """
    POST /api/findings?size=N&afterKey=K
    Body: {"filters": {...}, "maxSize": N}
    Pagination is cursor based: keep going while pagination.hasMore is true.
    Filter key "repositoryName" is listed in the Swagger filter enum.
    VERIFY: response shape is taken from the Swagger example:
            data.findings[] and data.pagination.{afterKey, hasMore}.
    """
    findings: list[dict] = []
    after_key = None
    for _ in range(10):  # safety cap on pages
        params = {"size": 1000}
        if after_key not in (None, -1):
            params["afterKey"] = after_key
        body = {
            "filters": {
                "repositoryName": [repo_name],
                "status": ["OPEN", "CONFIRMED"],
            },
            "maxSize": 1000,
        }
        resp = requests.post(
            f"{ARMORCODE_BASE_URL}/api/findings",
            headers=armorcode_headers,
            params=params,
            json=body,
        )
        resp.raise_for_status()
        payload = resp.json()
        data = payload.get("data", payload)
        batch = data.get("findings") or data.get("content") or []
        findings.extend(batch)
        pagination = data.get("pagination", {})
        after_key = pagination.get("afterKey", -1)
        if not batch or not pagination.get("hasMore") or after_key in (None, -1):
            break
    return findings


def find_assignable_exception(finding_ids: list[int]) -> dict | None:
    """
    POST /api/risk-register/assignable
    Returns {"data": [{"id": ..., "name": ...}], "success": true}.
    Swagger examples filter findings by "id_term".
    """
    body = {"findingFilterRequest": {"filters": {"id_term": finding_ids}}}
    resp = requests.post(
        f"{ARMORCODE_BASE_URL}/api/risk-register/assignable",
        headers=armorcode_headers,
        json=body,
    )
    resp.raise_for_status()
    candidates = resp.json().get("data") or []
    return candidates[0] if candidates else None


def create_exception(product_id: int, subproduct_ids: list[int], reason: str) -> dict:
    """
    POST /api/risk-register
    Required by the Swagger example: name, description, startDate, endDate,
    reasons, scope.productId. Name must be unique, so include PR number + time.
    New exceptions start in DRAFT and need approval in ArmorCode.
    """
    now = datetime.now(timezone.utc)
    scope = {"productId": product_id}
    if subproduct_ids:
        scope["subProductIds"] = subproduct_ids
    if EXCEPTION_ENVIRONMENT:
        scope["environmentName"] = EXCEPTION_ENVIRONMENT
    body = {
        "name": f"PR exception request: {REPO_NAME} PR {PR_NUMBER} ({now:%Y%m%d%H%M})",
        "description": f"{reason}\n\nRequested by {COMMENT_AUTHOR} via GitHub PR #{PR_NUMBER} in {REPO}.",
        "startDate": iso(now),
        "endDate": iso(now + timedelta(days=EXCEPTION_DAYS)),
        "reasons": ["Business Risk Accepted"],
        "scope": scope,
    }
    resp = requests.post(
        f"{ARMORCODE_BASE_URL}/api/risk-register", headers=armorcode_headers, json=body
    )
    resp.raise_for_status()
    return resp.json()


def append_findings_to_exception(finding_ids: list[int], exception_id: int) -> str | None:
    """
    PUT /user/findings/v2/bulk/change-basic-details  (async job)
    BulkFindingUpdateStringRequest supports riskRegister + riskRegisterUpdateMode.
    findingFilterRequest is required by the schema; the Swagger example passes
    an empty object together with findingIds.
    VERIFY: the Swagger has no worked example that sets riskRegister here.
    Returns the job id if the response carries one.
    """
    body = {
        "findingIds": finding_ids,
        "riskRegister": [exception_id],
        "riskRegisterUpdateMode": "APPEND",
        "notes": f"Added via GitHub PR #{PR_NUMBER} by {COMMENT_AUTHOR}",
        "findingFilterRequest": {},
    }
    resp = requests.put(
        f"{ARMORCODE_BASE_URL}/user/findings/v2/bulk/change-basic-details",
        headers=armorcode_headers,
        json=body,
    )
    resp.raise_for_status()
    data = resp.json().get("data") or {}
    return data.get("jobId")


def wait_for_job(job_id: str | None) -> str:
    """GET /api/v2/jobs/{referenceId}: PENDING, IN_PROGRESS, COMPLETED, FAILED."""
    if not job_id:
        return "submitted"
    status = "PENDING"
    for _ in range(12):  # about 60 seconds
        resp = requests.get(
            f"{ARMORCODE_BASE_URL}/api/v2/jobs/{job_id}", headers=armorcode_headers
        )
        if resp.status_code != 200:
            break
        status = (resp.json().get("data") or {}).get("status", status)
        if status in ("COMPLETED", "FAILED"):
            break
        time.sleep(5)
    return status


def post_pr_comment(message: str) -> None:
    url = f"https://api.github.com/repos/{REPO}/issues/{PR_NUMBER}/comments"
    resp = requests.post(url, headers=github_headers, json={"body": message})
    resp.raise_for_status()


def main() -> None:
    reason = parse_reason(COMMENT_BODY)

    findings = find_findings_for_repo(REPO_NAME)
    if not findings:
        post_pr_comment(
            "Could not find any open ArmorCode findings for this repository. "
            "If this seems wrong, check that the repo is connected to ArmorCode."
        )
        return

    # An exception is scoped to one product. Use the first finding's product
    # and only attach findings that belong to it.
    product_id = product_id_of(findings[0])
    if not product_id:
        raise RuntimeError("Could not read a product ID from the findings response.")
    findings = [f for f in findings if product_id_of(f) == product_id]

    finding_ids = [f["id"] for f in findings]
    subproduct_ids = sorted({sid for f in findings if (sid := subproduct_id_of(f))})

    existing = find_assignable_exception(finding_ids)
    if existing:
        exception_id, exception_name = existing["id"], existing.get("name", "")
        action_taken = "added to an existing exception"
    else:
        created = create_exception(product_id, subproduct_ids, reason)
        data = created.get("data", created)
        exception_id, exception_name = data["id"], data.get("name", "")
        action_taken = "filed under a new exception (Draft)"

    job_status = wait_for_job(append_findings_to_exception(finding_ids, exception_id))

    post_pr_comment(
        f"**Exception requested** by @{COMMENT_AUTHOR}\n\n"
        f"- {len(finding_ids)} finding(s) {action_taken}: {exception_name} (ID {exception_id})\n"
        f"- Reason: {reason}\n"
        f"- Attach status: {job_status}\n"
        f"- Next step: security reviews and approves in ArmorCode under Exceptions.\n\n"
        f"This does not unblock anything automatically."
    )


if __name__ == "__main__":
    try:
        main()
    except requests.HTTPError as e:
        print(f"API call failed: {e}", file=sys.stderr)
        print(f"Response body: {e.response.text}", file=sys.stderr)
        sys.exit(1)
