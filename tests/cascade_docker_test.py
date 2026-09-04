#!/usr/bin/env python3
"""Docker integration test for the 3x-ui cascade skill (v2.0.1).

Simulates the two-server cascade on one Docker host:

    test client (requests) --> RU container (http inbound :11112, tag hiddify-entry)
        --> routing rule --> VLESS outbound to-foreign
        --> foreign container (VLESS inbound :22220, tag from-ru, service client)
        --> freedom --> internet

Checks:
  1. Modern/legacy API discovery (capability detection, not version guessing).
  2. Cascade TCP flow end-to-end through the foreign container.
  3. Foreign inbound traffic counters increase (service-client architecture).
  4. Template backup -> modify -> rollback -> config exactly restored.
  5. Semantic pre/post diff: every update matches the change plan; an
     unplanned side effect raises UNPLANNED CONFIG DRIFT and is rolled back.
  6. Idempotency: a second configuration run reports NO CHANGES REQUIRED.
  7. Negative probe: broken routing tag does not destroy the setup.

Uses the OFFICIAL image with a PINNED version. admin/admin is valid ONLY for
this fresh Docker test environment — never in production.
"""

import sys
import time
import uuid as uuidlib

import cascade_lib as lib
from cascade_lib import (
    Results, sh, docker_available, docker_cleanup, docker_up, wait_panel,
    connect_panels, traffic_through_cascade, foreign_traffic_nonzero,
    ensure_foreign_inbound, ensure_foreign_egress, ensure_ru_inbound,
    ensure_ru_outbound_and_routing, count_outbound_tag, count_routing_rule,
    canonical, as_dict,
)

results = Results()


