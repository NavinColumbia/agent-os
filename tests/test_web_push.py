from __future__ import annotations

import base64
from datetime import datetime, timezone
from pathlib import Path
import runpy
from typing import Any, Mapping

from cryptography.exceptions import InvalidTag
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, select
from py_vapid import Vapid02

from agent_os.api.app import create_app
from agent_os.application.command_worker import CommandRunStatus
from agent_os.domain.notifications import (
    Notification,
    NotificationCategory,
    NotificationPreferences,
)
from agent_os.domain.web_push import WebPushSubscriptionMaterial
from agent_os.infrastructure.memory import InMemoryWorkflowEngine
from agent_os.infrastructure.sql_notifications import (
    SQLNotificationStore,
    _push_available_at,
    push_subscriptions,
    web_push_deliveries,
)
from agent_os.infrastructure.web_push import WebPushSubscriptionProtector
from agent_os.infrastructure.web_push_delivery import (
    DurableWebPushDeliveryWorker,
    ExpiredWebPushSubscription,
    WebPushSender,
)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


PUBLIC_KEY = _b64(b"\x04" + b"p" * 64)
P256DH = _b64(b"\x04" + b"d" * 64)
AUTH = _b64(b"a" * 16)
ENDPOINT = "https://fcm.googleapis.com/wp/example-capability"
ROOT_SECRET = "push-subscription-protection-secret-long-enough"


def material(endpoint: str = ENDPOINT) -> WebPushSubscriptionMaterial:
    return WebPushSubscriptionMaterial(
        endpoint=endpoint,
        p256dh=P256DH,
        auth=AUTH,
        expiration_time=1_900_000_000_000,
    )


def vapid_keys() -> tuple[str, str]:
    vapid = Vapid02()
    vapid.generate_keys()
    private_number = vapid.private_key.private_numbers().private_value
    private_key = _b64(private_number.to_bytes(32, "big"))
    numbers = vapid.public_key.public_numbers()
    public_key = _b64(
        b"\x04" + numbers.x.to_bytes(32, "big") + numbers.y.to_bytes(32, "big")
    )
    return private_key, public_key


def test_deployment_key_generator_emits_a_matching_vapid_pair(capsys):
    root = Path(__file__).resolve().parents[1]
    runpy.run_path(str(root / "deploy" / "generate_web_push_keys.py"), run_name="__main__")
    generated = dict(
        line.split("=", 1) for line in capsys.readouterr().out.strip().splitlines()
    )
    WebPushSender(
        vapid_private_key=generated["AOS_V2_WEB_PUSH_PRIVATE_KEY"],
        vapid_public_key=generated["AOS_V2_WEB_PUSH_PUBLIC_KEY"],
        vapid_subject=generated["AOS_V2_WEB_PUSH_SUBJECT"],
    )

    _, another_public = vapid_keys()
    with pytest.raises(ValueError, match="do not match"):
        WebPushSender(
            vapid_private_key=generated["AOS_V2_WEB_PUSH_PRIVATE_KEY"],
            vapid_public_key=another_public,
            vapid_subject=generated["AOS_V2_WEB_PUSH_SUBJECT"],
        )


class Identity:
    def authenticate(self, authorization: str | None, session: str | None) -> Mapping[str, Any]:
        if authorization == "Bearer person-a":
            return {"sub": "person-a", "org": "tenant-a", "roles": ["owner"]}
        if authorization == "Bearer person-b":
            return {"sub": "person-b", "org": "tenant-a", "roles": ["viewer"]}
        raise ValueError("authentication required")


def test_subscription_material_rejects_non_push_and_ssrf_endpoints():
    for endpoint in (
        "http://fcm.googleapis.com/wp/token",
        "https://127.0.0.1/internal",
        "https://metadata.google.internal/computeMetadata/v1/",
        "https://fcm.googleapis.com.evil.example/wp/token",
        "https://user:secret@fcm.googleapis.com/wp/token",
    ):
        with pytest.raises(ValueError):
            material(endpoint)
    assert material("https://api.push.apple.com/3/device/token").endpoint.startswith("https://")
    with pytest.raises(ValueError):
        WebPushSubscriptionMaterial(ENDPOINT, P256DH + "!", AUTH)
    with pytest.raises(ValueError, match="uncompressed"):
        WebPushSubscriptionMaterial(ENDPOINT, _b64(b"\x03" + b"d" * 64), AUTH)
    with pytest.raises(ValueError):
        WebPushSubscriptionMaterial(ENDPOINT, P256DH, _b64(b"a" * 17))


