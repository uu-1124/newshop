"""Verify bounded core-feature delivery evidence without changing business state."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUN = ROOT / "deployment/.run"


def log(name):
    return (RUN / name).read_text(encoding="utf-8", errors="replace")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


assert "111 passed" in log("core-final-acceptance-tests-20261114.log")
assert "pass 61" in log("core-buyer-final-tests-20261114.log")
for name in ["core-buyer-final-types-20261114.log", "core-admin-delivery-types-20261114.log"]:
    assert not log(name).strip()
assert "Build complete" in log("core-buyer-final-build-20261114.log")
assert "built in" in log("core-admin-delivery-build-20261114.log")
java = log("core-rules-java-expanded-20261114.log")
assert "BUILD SUCCESS" in java
assert all(f"Tests run: {n}, Failures: 0, Errors: 0, Skipped: 0" in java for n in [3, 38])
java = log("core-rules-java-focused-20261114.log")
assert "BUILD SUCCESS" in java and "Tests run: 3, Failures: 0, Errors: 0, Skipped: 0" in java
order = json.loads((RUN / "completion-order-choices-final-state-20261114.json").read_text(encoding="utf-8"))
assert (
    order["cleaned"]
    and order["original_business_stocks_policies_unchanged"]
    and order["historical_calls_unchanged"]
)
assert all(v == 0 for v in order["active"].values()) and order["new_unlinked_calls"] == 0
for case, n in [("GENERAL", 7), ("ENTRY", 2)]:
    assert len(order["cases"][case]) == n and all(t["review"] == "PASSED" for t in order["cases"][case])
core = json.loads((RUN / "core-functions-live-20261114.json").read_text(encoding="utf-8"))
assert core["cleaned"] and core["rule_read"]["active_rule_version"] == "ITEM_NET_DEMO_V2"
for service in ["admin", "portal"]:
    assert sha(ROOT / f"mall-swarm/mall-{service}/target/mall-{service}-1.0-SNAPSHOT.jar") == sha(
        RUN / f"agent-jars/mall-{service}.jar"
    )
groups = {
    "mall-agent/app/application/": [
        "feedback.py",
        "media.py",
        "attachments.py",
        "conversations.py",
        "conversation_cleanup.py",
        "retention.py",
        "governance.py",
        "runtime_check.py",
    ],
    "mall-agent/app/infrastructure/": [
        "audio.py",
        "aliyun_models.py",
        "model_call_context.py",
        "model_ledger.py",
    ],
    "mall-agent/app/api/": ["member.py", "dto.py", "orders.py"],
    "mall-agent/app/": ["bootstrap.py", "main.py"],
    "mall-agent/app/agents/": ["dialog.py", "order_choices.py"],
    "mall-agent/tests/": [
        "test_member_media.py",
        "test_feedback_and_human_images.py",
        "test_return_rule_api.py",
        "test_order_choices_graph.py",
    ],
    "mall-agent/scripts/": [
        "verify_core_live.py",
        "verify_core_java.py",
        "verify_core_delivery.py",
        "evaluate_order_choices.py",
    ],
    "mall-app-web/src/services/": ["agent.ts", "agentAudio.ts", "agent-contracts.ts"],
    "mall-app-web/src/pages/agent/": [
        "chat.vue",
        "AnswerFeedback.vue",
        "AnswerSpeech.vue",
        "VoiceComposer.vue",
        "OrderBrowser.vue",
    ],
    "mall-app-web/test/": ["agent-audio.test.mjs", "media-components.test.mjs"],
    "mall-admin-web/src/apis/": ["agent.ts", "agent-contracts.ts"],
    "mall-admin-web/src/views/agent/": ["Operations.vue", "ReturnRules.vue", "Governance.vue"],
    "deployment/": ["init/agent-after-sale-rules.sql", "prepare-order-return-schema.cjs", "backup_data.py"],
    "docs/": ["api/openapi.json", "architecture/coverage.json", "47-核心媒体反馈与售后规则收尾验收.md"],
}
sources = [ROOT / (prefix + name) for prefix, names in groups.items() for name in names]
sources += [
    ROOT / name
    for name in [
        "mall-agent/migrations/versions/0011_media_requests.py",
        "mall-swarm/mall-mbg/src/main/java/com/macro/mall/order/AgentAfterSaleStore.java",
        "mall-swarm/mall-admin/src/main/java/com/macro/mall/controller/AgentAfterSaleBridgeController.java",
        "mall-swarm/mall-portal/src/main/java/com/macro/mall/portal/service/AgentOrderLifecycle.java",
        "mall-swarm/mall-portal/src/test/java/com/macro/mall/portal/service/AgentOrderDatabaseTest.java",
        "mall-swarm/mall-admin/src/test/java/com/macro/mall/controller/AgentAfterSaleBridgeTest.java",
    ]
]
evidence = [p for p in RUN.glob("core-*20261114*") if p.is_file()]
result = {
    "verified_at": datetime.now(UTC).isoformat(),
    "scope": ["voice", "feedback", "human_member_images", "return_rule_versions", "order_choices"],
    "checks": {
        "python": 111,
        "buyer_frontend": 61,
        "java_expanded": 41,
        "java_final_focused": 3,
        "order_general_turns": 7,
        "order_entry_turns": 2,
        "livez": order["livez"],
        "readyz": order["readyz"],
        "migration": order["counts"]["migration"],
        "unknown_meters_preserved": order["counts"]["unknown_meters"],
        "original_facts_unchanged": True,
        "cleaned": True,
    },
    "limits": [
        "Physical microphone capture awaits device permission; request/cancel verified in browser, capture/late-result/encoding verified by component tests, real ASR uses synthesized test voice.",
        "H5 acceptance only; whole-project performance and fault matrix remain separately scoped.",
    ],
    "files": [{"path": p.relative_to(ROOT).as_posix(), "sha256": sha(p)} for p in sources + evidence],
    "java_packages": {s: sha(RUN / f"agent-jars/mall-{s}.jar") for s in ["admin", "portal"]},
}
(ROOT / "deployment/core-functions-manifest.json").write_text(
    json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
print(
    json.dumps(
        {
            "verified": True,
            "source_files": len(sources),
            "evidence_files": len(evidence),
            "checks": result["checks"],
        },
        ensure_ascii=False,
    )
)
