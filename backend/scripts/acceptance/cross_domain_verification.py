"""Business-semantic verification methods for cross-domain acceptance."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from scripts.acceptance.cross_domain_matrix import (
    ROLES,
    DeliverableContract,
    Scenario,
    scenario_deliverables,
)


class CrossDomainVerificationMixin:

    @staticmethod
    def _normalized_business_text(value: str) -> str:
        return " ".join(value.casefold().split())

    @staticmethod
    def _contains_affirmed_term(value: str, terms: tuple[str, ...]) -> bool:
        """Do not award semantic credit to explicitly negated keywords."""

        normalized = AcceptanceRunner._normalized_business_text(value)
        for term in terms:
            needle = term.casefold()
            start = 0
            while (index := normalized.find(needle, start)) >= 0:
                clause_start = max(
                    (normalized.rfind(separator, 0, index) for separator in "，。；;.!?！？\n"),
                    default=-1,
                )
                prefix = normalized[clause_start + 1 : index]
                negated = re.search(
                    r"(?:不|未|无|没有|无需|暂不|not|no|without)\s*[^，。；;.!?！？\n]{0,16}$",
                    prefix,
                )
                if negated is None:
                    return True
                start = index + len(needle)
        return False

    @classmethod
    def _assess_deliverable_semantics(
        cls,
        scenario: Scenario,
        contents: dict[str, str],
    ) -> dict[str, Any]:
        """Reject generic or role-agnostic output masquerading as domain work."""

        contracts = scenario_deliverables(scenario)
        contract_paths = tuple(contract.path for contract in contracts)
        configuration_errors: list[str] = []
        if set(contract_paths) != set(scenario.outputs):
            configuration_errors.append("deliverable contracts do not match required output paths")

        empty_outputs: list[str] = []
        concept_failures: dict[str, dict[str, Any]] = {}
        normalized_by_path: dict[str, str] = {}
        for contract in contracts:
            normalized = cls._normalized_business_text(contents.get(contract.path, ""))
            normalized_by_path[contract.path] = normalized
            if len(normalized) < 40:
                empty_outputs.append(contract.path)
                continue
            matched = [concept for concept in contract.concepts if concept.casefold() in normalized]
            required_count = min(2, len(contract.concepts))
            if len(matched) < required_count:
                concept_failures[contract.path] = {
                    "owner_role": contract.owner_role,
                    "purpose": contract.purpose,
                    "required": list(contract.concepts),
                    "matched": matched,
                }

        duplicate_output_groups: list[list[str]] = []
        paths_by_content: dict[str, list[str]] = {}
        for path, normalized in normalized_by_path.items():
            if normalized:
                paths_by_content.setdefault(normalized, []).append(path)
        duplicate_output_groups.extend(paths for paths in paths_by_content.values() if len(paths) > 1)

        combined = "\n".join(normalized_by_path.values())
        missing_decision_signals = [
            list(group) for group in scenario.decision_signals if not any(term.casefold() in combined for term in group)
        ]
        output_signal_failures: dict[str, list[list[str]]] = {}
        for path, normalized in normalized_by_path.items():
            if Path(path).suffix.casefold() not in {".md", ".mdx", ".txt"}:
                continue
            missing_groups = [
                list(group)
                for group in scenario.decision_signals
                if not any(term.casefold() in normalized for term in group)
            ]
            if missing_groups:
                output_signal_failures[path] = missing_groups
        review_outputs_without_challenge = [
            contract.path
            for contract in contracts
            if contract.owner_role in cls.INDEPENDENT_REVIEW_ROLES
            and not any(term in normalized_by_path.get(contract.path, "") for term in cls.CRITIQUE_TERMS)
        ]
        return {
            "configuration_errors": configuration_errors,
            "empty_or_trivial_outputs": empty_outputs,
            "concept_failures": concept_failures,
            "duplicate_output_groups": duplicate_output_groups,
            "missing_decision_signal_groups": missing_decision_signals,
            "output_signal_failures": output_signal_failures,
            "review_outputs_without_challenge": review_outputs_without_challenge,
        }

    @classmethod
    def _assess_role_run_semantics(
        cls,
        scenario: Scenario,
        role_agent_ids: dict[str, str],
        runs: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Verify each professional role produced a distinct, evidence-based decision."""

        agent_roles = {agent_id: role for role, agent_id in role_agent_ids.items() if agent_id}
        results_by_role: dict[str, list[str]] = {role: [] for role in scenario.roles}
        for run in runs:
            role = agent_roles.get(str(run.get("agent_id") or ""))
            if role is None or run.get("status") != "succeeded" or not run.get("work_item_id"):
                continue
            output = run.get("output") if isinstance(run.get("output"), dict) else {}
            result = str(output.get("result") or "").strip()
            if result:
                results_by_role[role].append(result)

        normalized_by_role = {
            role: cls._normalized_business_text("\n".join(results)) for role, results in results_by_role.items()
        }
        missing_or_trivial_roles = [
            ROLES[role].name for role, normalized in normalized_by_role.items() if len(normalized) < 40
        ]
        role_signal_failures: dict[str, list[list[str]]] = {}
        for role, normalized in normalized_by_role.items():
            missing_groups = [
                list(group)
                for group in scenario.decision_signals
                if not any(term.casefold() in normalized for term in group)
            ]
            if missing_groups:
                role_signal_failures[ROLES[role].name] = missing_groups

        role_concept_failures: dict[str, list[str]] = {}
        for role in scenario.roles:
            concepts = {
                concept.casefold()
                for contract in scenario_deliverables(scenario)
                if contract.owner_role == role or contract.review_role == role
                for concept in contract.concepts
            }
            if not concepts:
                continue
            normalized = normalized_by_role[role]
            if sum(concept in normalized for concept in concepts) < min(2, len(concepts)):
                role_concept_failures[ROLES[role].name] = sorted(concepts)

        roles_by_content: dict[str, list[str]] = {}
        for role, normalized in normalized_by_role.items():
            if normalized:
                roles_by_content.setdefault(normalized, []).append(ROLES[role].name)
        duplicate_role_result_groups = [roles for roles in roles_by_content.values() if len(roles) > 1]
        reviewers_without_challenge = [
            ROLES[role].name
            for role, normalized in normalized_by_role.items()
            if role in cls.INDEPENDENT_REVIEW_ROLES
            and normalized
            and not any(term in normalized for term in cls.CRITIQUE_TERMS)
        ]
        mechanical_status_role_results = []
        for role, normalized in normalized_by_role.items():
            matched_status_terms = [term for term in cls.MECHANICAL_STATUS_TERMS if term in normalized]
            missing_decision_groups = [
                group for group in scenario.decision_signals if not any(term.casefold() in normalized for term in group)
            ]
            if matched_status_terms and (len(matched_status_terms) >= 2 or missing_decision_groups):
                mechanical_status_role_results.append(ROLES[role].name)
        return {
            "missing_or_trivial_role_results": missing_or_trivial_roles,
            "role_signal_failures": role_signal_failures,
            "role_concept_failures": role_concept_failures,
            "duplicate_role_result_groups": duplicate_role_result_groups,
            "reviewers_without_challenge": reviewers_without_challenge,
            "mechanical_status_role_results": mechanical_status_role_results,
        }

    @classmethod
    def _assess_conversation_semantics(
        cls,
        scenario: Scenario,
        role_agent_ids: dict[str, str],
        conversations: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Verify visible project dialogue contains real role-specific reasoning.

        Deliverables and Run results can look complete even when the actual chat is
        only acknowledgements and status narration.  This check reads the standard
        Web Chat message rows from every project Run/A2A session plus the group
        session, and evaluates the assistant turns that users can actually inspect.
        """

        agent_roles = {agent_id: role for role, agent_id in role_agent_ids.items() if agent_id}
        turns_by_role: dict[str, list[tuple[str, str]]] = {role: [] for role in scenario.roles}
        for conversation in conversations:
            fallback_agent_id = str(conversation.get("agent_id") or "")
            session_id = str(conversation.get("session_id") or "unknown-session")
            for index, message in enumerate(conversation.get("messages") or []):
                if str(message.get("role") or "") != "assistant":
                    continue
                sender_agent_id = str(message.get("sender_agent_id") or fallback_agent_id)
                role = agent_roles.get(sender_agent_id)
                content = str(message.get("content") or "").strip()
                if role is None or not content:
                    continue
                message_id = str(message.get("id") or f"{session_id}:{index}")
                turns_by_role[role].append((message_id, content))

        missing_professional_dialogue_roles = [ROLES[role].name for role, turns in turns_by_role.items() if not turns]
        trivial_professional_turns: list[str] = []
        mechanical_professional_turns: list[str] = []
        unprofessional_a2a_request_turns: list[str] = []
        for conversation in conversations:
            if conversation.get("kind") != "a2a":
                continue
            session_id = str(conversation.get("session_id") or "unknown-session")
            for index, message in enumerate(conversation.get("messages") or []):
                if str(message.get("role") or "") != "user":
                    continue
                content = cls._normalized_business_text(str(message.get("content") or ""))
                if not content:
                    continue
                message_id = str(message.get("id") or f"{session_id}:{index}")
                has_professional_request = cls._contains_affirmed_term(
                    content,
                    cls.PROFESSIONAL_REQUEST_TERMS,
                ) or any(mark in content for mark in ("?", "？"))
                has_mechanical_status = any(term in content for term in cls.MECHANICAL_STATUS_TERMS)
                if (len(content) < 20 or has_mechanical_status) and not has_professional_request:
                    unprofessional_a2a_request_turns.append(message_id)
        normalized_by_role: dict[str, str] = {}
        for role, turns in turns_by_role.items():
            normalized_turns: list[str] = []
            role_concepts = {
                concept.casefold()
                for contract in scenario_deliverables(scenario)
                if contract.owner_role == role or contract.review_role == role
                for concept in contract.concepts
            }
            for message_id, content in turns:
                normalized = cls._normalized_business_text(content)
                normalized_turns.append(normalized)
                if len(normalized) < 40:
                    trivial_professional_turns.append(message_id)
                    continue
                signal_count = sum(
                    cls._contains_affirmed_term(normalized, group) for group in scenario.decision_signals
                )
                concept_count = sum(concept in normalized for concept in role_concepts)
                matched_status_terms = [term for term in cls.MECHANICAL_STATUS_TERMS if term in normalized]
                if matched_status_terms and (signal_count < 3 or concept_count == 0):
                    mechanical_professional_turns.append(message_id)
            normalized_by_role[role] = "\n".join(normalized_turns)

        role_dialogue_signal_failures: dict[str, list[list[str]]] = {}
        role_dialogue_concept_failures: dict[str, list[str]] = {}
        for role, normalized in normalized_by_role.items():
            if not normalized:
                continue
            missing_groups = [
                list(group) for group in scenario.decision_signals if not cls._contains_affirmed_term(normalized, group)
            ]
            if missing_groups:
                role_dialogue_signal_failures[ROLES[role].name] = missing_groups
            concepts = {
                concept.casefold()
                for contract in scenario_deliverables(scenario)
                if contract.owner_role == role or contract.review_role == role
                for concept in contract.concepts
            }
            if concepts and sum(concept in normalized for concept in concepts) < min(2, len(concepts)):
                role_dialogue_concept_failures[ROLES[role].name] = sorted(concepts)

        reviewers_without_dialogue_challenge = [
            ROLES[role].name
            for role, normalized in normalized_by_role.items()
            if role in cls.INDEPENDENT_REVIEW_ROLES
            and normalized
            and not any(term in normalized for term in cls.CRITIQUE_TERMS)
        ]
        all_dialogue = "\n".join(normalized_by_role.values())
        dialogue_without_tradeoff = not any(term in all_dialogue for term in cls.TRADEOFF_TERMS)
        return {
            "missing_professional_dialogue_roles": missing_professional_dialogue_roles,
            "trivial_professional_turn_ids": trivial_professional_turns,
            "mechanical_professional_turn_ids": mechanical_professional_turns,
            "unprofessional_a2a_request_turn_ids": unprofessional_a2a_request_turns,
            "role_dialogue_signal_failures": role_dialogue_signal_failures,
            "role_dialogue_concept_failures": role_dialogue_concept_failures,
            "reviewers_without_dialogue_challenge": reviewers_without_dialogue_challenge,
            "dialogue_without_tradeoff": dialogue_without_tradeoff,
        }

    @classmethod
    def _assess_collaboration_topology(
        cls,
        scenario: Scenario,
        role_agent_ids: dict[str, str],
        conversations: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Require a connected, professional point-to-point collaboration graph.

        Distinct role outputs are not sufficient evidence of collaboration: five
        agents can independently write files while the project owner performs all
        integration.  A real cross-functional project must contain professional
        A2A requests that connect every role, and every declared independent review
        contract must be represented by a direct owner-reviewer exchange.
        """

        role_by_agent_id = {agent_id: role for role, agent_id in role_agent_ids.items() if agent_id}
        professional_edges: set[frozenset[str]] = set()
        directed_edges: set[tuple[str, str]] = set()
        requests_without_sender: list[str] = []

        for conversation in conversations:
            if conversation.get("kind") != "a2a":
                continue
            target_role = role_by_agent_id.get(str(conversation.get("agent_id") or ""))
            if target_role is None:
                continue
            session_id = str(conversation.get("session_id") or "unknown-session")
            for index, message in enumerate(conversation.get("messages") or []):
                if str(message.get("role") or "") != "user":
                    continue
                content = cls._normalized_business_text(str(message.get("content") or ""))
                if not content:
                    continue
                has_professional_request = cls._contains_affirmed_term(
                    content,
                    cls.PROFESSIONAL_REQUEST_TERMS,
                ) or any(mark in content for mark in ("?", "？"))
                if not has_professional_request:
                    continue
                message_id = str(message.get("id") or f"{session_id}:{index}")
                source_role = role_by_agent_id.get(str(message.get("sender_agent_id") or ""))
                if source_role is None:
                    requests_without_sender.append(message_id)
                    continue
                if source_role == target_role:
                    continue
                directed_edges.add((source_role, target_role))
                professional_edges.add(frozenset((source_role, target_role)))

        adjacency: dict[str, set[str]] = {role: set() for role in scenario.roles}
        for edge in professional_edges:
            first, second = tuple(edge)
            adjacency[first].add(second)
            adjacency[second].add(first)

        isolated_roles = [ROLES[role].name for role in scenario.roles if not adjacency[role]]
        visited: set[str] = set()
        if scenario.roles:
            stack = [scenario.roles[0]]
            while stack:
                role = stack.pop()
                if role in visited:
                    continue
                visited.add(role)
                stack.extend(adjacency[role] - visited)
        disconnected_roles = [ROLES[role].name for role in scenario.roles if role not in visited]

        required_review_pairs = {
            frozenset((contract.owner_role, contract.review_role))
            for contract in scenario_deliverables(scenario)
            if contract.review_role and contract.review_role != contract.owner_role
        }
        missing_review_handoffs = [
            " ↔ ".join(sorted(ROLES[role].name for role in pair))
            for pair in sorted(required_review_pairs - professional_edges, key=lambda value: sorted(value))
        ]
        rendered_edges = [f"{ROLES[source].name} → {ROLES[target].name}" for source, target in sorted(directed_edges)]
        return {
            "professional_a2a_edges": rendered_edges,
            "professional_a2a_edge_count": len(professional_edges),
            "professional_requests_without_sender_ids": requests_without_sender,
            "isolated_collaboration_roles": isolated_roles,
            "disconnected_collaboration_roles": disconnected_roles,
            "missing_review_handoffs": missing_review_handoffs,
        }

    async def _load_project_conversations(
        self,
        *,
        project_id: str,
        group_session_id: str,
        runs: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Load the visible group and exact Run sessions through public APIs."""

        group_payload = await self.request(
            "GET",
            f"/projects/{project_id}/group-sessions/{group_session_id}/messages",
            params={"limit": 500},
        )
        conversations: list[dict[str, Any]] = [
            {
                "kind": "group",
                "session_id": group_session_id,
                "agent_id": "",
                "messages": group_payload.get("items", []),
            }
        ]
        session_kinds: dict[tuple[str, str], str] = {}
        for run in runs:
            key = (str(run.get("agent_id") or ""), str(run.get("session_id") or ""))
            if not all(key) or run.get("status") != "succeeded":
                continue
            kind = "a2a" if run.get("trigger_type") == "a2a" else "run"
            if kind == "a2a" or key not in session_kinds:
                session_kinds[key] = kind
        ordered_sessions = sorted(session_kinds)
        payloads = await asyncio.gather(
            *(
                self.request(
                    "GET",
                    f"/agents/{agent_id}/sessions/{session_id}/messages",
                    params={"limit": 500},
                )
                for agent_id, session_id in ordered_sessions
            )
        )
        conversations.extend(
            {
                "kind": session_kinds[(agent_id, session_id)],
                "session_id": session_id,
                "agent_id": agent_id,
                "messages": payload,
            }
            for (agent_id, session_id), payload in zip(ordered_sessions, payloads, strict=True)
        )
        return conversations