def test_quiet_hours_delay_normal_push_but_not_explicit_safety_bypass():
    created = datetime(2026, 9, 17, 23, 30, tzinfo=timezone.utc)
    preferences = {
        "quiet_hours_start": "22:00",
        "quiet_hours_end": "07:00",
        "timezone": "UTC",
    }
    assert _push_available_at(
        created, preferences, bypass_quiet_hours=False,
    ) == datetime(2026, 9, 18, 7, 0, tzinfo=timezone.utc)
    assert _push_available_at(
        created, preferences, bypass_quiet_hours=True,
    ) == created


def test_subscription_protector_binds_ciphertext_to_tenant_person_and_device():
    protector = WebPushSubscriptionProtector(ROOT_SECRET)
    subscription_id = protector.subscription_id("tenant-a", "person-a", "device-identifier-0001")
    sealed = protector.seal(
        material(), tenant_id="tenant-a", subject_id="person-a",
        subscription_id=subscription_id,
    )

    assert ENDPOINT.encode() not in sealed
    assert P256DH.encode() not in sealed
    assert protector.open(
        sealed, tenant_id="tenant-a", subject_id="person-a",
        subscription_id=subscription_id,
    ) == material()
    with pytest.raises(InvalidTag):
        protector.open(
            sealed, tenant_id="tenant-b", subject_id="person-a",
            subscription_id=subscription_id,
        )


def test_sql_subscription_lifecycle_is_private_idempotent_and_person_scoped(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'push.sqlite3'}"
    protector = WebPushSubscriptionProtector(ROOT_SECRET)
    store = SQLNotificationStore(
        database_url, push_protector=protector, create_schema=True,
        clock=lambda: datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc),
    )
    try:
        created = store.register_push_subscription(
            tenant_id="tenant-a", subject_id="person-a",
            device_id="device-identifier-0001", device_name="Work phone",
            audience_ids=("person-a", "human:ceo"),
            material=material(), actor_id="person-a", idempotency_key="register-device-0001",
        )
        replay = store.register_push_subscription(
            tenant_id="tenant-a", subject_id="person-a",
            device_id="device-identifier-0001", device_name="Work phone",
            audience_ids=("person-a", "human:ceo"),
            material=material(), actor_id="person-a", idempotency_key="register-device-0001",
        )
        assert created["active"] is True and created["duplicate"] is False
        assert replay["subscription_id"] == created["subscription_id"]
        assert replay["duplicate"] is True
        assert ENDPOINT not in str(created)
        assert P256DH not in str(store.list_push_subscriptions(
            "tenant-a", subject_id="person-a",
        ))
        assert store.list_push_subscriptions("tenant-a", subject_id="person-b") == ()

        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                row = connection.execute(select(push_subscriptions)).mappings().one()
            assert ENDPOINT.encode() not in bytes(row["sealed_subscription"])
            assert row["endpoint_hash"] != ENDPOINT
        finally:
            engine.dispose()

        with pytest.raises(ValueError, match="different content"):
            store.register_push_subscription(
                tenant_id="tenant-a", subject_id="person-a",
                device_id="device-identifier-0001", device_name="Changed name",
                audience_ids=("person-a", "human:ceo"),
                material=material(), actor_id="person-a", idempotency_key="register-device-0001",
            )

        assert store.revoke_push_subscription(
            tenant_id="tenant-a", subject_id="person-b",
            subscription_id=created["subscription_id"], actor_id="person-b",
            reason="Not mine", idempotency_key="revoke-wrong-person",
        ) is None
        revoked = store.revoke_push_subscription(
            tenant_id="tenant-a", subject_id="person-a",
            subscription_id=created["subscription_id"], actor_id="person-a",
            reason="Device retired", idempotency_key="revoke-right-person",
        )
        assert revoked is not None and revoked["active"] is False
        assert revoked["version"] == 2
    finally:
        store.close()


