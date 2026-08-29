"""Capability catalog for Jarvis feature discovery and approval routing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Risk = Literal["low", "medium", "high", "blocked"]


@dataclass(frozen=True, slots=True)
class Capability:
    key: str
    name: str
    description: str
    risk: Risk
    requires_approval: bool
    sandbox_required: bool
    default_enabled: bool = False


CAPABILITIES: tuple[Capability, ...] = (
    Capability("files.index_search", "File indexing and search", "Index, classify, deduplicate, and retrieve authorized files and documents.", "medium", True, True),
    Capability("files.ocr_extract", "OCR and document extraction", "Extract text and metadata from authorized PDFs, images, and scans.", "medium", True, True),
    Capability("files.lineage", "Data lineage", "Track source, transformations, and artifact lineage inside the user's workspace.", "low", False, False),
    Capability("coding.project_builder", "Project builder", "Create and modify projects, tests, documentation, containers, and CI files.", "medium", True, True),
    Capability("coding.test_repair", "Test and repair", "Run tests and static checks in an isolated workspace and propose patches.", "medium", True, True),
    Capability("coding.sandbox_execute", "Sandboxed code execution", "Execute reviewed code with no network by default and bounded resources.", "high", True, True),
    Capability("vision.screenshot_understanding", "Screenshot understanding", "Inspect user-provided screenshots and authorized browser screenshots.", "medium", True, True),
    Capability("automation.workflow_orchestration", "Workflow orchestration", "Run bounded dependency graphs with checkpoints, progress, retries, and cancellation.", "medium", True, True),
    Capability("simulation.3d_preview", "3D asset preview", "Inspect and render previews of authorized STL, OBJ, glTF, and related assets.", "medium", True, True),
    Capability("simulation.engineering_report", "Engineering simulation report", "Run available engineering/physics simulations in a separate worker and produce measurements.", "high", True, True),
    Capability("security.asset_audit", "Authorized security audit", "Audit explicitly allowlisted assets, dependencies, TLS, containers, and logs.", "high", True, True),
    Capability("security.malware_quarantine", "Malware quarantine", "Hash, isolate, and report suspicious files without deleting evidence automatically.", "high", True, True),
    Capability("security.unauthorized_intrusion", "Unauthorized intrusion", "Break into systems, bypass firewalls, steal data, or evade access controls.", "blocked", True, True),
    Capability("browser.navigation", "Chrome navigation", "Navigate, scroll, inspect DOM/accessibility, fill drafts, and capture screenshots.", "medium", True, True),
    Capability("browser.external_submit", "External submission", "Send forms, publish, buy, delete, or change account settings.", "high", True, True),
    Capability("integrations.oauth_connector", "OAuth connector factory", "Build least-privilege connectors from public documentation and mocks.", "high", True, True),
    Capability("proactive.checkins", "Proactive check-ins", "Send bounded text/voice check-ins using schedules, cooldowns, quiet hours, and caps.", "medium", True, False),
    Capability("natural.voice_context", "Natural voice interaction", "Transcribe, understand context, and respond with provider-backed speech.", "medium", True, True),
    Capability("planning.alternative_paths", "Alternative planning", "Generate and compare fallback plans when a task fails, without applying side effects automatically.", "low", False, False),
    Capability("engineering.architecture_review", "Architecture review", "Compare system designs, tradeoffs, dependencies, risks, and migration paths.", "medium", True, True),
    Capability("engineering.adr_writer", "ADR and documentation writer", "Create architecture decision records, runbooks, API docs, and release notes from approved context.", "low", False, False),
    Capability("engineering.release_manager", "Release manager", "Prepare changelogs, version checks, CI gates, staged rollouts, and rollback plans.", "high", True, True),
    Capability("engineering.incident_response", "Incident response", "Correlate logs and alerts, build an incident timeline, propose containment, and draft a postmortem.", "high", True, True),
    Capability("product.roadmap", "Product roadmap assistant", "Turn goals and feedback into scoped milestones, dependencies, acceptance criteria, and priorities.", "medium", True, True),
    Capability("product.requirements", "Requirements analyst", "Convert natural-language ideas into specifications, edge cases, test plans, and delivery estimates.", "low", False, False),
    Capability("business.executive_digest", "Executive digest", "Summarize approved project, delivery, risk, and operations signals for leadership review.", "medium", True, True),
    Capability("business.cost_observer", "Cost and usage observer", "Track configured provider usage, quotas, latency, and estimated spend without exposing secrets.", "medium", True, True),
    Capability("research.news_monitor", "News and change monitor", "Watch allowlisted sources, deduplicate changes, summarize them, and notify through approved channels.", "medium", True, True),
    Capability("quality.evaluation", "Evaluation harness", "Run regression suites, benchmark agents, compare outputs, and retain auditable scorecards.", "medium", True, True),
    Capability("operations.backup_restore", "Backup and restore assistant", "Plan, verify, and restore encrypted configuration/data backups with explicit confirmation.", "high", True, True),
    Capability("finance.market_observer", "Market observer", "Read authorized market data, timestamp it, and surface risk/opportunity signals without trading.", "high", True, True),
    Capability("finance.project_budget", "Project budget planner", "Estimate materials, compute, labor, and provider costs with scenario assumptions.", "medium", True, True),
    Capability("finance.risk_report", "Financial risk report", "Produce source-dated risk dashboards and scenario analysis; never place trades automatically.", "high", True, True),
    Capability("operations.resource_allocator", "Compute resource allocator", "Assign jobs to approved workers under CPU, RAM, concurrency, and quota budgets.", "high", True, True),
    Capability("operations.disaster_recovery", "Disaster recovery", "Verify encrypted backups, rehearse restore, and prepare rollback plans.", "high", True, True),
    Capability("devices.home_automation", "Owned-device automation", "Control allowlisted Home Assistant/MQTT devices with state checks and confirmation.", "high", True, True),
    Capability("devices.device_status", "Device status monitor", "Read status from explicitly paired devices and notify on configured changes.", "medium", True, True),
)


def get_capability(key: str) -> Capability:
    for capability in CAPABILITIES:
        if capability.key == key:
            return capability
    raise KeyError(key)


def capabilities_for_catalog() -> list[dict[str, object]]:
    return [
        {
            "key": item.key,
            "name": item.name,
            "description": item.description,
            "risk": item.risk,
            "requires_approval": item.requires_approval,
            "sandbox_required": item.sandbox_required,
            "default_enabled": item.default_enabled,
        }
        for item in CAPABILITIES
    ]


__all__ = ["CAPABILITIES", "Capability", "capabilities_for_catalog", "get_capability"]
