"""Narrow, dependency-preserving search request adapter and provenance helpers."""
from copy import deepcopy
import hashlib
import json

SEARCH_METHOD = "xhs-skill-sort-filter-aligned-v1"
LEGACY_SEARCH_METHOD = "legacy-unversioned-sort-filter-v0"
SEARCH_ENDPOINT = "/api/sns/web/v1/search/notes"
SEARCH_SORTS = {"general", "time_descending", "popularity_descending"}


def adapt_request(data, method=SEARCH_METHOD):
    """Copy only; never mutate the dependency's shared default filter list."""
    if method not in {SEARCH_METHOD, LEGACY_SEARCH_METHOD}:
        raise ValueError("Unknown search method version")
    result = deepcopy(data)
    if method == LEGACY_SEARCH_METHOD:
        return result  # Explicit A/B control only, never ordinary collection.
    if result.get("sort") not in SEARCH_SORTS:
        raise ValueError("Unsupported actual search sort")
    filters = result.get("filters")
    if not isinstance(filters, list):
        raise ValueError("Search filter schema changed")
    matching = [f for f in filters if isinstance(f, dict) and f.get("type") == "sort_type"]
    if len(matching) != 1:
        raise ValueError("Expected exactly one search sort filter")
    matching[0]["tags"] = [result["sort"]]
    return result


def request_receipt(data, method, dependency_version):
    body = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    return {"method_version": method, "dependency_version": dependency_version,
            "endpoint": SEARCH_ENDPOINT, "payload": deepcopy(data),
            "body_json": body, "body_sha256": hashlib.sha256(body.encode()).hexdigest()}


def task_key(query, sort, method=SEARCH_METHOD):
    base = query + "\0" + sort
    # General requests are byte-for-byte unchanged by the adapter.
    return base if sort == "general" or method == LEGACY_SEARCH_METHOD else base + "\0" + method


def method_of(record):
    method = record.get("search_method_version", LEGACY_SEARCH_METHOD)
    if method not in {SEARCH_METHOD, LEGACY_SEARCH_METHOD}:
        raise ValueError("Unknown search method version")
    return method


def collection_metadata(api, query, page, api_sort):
    receipt = getattr(api, "last_search_request", None)
    return validate_receipt(receipt, query, page, api_sort)


def validate_receipt(receipt, query, page, api_sort):
    if not isinstance(receipt, dict) or receipt.get("method_version") != SEARCH_METHOD:
        raise ValueError("Actual adapted search request evidence is missing")
    data = receipt.get("payload", {})
    if (data.get("keyword"), data.get("page"), data.get("sort")) != (query, page, api_sort):
        raise ValueError("Search request evidence does not match the collected page")
    if adapt_request(data) != data:
        raise ValueError("Search request has conflicting sort fields")
    if request_receipt(data, SEARCH_METHOD, receipt.get("dependency_version")) != receipt:
        raise ValueError("Search request serialization or hash changed")
    return {"search_method_version": SEARCH_METHOD, "search_request": deepcopy(receipt),
            "legacy_general_compatible": api_sort == "general"}


def historical(task):
    payload = task.get("payload", {})
    return (task.get("kind") == "search" and payload.get("sort") in {"latest", "popular"}
            and method_of(payload) == LEGACY_SEARCH_METHOD and bool(payload.get("superseded_by")))


def collection_allowed(task):
    if historical(task):
        return False
    if task.get("kind") != "search":
        return True
    payload = task.get("payload", {})
    return payload.get("sort") == "general" or method_of(payload) == SEARCH_METHOD