def test_control_api_enrolls_and_revokes_only_the_authenticated_persons_device(tmp_path):
    store = SQLNotificationStore(
        f"sqlite:///{tmp_path / 'push-api.sqlite3'}",
        push_protector=WebPushSubscriptionProtector(ROOT_SECRET),
        create_schema=True,
    )
    api = TestClient(create_app(
        engine=InMemoryWorkflowEngine(),
        identity=Identity(),
        notification_store=store,
        web_push_public_key=PUBLIC_KEY,
        shutdown=store.close,
    ))
    payload = {
        "device_id": "device-identifier-0001",
        "device_name": "This browser",
        "endpoint": ENDPOINT,
        "expiration_time": None,
        "keys": {"p256dh": P256DH, "auth": AUTH},
    }
    with api:
        assert api.get("/v2/client-config").json()["web_push_public_key"] == PUBLIC_KEY
        created = api.post(
            "/v2/me/push-subscriptions",
            headers={
                "Authorization": "Bearer person-a",
                "Idempotency-Key": "register-api-device",
            },
            json=payload,
        )
        assert created.status_code == 201
        subscription_id = created.json()["subscription_id"]
        assert "endpoint" not in created.json() and "keys" not in created.json()
        assert api.get(
            "/v2/me/push-subscriptions",
            headers={"Authorization": "Bearer person-b"},
        ).json()["items"] == []
        assert api.request(
            "DELETE",
            f"/v2/me/push-subscriptions/{subscription_id}",
            headers={
                "Authorization": "Bearer person-b",
                "Idempotency-Key": "revoke-other-device",
            },
            json={"reason": "Try another person"},
        ).status_code == 404
        revoked = api.request(
            "DELETE",
            f"/v2/me/push-subscriptions/{subscription_id}",
            headers={
                "Authorization": "Bearer person-a",
                "Idempotency-Key": "revoke-own-device",
            },
            json={"reason": "Signed out"},
        )
        assert revoked.status_code == 200 and revoked.json()["active"] is False


def test_control_api_refuses_enrollment_until_vapid_is_provisioned(tmp_path):
    store = SQLNotificationStore(
        f"sqlite:///{tmp_path / 'push-disabled.sqlite3'}",
        push_protector=WebPushSubscriptionProtector(ROOT_SECRET),
        create_schema=True,
    )
    api = TestClient(create_app(
        engine=InMemoryWorkflowEngine(), identity=Identity(), notification_store=store,
    ))
    try:
        response = api.post(
            "/v2/me/push-subscriptions",
            headers={
                "Authorization": "Bearer person-a",
                "Idempotency-Key": "register-without-vapid",
            },
            json={
                "device_id": "device-identifier-0001", "device_name": "Browser",
                "endpoint": ENDPOINT, "expiration_time": None,
                "keys": {"p256dh": P256DH, "auth": AUTH},
            },
        )
        assert response.status_code == 503
        assert "VAPID" in response.json()["detail"]
    finally:
        store.close()


