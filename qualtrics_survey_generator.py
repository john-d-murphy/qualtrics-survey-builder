#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Generic Qualtrics Survey Builder — YAML-driven.

Supports:
  - Question types: single_select, multi_select, dropdown, bipolar_group,
    slider_group, matrix, text_entry, descriptive
  - Display logic (conditional show/hide of questions and blocks)
  - Screen-out questions (end survey on condition)
  - Flow control: linear ordering, randomizers, end-of-survey elements
  - Algorithm rating blocks with balanced randomization
  - Consent blocks (inline HTML or builtin)
  - allow_other on multi-select / single-select
  - Matrix (Likert) questions

Usage:
  python qualtrics_survey_generator.py --base-url https://your-org.qualtrics.com \\
                   --yaml survey.yaml \\
                   --out survey_definition.json

  # Or with --dry-run to generate the QSF-like JSON without calling the API:
  python qualtrics_survey_generator.py --yaml survey.yaml --out survey_definition.json --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import requests
import yaml

LOG = logging.getLogger("survey_builder")
logging.basicConfig(level=logging.DEBUG, format="[%(levelname)s] %(message)s")

DEFAULT_TIMEOUT = 30


# ═══════════════════════════════════════════════
# Utilities
# ═══════════════════════════════════════════════


def slug(*parts: Any, limit: int = 48) -> str:
    """Create a clean export-tag slug from parts."""
    s = "_".join(str(p) for p in parts if p)
    s = re.sub(r"[^A-Za-z0-9_]+", "_", s).upper()
    return s.strip("_")[:limit] or "Q"


def require_keys(d: Dict[str, Any], path: str, keys: List[str]) -> None:
    miss = [k for k in keys if k not in d]
    if miss:
        raise SystemExit(
            f"YAML error at '{path}': missing keys {miss}. Got: {list(d.keys())}"
        )


# ═══════════════════════════════════════════════
# Qualtrics API Client
# ═══════════════════════════════════════════════


