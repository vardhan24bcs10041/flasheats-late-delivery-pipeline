"""Validation rules behave as a gate: schema breaks FAIL, known issues WARN, ambiguity stays UNKNOWN."""
import pandas as pd

from pipeline.common import load_category_map, map_categories
from pipeline.config import PROJECT_ROOT, load_config
from pipeline.validate import check_required_columns, gate_decision, CheckResult

CMAP = load_category_map(PROJECT_ROOT / "config" / "category_map.csv")


def test_missing_required_column_fails_gate():
    cfg = load_config()
    frames = {"orders": pd.DataFrame(columns=[c for c in cfg.required_columns["orders"] if c != "promised_eta"])}
    res = check_required_columns(frames, {"orders": cfg.required_columns["orders"]})
    assert res[0].status == "FAIL" and "promised_eta" in res[0].evidence


def test_gate_blocks_publish_on_any_fail_but_not_on_warn_or_unknown():
    ok = [CheckResult("V1", "t", "a", "WARN", "", ""), CheckResult("V24", "o", "b", "UNKNOWN", "", "")]
    assert gate_decision(ok)["publish_outputs"] is True
    assert gate_decision(ok + [CheckResult("V6", "t", "c", "FAIL", "", "")])["publish_outputs"] is False


def test_representation_is_normalised_semantic_is_flagged_unknown_is_kept_visible():
    values = pd.Series(["Late Delivery", "ETA issue", "gridlock_complaint", None])
    mapped, report = map_categories(values, "ticket_category", CMAP)
    assert mapped.iloc[0] == "late_delivery"                       # case/separator only -> safe
    assert "ETA issue" in report["unconfirmed_semantic"]            # merged provisionally, owner must confirm
    assert mapped.iloc[2] == "UNMAPPED:gridlock_complaint"          # never guessed, never dropped
    assert pd.isna(mapped.iloc[3])


def test_attribution_is_not_ready_while_arrival_event_is_missing():
    res = [CheckResult("V24", "organizational", "arrival event", "UNKNOWN", "", "")]
    fit = gate_decision(res)["fitness_by_decision"]
    assert fit["Weekly late-delivery KPI report"].startswith("READY")
    assert fit["Restaurant-vs-driver blame for pickup delay"].startswith("NOT READY")
