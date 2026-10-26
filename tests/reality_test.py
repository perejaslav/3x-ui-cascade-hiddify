#!/usr/bin/env python3
"""Reality compatibility test for the 3x-ui cascade skill (v2.0.1).

Production-default scheme, end to end in Docker:

    xray client (http inbound, client container)
        → VLESS + Reality + xtls-rprx-vision → RU (inbound hiddify-entry)
        → routing → VLESS + Reality + xtls-rprx-vision → Foreign (from-ru)
        → freedom → internet

Checks:
  - x25519 key pairs generated per server (xray x25519);
  - private keys ONLY on the server side (inbound), public keys on the
    client side (outbound / share URI) — role-mismatch sanity;
  - shortId hex, SNI/fingerprint/flow consistent between all three sides;
  - generated VLESS share URI points to the RU entry host (never foreign);
  - TCP flow through the full Reality chain works;
  - foreign inbound traffic counters increase;
  - domain-based exit probe (informational; WARN under fake-ip DNS).

Run: python tests/reality_test.py   (also wired into CI scheduled run)
Uses the OFFICIAL image with a PINNED version. admin/admin valid ONLY for the
fresh Docker test environment.
"""

import os
import re
import sys
import time
import urllib.parse
import uuid as uuidlib

import requests

import cascade_lib as lib
from cascade_lib import (
    Results, docker_available, docker_cleanup, docker_up, wait_panel,
    connect_panels, sh, ensure_foreign_egress, foreign_traffic_nonzero,
    traffic_through_cascade, rand_subid,
)

results = Results()

REALITY_DEST = "1.1.1.1:443"           # IP literal: no DNS dependency
REALITY_SNI = "cloudflare-dns.com"     # cert served by 1.1.1.1 (verified TLS1.3)
REALITY_SID = "abcdef1234567890"       # hex shortId
CLIENT_NAME = "reality-client"
CLIENT_HTTP_PORT = 10808

XRAY_BIN_CANDIDATES = ["/app/bin/xray-linux-amd64", "/app/bin/xray",
                       "/usr/local/x-ui/bin/xray-linux-amd64"]


def xray_binary(container):
    for cand in XRAY_BIN_CANDIDATES:
        if sh(["docker", "exec", container, "ls", cand]).returncode == 0:
            return cand
    r = sh(["docker", "exec", container, "sh", "-c",
            "find /app /usr/local -maxdepth 4 -name 'xray*' -type f 2>/dev/null | head -1"])
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip().splitlines()[0]
    raise RuntimeError(f"xray binary not found in {container}")


def x25519_keypair(container):
    binary = xray_binary(container)
    r = sh(["docker", "exec", container, binary, "x25519"], check_rc=True)
    out = r.stdout
    priv = re.search(r"PrivateKey:\s*(\S+)", out)
    pub = re.search(r"Password \(PublicKey\):\s*(\S+)", out)
    if not priv or not pub:
        raise RuntimeError("cannot parse x25519 output (redacted; may contain private key)")
    return priv.group(1), pub.group(1)


def reality_stream_server(private_key):
    return {
        "network": "tcp", "security": "reality", "externalProxy": [],
        "realitySettings": {
            "show": False, "dest": REALITY_DEST, "xver": 0,
            "serverNames": [REALITY_SNI], "privateKey": private_key,
            "minClient": "", "maxClient": "", "maxTimediff": 0,
            "shortIds": [REALITY_SID], "spiderX": "",
        },
        "tcpSettings": {"acceptProxyProtocol": False, "header": {"type": "none"}},
    }


def reality_stream_client(public_key):
    return {
        "network": "tcp", "security": "reality",
        "realitySettings": {
            "show": False, "fingerprint": "chrome", "serverName": REALITY_SNI,
            "publicKey": public_key, "shortId": REALITY_SID, "spiderX": "/",
        },
    }


def build_share_uri(user_uuid, host, port, public_key):
    q = urllib.parse.urlencode({
        "type": "tcp", "security": "reality", "pbk": public_key,
        "fp": "chrome", "sni": REALITY_SNI, "sid": REALITY_SID,
        "flow": "xtls-rprx-vision", "encryption": "none",
    })
    return f"vless://{user_uuid}@{host}:{port}?{q}#hiddify-entry"