class QualtricsClient:
    """Thin wrapper around the Qualtrics v3 API with retry logic."""

    def __init__(self, base_url: str, token: str, dry_run: bool = False):
        self.base_url = base_url.rstrip("/")
        if self.base_url.lower().endswith("/api/v3"):
            self.base_url = self.base_url[:-8]
        self.token = token
        self.dry_run = dry_run
        # For dry-run mode: track generated IDs
        self._dry_counter = 0
        self._dry_survey: Dict[str, Any] = {}
        self._dry_blocks: Dict[str, Dict] = {}
        self._dry_questions: List[Dict] = []

    def _next_id(self, prefix: str = "SV") -> str:
        self._dry_counter += 1
        return f"{prefix}_{self._dry_counter:04d}"

    def api(self, method: str, path: str, **kwargs) -> Dict[str, Any]:
        if self.dry_run:
            return self._dry_api(method, path, **kwargs)
        return self._live_api(method, path, **kwargs)

    def _live_api(self, method: str, path: str, **kwargs) -> Dict[str, Any]:
        url = f"{self.base_url}/API/v3{path}"
        headers = kwargs.pop("headers", {})
        headers.update(
            {
                "X-API-TOKEN": self.token,
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        )
        timeout = kwargs.pop("timeout", DEFAULT_TIMEOUT)

        max_tries, backoff = 6, 0.5
        for attempt in range(1, max_tries + 1):
            LOG.debug("API %s %s", method, url)
            resp = requests.request(
                method, url, headers=headers, timeout=timeout, **kwargs
            )
            if resp.ok:
                return resp.json()

            status = resp.status_code
            rqid = None
            try:
                j = resp.json()
                rqid = j.get("meta", {}).get("requestId")
            except Exception:
                pass

            if status == 429:
                retry_after = resp.headers.get("Retry-After")
                try:
                    sleep_s = float(retry_after) if retry_after else backoff
                except ValueError:
                    sleep_s = backoff
            elif 500 <= status < 600:
                sleep_s = backoff
            else:
                raise RuntimeError(
                    f"{method} {path} failed [{status}] rqid={rqid}: {resp.text}"
                )

            if attempt == max_tries:
                raise RuntimeError(
                    f"{method} {path} failed after retries [{status}] rqid={rqid}: {resp.text}"
                )
            time.sleep(sleep_s + random.uniform(0, 0.25 * sleep_s))
            backoff = min(backoff * 2, 8.0)

    def _dry_api(self, method: str, path: str, **kwargs) -> Dict[str, Any]:
        """In dry-run mode, simulate API calls and collect the survey structure."""
        body = kwargs.get("json", {})

        if method == "POST" and path.endswith("/survey-definitions"):
            sid = self._next_id("SV")
            self._dry_survey = {
                "SurveyID": sid,
                "SurveyName": body.get("SurveyName", ""),
            }
            return {"result": {"SurveyID": sid}}

        if method == "POST" and "/blocks" in path:
            bid = self._next_id("BL")
            self._dry_blocks[bid] = {
                "Description": body.get("Description", ""),
                "Questions": [],
            }
            return {"result": {"BlockID": bid}}

        if method == "POST" and "/questions" in path:
            qid = self._next_id("QID")
            block_id = kwargs.get("params", {}).get("blockId", "unknown")
            entry = {"QuestionID": qid, "BlockID": block_id, **body}
            self._dry_questions.append(entry)
            if block_id in self._dry_blocks:
                self._dry_blocks[block_id]["Questions"].append(qid)
            return {"result": {"QuestionID": qid}}

        if method == "GET" and "/flow" in path:
            return {
                "result": {
                    "Type": "Root",
                    "FlowID": "FL_1",
                    "Flow": [],
                    "Properties": {"Count": 1},
                }
            }

        if method == "PUT" and "/flow" in path:
            self._dry_survey["Flow"] = body
            return {"result": {}}

        if method == "GET" and "/survey-definitions/" in path:
            return {
                "result": {
                    **self._dry_survey,
                    "Blocks": self._dry_blocks,
                    "Questions": {
                        q["QuestionID"]: q for q in self._dry_questions
                    },
                }
            }

        return {"result": {}}


# ═══════════════════════════════════════════════
# Survey Operations — Question Builders
# ═══════════════════════════════════════════════


class SurveyBuilder:
    """Builds a Qualtrics survey from a parsed YAML spec."""

    def __init__(self, client: QualtricsClient, spec: Dict[str, Any]):
        self.client = client
        self.spec = spec
        self.meta = spec["meta"]
        self.survey_id: Optional[str] = None
        # Maps block YAML id -> Qualtrics block ID
        self.block_map: Dict[str, str] = {}
        # Maps question YAML id -> Qualtrics question ID (for display logic refs)
        self.question_map: Dict[str, str] = {}
        # Maps question YAML id -> {choice_text: recode_value} for display logic
        self.choice_map: Dict[str, Dict[str, str]] = {}
        # Algorithm block IDs for randomizer
        self.algo_block_ids: List[str] = []

    # ── Survey lifecycle ─────────────────────

    def create_survey(self) -> str:
        r = self.client.api(
            "POST",
            "/survey-definitions",
            json={
                "SurveyName": self.meta["name"],
                "Language": self.meta.get("language", "EN"),
                "ProjectCategory": self.meta.get("project_category", "CORE"),
            },
        )
        self.survey_id = r["result"]["SurveyID"]
        LOG.info("Created survey: %s", self.survey_id)
        return self.survey_id

    def create_block(self, description: str) -> str:
        r = self.client.api(
            "POST",
            f"/survey-definitions/{self.survey_id}/blocks",
            json={"Type": "Standard", "Description": description},
        )
        bid = r["result"]["BlockID"]
        LOG.info("  Block '%s': %s", description, bid)
        return bid

    def add_question(
        self,
        block_id: str,
        body: Dict[str, Any],
        yaml_id: Optional[str] = None,
        choices_list: Optional[List[str]] = None,
    ) -> str:
        r = self.client.api(
            "POST",
            f"/survey-definitions/{self.survey_id}/questions",
            params={"blockId": block_id},
            json=body,
        )
        qid = r["result"]["QuestionID"]
        if yaml_id:
            self.question_map[yaml_id] = qid
            # Store choice text -> recode value mapping for display logic
            if choices_list:
                self.choice_map[yaml_id] = {
                    text: str(i + 1) for i, text in enumerate(choices_list)
                }
        return qid

    def export_definition(self) -> Dict[str, Any]:
        r = self.client.api("GET", f"/survey-definitions/{self.survey_id}")
        return r["result"]

    # ── Question type builders ───────────────

    def _q_descriptive(self, html: str) -> Dict[str, Any]:
        return {
            "QuestionText": html,
            "QuestionType": "DB",
            "Selector": "TB",
            "Configuration": {"QuestionDescriptionOption": "UseText"},
            "Validation": {
                "Settings": {"ForceResponse": "OFF", "Type": "None"}
            },
        }

    def _q_text_entry(
        self,
        text: str,
        tag: str,
        *,
        subtype: str = "multi_line",
        force_response: bool = False,
    ) -> Dict[str, Any]:
        selector = "SL" if subtype == "single_line" else "ML"
        return {
            "QuestionText": text,
            "QuestionDescription": tag,
            "DataExportTag": tag,
            "QuestionType": "TE",
            "Selector": selector,
            "Configuration": {"QuestionDescriptionOption": "UseText"},
            "Validation": {
                "Settings": {
                    "ForceResponse": "ON" if force_response else "OFF",
                    "Type": "None",
                }
            },
        }

    def _q_single_select(
        self,
        text: str,
        choices: List[str],
        tag: str,
        *,
        force_response: bool = False,
        allow_other: bool = False,
    ) -> Dict[str, Any]:
        """MC single answer, vertical radio buttons."""
        choice_dict = {
            str(i + 1): {"Display": c} for i, c in enumerate(choices)
        }
        choice_order = [str(i + 1) for i in range(len(choices))]
        body: Dict[str, Any] = {
            "QuestionText": text,
            "QuestionDescription": tag,
            "DataExportTag": tag,
            "QuestionType": "MC",
            "Selector": "SAVR",  # Single Answer Vertical
            "SubSelector": "TX",
            "Configuration": {"QuestionDescriptionOption": "UseText"},
            "Validation": {
                "Settings": {
                    "ForceResponse": "ON" if force_response else "OFF",
                    "Type": "None",
                }
            },
            "Choices": choice_dict,
            "ChoiceOrder": choice_order,
        }
        if allow_other:
            next_key = str(len(choices) + 1)
            body["Choices"][next_key] = {
                "Display": "Other",
                "TextEntry": "true",
            }
            body["ChoiceOrder"].append(next_key)
        return body

    def _q_multi_select(
        self,
        text: str,
        choices: List[str],
        tag: str,
        *,
        force_response: bool = False,
        allow_other: bool = False,
        min_choices: Optional[int] = None,
        max_choices: Optional[int] = None,
    ) -> Dict[str, Any]:
        """MC multiple answer, vertical checkboxes."""
        choice_dict = {
            str(i + 1): {"Display": c} for i, c in enumerate(choices)
        }
        choice_order = [str(i + 1) for i in range(len(choices))]

        validation: Dict[str, Any] = {
            "ForceResponse": "ON" if force_response else "OFF",
        }
        # If force_response but no explicit min, default to requiring at least 1
        effective_min = min_choices
        if force_response and min_choices is None:
            effective_min = 1

        if effective_min is not None or max_choices is not None:
            if max_choices is not None:
                # Both min and max (or just max) → ChoiceRange
                validation["Type"] = "ChoiceRange"
                if effective_min is not None:
                    validation["MinChoices"] = str(effective_min)
                validation["MaxChoices"] = str(max_choices)
            else:
                # Min only → MinChoices
                validation["Type"] = "MinChoices"
                validation["MinChoices"] = str(effective_min)
        else:
            validation["Type"] = "None"

        body: Dict[str, Any] = {
            "QuestionText": text,
            "QuestionDescription": tag,
            "DataExportTag": tag,
            "QuestionType": "MC",
            "Selector": "MAVR",  # Multiple Answer Vertical
            "SubSelector": "TX",
            "Configuration": {"QuestionDescriptionOption": "UseText"},
            "Validation": {"Settings": validation},
            "Choices": choice_dict,
            "ChoiceOrder": choice_order,
        }
        if allow_other:
            next_key = str(len(choices) + 1)
            body["Choices"][next_key] = {
                "Display": "Other",
                "TextEntry": "true",
            }
            body["ChoiceOrder"].append(next_key)
        return body

    def _q_dropdown(
        self,
        text: str,
        choices: List[str],
        tag: str,
        *,
        force_response: bool = False,
    ) -> Dict[str, Any]:
        """MC dropdown."""
        choice_dict = {
            str(i + 1): {"Display": c} for i, c in enumerate(choices)
        }
        choice_order = [str(i + 1) for i in range(len(choices))]
        return {
            "QuestionText": text,
            "QuestionDescription": tag,
            "DataExportTag": tag,
            "QuestionType": "MC",
            "Selector": "DL",  # Dropdown List
            "Configuration": {"QuestionDescriptionOption": "UseText"},
            "Validation": {
                "Settings": {
                    "ForceResponse": "ON" if force_response else "OFF",
                    "Type": "None",
                }
            },
            "Choices": choice_dict,
            "ChoiceOrder": choice_order,
        }

    def _q_bipolar_sahr(
        self,
        text: str,
        left: str,
        right: str,
        tag: str,
        *,
        points: int = 7,
        middle_label: str = "Neither",
        force_response: bool = False,
    ) -> Dict[str, Any]:
        """7-point bipolar scale via MC/SAHR (matches Qualtrics UI export)."""
        if points == 7:
            ticks = [
                left,
                "&nbsp;",
                "&nbsp;",
                middle_label,
                "&nbsp;",
                "&nbsp;",
                right,
            ]
        elif points == 5:
            ticks = [left, "&nbsp;", middle_label, "&nbsp;", right]
        else:
            # Generic: endpoints + midpoint
            ticks = [left] + ["&nbsp;"] * (points - 2) + [right]
            mid = points // 2
            ticks[mid] = middle_label

        return {
            "QuestionText": text,
            "QuestionDescription": tag,
            "DataExportTag": tag,
            "QuestionType": "MC",
            "Selector": "SAHR",
            "SubSelector": "TX",
            "Configuration": {
                "QuestionDescriptionOption": "UseText",
                "LabelPosition": "BELOW",
            },
            "Validation": {
                "Settings": {
                    "ForceResponse": "ON" if force_response else "OFF",
                    "Type": "None",
                }
            },
            "Choices": {
                str(i + 1): {"Display": ticks[i]} for i in range(len(ticks))
            },
            "ChoiceOrder": list(range(1, len(ticks) + 1)),
        }

    def _q_slider(
        self, text: str, tag: str, cfg: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Single horizontal slider."""
        return {
            "QuestionText": text,
            "QuestionDescription": tag,
            "DataExportTag": tag,
            "QuestionType": "Slider",
            "Selector": "HSLIDER",
            "Configuration": {
                "QuestionDescriptionOption": "UseText",
                "CSSliderMin": cfg.get("min", 1),
                "CSSliderMax": cfg.get("max", 7),
                "GridLines": cfg.get("grid_lines", 7),
                "SnapToGrid": cfg.get("snap_to_grid", False),
                "NumDecimals": str(cfg.get("num_decimals", 0)),
                "ShowValue": cfg.get("show_value", True),
                "CustomStart": False,
                "NotApplicable": False,
                "MobileFirst": True,
            },
            "Choices": {"1": {"Display": ""}},
            "Validation": {
                "Settings": {"ForceResponse": "OFF", "Type": "None"}
            },
        }

    def _q_matrix(
        self,
        text: str,
        rows: List[str],
        columns: List[str],
        tag: str,
        *,
        force_response: bool = False,
    ) -> Dict[str, Any]:
        """Matrix/Likert single-answer per row."""
        answers = {str(i + 1): {"Display": c} for i, c in enumerate(columns)}
        choices = {str(i + 1): {"Display": r} for i, r in enumerate(rows)}
        return {
            "QuestionText": text,
            "QuestionDescription": tag,
            "DataExportTag": tag,
            "QuestionType": "Matrix",
            "Selector": "Likert",
            "SubSelector": "SingleAnswer",
            "Configuration": {"QuestionDescriptionOption": "UseText"},
            "Validation": {
                "Settings": {
                    "ForceResponse": "ON" if force_response else "OFF",
                    "Type": "None",
                }
            },
            "Choices": choices,
            "ChoiceOrder": [str(i + 1) for i in range(len(rows))],
            "Answers": answers,
            "AnswerOrder": [str(i + 1) for i in range(len(columns))],
        }

    def _q_bipolar_matrix(
        self,
        text: str,
        pairs: List[List[str]],
        tag: str,
        *,
        points: int = 7,
        force_response: bool = False,
    ) -> tuple:
        """
        Matrix/Bipolar — semantic differential scale.
        Each row has a left anchor and a right anchor with radio buttons between.

        Returns (body, labels) — body for POST, labels for a follow-up PUT
        since the API rejects Labels on creation.
        """
        # Choices = left labels
        choices = {}
        labels = {}
        for i, pair in enumerate(pairs):
            key = str(i + 1)
            choices[key] = {"Display": pair[0]}
            labels[key] = pair[1]

        # Answers = scale columns (unlabeled)
        answers = {str(i + 1): {"Display": " "} for i in range(points)}

        body = {
            "QuestionText": text,
            "QuestionDescription": tag,
            "DataExportTag": tag,
            "QuestionType": "Matrix",
            "Selector": "Bipolar",
            "SubSelector": "SingleAnswer",
            "Configuration": {"QuestionDescriptionOption": "UseText"},
            "Validation": {
                "Settings": {
                    "ForceResponse": "ON" if force_response else "OFF",
                    "Type": "None",
                }
            },
            "Choices": choices,
            "ChoiceOrder": [str(i + 1) for i in range(len(pairs))],
            "Answers": answers,
            "AnswerOrder": [str(i + 1) for i in range(points)],
        }
        return body, labels

    def _update_question(self, qid: str, updates: Dict[str, Any]) -> None:
        """Update an existing question by GET-merge-PUT."""
        # GET current question definition
        r = self.client.api(
            "GET",
            f"/survey-definitions/{self.survey_id}/questions/{qid}",
        )
        current = r.get("result", r)
        # Merge updates
        current.update(updates)
        # PUT back
        self.client.api(
            "PUT",
            f"/survey-definitions/{self.survey_id}/questions/{qid}",
            json=current,
        )

    # ── Display logic helper ─────────────────

    def _build_display_logic(
        self, cond: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """
        Build Qualtrics API v3 DisplayLogic JSON from a simple condition dict.

        The API v3 requires:
          - LogicType: "EmbeddedField"
          - LeftOperand: "q://<QID>/SelectedChoicesRecode"
          - Operator: "EqualTo" | "NotEqualTo" (not "Selected"/"NotSelected")
          - RightOperand: the choice recode value (1-indexed string)
          - No QuestionIsInLoop or ChoiceLocator fields

        Supported forms:
          { question: <yaml_id>, selected: <choice_text> }
          { question: <yaml_id>, is_not: <choice_text> }
        """
        ref_id = cond.get("question")
        if not ref_id or ref_id not in self.question_map:
            LOG.warning(
                "Display logic references unknown question '%s' — skipping",
                ref_id,
            )
            return None

        qid = self.question_map[ref_id]
        choices = self.choice_map.get(ref_id, {})

        if "selected" in cond:
            choice_text = cond["selected"]
            recode = choices.get(choice_text, "1")
            return {
                "Type": "BooleanExpression",
                "inPage": False,
                "0": {
                    "0": {
                        "LogicType": "EmbeddedField",
                        "LeftOperand": f"q://{qid}/SelectedChoicesRecode",
                        "Operator": "EqualTo",
                        "RightOperand": recode,
                        "Type": "Expression",
                        "Description": f"If {ref_id} = {choice_text}",
                    }
                },
            }
        elif "is_not" in cond:
            choice_text = cond["is_not"]
            recode = choices.get(choice_text, "1")
            return {
                "Type": "BooleanExpression",
                "inPage": False,
                "0": {
                    "0": {
                        "LogicType": "EmbeddedField",
                        "LeftOperand": f"q://{qid}/SelectedChoicesRecode",
                        "Operator": "NotEqualTo",
                        "RightOperand": recode,
                        "Type": "Expression",
                        "Description": f"If {ref_id} != {choice_text}",
                    }
                },
            }
        return None

    # ── Screen-out / flow-level branch logic ──

    def _build_branch_logic(
        self, cond: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """
        Build BranchLogic for FLOW-LEVEL Branch elements.

        Flow BranchLogic uses:
          - LogicType: "Question"
          - QuestionID: "<QID>"
          - ChoiceLocator: "q://<QID>/SelectableChoice/<recode>"
          - Operator: "Selected" | "NotSelected"

        NOTE: Do NOT include QuestionIsInLoop — the API rejects it.
        """
        ref_id = cond.get("question")
        if not ref_id or ref_id not in self.question_map:
            LOG.warning(
                "Branch logic references unknown question '%s' — skipping",
                ref_id,
            )
            return None

        qid = self.question_map[ref_id]
        choices = self.choice_map.get(ref_id, {})

        if "selected" in cond:
            choice_text = cond["selected"]
            recode = choices.get(choice_text, "1")
            return {
                "Type": "BooleanExpression",
                "0": {
                    "0": {
                        "LogicType": "Question",
                        "QuestionID": qid,
                        "ChoiceLocator": f"q://{qid}/SelectableChoice/{recode}",
                        "Operator": "Selected",
                        "Type": "Expression",
                        "Description": f"If {ref_id} = {choice_text}",
                    }
                },
            }
        elif "is_not" in cond:
            choice_text = cond["is_not"]
            recode = choices.get(choice_text, "1")
            return {
                "Type": "BooleanExpression",
                "0": {
                    "0": {
                        "LogicType": "Question",
                        "QuestionID": qid,
                        "ChoiceLocator": f"q://{qid}/SelectableChoice/{recode}",
                        "Operator": "NotSelected",
                        "Type": "Expression",
                        "Description": f"If {ref_id} != {choice_text}",
                    }
                },
            }
        return None

    def _build_screen_out_flow(
        self,
        question_yaml_id: str,
        screen_out: Dict[str, Any],
        flow_id_counter: int,
    ) -> Optional[Dict[str, Any]]:
        """
        Build a Branch flow element that ends the survey if a condition is met.
        The condition implicitly refers to the question it's attached to.
        Uses flow-level BranchLogic schema (Question + Selected + ChoiceLocator).
        """
        cond = screen_out.get("condition", {})
        ref_qid = self.question_map.get(question_yaml_id)
        if not ref_qid:
            LOG.warning(
                "Screen-out: can't resolve question '%s'", question_yaml_id
            )
            return None

        choices = self.choice_map.get(question_yaml_id, {})

        # Determine operator and choice
        if "selected" in cond:
            choice_text = cond["selected"]
            operator = "Selected"
        elif "is_not" in cond:
            choice_text = cond["is_not"]
            operator = "NotSelected"
        else:
            LOG.warning(
                "Screen-out: no 'selected' or 'is_not' in condition for '%s'",
                question_yaml_id,
            )
            return None

        recode = choices.get(choice_text, "1")

        return {
            "Type": "Branch",
            "FlowID": f"FL_{flow_id_counter}",
            "Flow": [
                {
                    "Type": "EndSurvey",
                    "FlowID": f"FL_{flow_id_counter + 1}",
                }
            ],
            "BranchLogic": {
                "Type": "BooleanExpression",
                "0": {
                    "0": {
                        "LogicType": "Question",
                        "QuestionID": ref_qid,
                        "ChoiceLocator": f"q://{ref_qid}/SelectableChoice/{recode}",
                        "Operator": operator,
                        "Type": "Expression",
                        "Description": f"Screen out: {question_yaml_id} {operator} {choice_text}",
                    }
                },
            },
        }

    # ── High-level question dispatch ─────────

    def build_question(self, block_id: str, q: Dict[str, Any]) -> None:
        """Dispatch a single YAML question dict to the appropriate builder."""
        qtype = q["type"]
        qid_yaml = q.get("id", "")
        tag = (
            slug(q.get("tag_prefix", ""), qid_yaml) if qid_yaml else slug("Q")
        )
        force = q.get("force_response", False)

        body: Optional[Dict[str, Any]] = None

        if qtype == "descriptive":
            body = self._q_descriptive(q.get("html", q.get("text", "")))

        elif qtype == "text_entry":
            subtype = q.get("subtype", "multi_line")
            body = self._q_text_entry(
                q["text"], tag, subtype=subtype, force_response=force
            )

        elif qtype == "single_select":
            body = self._q_single_select(
                q["text"],
                q["choices"],
                tag,
                force_response=force,
                allow_other=q.get("allow_other", False),
            )

        elif qtype == "multi_select":
            body = self._q_multi_select(
                q["text"],
                q["choices"],
                tag,
                force_response=force,
                allow_other=q.get("allow_other", False),
                min_choices=q.get("min_choices"),
                max_choices=q.get("max_choices"),
            )

        elif qtype == "dropdown":
            body = self._q_dropdown(
                q["text"], q["choices"], tag, force_response=force
            )

        elif qtype == "matrix":
            body = self._q_matrix(
                q["text"], q["rows"], q["columns"], tag, force_response=force
            )

        elif qtype == "bipolar_matrix":
            # Single matrix question with left/right anchors per row
            # Two-step: API rejects Labels on POST, so we create then PUT
            bp_defaults = self.meta.get("bipolar_defaults", {})
            points = q.get("points", bp_defaults.get("points", 7))
            body, labels = self._q_bipolar_matrix(
                q["text"], q["pairs"], tag, points=points, force_response=force
            )
            qid = self.add_question(block_id, body, yaml_id=qid_yaml)
            if labels:
                self._update_question(qid, {"Labels": labels})
            return  # already added

        elif qtype == "bipolar_group":
            # Expands into N individual bipolar scale questions
            bp_defaults = self.meta.get("bipolar_defaults", {})
            points = q.get("points", bp_defaults.get("points", 7))
            middle = q.get(
                "middle_label", bp_defaults.get("middle_label", "Neither")
            )
            prefix = q.get("tag_prefix", qid_yaml)
            title = q.get("title", "")

            for pair in q["pairs"]:
                left, right = pair
                pair_tag = slug(prefix, left, right)
                pair_text = (
                    f"{title} {left}/{right}" if title else f"{left}/{right}"
                )
                pair_body = self._q_bipolar_sahr(
                    pair_text,
                    left,
                    right,
                    pair_tag,
                    points=points,
                    middle_label=middle,
                    force_response=force,
                )
                self.add_question(
                    block_id, pair_body, yaml_id=f"{qid_yaml}_{slug(left)}"
                )
            return  # already added individually

        elif qtype == "slider_group":
            # Expands into N individual slider questions
            slider_cfg = self.meta.get("slider_defaults", {})
            title = q.get("title", "")
            labels = q.get("labels", {})
            prefix = q.get("tag_prefix", qid_yaml)

            for item in q["items"]:
                item_tag = slug(prefix, item)
                item_text = f"{title} <b>{item}</b>"
                if labels:
                    item_text += f" ({labels.get('left', '')} — {labels.get('right', '')})"
                item_body = self._q_slider(item_text, item_tag, slider_cfg)
                self.add_question(
                    block_id, item_body, yaml_id=f"{qid_yaml}_{slug(item)}"
                )
            return  # already added individually

        else:
            LOG.warning(
                "Unknown question type '%s' for '%s' — skipping",
                qtype,
                qid_yaml,
            )
            return

        if body is not None:
            # Attach display logic if present
            dl = q.get("display_logic")
            if dl and "condition" in dl:
                logic = self._build_display_logic(dl["condition"])
                if logic:
                    body["DisplayLogic"] = logic

            # Pass choices list so we can resolve display logic references later
            choices_list = (
                q.get("choices")
                if qtype in ("single_select", "multi_select", "dropdown")
                else None
            )
            self.add_question(
                block_id, body, yaml_id=qid_yaml, choices_list=choices_list
            )

    # ── Block builders ───────────────────────

    def build_block(self, block_spec: Dict[str, Any]) -> str:
        """Build a complete block from its YAML spec. Returns the Qualtrics block ID."""
        block_id_yaml = block_spec["id"]
        title = block_spec.get("title", block_id_yaml)
        bid = self.create_block(title)
        self.block_map[block_id_yaml] = bid

        # Block-level display logic (note: Qualtrics handles this via branch flow,
        # not block-level DL. We'll handle it in flow construction.)
        for q in block_spec.get("questions", []):
            self.build_question(bid, q)

        return bid

    def build_consent_block(self) -> Optional[str]:
        """Build the consent block if specified."""
        consent_spec = self.spec.get("consent")
        if not consent_spec:
            return None

        bid = self.create_block("Consent")
        source = consent_spec.get("source", "")
        if source == "inline":
            html = consent_spec.get("html", "")
        else:
            html = consent_spec.get("html", source)

        body = self._q_descriptive(html)
        self.add_question(bid, body)
        self.block_map["consent"] = bid
        return bid

    def build_algorithm_blocks(self) -> List[str]:
        """Build per-algorithm rating blocks from the algorithms spec."""
        alg = self.spec.get("algorithms")
        if not alg:
            return []

        names = alg["names"]
        ratings = alg.get("ratings", {})
        style = alg.get("rating_style", "bipolar")
        labels = alg.get(
            "rating_labels", {"left": "Not at all", "right": "Very"}
        )
        bp_defaults = self.meta.get("bipolar_defaults", {})

        bids = []
        for algo_name in names:
            # Interstitial "go test" block
            interstitial_bid = self.create_block(f"Test Next — {algo_name}")
            interstitial_html = (
                f"<h2>HEAR-MI — {algo_name}</h2>"
                f"<p>Please go to the primary computer and work with the "
                f"<b>{algo_name}</b> algorithm. Come back here when you're done.</p>"
            )
            self.add_question(
                interstitial_bid, self._q_descriptive(interstitial_html)
            )
            # Don't add interstitial to flow separately; pair it with the rating block
            # Actually, let's make one combined block per algorithm for cleaner randomization
            bid = self.create_block(f"Algorithm — {algo_name}")
            header_html = f"<h2>Rate: {algo_name}</h2>"
            self.add_question(bid, self._q_descriptive(header_html))

            for cat_key, cat_spec in ratings.items():
                cat_title = cat_spec.get("title", cat_key)
                items = cat_spec.get("items", cat_spec.get("sliders", []))

                for item in items:
                    tag = slug("ALG", algo_name, cat_key, item)
                    text = f"{cat_title} <b>{item}</b>:"

                    if style == "slider":
                        slider_cfg = self.meta.get("slider_defaults", {})
                        body = self._q_slider(text, tag, slider_cfg)
                    else:
                        # bipolar
                        body = self._q_bipolar_sahr(
                            text,
                            labels.get("left", "Not at all"),
                            labels.get("right", "Very"),
                            tag,
                            points=bp_defaults.get("points", 7),
                            middle_label=bp_defaults.get(
                                "middle_label", "Neutral"
                            ),
                        )
                    self.add_question(bid, body)

            # Store the interstitial + rating as a pair
            algo_block_pair = [interstitial_bid, bid]
            bids.extend(algo_block_pair)
            self.block_map[f"algo_{algo_name}"] = bid

        self.algo_block_ids = bids
        return bids

    # ── Flow construction ────────────────────

    def build_flow(
        self, dump_flow_path: Optional[str] = None, simple_flow: bool = False
    ) -> None:
        """
        Set up the survey flow.

        Strategy: Qualtrics auto-adds blocks to the flow when they're created.
        Rather than constructing flow elements from scratch (which breaks on some
        brands), we GET the existing flow, find the block elements Qualtrics created,
        and reorder them to match our YAML spec.
        """
        flow_spec = self.spec.get("flow", {})
        order = flow_spec.get("order", [])
        if not order:
            order = [{"block": bid} for bid in self.block_map]

        # 1) GET the current flow — Qualtrics has auto-added all our blocks
        raw_payload = self.client.api(
            "GET", f"/survey-definitions/{self.survey_id}/flow"
        )
        raw_result = raw_payload.get("result", raw_payload)

        # Dump raw GET for debugging
        if dump_flow_path:
            get_path = dump_flow_path.replace(".json", "_get.json")
            with open(get_path, "w", encoding="utf-8") as f:
                json.dump(raw_result, f, ensure_ascii=False, indent=2)
            LOG.info("Dumped GET flow → %s", get_path)

        root = self._parse_root_flow(raw_result)
        existing_children = list(root.get("Flow", []))

        # 2) Index existing flow elements by their block ID
        #    Qualtrics might use "ID" or "BlockID" — detect the key
        bid_key = "ID"
        for el in existing_children:
            if isinstance(el, dict) and el.get("Type") == "Block":
                if "BlockID" in el:
                    bid_key = "BlockID"
                break

        existing_by_block: Dict[str, Dict[str, Any]] = {}
        other_elements: List[Dict[str, Any]] = (
            []
        )  # non-block elements to preserve
        for el in existing_children:
            if isinstance(el, dict) and el.get("Type") == "Block":
                block_id = el.get(bid_key) or el.get("ID") or el.get("BlockID")
                if block_id:
                    existing_by_block[block_id] = el
                else:
                    other_elements.append(el)
            elif isinstance(el, dict) and el.get("Type") in (
                "Standard",
                "Default",
            ):
                # Keep default/standard blocks
                other_elements.append(el)
            else:
                other_elements.append(el)

        LOG.info(
            "Existing flow has %d block elements, %d other elements",
            len(existing_by_block),
            len(other_elements),
        )
        for bid, el in existing_by_block.items():
            LOG.debug(
                "  Existing block: %s (FlowID=%s)", bid, el.get("FlowID", "?")
            )
        for el in other_elements:
            LOG.debug(
                "  Other element: Type=%s FlowID=%s",
                el.get("Type", "?"),
                el.get("FlowID", "?"),
            )

        # 3) Build desired order by looking up existing elements
        reordered: List[Dict[str, Any]] = []
        for step in order:
            if "block" in step:
                block_yaml_id = step["block"]
                bid = self.block_map.get(block_yaml_id)
                if bid and bid in existing_by_block:
                    reordered.append(existing_by_block.pop(bid))
                elif bid:
                    LOG.warning(
                        "Block '%s' (%s) not found in existing flow — "
                        "creating element",
                        block_yaml_id,
                        bid,
                    )
                    reordered.append({"Type": "Block", bid_key: bid})
                else:
                    LOG.warning(
                        "Flow references unknown block '%s'", block_yaml_id
                    )

            elif "randomizer" in step or "randomizer_of" in step:
                # Add remaining algo blocks linearly
                for bid in self.algo_block_ids:
                    if bid in existing_by_block:
                        reordered.append(existing_by_block.pop(bid))
                    else:
                        reordered.append({"Type": "Block", bid_key: bid})

        # Append any blocks we didn't explicitly order (e.g. default blocks)
        for bid, el in existing_by_block.items():
            LOG.debug("Appending unordered block: %s", bid)
            reordered.append(el)

        # 4) Reconstruct root — preserve everything except Flow children
        new_root = {k: v for k, v in root.items() if k != "Flow"}
        if "Properties" in new_root:
            new_root["Properties"]["Count"] = len(reordered)
        new_root["Flow"] = reordered

        # 5) Try multiple PUT formats — different brands accept different structures
        put_formats = [
            ("wrapped", {"Flow": new_root}),
            ("direct", new_root),
            (
                "children-only",
                {
                    "FlowID": new_root.get("FlowID", "FL_1"),
                    "Type": "Root",
                    "Flow": reordered,
                    "Properties": {"Count": len(reordered)},
                },
            ),
        ]

        if dump_flow_path:
            for label, body in put_formats:
                path = dump_flow_path.replace(".json", f"_{label}.json")
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(body, f, ensure_ascii=False, indent=2)
            LOG.info(
                "Dumped PUT payloads → %s",
                dump_flow_path.replace(".json", "_*.json"),
            )

        for label, body in put_formats:
            try:
                LOG.info("Trying flow PUT format: %s", label)
                self.client.api(
                    "PUT",
                    f"/survey-definitions/{self.survey_id}/flow",
                    json=body,
                )
                LOG.info(
                    "Flow PUT succeeded with format: %s (%d elements)",
                    label,
                    len(reordered),
                )
                return
            except RuntimeError as e:
                LOG.warning(
                    "Flow PUT format '%s' failed: %s", label, str(e)[:120]
                )
                continue

        # 6) All formats failed — save everything for manual inspection
        LOG.error("All flow PUT formats failed.")
        LOG.error(
            "Survey %s was created with all blocks and questions intact.",
            self.survey_id,
        )
        LOG.error("The block order can be set manually in the Qualtrics UI.")
        LOG.error("Desired block order:")
        for step in order:
            if "block" in step:
                bid = self.block_map.get(step["block"], "???")
                LOG.error("  %s → %s", step["block"], bid)

    def _parse_root_flow(self, res: Dict[str, Any]) -> Dict[str, Any]:
        """Extract the root flow object from a GET /flow response."""
        if (
            isinstance(res.get("Flow"), dict)
            and res["Flow"].get("Type") == "Root"
        ):
            return res["Flow"]
        if res.get("Type") == "Root" and isinstance(res.get("Flow"), list):
            return res
        if isinstance(res.get("Flow"), list):
            return {
                "Type": "Root",
                "Flow": res["Flow"],
                "Properties": res.get("Properties", {"Count": 1}),
                "FlowID": res.get("FlowID", "FL_1"),
            }
        raise RuntimeError(f"Unrecognized flow response: {list(res.keys())}")

    def _find_block_spec(self, block_yaml_id: str) -> Optional[Dict[str, Any]]:
        for b in self.spec.get("blocks", []):
            if b.get("id") == block_yaml_id:
                return b
        return None

    # ── Main build orchestration ─────────────

    def build(
        self,
        out_path: str,
        dump_flow_path: Optional[str] = None,
        simple_flow: bool = False,
    ) -> None:
        """Full build pipeline."""
        self.create_survey()

        # 1) Consent
        self.build_consent_block()

        # 2) All defined blocks
        for block_spec in self.spec.get("blocks", []):
            self.build_block(block_spec)

        # 3) Algorithm blocks
        self.build_algorithm_blocks()

        # 4) Flow
        self.build_flow(dump_flow_path=dump_flow_path, simple_flow=simple_flow)

        # 5) Export
        qsf = self.export_definition()
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(qsf, f, ensure_ascii=False, indent=2)
        LOG.info("Survey ID: %s", self.survey_id)
        LOG.info("Wrote definition → %s", out_path)


# ═══════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Build a Qualtrics survey from a YAML specification.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Live mode (posts to Qualtrics API):
  python qualtrics_survey_generator.py --base-url https://your-org.qualtrics.com \\
                   --yaml survey.yaml --out survey.json

  # Dry-run mode (generates JSON locally, no API calls):
  python qualtrics_survey_generator.py --yaml survey.yaml --out survey.json --dry-run
        """,
    )
    ap.add_argument(
        "--base-url",
        default=os.getenv("QUALTRICS_BASE_URL", ""),
        help="Qualtrics base URL (or set QUALTRICS_BASE_URL)",
    )
    ap.add_argument(
        "--api-token",
        default=os.getenv("QUALTRICS_API_TOKEN"),
        help="Qualtrics API token (or set QUALTRICS_API_TOKEN)",
    )
    ap.add_argument("--yaml", required=True, help="YAML survey spec path")
    ap.add_argument("--out", required=True, help="Output JSON path")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate JSON locally without calling the API",
    )
    ap.add_argument(
        "--dump-flow",
        default=None,
        metavar="PATH",
        help="Dump the flow JSON payload to a file before sending "
        "(useful for debugging 500 errors)",
    )
    ap.add_argument(
        "--simple-flow",
        action="store_true",
        help="Skip branch logic / randomizers — linear blocks only. "
        "Useful if flow PUT keeps failing; add logic in Qualtrics UI.",
    )
    args = ap.parse_args()

    if not args.dry_run and not args.api_token:
        sys.exit(
            "Missing API token. Provide --api-token or set QUALTRICS_API_TOKEN, "
            "or use --dry-run."
        )

    if not args.dry_run and not args.base_url:
        sys.exit(
            "Missing base URL. Provide --base-url or set QUALTRICS_BASE_URL, "
            "or use --dry-run."
        )

    with open(args.yaml, "r", encoding="utf-8") as f:
        spec = yaml.safe_load(f)

    client = QualtricsClient(
        base_url=args.base_url,
        token=args.api_token or "",
        dry_run=args.dry_run,
    )
    builder = SurveyBuilder(client, spec)
    builder.build(
        args.out, dump_flow_path=args.dump_flow, simple_flow=args.simple_flow
    )


if __name__ == "__main__":
    main()
