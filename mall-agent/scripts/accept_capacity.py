"""A08 real local gateway acceptance: 10 buyers, 3 staff, >=15 minutes.

No fixture provider, database writes, payment, refund or order confirmation.
Dedicated conversations/accounts are recorded before use and cleaned via APIs.
Credentials, message text and SSE payloads are never written to evidence/logs.
"""
# ruff: noqa: E402 -- install the registered workspace helper paths before imports.

import argparse
import asyncio
import io
import json
import secrets
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "deployment"), str(Path(__file__).parent)]
from backup_data import pg_connection
from expansion_client import ADMIN, MEMBER, login

from app.runtime import run

BASE = "http://127.0.0.1:8201"
BUYERS = [
    "agent-order-af9d323d10",
    "agent-order-d6695cb5e6",
    "agent-order-00ca27bf08",
    "agent-order-ab2d711b5c",
    "agent-order-826885e839",
    "agent-pricing-68455e784b",
    "agent-conversation-bd63b203",
    "agent-lifecycle-c1eeefdd",
    "agent-lifecycle-6c72a0f5",
    "member",
]


class Rejected(Exception):
    pass


def percentile(values, q):
    values = sorted(values)
    return round(values[min(len(values) - 1, int(len(values) * q))], 2) if values else None


class Acceptance:
    def __init__(self, duration):
        if duration < 900:
            raise Rejected("A08 requires at least 900 seconds")
        self.duration = duration
        self.id = str(uuid4())
        self.path = ROOT / "deployment/.run" / ("capacity-" + self.id + ".json")
        self.state = {
            "id": self.id,
            "created_at": datetime.now(UTC).isoformat(),
            "result": "PREPARING",
            "required_seconds": duration,
            "buyers": [],
            "staff": [],
            "checks": [],
            "errors": [],
            "samples": [],
            "model_runs": [],
            "paid_models_enabled": True,
            "business_confirmations": 0,
            "real_financial_calls": 0,
        }
        self.metrics = defaultdict(list)
        self.clients, self.staff_clients, self.tasks = [], [], []
        self.streams = {}
        self.expected_messages = defaultdict(set)
        self.owned_by_staff = defaultdict(set)
        self.stop = asyncio.Event()
        self.started = self.deadline = None
        self.operator = None
        self.save()

    def save(self):
        self.state["http"] = {
            name: {
                "samples": len(v),
                "p50_ms": percentile(v, 0.5),
                "p95_ms": percentile(v, 0.95),
                "p99_ms": percentile(v, 0.99),
            }
            for name, v in self.metrics.items()
        }
        self.state["streams"] = {
            name: {k: (sorted(v) if isinstance(v, set) else v) for k, v in value.items()}
            for name, value in self.streams.items()
        }
        self.path.write_text(
            json.dumps(self.state, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
        )

    async def api(
        self,
        client,
        path,
        method="GET",
        body=None,
        key=None,
        expected=200,
        label=None,
        legacy=False,
        **kwargs,
    ):
        headers = {"Idempotency-Key": str(key or uuid4())} if method != "GET" and not legacy else {}
        started = time.monotonic()
        response = await client.request(method, path, json=body, headers=headers, **kwargs)
        elapsed = (time.monotonic() - started) * 1000
        code = response.json().get("code")
        if response.status_code != expected or code != (200 if legacy else "OK"):
            safe_code = code if isinstance(code, (str, int)) and len(str(code)) < 100 else "INVALID_CODE"
            raise Rejected(f"{label or 'gateway'} HTTP={response.status_code} code={safe_code}")
        if self.started and time.monotonic() <= self.deadline and not legacy:
            self.metrics[label or method].append(elapsed)
        return response.json().get("data")

    async def pause(self, seconds):
        try:
            await asyncio.wait_for(self.stop.wait(), seconds)
        except TimeoutError:
            pass

    def task(self, awaitable):
        async def guarded():
            try:
                await awaitable
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.state["errors"].append(
                    str(error) if isinstance(error, Rejected) else type(error).__name__
                )
                self.save()
                self.stop.set()

        value = asyncio.create_task(guarded())
        self.tasks.append(value)
        return value

    @staticmethod
    def client():
        return httpx.AsyncClient(
            base_url=BASE,
            trust_env=False,
            timeout=httpx.Timeout(45, read=45),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=8),
        )

    def database(self):
        cids = [entry["conversation_id"] for entry in self.state["buyers"] if entry.get("conversation_id")]
        with pg_connection(ROOT, True) as conn:
            runs = [
                dict(r)
                for r in conn.execute(
                    """SELECT id,conversation_id,state,created_at,started_at,finished_at,error_code,
                (SELECT min(created_at) FROM ag_event_outbox e WHERE e.event_type='message.delta' AND e.payload->>'run_id'=r.id::text) first_text_at
                FROM ag_run r WHERE conversation_id=ANY(%s::uuid[]) ORDER BY created_at""",
                    (cids,),
                )
            ]
            counts = dict(
                conn.execute("""SELECT
                (SELECT count(*) FROM ag_generation_slot WHERE run_id IS NOT NULL) active_slots,
                (SELECT count(*) FROM ag_run WHERE state='RUNNING') running,
                (SELECT count(*) FROM ag_run WHERE state='QUEUED') queued,
                (SELECT count(*) FROM ag_run WHERE state IN ('QUEUED','RUNNING','STOP_REQUESTED')
                    GROUP BY conversation_id HAVING count(*)>1 LIMIT 1) duplicated_conversation_runs""").fetchone()
            )
            messages = [
                dict(r)
                for r in conn.execute(
                    """SELECT id,conversation_id,client_message_id,sender_kind,sender_id,seq
                FROM ag_message WHERE conversation_id=ANY(%s::uuid[]) AND deleted_at IS NULL""",
                    (cids,),
                )
            ]
            meters = [
                dict(r)
                for r in conn.execute(
                    """SELECT c.id,c.run_id,c.capability,c.status,c.latency_ms,m.usage_state
                FROM ag_model_call c LEFT JOIN ag_model_meter m ON m.call_id=c.id
                WHERE c.run_id IN (SELECT id FROM ag_run WHERE conversation_id=ANY(%s::uuid[])) ORDER BY c.created_at""",
                    (cids,),
                )
            ]
            runtime = conn.execute(
                "SELECT value FROM ag_setting WHERE key='agent-observability-v1'"
            ).fetchone()
            actions = conn.execute(
                "SELECT count(*) n FROM ag_action WHERE conversation_id=ANY(%s::uuid[])", (cids,)
            ).fetchone()["n"]
        return {
            "runs": runs,
            "messages": messages,
            "model_calls": meters,
            "runtime": runtime["value"] if runtime else None,
            "counts": counts,
            "actions": actions,
        }

    async def prepare(self):
        self.operator = self.client()
        await login(self.operator, True)
        resources = await self.api(self.operator, "/mall-admin/role/listResource/10", legacy=True)
        names = {r["name"] for r in resources}
        if not {"agent:events:subscribe", "agent:conversation:reply", "agent:attachment:upload"} <= names:
            raise Rejected("The registered reception role is not sufficient")
        # Existing synthetic buyers plus the normal demo member, each in a new owned conversation.
        for username in BUYERS:
            client = self.client()
            self.clients.append(client)
            await login(client, username=username)
            identity = await self.api(client, MEMBER + "/me")
            self.state["buyers"].append({"username": username, "member_id": identity["id"]})
            self.save()
            conv = await self.api(
                client,
                MEMBER + "/conversations",
                "POST",
                {"title": "[TEST ONLY] capacity " + self.id[:8], "entry_context": {"kind": "GENERAL"}},
                expected=201,
            )
            self.state["buyers"][-1]["conversation_id"] = conv["id"]
            self.save()
        if len({b["member_id"] for b in self.state["buyers"]}) != 10:
            raise Rejected("Ten distinct buyer identities are required")
        for i in range(3):
            username = "capacity_" + self.id[:8] + "_" + str(i)
            password = secrets.token_urlsafe(24) + "aA1!"
            account = await self.api(
                self.operator,
                "/mall-admin/admin/register",
                "POST",
                {
                    "username": username,
                    "password": password,
                    "nickName": "[TEST ONLY] capacity " + str(i),
                    "email": username + "@example.invalid",
                    "note": "A08 synthetic staff; disable after acceptance",
                },
                legacy=True,
            )
            self.state["staff"].append({"username": username, "id": str(account["id"])})
            self.save()
            await self.api(
                self.operator,
                "/mall-admin/admin/role/update",
                "POST",
                legacy=True,
                data={"adminId": account["id"], "roleIds": "10"},
            )
            client = self.client()
            self.staff_clients.append(client)
            response = await client.post(
                "/mall-admin/admin/login", json={"username": username, "password": password}
            )
            value = response.json()
            if value.get("code") != 200:
                raise Rejected("Temporary staff login failed")
            client.headers["Authorization"] = value["data"]["tokenHead"] + value["data"]["token"]
            await self.api(client, ADMIN + "/me")
            profile = next(
                x
                for x in (await self.api(client, ADMIN + "/staff?limit=100"))["items"]
                if x["id"] == str(account["id"])
            )
            await self.api(
                client,
                ADMIN + "/presence",
                "PUT",
                {"expected_version": profile["version"], "presence": "ONLINE"},
            )
            self.task(self.staff_heartbeat(i))
        # Six waiting/human conversations first; buyer 3 exercises the fourth AI slot before joining them.
        for i in range(4, 10):
            await self.handoff(i)
        self.state["checks"].append(
            "10 distinct buyers, 3 distinct reception staff, API-created owned fixtures"
        )
        self.save()

    async def handoff(self, index):
        client = self.clients[index]
        cid = self.state["buyers"][index]["conversation_id"]
        snap = await self.api(client, MEMBER + "/conversations/" + cid)
        handoff = await self.api(
            client,
            MEMBER + f"/conversations/{cid}/handoff",
            "POST",
            {
                "expected_conversation_version": snap["conversation"]["version"],
                "reason": "[TEST ONLY] A08 continuous reception",
            },
            expected=201,
        )
        si = (index - 3) % 3
        claimed = await self.api(
            self.staff_clients[si],
            ADMIN + "/handoffs/" + handoff["id"] + "/claim",
            "POST",
            {"expected_version": handoff["version"]},
        )
        self.state["buyers"][index]["handoff_id"] = claimed["id"]
        self.state["buyers"][index]["staff_index"] = si
        self.owned_by_staff[si].add(cid)
        self.save()

    async def stream(self, name, client, path, cid=None, staff_index=None):
        state = self.streams.setdefault(
            name,
            {
                "connected": False,
                "connections": 0,
                "events": 0,
                "cursor": "0",
                "message_ids": set(),
                "event_ids": set(),
                "duplicate_events": 0,
                "first_delta": {},
            },
        )
        while not self.stop.is_set():
            after = "&" if "?" in path else "?"
            async with client.stream(
                "GET", path + after + "after=" + state["cursor"], timeout=httpx.Timeout(45, read=45)
            ) as response:
                if response.status_code != 200:
                    raise Rejected(name + " SSE HTTP=" + str(response.status_code))
                state["connected"] = True
                state["connections"] += 1
                data = []
                try:
                    async for line in response.aiter_lines():
                        if self.stop.is_set():
                            break
                        if line.startswith("data:"):
                            data.append(line[5:].strip())
                        if line == "" and data:
                            envelope = json.loads("".join(data))
                            data = []
                            event_id = envelope["event_id"]
                            ecid = envelope["conversation_id"]
                            if cid and ecid != cid:
                                raise Rejected(name + " foreign conversation event")
                            if (
                                staff_index is not None
                                and ecid
                                and ecid not in self.owned_by_staff[staff_index]
                            ):
                                raise Rejected(name + " unassigned conversation event")
                            if cid and envelope["type"] == "note.created":
                                raise Rejected("Internal note reached buyer")
                            state["duplicate_events"] += int(event_id in state["event_ids"])
                            state["event_ids"].add(event_id)
                            state["events"] += 1
                            state["cursor"] = event_id
                            payload = envelope["payload"]
                            mid = payload.get("message_id") or payload.get("message", {}).get("id")
                            if mid:
                                state["message_ids"].add(mid)
                            if envelope["type"] == "message.delta":
                                state["first_delta"].setdefault(
                                    payload["run_id"], datetime.now(UTC).isoformat()
                                )
                finally:
                    state["connected"] = False
            await self.pause(0.3)

    async def send(self, index, text):
        client = self.clients[index]
        cid = self.state["buyers"][index]["conversation_id"]
        key = str(uuid4())
        body = {"client_message_id": str(uuid4()), "text": text, "attachment_ids": []}
        result = await self.api(
            client, MEMBER + f"/conversations/{cid}/messages", "POST", body, key, 202, label="member_send"
        )
        self.expected_messages[cid].add(result["message"]["id"])
        return result, body, key

    async def initial_slots(self):
        initial = await asyncio.gather(
            *(
                self.send(
                    i, "[TEST ONLY] 请依据商城演示规则说明确认收货168小时的退货边界、可退数量和模拟退款阶段。"
                )
                for i in range(3)
            )
        )
        for result, _, _ in initial:
            self.state["model_runs"].append(result["run"]["id"])
        until = time.monotonic() + 35
        while time.monotonic() < until:
            snapshot = await asyncio.to_thread(self.database)
            if snapshot["counts"]["running"] == 3 and snapshot["counts"]["active_slots"] == 3:
                break
            await self.pause(0.5)
        else:
            raise Rejected("Three real concurrent AI runs were not observed")
        fourth, body, key = await self.send(3, "[TEST ONLY] 第四个任务排队并取消，不应调用模型。")
        rid = fourth["run"]["id"]
        queued = await self.api(self.clients[3], MEMBER + "/runs/" + rid)
        snapshot = await asyncio.to_thread(self.database)
        if queued["state"] != "QUEUED" or snapshot["counts"]["active_slots"] != 3:
            raise Rejected("Fourth real AI task did not remain queued behind three slots")
        self.state["slot_evidence"] = {
            "snapshot": snapshot["counts"],
            "fourth_run_id": rid,
            "fourth_state": queued["state"],
        }
        replay = await self.api(
            self.clients[3],
            MEMBER + f"/conversations/{self.state['buyers'][3]['conversation_id']}/messages",
            "POST",
            body,
            key,
            202,
            label="member_replay",
        )
        if replay["run"]["id"] != rid:
            raise Rejected("Duplicate send created a second run")
        stopped = await self.api(
            self.clients[3],
            MEMBER + "/runs/" + rid + "/stop",
            "POST",
            {"expected_version": queued["version"]},
            expected=202,
            label="stop_queued",
        )
        if stopped["state"] != "STOPPED":
            raise Rejected("Fourth queued task could not be cancelled")
        cid = self.state["buyers"][0]["conversation_id"]
        duplicate = await self.clients[0].post(
            MEMBER + f"/conversations/{cid}/messages",
            headers={"Idempotency-Key": str(uuid4())},
            json={
                "client_message_id": str(uuid4()),
                "text": "[TEST ONLY] 同会话并发应拒绝",
                "attachment_ids": [],
            },
        )
        if duplicate.status_code != 409 or duplicate.json().get("code") != "RUN_ALREADY_ACTIVE":
            raise Rejected("Second active run in one conversation was not rejected")
        await self.handoff(3)
        self.state["checks"].append(
            "3 real AI slots; fourth queued/cancelled without replay; same-conversation concurrency rejected"
        )
        self.save()

    async def human(self, index):
        cid = self.state["buyers"][index]["conversation_id"]
        si = self.state["buyers"][index]["staff_index"]
        staff = self.staff_clients[si]
        round_no = 0
        while time.monotonic() < self.deadline and not self.stop.is_set():
            result, body, key = await self.send(
                index, f"[TEST ONLY] A08 {self.id[:8]} buyer {index} round {round_no}"
            )
            if result["run"] is not None:
                raise Rejected("AI run created during human reception")
            if round_no % 5 == 0:
                replay = await self.api(
                    self.clients[index],
                    MEMBER + f"/conversations/{cid}/messages",
                    "POST",
                    body,
                    key,
                    202,
                    label="member_replay",
                )
                if replay["message"]["id"] != result["message"]["id"]:
                    raise Rejected("Duplicate member message was persisted twice")
            message = await self.api(
                staff,
                ADMIN + f"/conversations/{cid}/messages",
                "POST",
                {
                    "client_message_id": str(uuid4()),
                    "text": f"[TEST ONLY] A08 staff {si} reply {round_no}",
                    "attachment_ids": [],
                    "cards": [],
                },
                expected=201,
                label="staff_send",
            )
            self.expected_messages[cid].add(message["id"])
            page = await self.api(
                self.clients[index], MEMBER + f"/conversations/{cid}/messages?limit=5", label="member_history"
            )
            if message["id"] not in {m["id"] for m in page["items"]}:
                raise Rejected("Committed staff reply missing from buyer history")
            if round_no % 3 == 0:
                await self.api(staff, ADMIN + f"/conversations/{cid}/messages?limit=5", label="staff_history")
                if index == 8:
                    # Existing member-22 demo order/return: read original facts only.
                    await self.api(self.clients[index], MEMBER + "/orders/116", label="original_order_read")
                    await self.api(
                        self.clients[index], MEMBER + "/orders/116/returns", label="original_return_read"
                    )
            round_no += 1
            self.state["buyers"][index]["exchange_rounds"] = round_no
            await self.pause(20)

    async def ai(self, index):
        cid = self.state["buyers"][index]["conversation_id"]
        client = self.clients[index]
        next_model = self.started + 300
        while time.monotonic() < self.deadline and not self.stop.is_set():
            if time.monotonic() >= next_model:
                snap = await self.api(client, MEMBER + "/conversations/" + cid, label="member_snapshot")
                if not snap["active_run"]:
                    result, _, _ = await self.send(
                        index,
                        "[TEST ONLY] 请核对商城演示规则：已发货未确认收货能否申请退货？确认收货后168小时边界如何处理？",
                    )
                    self.state["model_runs"].append(result["run"]["id"])
                    self.save()
                    next_model += 300
            await self.api(client, MEMBER + f"/conversations/{cid}/messages?limit=5", label="member_history")
            await self.api(client, MEMBER + "/conversations?limit=5", label="member_list")
            await self.api(client, MEMBER + "/orders?limit=5", label="member_orders")
            await self.pause(20)

    async def staff_heartbeat(self, index):
        while not self.stop.is_set():
            await self.api(
                self.staff_clients[index], ADMIN + "/presence/heartbeat", "POST", {}, label="staff_heartbeat"
            )
            await self.pause(15)

    async def attachments_and_notes(self):
        await self.pause(45)
        if self.stop.is_set():
            return
        raw = io.BytesIO()
        Image.new("RGB", (48, 48), (80, 120, 150)).save(raw, format="PNG")
        for si in range(3):
            index = next(i for i in range(3, 10) if self.state["buyers"][i]["staff_index"] == si)
            cid = self.state["buyers"][index]["conversation_id"]
            staff = self.staff_clients[si]
            response = await staff.post(
                ADMIN + "/attachments",
                headers={"Idempotency-Key": str(uuid4())},
                data={"purpose": "CHAT_IMAGE"},
                files={"file": ("capacity.png", raw.getvalue(), "image/png")},
            )
            if response.status_code != 201 or response.json().get("code") != "OK":
                raise Rejected("Capacity image upload failed")
            aid = response.json()["data"]["id"]
            message = await self.api(
                staff,
                ADMIN + f"/conversations/{cid}/messages",
                "POST",
                {
                    "client_message_id": str(uuid4()),
                    "text": "[TEST ONLY] A08 private image",
                    "attachment_ids": [aid],
                    "cards": [],
                },
                expected=201,
                label="staff_image_send",
            )
            self.expected_messages[cid].add(message["id"])
            image = await self.clients[index].get(MEMBER + "/attachments/" + aid + "/content")
            if image.status_code != 200 or not image.content:
                raise Rejected("Buyer cannot read authorized staff image")
            await self.api(
                staff,
                ADMIN + f"/conversations/{cid}/notes",
                "POST",
                {"client_note_id": str(uuid4()), "body": "[TEST ONLY] A08 internal note"},
                expected=201,
                label="staff_note",
            )
        self.state["checks"].append(
            "3 private image sends/readbacks and internal notes during sustained load"
        )
        self.save()

    async def observe(self):
        while time.monotonic() < self.deadline and not self.stop.is_set():
            snapshot = await asyncio.to_thread(self.database)
            connected = sum(s["connected"] for s in self.streams.values())
            sample = {
                "elapsed_seconds": round(time.monotonic() - self.started, 2),
                "sse_connected": connected,
                "counts": snapshot["counts"],
                "runtime": snapshot["runtime"],
            }
            self.state["samples"].append(sample)
            if snapshot["counts"]["active_slots"] > 3 or snapshot["counts"]["duplicated_conversation_runs"]:
                raise Rejected("Generation concurrency invariant violated")
            self.save()
            print(
                "CAPACITY elapsed="
                + str(sample["elapsed_seconds"])
                + "s connected="
                + str(connected)
                + "/13",
                flush=True,
            )
            await self.pause(30)

    async def verify(self):
        snapshot = await asyncio.to_thread(self.database)
        rows = snapshot["messages"]
        ids = {str(m["id"]) for m in rows}
        expected = set().union(*self.expected_messages.values())
        if expected - ids:
            raise Rejected("Accepted messages missing from PostgreSQL")
        identities = {
            (str(m["conversation_id"]), str(m["client_message_id"])) for m in rows if m["client_message_id"]
        }
        if len(identities) != sum(bool(m["client_message_id"]) for m in rows):
            raise Rejected("Duplicate client message identity")
        by_cid = {b["conversation_id"]: b for b in self.state["buyers"]}
        for message in rows:
            owner = by_cid[str(message["conversation_id"])]
            if message["sender_kind"] == "MEMBER" and str(message["sender_id"]) != owner["member_id"]:
                raise Rejected("Message sender ownership mismatch")
        missing_sse = {
            cid: sorted(mids - self.streams["buyer-" + str(i)]["message_ids"])
            for i, b in enumerate(self.state["buyers"])
            if (cid := b["conversation_id"]) in self.expected_messages
            if (mids := self.expected_messages[cid]) - self.streams["buyer-" + str(i)]["message_ids"]
        }
        if missing_sse:
            raise Rejected("Committed messages not observed through buyer SSE")
        if any(
            r["state"] != "COMPLETED" for r in snapshot["runs"] if str(r["id"]) in self.state["model_runs"]
        ):
            raise Rejected("A sustained real model run did not complete successfully")
        fourth = self.state["slot_evidence"]["fourth_run_id"]
        if any(str(c["run_id"]) == fourth for c in snapshot["model_calls"]):
            raise Rejected("Cancelled queued run called a model")
        if snapshot["actions"]:
            raise Rejected("Capacity test unexpectedly prepared a business action")
        if any(sample["sse_connected"] != 13 for sample in self.state["samples"]):
            raise Rejected("A periodic sample did not have all 13 authenticated streams online")
        if any(not stream["connected"] or stream["connections"] != 1 for stream in self.streams.values()):
            raise Rejected("An authenticated stream disconnected during continuous acceptance")
        self.state["final_database"] = {k: v for k, v in snapshot.items() if k != "messages"}
        self.state["messages_verified"] = len(rows)
        self.state["expected_messages_verified"] = len(expected)
        self.state["checks"].append(
            "Persisted identities, buyer SSE delivery, sender isolation, completed real runs, queued cancellation and zero business actions"
        )

    async def cleanup(self):
        self.stop.set()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        failures = []
        for i, buyer in enumerate(self.state["buyers"]):
            cid = buyer.get("conversation_id")
            if not cid:
                continue
            try:
                snap = await self.api(self.clients[i], MEMBER + "/conversations/" + cid)
                await self.api(
                    self.clients[i],
                    MEMBER + "/conversations/" + cid,
                    "DELETE",
                    {"expected_version": snap["conversation"]["version"]},
                    label="fixture_cleanup",
                )
                buyer["conversation_deleted"] = True
            except Exception as error:
                failures.append("conversation " + cid + " " + type(error).__name__)
        if self.operator:
            for staff in self.state["staff"]:
                try:
                    await self.api(
                        self.operator,
                        "/mall-admin/admin/updateStatus/" + staff["id"],
                        "POST",
                        legacy=True,
                        data={"status": 0},
                    )
                    staff["disabled"] = True
                except Exception as error:
                    failures.append("temporary staff " + staff["id"] + " " + type(error).__name__)
        self.state["cleanup_errors"] = failures
        self.state["cleanup_completed_at"] = datetime.now(UTC).isoformat()
        self.save()
        for client in self.clients + self.staff_clients + ([self.operator] if self.operator else []):
            await client.aclose()

    async def main(self):
        try:
            await self.prepare()
            for i, b in enumerate(self.state["buyers"]):
                self.task(
                    self.stream(
                        "buyer-" + str(i),
                        self.clients[i],
                        MEMBER + f"/conversations/{b['conversation_id']}/events",
                        cid=b["conversation_id"],
                    )
                )
            await self.initial_slots()
            for i in range(3):
                self.task(
                    self.stream("staff-" + str(i), self.staff_clients[i], ADMIN + "/events", staff_index=i)
                )
            until = time.monotonic() + 45
            while time.monotonic() < until and not self.stop.is_set():
                if len(self.streams) == 13 and all(s["connected"] for s in self.streams.values()):
                    break
                await self.pause(0.5)
            else:
                raise Rejected("All 13 authenticated SSE sessions were not online")
            self.started = time.monotonic()
            self.deadline = self.started + self.duration
            self.state["load_started_at"] = datetime.now(UTC).isoformat()
            self.state["result"] = "RUNNING"
            self.save()
            workloads = [self.task(self.ai(i) if i < 3 else self.human(i)) for i in range(10)]
            self.task(self.attachments_and_notes())
            observer = self.task(self.observe())
            await observer
            await asyncio.gather(*workloads)
            if self.state["errors"]:
                raise Rejected("Load task failed; see preserved error codes")
            elapsed = time.monotonic() - self.started
            if elapsed < self.duration:
                raise Rejected("Continuous load did not reach 15 minutes")
            self.state["actual_seconds"] = round(elapsed, 2)
            # Drain accepted writes/runs and allow their SSE events to arrive.
            until = time.monotonic() + 180
            while time.monotonic() < until:
                snap = await asyncio.to_thread(self.database)
                if not any(r["state"] in {"RUNNING", "QUEUED", "STOP_REQUESTED"} for r in snap["runs"]):
                    break
                await self.pause(2)
            await self.pause(5)
            await self.verify()
            self.state["result"] = "A08_CONTINUOUS_WORKLOAD_PASSED"
            self.state["performance_status"] = "MEASURED_SEPARATELY_REVIEW_P95"
        except Exception as error:
            self.state["result"] = "FAILED"
            self.state["errors"].append(str(error) if isinstance(error, Rejected) else type(error).__name__)
        finally:
            self.state["finished_at"] = datetime.now(UTC).isoformat()
            self.save()
            await self.cleanup()
        print("CAPACITY result=" + self.state["result"] + " evidence=" + self.path.name, flush=True)
        if self.state["result"] == "FAILED" or self.state.get("cleanup_errors"):
            raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=int, default=900)
    args = parser.parse_args()
    run(Acceptance(args.duration).main())