def main():
    version = sys.argv[1] if len(sys.argv) > 1 else None
    print(f"3x-ui cascade Docker integration test — image "
          f"{lib.IMAGE}:{version or lib.THREEXUI_VERSION_DEFAULT}\n")
    if not docker_available():
        return 2

    keep = __import__("os").environ.get("KEEP_CONTAINERS", "") == "1"

    print("Phase 0: environment")
    docker_cleanup()
    docker_up(version)
    wait_panel(lib.FOREIGN_URL, "foreign")
    wait_panel(lib.RU_URL, "ru")

    print("\nPhase 1: API discovery + login (both panels)")
    foreign, ru = connect_panels()
    results.check("API discovery modern/legacy",
                  foreign.api_mode and ru.api_mode,
                  f"foreign={foreign.api_mode}, ru={ru.api_mode}")

    service_uuid = str(uuidlib.uuid4())

    print("\nPhase 2: template backup (byte-exact snapshot)")
    foreign_backup = foreign.backup_template()
    ru_backup = ru.backup_template()
    results.check("Templates snapshotted", bool(foreign_backup) and bool(ru_backup),
                  f"foreign {len(canonical(foreign_backup))}B, "
                  f"ru {len(canonical(ru_backup))}B")

    print("\nPhase 3: configure cascade (planned updates + drift detection)")
    ensure_foreign_inbound(foreign, service_uuid)
    ensure_foreign_egress(foreign)
    ensure_ru_inbound(ru)
    ensure_ru_outbound_and_routing(ru, service_uuid)

    print("\nPhase 4: end-to-end traffic")
    ok, detail = traffic_through_cascade()
    results.check("TCP end-to-end through cascade", ok, detail)
    ok2, detail2 = foreign_traffic_nonzero(foreign)
    results.check("Foreign inbound traffic counters > 0", ok2, detail2)

    print("\nPhase 5: idempotency re-run")
    created_foreign = ensure_foreign_inbound(foreign, service_uuid)
    created_egress = ensure_foreign_egress(foreign)
    created_ru = ensure_ru_inbound(ru)
    created_tpl = ensure_ru_outbound_and_routing(ru, service_uuid)
    results.check("Re-run creates no duplicates",
                  not (created_foreign or created_egress or created_ru or created_tpl),
                  f"foreign_inbound={created_foreign}, egress={created_egress}, "
                  f"ru_inbound={created_ru}, template={created_tpl}")

    tag_f = [i["tag"] for i in foreign.list_inbounds()]
    tag_r = [i["tag"] for i in ru.list_inbounds()]
    results.check("Single inbound per tag",
                  tag_f.count(lib.FOREIGN_TAG) == 1 and tag_r.count(lib.RU_TAG) == 1,
                  f"foreign tags={tag_f}, ru tags={tag_r}")

    fsettings = as_dict(foreign.find_inbound(lib.FOREIGN_TAG)["settings"])
    clients = [c.get("email") for c in fsettings.get("clients", [])]
    results.check("Single service client", clients.count(lib.SERVICE_CLIENT_EMAIL) == 1,
                  f"clients={clients}")

    tpl = ru.get_saved_template()
    results.check("Single outbound + single routing rule",
                  count_outbound_tag(tpl, lib.RU_OUTBOUND_TAG) == 1
                  and count_routing_rule(tpl) == 1,
                  f"outbounds={count_outbound_tag(tpl, lib.RU_OUTBOUND_TAG)}, "
                  f"rules={count_routing_rule(tpl)}")

    if not (created_foreign or created_egress or created_ru or created_tpl):
        print("  >>> NO CHANGES REQUIRED")

    print("\nPhase 6: unplanned config drift detection")
    try:
        # deliberately send a payload that drops the cascade outbound
        # (unplanned deletion) — planned_update must raise
        def bad_mutate(config):
            config["outbounds"] = [o for o in config["outbounds"]
                                   if o.get("tag") != lib.RU_OUTBOUND_TAG]
            config["routing"]["rules"] = [
                r for r in config["routing"]["rules"]
                if r.get("outboundTag") != lib.RU_OUTBOUND_TAG]
            return config

        plan = {"outbounds": [], "routing": {"rules": []}}  # plan says nothing changes
        ru.planned_update(plan, bad_mutate, label="drift probe")
        results.check("UNPLANNED CONFIG DRIFT detected", False,
                      "drift probe did not raise")
    except RuntimeError as e:
        results.check("UNPLANNED CONFIG DRIFT detected",
                      "UNPLANNED CONFIG DRIFT" in str(e), str(e)[:120])
    # restore the cascade after the probe (rollback of the drifted state)
    tpl_now = ru.get_saved_template()
    for r in tpl_now.get("routing", {}).get("rules", []):
        if lib.RU_TAG in (r.get("inboundTag") or []):
            r["outboundTag"] = lib.RU_OUTBOUND_TAG
    if not any(o.get("tag") == lib.RU_OUTBOUND_TAG for o in tpl_now["outbounds"]):
        tpl_now["outbounds"].append({
            "tag": lib.RU_OUTBOUND_TAG, "protocol": "vless",
            "settings": {"vnext": [{"address": lib.FOREIGN_NAME,
                                    "port": lib.FOREIGN_INBOUND_PORT,
                                    "users": [{"id": service_uuid,
                                               "encryption": "none", "flow": ""}]}]},
            "streamSettings": {"network": "tcp", "security": "none"},
        })
    ru.update_template(tpl_now)
    time.sleep(3)
    ok_after, _ = traffic_through_cascade()
    results.check("Cascade works after drift-probe rollback", ok_after)

    print("\nPhase 7: backup → modify → rollback → exact restore")
    # isolated cycle on the RU template: snapshot the CURRENT (cascade) state,
    # add a marker outbound, restore, require exact match; cascade must survive
    tpl_before_mod = ru.get_saved_template()
    tpl_mod = {**tpl_before_mod, "outbounds": tpl_before_mod["outbounds"] + [
        {"tag": "rollback-test-marker", "protocol": "freedom",
         "settings": {"domainStrategy": "AsIs"}}]}
    resp = ru.update_template(tpl_mod)
    modified = bool(resp.get("success"))
    restored = ru.restore_template(tpl_before_mod)
    results.check("Rollback restores template exactly", modified and restored,
                  f"modified={modified}, restored={restored}")
    ok_cascade, _ = traffic_through_cascade()
    results.check("Cascade unaffected by backup/restore cycle", ok_cascade)

    print("\nPhase 8: negative probe (broken routing tag)")
    tpl_bad = ru.get_saved_template()
    for r in tpl_bad.get("routing", {}).get("rules", []):
        if lib.RU_TAG in (r.get("inboundTag") or []):
            r["outboundTag"] = "nonexistent-outbound"
    ru.update_template(tpl_bad)
    # restore by rewriting the rule back to the working outbound
    tpl_now = ru.get_saved_template()
    fixed = False
    for r in tpl_now.get("routing", {}).get("rules", []):
        if lib.RU_TAG in (r.get("inboundTag") or []):
            r["outboundTag"] = lib.RU_OUTBOUND_TAG
            fixed = True
    if fixed:
        ru.update_template(tpl_now)
    time.sleep(3)
    ok_after, _ = traffic_through_cascade()
    results.check("Broken tag does not destroy setup; restored + traffic works",
                  fixed and ok_after, f"rule_rewritten={fixed}, traffic={ok_after}")

    print("\nPhase 9: final restore (leave containers in snapshot state)")
    ru.restore_template(ru_backup)
    foreign.restore_template(foreign_backup)
    for inb in list(ru.list_inbounds()):
        if inb["tag"] == lib.RU_TAG:
            ru.del_inbound(inb["id"])
    for inb in list(foreign.list_inbounds()):
        if inb["tag"] == lib.FOREIGN_TAG:
            foreign.del_inbound(inb["id"])

    rc = results.summary()
    if not keep:
        print("\nCleaning up containers/network ...")
        docker_cleanup()
    else:
        print(f"\nKEEP_CONTAINERS=1 — containers left running "
              f"({lib.RU_NAME}, {lib.FOREIGN_NAME})")
    return rc


if __name__ == "__main__":
    sys.exit(main())