def main():
    print(f"3x-ui Reality compatibility test — image "
          f"{lib.IMAGE}:{lib.THREEXUI_VERSION_DEFAULT}\n")
    if not docker_available():
        return 2
    keep = os.environ.get("KEEP_CONTAINERS", "") == "1"

    print("Phase 0: environment (foreign + RU + xray client container)")
    docker_cleanup(names=(lib.RU_NAME, lib.FOREIGN_NAME, CLIENT_NAME))
    docker_up()
    img = f"{lib.IMAGE}:{lib.THREEXUI_VERSION_DEFAULT}"
    lib.docker_run_owned(["--network", lib.NETWORK,
        "--hostname", CLIENT_NAME, "-p", f"127.0.0.1:{CLIENT_HTTP_PORT}:{CLIENT_HTTP_PORT}",
        "--entrypoint", "/bin/sh", img, "-c", "sleep 3600"], CLIENT_NAME)
    wait_panel(lib.FOREIGN_URL, "foreign")
    wait_panel(lib.RU_URL, "ru")
    foreign, ru = connect_panels()

    print("\nPhase 1: key pairs (xray x25519 per server)")
    f_priv, f_pub = x25519_keypair(lib.FOREIGN_NAME)
    r_priv, r_pub = x25519_keypair(lib.RU_NAME)
    results.check("Reality key pairs generated (both servers)",
                  bool(f_priv and f_pub and r_priv and r_pub),
                  f"foreign pub={f_pub[:8]}…, ru pub={r_pub[:8]}…")
    results.check("Key roles distinct (no shared private/public values)",
                  f_priv != f_pub and r_priv != r_pub and f_priv != r_priv)

    print("\nPhase 2: configure cascade (Reality, xtls-rprx-vision)")
    svc_uuid = str(uuidlib.uuid4())
    user_uuid = str(uuidlib.uuid4())
    results.check("Service client UUID valid",
                  re.fullmatch(r"[0-9a-f-]{36}", svc_uuid) is not None)

    # Foreign: VLESS + Reality inbound for the RU -> foreign hop
    resp = foreign.add_inbound({
        "up": 0, "down": 0, "total": 0, "remark": lib.FOREIGN_TAG,
        "enable": True, "expiryTime": 0, "listen": "",
        "port": lib.FOREIGN_INBOUND_PORT, "protocol": "vless",
        "tag": lib.FOREIGN_TAG,
        "settings": {
            "clients": [{
                "id": svc_uuid, "flow": "xtls-rprx-vision",
                "email": lib.SERVICE_CLIENT_EMAIL,
                "limitIp": 0, "totalGB": 0, "expiryTime": 0, "enable": True,
                "tgId": 0, "subId": rand_subid(), "reset": 0,
            }],
            "decryption": "none", "fallbacks": [],
        },
        "streamSettings": reality_stream_server(f_priv),
        "sniffing": {"enabled": False, "destOverride": [], "metadataOnly": False},
        "allocate": {"strategy": "always", "refresh": 5, "concurrency": 3},
    })
    results.check("Foreign Reality inbound created",
                  bool(resp.get("success")), str(resp.get("msg", ""))[:60])

    ensure_foreign_egress(foreign)

    # RU: VLESS + Reality inbound as the Hiddify-facing entry
    resp = ru.add_inbound({
        "up": 0, "down": 0, "total": 0, "remark": lib.RU_TAG,
        "enable": True, "expiryTime": 0, "listen": "",
        "port": lib.RU_INBOUND_PORT, "protocol": "vless",
        "tag": lib.RU_TAG,
        "settings": {
            "clients": [{
                "id": user_uuid, "flow": "xtls-rprx-vision",
                "email": "windows-hiddify-client",
                "limitIp": 0, "totalGB": 0, "expiryTime": 0, "enable": True,
                "tgId": 0, "subId": rand_subid(), "reset": 0,
            }],
            "decryption": "none", "fallbacks": [],
        },
        "streamSettings": reality_stream_server(r_priv),
        "sniffing": {"enabled": False, "destOverride": [], "metadataOnly": False},
        "allocate": {"strategy": "always", "refresh": 5, "concurrency": 3},
    })
    results.check("RU Reality inbound created",
                  bool(resp.get("success")), str(resp.get("msg", ""))[:60])

    # RU template: Reality outbound to foreign + routing (planned update)
    from cascade_lib import ensure_ru_outbound_and_routing
    ensure_ru_outbound_and_routing(
        ru, svc_uuid,
        stream_settings=reality_stream_client(f_pub),
        client_flow="xtls-rprx-vision")

    # sanity: private keys must not leak into client-side artifacts
    tpl = ru.get_saved_template()
    out = next(o for o in tpl["outbounds"] if o.get("tag") == lib.RU_OUTBOUND_TAG)
    out_ser = __import__("json").dumps(out)
    results.check("No private key in RU outbound (role check)",
                  r_priv not in out_ser and f_priv not in out_ser)

    print("\nPhase 3: share URI validation")
    uri = build_share_uri(user_uuid, lib.RU_NAME, lib.RU_INBOUND_PORT, r_pub)
    parsed = urllib.parse.urlparse(uri)
    q = dict(urllib.parse.parse_qsl(parsed.query))
    host_is_ru = parsed.hostname == lib.RU_NAME
    host_not_foreign = parsed.hostname != lib.FOREIGN_NAME
    fields_ok = (q.get("security") == "reality" and q.get("sni") == REALITY_SNI
                 and q.get("fp") == "chrome" and q.get("sid") == REALITY_SID
                 and q.get("pbk") == r_pub
                 and q.get("flow") == "xtls-rprx-vision")
    no_priv = r_priv not in uri and f_priv not in uri
    results.check("Share URI points to RU entry (never foreign)",
                  host_is_ru and host_not_foreign,
                  f"host={parsed.hostname}, port={parsed.port}")
    results.check("Share URI Reality fields consistent",
                  fields_ok and no_priv,
                  f"sni={q.get('sni')}, sid={q.get('sid')}, "
                  f"fp={q.get('fp')}, flow={q.get('flow')}, priv_leak={not no_priv}")

    print("\nPhase 4: xray client container (VLESS+Reality to RU)")
    client_cfg = {
        "log": {"loglevel": "warning"},
        "inbounds": [{
            "tag": "http-in", "listen": "0.0.0.0", "port": CLIENT_HTTP_PORT,
            "protocol": "http", "settings": {"allowTransparent": False},
        }],
        "outbounds": [{
            "tag": "to-ru", "protocol": "vless",
            "settings": {"vnext": [{
                "address": lib.RU_NAME, "port": lib.RU_INBOUND_PORT,
                "users": [{"id": user_uuid, "encryption": "none",
                           "flow": "xtls-rprx-vision"}],
            }]},
            "streamSettings": reality_stream_client(r_pub),
        }],
    }
    import json as _json
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                     encoding="utf-8") as f:
        _json.dump(client_cfg, f)
        cfg_path = f.name
    sh(["docker", "cp", cfg_path, f"{CLIENT_NAME}:/tmp/client.json"], check_rc=True)
    xbin = xray_binary(CLIENT_NAME)
    sh(["docker", "exec", "-d", CLIENT_NAME, xbin, "run", "-c", "/tmp/client.json"])
    os.unlink(cfg_path)
    time.sleep(5)

    print("\nPhase 5: end-to-end traffic through full Reality chain")
    ok, detail = traffic_through_cascade(port=CLIENT_HTTP_PORT)
    results.check("TCP end-to-end (Reality + vision, both legs)", ok, detail)

    ok2, detail2 = foreign_traffic_nonzero(foreign)
    results.check("Foreign Reality inbound counters > 0", ok2, detail2)

    fake, sample_ip = lib.host_dns_is_fakeip()
    if fake:
        results.warn("Domain-exit probe skipped (fake-ip DNS on host)",
                     f"host resolves domains to {sample_ip} (198.18.0.0/15)")
    else:
        try:
            r = requests.get(
                "http://ip-api.com/json/?fields=query,countryCode",
                proxies={"http": f"http://127.0.0.1:{CLIENT_HTTP_PORT}"},
                timeout=20)
            results.check("Domain-exit probe works", r.status_code == 200,
                          r.text[:80])
        except requests.RequestException as e:
            results.warn("Domain-exit probe failed", str(e)[:80])

    rc = results.summary()
    if not keep:
        print("\nCleaning up containers/network ...")
        docker_cleanup(names=(lib.RU_NAME, lib.FOREIGN_NAME, CLIENT_NAME))
    else:
        print(f"\nKEEP_CONTAINERS=1 — containers left running")
    return rc


if __name__ == "__main__":
    sys.exit(main())
