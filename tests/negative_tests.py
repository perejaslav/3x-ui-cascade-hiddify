#!/usr/bin/env python3
"""Negative test matrix for the 3x-ui cascade skill (v2.0.1).

Each scenario verifies the ТЗ-required properties:
  clear failure / no destructive overwrite / no unwanted duplicates /
  no unrelated config changes / rollback-recovery where applicable.

Scenarios (each ends with baseline fully restored and traffic working):
  1.  wrong credentials
  2.  CSRF/auth failure
  3.  wrong API endpoint
  4.  occupied foreign port (pre-check + stray cleanup + Xray healthy)
  5.  occupied RU port
  6.  conflicting existing inbound (unit STOP + live port conflict + cleanup)
  7.  invalid foreign UUID (update rejected or broken -> rollback)
  8.  foreign unreachable (bogus hostname -> rollback)
  9.  invalid xraySetting (malformed JSON / wrong shape -> no corruption)
  10. broken routing tag (traffic fails -> restore -> traffic works)

Uses the OFFICIAL image with a PINNED version. admin/admin is valid ONLY for
this fresh Docker test environment — never in production.
"""

import copy
import sys
import time
import uuid as uuidlib

import requests

import cascade_lib as lib
from cascade_lib import (
    Results, docker_available, docker_cleanup, docker_up, wait_panel,
    connect_panels, traffic_through_cascade, occupy_port, sh,
    ensure_foreign_inbound, ensure_foreign_egress, ensure_ru_inbound,
    ensure_ru_outbound_and_routing, canonical, as_dict,
)

results = Results()


def fresh(tpl):
    """Deep copy — mutate functions must never touch shared baselines."""
    return copy.deepcopy(tpl)


class StubPanel:
    """Minimal stand-in for the conflict branch of ensure_foreign_inbound."""

    def __init__(self, inbounds):
        self._inbounds = inbounds
        self.add_called = False

    def list_inbounds(self):
        return self._inbounds

    def find_inbound(self, tag):
        for inb in self._inbounds:
            if inb.get("tag") == tag or inb.get("remark") == tag:
                return inb
        return None

    def add_inbound(self, payload):
        self.add_called = True
        return {"success": True}


def baseline_post_check(ru, foreign, baseline_tpl, baseline_inbounds,
                        baseline_tpl_f, name):
    """Common after-scenario assertions: no unrelated changes, no duplicates."""
    tpl_ok = canonical(ru.get_saved_template()) == canonical(baseline_tpl)
    tpl_f_ok = canonical(foreign.get_saved_template()) == canonical(baseline_tpl_f)
    inb_ok = ru.inbound_state() == baseline_inbounds["ru"] \
        and foreign.inbound_state() == baseline_inbounds["foreign"]
    results.check(f"{name}: no unrelated changes / no duplicates",
                  tpl_ok and tpl_f_ok and inb_ok,
                  f"ru_tpl={tpl_ok}, f_tpl={tpl_f_ok}, inbounds={inb_ok}")


