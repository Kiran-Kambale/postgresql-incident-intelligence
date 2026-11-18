"""Token authentication and role-based authorization (viewer < operator < admin), plus audit of denials."""
import json

from tests.conftest import H

VIEWER, OPERATOR, ADMIN = "viewer-token-0123456789", "operator-token-012345", "admin-token-0123456789"


def test_anonymous_and_bad_tokens_are_rejected_but_liveness_and_ui_stay_open(secured):
    c = secured
    assert c.get("/api/health/live").status_code == 200 and c.get("/api/auth/status").json()["authentication_required"] is True and c.get("/").status_code == 200
    for path in ("/api/health", "/api/evidence", "/api/live/overview", "/api/incidents", "/api/system/status", "/api/runbooks"):
        r = c.get(path)
        assert r.status_code == 401 and r.json()["error"]["code"] == "UNAUTHENTICATED", path
        assert c.get(path, headers=H("wrong-token-0123456789")).status_code == 401, path
    assert c.get("/api/health", headers={"Authorization": "Basic abc"}).status_code == 401


def test_viewer_can_read_but_not_change_or_export(secured):
    c = secured
    assert c.get("/api/live/overview", headers=H(VIEWER)).status_code == 200 and c.get("/api/evidence", headers=H(VIEWER)).status_code == 200
    assert c.post("/api/incidents", json={"title": "viewer tries"}, headers=H(VIEWER)).status_code == 403
    assert c.post("/api/live/refresh", json={}, headers=H(VIEWER)).status_code == 403
    assert c.get("/api/evidence/export?format=csv", headers=H(VIEWER)).status_code == 403
    assert c.get("/api/audit", headers=H(VIEWER)).status_code == 403 and c.put("/api/settings", json={"values": {"long_query_s": 40}}, headers=H(VIEWER)).status_code == 403


def test_operator_can_investigate_but_not_administer(secured):
    c = secured
    r = c.post("/api/incidents", json={"title": "operator incident"}, headers=H(OPERATOR))
    assert r.status_code == 200
    iid = r.json()["id"]
    assert c.patch(f"/api/incidents/{iid}", json={"status": "investigating"}, headers=H(OPERATOR)).status_code == 200
    assert c.get("/api/evidence/export?format=csv", headers=H(OPERATOR)).status_code == 200
    assert c.put("/api/settings", json={"values": {"long_query_s": 40}}, headers=H(OPERATOR)).status_code == 403
    assert c.post("/api/maintenance/retention", headers=H(OPERATOR)).status_code == 403 and c.post("/api/instances/demo/monitoring", json={"enabled": False}, headers=H(OPERATOR)).status_code == 403
    assert c.get("/api/audit", headers=H(OPERATOR)).status_code == 403


def test_admin_has_full_access_and_actor_is_recorded(secured):
    c = secured
    assert c.put("/api/settings", json={"values": {"long_query_s": 40}}, headers=H(ADMIN)).status_code == 200
    c.post("/api/incidents", json={"title": "admin made this"}, headers=H(ADMIN))
    a = c.get("/api/audit?action=incident.create", headers=H(ADMIN)).json()["items"][0]
    assert a["actor"] == "admin" and a["role"] == "admin"


def test_denied_attempts_are_audited_and_tokens_never_appear_in_audit_or_responses(secured):
    c = secured
    c.post("/api/incidents", json={"title": "denied"}, headers=H(VIEWER))
    audit = c.get("/api/audit?outcome=denied", headers=H(ADMIN)).json()
    assert audit["total"] >= 1 and audit["items"][0]["action"] == "authz.denied" and audit["items"][0]["details"]["needed"] == "operator"
    blob = json.dumps(audit) + json.dumps(c.get("/api/system/status", headers=H(ADMIN)).json()) + json.dumps(c.get("/api/settings", headers=H(ADMIN)).json())
    for tok in (VIEWER, OPERATOR, ADMIN):
        assert tok not in blob


def test_invalid_incident_requests_still_validated_after_authorization(secured):
    assert secured.post("/api/incidents", json={"title": "x"}, headers=H(OPERATOR)).status_code == 422
