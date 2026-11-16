"""Run existing isolated MySQL acceptance with credentials kept out of command arguments."""

import argparse
import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
parser = argparse.ArgumentParser()
parser.add_argument("--focused", action="store_true")
args = parser.parse_args()
cfg = json.loads((ROOT / "deployment/config/portal.yaml").read_text(encoding="utf-8"))["spring"]["datasource"]
url = cfg["url"].replace("/mall?", "/mall_agent_cart_test?")
if not url.startswith("jdbc:mysql://127.0.0.1:13306/mall_agent_cart_test?"):
    raise SystemExit("Refusing a non-isolated Java acceptance target")
env = {
    **os.environ,
    "JAVA_HOME": r"D:\develop\jdk-17",
    "MALL_CART_TEST_JDBC": url,
    "MALL_CART_TEST_USER": json.loads((ROOT/'deployment/.run/cart-test-env.json').read_text())['user'],
    "MALL_CART_TEST_PASSWORD": json.loads((ROOT/'deployment/.run/cart-test-env.json').read_text())['password'],
}
selected = "AgentOrderDatabaseTest#rulesAreImmutableIdempotentAndScheduledRulesDoNotApplyEarly+ruleSwitchInvalidatesOldQuotesAndExistingReturnsKeepOriginalRule+changedRuleWindowAndShippedPermissionUseCurrentFacts"
if not args.focused:
    selected = "AgentOrderDatabaseTest,AgentAfterSaleBridgeTest,OmsPromotionPriceSecurityTest"
log = (
    ROOT
    / "deployment/.run"
    / ("core-rules-java-focused-20261114.log" if args.focused else "core-rules-java-expanded-20261114.log")
)
command = [
    r"D:\develop\maven\bin\mvn.cmd",
    r"-Dmaven.repo.local=D:\develop\cache\maven",
    "-pl",
    "mall-portal,mall-admin",
    "-am",
    "-Ddocker.skip=true",
    "-DskipTests=false",
    "-Dmaven.test.skip=false",
    "-Dsurefire.failIfNoSpecifiedTests=false",
    "-Duser.timezone=Asia/Shanghai",
    "-Dtest=" + selected,
    "package",
]
with log.open("w", encoding="utf-8") as output:
    result = subprocess.run(
        command, cwd=ROOT / "mall-swarm", env=env, stdout=output, stderr=subprocess.STDOUT
    )
print("Java acceptance exit", result.returncode, "log", log.name)
raise SystemExit(result.returncode)
