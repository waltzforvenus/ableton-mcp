"""Copy untrusted requests without manufacturing complete-read authority."""
from copy import deepcopy


def plan_note_edits(snapshot: dict, edits: list[dict]) -> dict:
    return {"snapshot": deepcopy(snapshot), "edits": deepcopy(edits)}


def apply_note_edits(plan: dict) -> dict:
    return {"plan": deepcopy(plan)}