def test_notification_enqueues_and_delivers_only_a_generic_durable_push(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'push-delivery.sqlite3'}"
    store = SQLNotificationStore(
        database_url,
        push_protector=WebPushSubscriptionProtector(ROOT_SECRET),
        create_schema=True,
    )
    try:
        store.set_notification_preferences(
            NotificationPreferences(
                "tenant-a", "person-a", browser_notifications=True,
            ),
            actor_id="person-a", idempotency_key="enable-device-alerts",
        )
        store.register_push_subscription(
            tenant_id="tenant-a", subject_id="person-a",
            device_id="device-identifier-0001", device_name="Work phone",
            audience_ids=("person-a", "human:ceo"), material=material(),
            actor_id="person-a", idempotency_key="register-delivery-device",
        )
        store.publish_notification(Notification(
            notification_id="notification-push-one",
            tenant_id="tenant-a",
            run_id="mission-secret",
            category=NotificationCategory.HUMAN_ACTION_REQUIRED,
            recipient_ids=("human:ceo",),
            subject="Secret acquisition decision",
            body="Private deal details must remain in Agent OS.",
            source_id="source-push-one",
            created_at=datetime.now(timezone.utc).isoformat(),
            payload={"attention_disposition": "interrupt", "secret": "do-not-push"},
        ))
        lease = store.claim_web_push_delivery(
            "tenant-a", worker_id="push-worker", lease_seconds=30,
        )
        assert lease is not None
        assert lease.subscription.endpoint == ENDPOINT
        assert "Secret acquisition" not in str(lease.payload)
        assert "Private deal" not in str(lease.payload)
        assert "do-not-push" not in str(lease.payload)
        assert lease.payload["url"] == f"/app#view=inbox&push={lease.delivery_id}"
        assert lease.payload["tag"] == lease.delivery_id
        assert "notification-push-one" not in str(lease.payload)

        sent = {}

        def send(**kwargs):
            sent.update(kwargs)
            return type("Response", (), {"status_code": 201})()

        private_key, public_key = vapid_keys()
        sender = WebPushSender(
            vapid_private_key=private_key,
            vapid_public_key=public_key,
            vapid_subject="mailto:push@example.com",
            send=send,
        )
        assert sender.deliver(lease)["payload_policy"] == "generic-wakeup-v1"
        assert "Secret acquisition" not in sent["data"]
        assert sent["subscription_info"]["endpoint"] == ENDPOINT
        assert store.complete_web_push_delivery(
            "tenant-a", lease.delivery_id, worker_id="push-worker",
            result={"status_code": 201},
        ) is True
        own_receipts = store.list_web_push_deliveries(
            "tenant-a", subject_id="person-a",
        )
        assert len(own_receipts) == 1
        assert own_receipts[0]["status"] == "delivered"
        assert own_receipts[0]["subscription_id"] == lease.subscription_id
        assert store.list_web_push_deliveries(
            "tenant-a", subject_id="person-b",
        ) == ()

        api = TestClient(create_app(
            engine=InMemoryWorkflowEngine(), identity=Identity(), notification_store=store,
        ))
        with api:
            response = api.get(
                "/v2/me/push-deliveries?limit=10",
                headers={"Authorization": "Bearer person-a"},
            )
            assert response.status_code == 200
            assert response.json()["items"][0]["delivery_id"] == lease.delivery_id
            direct_receipt = api.get(
                f"/v2/me/push-deliveries/{lease.delivery_id}",
                headers={"Authorization": "Bearer person-a"},
            )
            assert direct_receipt.status_code == 200
            assert direct_receipt.json()["notification_id"] == "notification-push-one"
            direct_notification = api.get(
                "/v2/notifications/notification-push-one",
                headers={"Authorization": "Bearer person-a"},
            )
            assert direct_notification.status_code == 200
            assert direct_notification.json()["presentation"]["level"] == "time_sensitive"
            assert api.get(
                "/v2/me/push-deliveries",
                headers={"Authorization": "Bearer person-b"},
            ).json()["items"] == []
            assert api.get(
                f"/v2/me/push-deliveries/{lease.delivery_id}",
                headers={"Authorization": "Bearer person-b"},
            ).status_code == 404
            assert api.get(
                "/v2/me/push-deliveries/not-a-capability",
                headers={"Authorization": "Bearer person-a"},
            ).status_code == 404
            assert api.get(
                "/v2/notifications/notification-push-one",
                headers={"Authorization": "Bearer person-b"},
            ).status_code == 404
        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                row = connection.execute(select(web_push_deliveries)).mappings().one()
            assert row["status"] == "delivered"
            assert row["delivered_at"] is not None
        finally:
            engine.dispose()
    finally:
        store.close()


def test_expired_push_failure_revokes_device_and_cancels_future_delivery(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'push-expired.sqlite3'}"
    store = SQLNotificationStore(
        database_url,
        push_protector=WebPushSubscriptionProtector(ROOT_SECRET),
        create_schema=True,
    )
    store.set_notification_preferences(
        NotificationPreferences("tenant-a", "person-a", browser_notifications=True),
        actor_id="person-a", idempotency_key="enable-expired-alerts",
    )
    store.register_push_subscription(
        tenant_id="tenant-a", subject_id="person-a",
        device_id="device-identifier-0001", device_name="Old phone",
        audience_ids=("person-a",), material=material(), actor_id="person-a",
        idempotency_key="register-expired-device",
    )
    store.publish_notification(Notification(
        notification_id="notification-expired-one", tenant_id="tenant-a",
        run_id="run-one", category=NotificationCategory.HUMAN_ACTION_REQUIRED,
        recipient_ids=("person-a",), subject="Decision", body="Review securely",
        source_id="source-expired-one", created_at=datetime.now(timezone.utc).isoformat(),
    ))

    class ExpiredSender:
        def deliver(self, lease):
            raise ExpiredWebPushSubscription("gone")

    worker = DurableWebPushDeliveryWorker(
        store=store, sender=ExpiredSender(), worker_id="push-worker", lease_seconds=3,
    )
    try:
        report = worker.run_one("tenant-a")
        assert report.status is CommandRunStatus.FAILED
        subscriptions = store.list_push_subscriptions("tenant-a", subject_id="person-a")
        assert subscriptions[0]["active"] is False
        assert store.claim_web_push_delivery(
            "tenant-a", worker_id="push-worker", lease_seconds=3,
        ) is None
    finally:
        store.close()
