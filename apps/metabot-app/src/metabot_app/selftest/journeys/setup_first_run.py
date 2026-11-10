"""Setup: what a first run finds. The key was made for this world alone, the core's
doctor reads clean, the app's doctor (GET /api/doctor) has no blocker and names
the local model server, and the server reports its capabilities."""
AXIS = "setup"
NEEDS = ()
BUDGET = {"doctor": 5000}
SCENARIOS = {}


def run(s):
    def key_made():
        key = s.home / "key"
        assert key.is_file(), "no signing key in the scratch home"
        assert key.stat().st_mode & 0o777 == 0o600, f"key mode {oct(key.stat().st_mode & 0o777)}"
    s.step("setup_first_run", "key made", key_made)

    def doctor():
        r = s.core(["doctor"], timeout=60)
        assert r.returncode == 0, f"doctor exit {r.returncode}: {r.stderr.strip()[:200]}"
        assert str(s.home / "key") in r.stdout, "doctor does not name this world's key"
        assert "present" in r.stdout, "doctor does not say the key is present"
        return r.stdout
    s.step("setup_first_run", "doctor", doctor, budget_ms=BUDGET["doctor"])

    def app_doctor():
        status, body = s.api("GET", "/api/doctor")
        assert status == 200, f"/api/doctor {status}"
        items = {i["id"]: i for i in body.get("items") or []}
        blockers = [i["id"] for i in items.values() if i.get("blocker")]
        assert body.get("ok") is True, f"doctor blockers: {blockers}"
        assert items["key"]["ok"], items["key"]
        assert items["engine.local"]["ok"], items["engine.local"]
        assert "model" in items["engine.local"]["what"], items["engine.local"]["what"]
        return body
    s.step("setup_first_run", "app doctor", app_doctor)

    def caps():
        status, body = s.api("GET", "/api/meta")
        assert status == 200, f"/api/meta {status}"
        caps = body.get("caps")
        assert isinstance(caps, dict), "no caps in /api/meta"
        for k in ("desks", "sandbox", "engines", "on_device", "service"):
            assert k in caps, f"caps lacks {k}"
        assert caps["engines"].get("openai") == "http", f"openai engine is {caps['engines'].get('openai')!r}"
        s.caps = caps
        return caps
    s.step("setup_first_run", "caps", caps)