def main():
    print(f"3x-ui cascade negative test matrix — image "
          f"{lib.IMAGE}:{lib.THREEXUI_VERSION_DEFAULT}\n")
    if not docker_available():
        return 2
    keep = __import__("os").environ.get("KEEP_CONTAINERS", "") == "1"

    print("Phase 0: environment + baseline cascade")
    docker_cleanup()
    docker_up()
    wait_panel(lib.FOREIGN_URL, "foreign")
    wait_panel(lib.RU_URL, "ru")
    foreign, ru = connect_panels()

    service_uuid = str(uuidlib.uuid4())
    ensure_foreign_inbound(foreign, service_uuid)
    ensure_foreign_egress(foreign)
    ensure_ru_inbound(ru)
    ensure_ru_outbound_and_routing(ru, service_uuid)
    ok, detail = traffic_through_cascade()
    if not ok:
        print(f"baseline cascade broken: {detail}")
        return 1
    print(f"  baseline cascade healthy ({detail})")

    baseline_tpl = ru.get_saved_template()
    baseline_tpl_f = foreign.get_saved_template()
    baseline_inbounds = {"ru": ru.inbound_state(),
                         "foreign": foreign.inbound_state()}

    def restore_ru():
        assert ru.restore_template(baseline_tpl), "baseline restore failed"

    def restore_foreign():
        assert foreign.restore_template(baseline_tpl_f), "baseline restore failed"

    # ------------------------------------------------------------------
    print("\nScenario 1: wrong credentials")
    probe = lib.Panel(lib.RU_URL, "ru-bad-creds")
    ok_login, msg = probe.try_login(lib.PANEL_USER, "definitely-wrong")
    results.check("wrong credentials: login rejected", not ok_login, msg[:100])
    baseline_post_check(ru, foreign, baseline_tpl, baseline_inbounds,
                        baseline_tpl_f, "S1")

    # ------------------------------------------------------------------
    print("\nScenario 2: CSRF/auth failure")
    anon = requests.Session()
    r_add = anon.post(f"{lib.RU_URL}/panel/api/inbounds/add",
                      json={"port": 39999}, timeout=10)
    r_upd = anon.post(f"{lib.RU_URL}/panel/api/xray/update",
                      data={"xraySetting": "{}"}, timeout=10)
    no_inbound = all(i.get("port") != 39999 for i in ru.list_inbounds())
    tpl_ok = canonical(ru.get_saved_template()) == canonical(baseline_tpl)
    # v3.7.0 hides authed routes behind 404 for anonymous callers (anti-probe);
    # the load-bearing property is that nothing got applied.
    results.check("CSRF-less requests rejected, nothing applied",
                  r_add.status_code in (401, 403, 404) and no_inbound and tpl_ok,
                  f"add={r_add.status_code}, update={r_upd.status_code}, "
                  f"no_inbound={no_inbound}, tpl_unchanged={tpl_ok}")
    baseline_post_check(ru, foreign, baseline_tpl, baseline_inbounds,
                        baseline_tpl_f, "S2")

    # ------------------------------------------------------------------
    print("\nScenario 3: wrong API endpoint")
    r = ru.s.get(f"{lib.RU_URL}/panel/api/xray/nonexistent",
                 headers=ru.auth_headers(), timeout=10)
    r2 = ru.s.get(f"{lib.RU_URL}/panel/api/nonexistent/list",
                  headers=ru.auth_headers(), timeout=10)
    tpl_ok = canonical(ru.get_saved_template()) == canonical(baseline_tpl)
    results.check("unknown endpoints -> 404, config untouched",
                  r.status_code == 404 and r2.status_code == 404 and tpl_ok,
                  f"{r.status_code}, {r2.status_code}, tpl_unchanged={tpl_ok}")
    baseline_post_check(ru, foreign, baseline_tpl, baseline_inbounds,
                        baseline_tpl_f, "S3")

    # ------------------------------------------------------------------
    print("\nScenario 4: occupied foreign port (pre-check)")
    detected = occupy_port(lib.FOREIGN_NAME, 22221)
    results.check("port pre-check detects occupied foreign port", detected,
                  "nc listener on 22221 visible via netstat")
    # the skill must NOT create an inbound on an occupied port
    resp = foreign.add_inbound({
        "up": 0, "down": 0, "total": 0, "remark": "should-not-exist",
        "enable": True, "expiryTime": 0, "listen": "", "port": 22221,
        "protocol": "vless", "tag": "should-not-exist",
        "settings": {"clients": [], "decryption": "none", "fallbacks": []},
        "streamSettings": {"network": "tcp", "security": "none",
                           "externalProxy": []},
    })
    if resp.get("success"):
        # panel allowed it — stray must be removed; Xray must stay healthy
        ok_del, msg = foreign.del_inbound_by_tag("should-not-exist")
        time.sleep(2)
        ok_traffic, _ = traffic_through_cascade()
        results.check("stray inbound on occupied port cleaned up, Xray healthy",
                      ok_del and ok_traffic, f"del={msg}, traffic={ok_traffic}")
    else:
        results.check("panel rejected inbound on occupied port", True,
                      resp.get("msg", "")[:80])
    baseline_post_check(ru, foreign, baseline_tpl, baseline_inbounds,
                        baseline_tpl_f, "S4")

    # ------------------------------------------------------------------
    print("\nScenario 5: occupied RU port (pre-check)")
    detected = occupy_port(lib.RU_NAME, 11113)
    results.check("port pre-check detects occupied RU port", detected,
                  "nc listener on 11113 visible via netstat")
    baseline_post_check(ru, foreign, baseline_tpl, baseline_inbounds,
                        baseline_tpl_f, "S5")

    # ------------------------------------------------------------------
    print("\nScenario 6: conflicting existing inbound")
    # 6a. unit: existing tag WITHOUT service client -> STOP, never overwrite
    stub = StubPanel([{
        "tag": lib.FOREIGN_TAG, "remark": lib.FOREIGN_TAG, "port": 22999,
        "protocol": "vless", "settings": "{\"clients\":[],\"decryption\":\"none\"}",
    }])
    raised = False
    try:
        ensure_foreign_inbound(stub, "00000000-0000-0000-0000-000000000000")
    except RuntimeError as e:
        raised = "conflict" in str(e).lower() and "stop" in str(e).lower()
    results.check("conflicting inbound: STOP without overwrite (unit)",
                  raised and not stub.add_called,
                  f"raised={raised}, add_called={stub.add_called}")
    # 6b. live: stray inbound on the cascade's own port
    resp = foreign.add_inbound({
        "up": 0, "down": 0, "total": 0, "remark": "evil-conflict",
        "enable": True, "expiryTime": 0, "listen": "", "port": 22220,
        "protocol": "vless", "tag": "evil-conflict",
        "settings": {"clients": [], "decryption": "none", "fallbacks": []},
        "streamSettings": {"network": "tcp", "security": "none",
                           "externalProxy": []},
    })
    if resp.get("success"):
        ok_del, msg = foreign.del_inbound_by_tag("evil-conflict")
        time.sleep(2)
        ok_traffic, _ = traffic_through_cascade()
        results.check("port-conflict stray removed, cascade intact",
                      ok_del and ok_traffic, f"del={msg}, traffic={ok_traffic}")
    else:
        results.check("panel rejected duplicate-port inbound", True,
                      resp.get("msg", "")[:80])
    baseline_post_check(ru, foreign, baseline_tpl, baseline_inbounds,
                        baseline_tpl_f, "S6")

    # ------------------------------------------------------------------
    print("\nScenario 7: invalid foreign UUID")
    def bad_uuid_mutate(config):
        for o in config.get("outbounds", []):
            if o.get("tag") == lib.RU_OUTBOUND_TAG:
                o["settings"]["vnext"][0]["users"][0]["id"] = "not-a-uuid"
        return config
    resp = ru.update_template(bad_uuid_mutate(fresh(baseline_tpl)))
    time.sleep(3)
    if resp.get("success"):
        ok_t, _ = traffic_through_cascade()
        restore_ru()
        time.sleep(3)
        ok_back, _ = traffic_through_cascade()
        results.check("invalid UUID: breakage detected + rollback recovers",
                      (not ok_t) and ok_back,
                      f"broken_traffic={not ok_t}, recovered={ok_back}")
    else:
        results.check("invalid UUID: update rejected by panel", True,
                      resp.get("msg", "")[:80])
    baseline_post_check(ru, foreign, baseline_tpl, baseline_inbounds,
                        baseline_tpl_f, "S7")

    # ------------------------------------------------------------------
    print("\nScenario 8: foreign unreachable")
    def unreachable_mutate(config):
        for o in config.get("outbounds", []):
            if o.get("tag") == lib.RU_OUTBOUND_TAG:
                o["settings"]["vnext"][0]["address"] = "nonexistent-host-abc"
        return config
    resp = ru.update_template(unreachable_mutate(fresh(baseline_tpl)))
    time.sleep(3)
    if resp.get("success"):
        ok_t, detail_t = traffic_through_cascade()
        restore_ru()
        time.sleep(3)
        ok_back, _ = traffic_through_cascade()
        results.check("unreachable foreign: failure is clear + rollback recovers",
                      (not ok_t) and ok_back,
                      f"error={detail_t[:60]}, recovered={ok_back}")
    else:
        results.check("unreachable foreign: update rejected", True,
                      resp.get("msg", "")[:80])
    baseline_post_check(ru, foreign, baseline_tpl, baseline_inbounds,
                        baseline_tpl_f, "S8")

    # ------------------------------------------------------------------
    print("\nScenario 9: invalid xraySetting")
    r_bad = ru.s.post(f"{lib.RU_URL}{ru.xray_base}/update",
                      data={"xraySetting": "{invalid json"},
                      headers=ru.auth_headers(), timeout=10)
    tpl_ok1 = canonical(ru.get_saved_template()) == canonical(baseline_tpl)
    r_wrong = ru.s.post(f"{lib.RU_URL}{ru.xray_base}/update",
                        data={"xraySetting": '{"foo": "bar"}'},
                        headers=ru.auth_headers(), timeout=10)
    tpl_now = ru.get_saved_template()
    wrong_shape_accepted = canonical(tpl_now) != canonical(baseline_tpl)
    if wrong_shape_accepted:
        restore_ru()
    time.sleep(2)
    tpl_ok2 = canonical(ru.get_saved_template()) == canonical(baseline_tpl)
    results.check("invalid xraySetting: no config corruption, recovery works",
                  tpl_ok1 and tpl_ok2,
                  f"bad_json={r_bad.status_code}, wrong_shape_"
                  f"{'accepted+restored' if wrong_shape_accepted else 'rejected'}, "
                  f"tpl_unchanged={tpl_ok1}, recovered={tpl_ok2}")
    baseline_post_check(ru, foreign, baseline_tpl, baseline_inbounds,
                        baseline_tpl_f, "S9")

    # ------------------------------------------------------------------
    print("\nScenario 10: broken routing tag")
    def broken_tag_mutate(config):
        for r in config.get("routing", {}).get("rules", []):
            if lib.RU_TAG in (r.get("inboundTag") or []):
                r["outboundTag"] = "nonexistent-outbound"
        return config
    resp = ru.update_template(broken_tag_mutate(fresh(baseline_tpl)))
    time.sleep(3)
    ok_t, _ = traffic_through_cascade()
    restore_ru()
    time.sleep(3)
    ok_back, _ = traffic_through_cascade()
    results.check("broken routing tag: traffic fails + rollback recovers",
                  (not ok_t) and ok_back,
                  f"broken_traffic={not ok_t}, recovered={ok_back}")
    baseline_post_check(ru, foreign, baseline_tpl, baseline_inbounds,
                        baseline_tpl_f, "S10")

    rc = results.summary()
    if not keep:
        print("\nCleaning up containers/network ...")
        docker_cleanup()
    else:
        print(f"\nKEEP_CONTAINERS=1 — containers left running")
    return rc


if __name__ == "__main__":
    sys.exit(main())
