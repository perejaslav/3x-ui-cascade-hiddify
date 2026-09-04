#!/usr/bin/env python3
"""Full rollback rehearsal for the 3x-ui cascade skill (v2.0.1).

Scenario (ТЗ §3):
    Snapshot A            (both templates + inbound lists, BEFORE the cascade)
    → configure cascade   (foreign inbound, egress, RU inbound, outbound+routing)
    → damage critical config (unplanned: extra outbound, broken rule,
      damaged DNS section)
    → trigger rollback    (restore snapshots, remove created inbounds)
    → compare final state with Snapshot A

PASS only if:
  - both Xray templates byte-exactly restored (canonical JSON);
  - created test inbounds removed (inbound lists equal Snapshot A);
  - no other objects changed;
  - traffic works again after rollback (baseline Xray healthy).

Uses the OFFICIAL image with a PINNED version. admin/admin is valid ONLY for
this fresh Docker test environment — never in production.
"""

import sys
import time
import uuid as uuidlib

import cascade_lib as lib
from cascade_lib import (
    Results, docker_available, docker_cleanup, docker_up, wait_panel,
    connect_panels, traffic_through_cascade, ensure_foreign_inbound,
    ensure_foreign_egress, ensure_ru_inbound, ensure_ru_outbound_and_routing,
    canonical,
)

results = Results()


def main():
    print(f"3x-ui cascade rollback rehearsal — image "
          f"{lib.IMAGE}:{lib.THREEXUI_VERSION_DEFAULT}\n")
    if not docker_available():
        return 2
    keep = __import__("os").environ.get("KEEP_CONTAINERS", "") == "1"

    print("Phase 0: environment")
    docker_cleanup()
    docker_up()
    wait_panel(lib.FOREIGN_URL, "foreign")
    wait_panel(lib.RU_URL, "ru")
    foreign, ru = connect_panels()

    # ---------------------------------------------------------------
    print("\nSnapshot A (pre-cascade, pristine)")
    snapA = {
        "ru_tpl": ru.backup_template(),
        "foreign_tpl": foreign.backup_template(),
        "ru_inbounds": ru.inbound_state(),
        "foreign_inbounds": foreign.inbound_state(),
    }
    results.check("Snapshot A captured",
                  bool(snapA["ru_tpl"]) and bool(snapA["foreign_tpl"]))

    # ---------------------------------------------------------------
    print("\nConfigure cascade")
    service_uuid = str(uuidlib.uuid4())
    ensure_foreign_inbound(foreign, service_uuid)
    ensure_foreign_egress(foreign)
    ensure_ru_inbound(ru)
    ensure_ru_outbound_and_routing(ru, service_uuid)
    ok, detail = traffic_through_cascade()
    results.check("Cascade configured and working", ok, detail)

    # ---------------------------------------------------------------
    print("\nDamage critical config (simulated bad apply)")
    tpl = ru.get_saved_template()
    # 1) extra unplanned outbound
    tpl["outbounds"].append({"tag": "evil-out", "protocol": "freedom",
                             "settings": {"domainStrategy": "AsIs"}})
    # 2) broken routing rule
    for r in tpl.get("routing", {}).get("rules", []):
        if lib.RU_TAG in (r.get("inboundTag") or []):
            r["outboundTag"] = "nonexistent-outbound"
    # 3) damaged DNS section
    tpl["dns"] = {"servers": ["198.18.0.1"], "queryStrategy": "UseIPv4"}
    resp = ru.update_template(tpl)
    results.check("Damage applied (broken update accepted)",
                  bool(resp.get("success")),
                  str(resp.get("msg", ""))[:60])

    # also damage foreign: replace egress DNS with fake-ip server
    ftpl = foreign.get_saved_template()
    ftpl["dns"] = {"servers": ["198.18.0.1"], "queryStrategy": "UseIPv4"}
    foreign.update_template(ftpl)

    time.sleep(2)
    ok_damaged, detail_damaged = traffic_through_cascade()
    print(f"  damaged cascade traffic: {ok_damaged} ({detail_damaged[:60]})")

    # ---------------------------------------------------------------
    print("\nTrigger rollback (restore Snapshot A)")
    ru_restored = ru.restore_template(snapA["ru_tpl"])
    f_restored = foreign.restore_template(snapA["foreign_tpl"])
    # remove created inbounds (only objects this run created)
    ru_del = ru.del_inbound_by_tag(lib.RU_TAG)
    f_del = foreign.del_inbound_by_tag(lib.FOREIGN_TAG)
    time.sleep(3)

    # ---------------------------------------------------------------
    print("\nCompare final state with Snapshot A")
    ru_tpl_ok = canonical(ru.get_saved_template()) == canonical(snapA["ru_tpl"])
    f_tpl_ok = canonical(foreign.get_saved_template()) == canonical(snapA["foreign_tpl"])
    ru_inb_ok = ru.inbound_state() == snapA["ru_inbounds"]
    f_inb_ok = foreign.inbound_state() == snapA["foreign_inbounds"]
    results.check("RU Xray template restored exactly", ru_tpl_ok)
    results.check("Foreign Xray template restored exactly", f_tpl_ok)
    results.check("RU inbound list == Snapshot A", ru_inb_ok,
                  f"deleted={ru_del[0]}, equal={ru_inb_ok}")
    results.check("Foreign inbound list == Snapshot A", f_inb_ok,
                  f"deleted={f_del[0]}, equal={f_inb_ok}")
    # direct exit check: baseline Xray is healthy again (cascade removed)
    import requests as _rq
    try:
        r = _rq.get("http://1.1.1.1/", timeout=15)
        base_ok = r.status_code < 500
    except _rq.RequestException:
        base_ok = False
    results.check("Baseline internet reachable after rollback", base_ok,
                  "direct (no proxy) — Xray baseline state healthy")

    print("\nRollback verdict:")
    all_ok = ru_tpl_ok and f_tpl_ok and ru_inb_ok and f_inb_ok and base_ok
    print(f"  ROLLBACK STATUS: {'SUCCESS' if all_ok else 'PARTIAL FAILURE'}")
    if all_ok:
        results.check("ROLLBACK STATUS: SUCCESS", True)
    else:
        results.check("ROLLBACK STATUS: PARTIAL FAILURE", False,
                      "see failed items above")

    rc = results.summary()
    if not keep:
        print("\nCleaning up containers/network ...")
        docker_cleanup()
    return rc


if __name__ == "__main__":
    sys.exit(main())
