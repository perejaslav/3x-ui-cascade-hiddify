#!/usr/bin/env python3
"""Docker integration test for the 3x-ui cascade skill (v2.0.0).

Simulates the two-server cascade on one Docker host:

    test client (requests) --> RU container (http inbound :11112, tag hiddify-entry)
        --> routing rule --> VLESS outbound to-foreign
        --> foreign container (VLESS inbound :22220, tag from-ru, service client)
        --> freedom --> internet

Checks:
  1. Modern/legacy API discovery works (capability detection, not version guessing).
  2. Cascade TCP flow works end-to-end (traffic exits through the foreign container).
  3. Foreign inbound traffic counters increase (service-client architecture proven).
  4. Idempotency: a second configuration run reports NO CHANGES REQUIRED and
     creates no duplicate inbounds/clients/outbounds/routing rules.
  5. DNS environment detection: container DNS may be poisoned by a fake-ip VPN
     on the Docker host (198.18.0.0/15) — detected and reported; the traffic
     test itself uses a public IP target so it stays deterministic.
  6. Negative probe: a deliberately broken routing tag does not destroy the
     working setup.

Uses the OFFICIAL image with a PINNED version. `latest` is opt-in via env.
Default credentials admin/admin are valid ONLY for this fresh Docker test
environment (historical/testing fact) — never assume them in production.

API notes baked in from 3x-ui v3.7.0 (see references/api-compatibility.md):
  - modern Xray API:  GET/POST under /panel/api/xray/* ; legacy /panel/xray/*
  - GET  getDefaultJsonConfig returns the DEFAULT template, not the current one;
    the CURRENT saved template is read via POST /panel/api/xray/ (getXraySetting)
    -> {"obj": "<json string>"} -> {"xraySetting": <template>}.
  - template update: POST form field `xraySetting`.
  - inbound payloads accept typed nested JSON AND legacy JSON-encoded strings;
    client field `tgId` must be an int (0), NOT a string.
  - inbound "settings" may be returned as dict (typed) or str (legacy).

Usage:
    python tests/cascade_docker_test.py
    THREEXUI_VERSION=v3.7.0 KEEP_CONTAINERS=1 python tests/cascade_docker_test.py
"""

import json
import os
import random
import shutil
import socket
import string
import subprocess
import sys
import time
import uuid as uuidlib

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

THREEXUI_VERSION = os.environ.get("THREEXUI_VERSION", "v3.7.0")
IMAGE = "ghcr.io/mhsanaei/3x-ui"
# Docker-test-only credentials (fresh container). Production must always ask.
PANEL_USER = os.environ.get("THREEXUI_TEST_USER", "admin")
PANEL_PASS = os.environ.get("THREEXUI_TEST_PASS", "admin")
KEEP = os.environ.get("KEEP_CONTAINERS", "") == "1"

NETWORK = "cascade-net"
FOREIGN_NAME = "foreign-cascade"
RU_NAME = "ru-cascade"
FOREIGN_PANEL_HOST_PORT = 22210  # -> container 2053
RU_PANEL_HOST_PORT = 11110       # -> container 2053
FOREIGN_INBOUND_PORT = 22220
RU_INBOUND_PORT = 11112  # deliberately NOT 11111 (common metrics listener)

FOREIGN_TAG = "from-ru"
SERVICE_CLIENT_EMAIL = "ru-to-foreign-client"
RU_TAG = "hiddify-entry"
RU_OUTBOUND_TAG = "to-foreign"

FOREIGN_URL = f"http://127.0.0.1:{FOREIGN_PANEL_HOST_PORT}"
RU_URL = f"http://127.0.0.1:{RU_PANEL_HOST_PORT}"

# Public IP target for the traffic test — deterministic, independent of the
# host's DNS environment (a fake-ip VPN on the Docker host poisons ALL DNS,
# including direct queries to 8.8.8.8, so domain targets are not reliable).
TEST_TARGET = "http://1.1.1.1/"

PROBE_TIMEOUT = 180

results = []


def check(name, ok, detail=""):
    status = "PASS" if ok else "FAIL"
    results.append((name, status, detail))
    print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def warn(name, detail=""):
    results.append((name, "WARN", detail))
    print(f"  [WARN] {name}" + (f" — {detail}" if detail else ""))


def sh(cmd, check_rc=False):
    print(f"  $ {' '.join(cmd)}")
    r = subprocess.run(cmd, capture_output=True, text=True, shell=False)
    if check_rc and r.returncode != 0:
        raise RuntimeError(f"command failed: {' '.join(cmd)}\n{r.stdout}\n{r.stderr}")
    return r


def rand_subid():
    return "".join(random.choices(string.ascii_letters + string.digits, k=16))


def as_dict(value):
    """API may return settings/streamSettings as dict (typed, v3.7.0+) or as
    a JSON-encoded string (legacy). Normalize to dict."""
    if isinstance(value, dict):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return {}


# ---------------------------------------------------------------------------
# Session auth (CSRF flow) + API capability discovery
# ---------------------------------------------------------------------------

class Panel:
    def __init__(self, base_url, name):
        self.base = base_url.rstrip("/")
        self.name = name
        self.s = requests.Session()
        self.csrf = None
        self.api_mode = None  # "modern" | "legacy"
        self.xray_base = None  # endpoint prefix for xray endpoints

    def login(self):
        r = self.s.get(f"{self.base}/csrf-token", timeout=10)
        r.raise_for_status()
        self.csrf = r.json()["obj"]
        r = self.s.post(
            f"{self.base}/login",
            json={"username": PANEL_USER, "password": PANEL_PASS},
            headers={"X-CSRF-Token": self.csrf},
            timeout=10,
        )
        if r.status_code == 403:
            r = self.s.post(
                f"{self.base}/login",
                json={"username": PANEL_USER, "password": PANEL_PASS},
                timeout=10,
            )
        data = r.json()
        if not data.get("success"):
            raise RuntimeError(f"{self.name}: login failed: {data}")
        print(f"  {self.name}: logged in (session mode)")

    def auth_headers(self):
        h = {}
        if self.csrf:
            h["X-CSRF-Token"] = self.csrf
        return h

    def discover_api(self):
        """Probe modern /panel/api/xray/* before legacy /panel/xray/*.

        401/403 means the endpoint exists but auth failed; 404 means absent.
        """
        for mode, path in (
            ("modern", "/panel/api/xray/getDefaultJsonConfig"),
            ("legacy", "/panel/xray/getDefaultJsonConfig"),
        ):
            r = self.s.get(f"{self.base}{path}", headers=self.auth_headers(), timeout=10)
            if r.status_code == 200:
                body = r.json()
                if body.get("success"):
                    self.api_mode = mode
                    self.xray_base = path.rsplit("/", 1)[0]
                    print(f"  {self.name}: API mode = {mode}")
                    return mode
            else:
                print(f"  {self.name}: probe {path} -> HTTP {r.status_code}")
        raise RuntimeError(f"{self.name}: no working Xray API endpoint found")

    def get_saved_template(self):
        """Read the CURRENT saved Xray template via POST <xray_base>/ (getXraySetting).

        NOTE: GET getDefaultJsonConfig returns the DEFAULT template — it must
        not be used as the read source for read-modify-write cycles.
        """
        r = self.s.post(f"{self.base}{self.xray_base}/",
                        headers=self.auth_headers(), timeout=10)
        r.raise_for_status()
        body = r.json()
        if not body.get("success"):
            raise RuntimeError(f"{self.name}: getXraySetting failed: {body}")
        data = json.loads(body["obj"]) if isinstance(body["obj"], str) else body["obj"]
        tpl = data.get("xraySetting", data)
        if isinstance(tpl, str):
            tpl = json.loads(tpl)
        return tpl

    def update_template(self, config):
        r = self.s.post(
            f"{self.base}{self.xray_base}/update",
            data={"xraySetting": json.dumps(config)},
            headers=self.auth_headers(),
            timeout=15,
        )
        try:
            return r.json()
        except ValueError:
            return {"success": False, "msg": f"HTTP {r.status_code}: {r.text[:200]}"}

    def list_inbounds(self):
        r = self.s.get(f"{self.base}/panel/api/inbounds/list",
                       headers=self.auth_headers(), timeout=10)
        r.raise_for_status()
        body = r.json()
        if not body.get("success"):
            raise RuntimeError(f"{self.name}: inbounds/list failed: {body}")
        return body["obj"]

    def add_inbound(self, payload):
        r = self.s.post(f"{self.base}/panel/api/inbounds/add",
                        json=payload, headers=self.auth_headers(), timeout=10)
        try:
            return r.json()
        except ValueError:
            return {"success": False, "msg": f"HTTP {r.status_code}: {r.text[:200]}"}

    def get_inbound(self, inbound_id):
        r = self.s.get(f"{self.base}/panel/api/inbounds/get/{inbound_id}",
                       headers=self.auth_headers(), timeout=10)
        r.raise_for_status()
        return r.json()


# ---------------------------------------------------------------------------
# Docker helpers
# ---------------------------------------------------------------------------

def docker_cleanup():
    for name in (RU_NAME, FOREIGN_NAME):
        sh(["docker", "rm", "-f", name])
    sh(["docker", "network", "rm", NETWORK])


def docker_up():
    sh(["docker", "network", "create", NETWORK], check_rc=False)
    img = f"{IMAGE}:{THREEXUI_VERSION}"
    print(f"  pulling {img} ...")
    sh(["docker", "pull", img], check_rc=True)
    sh(["docker", "run", "-d", "--name", FOREIGN_NAME, "--network", NETWORK,
        "--hostname", FOREIGN_NAME,
        "-p", f"{FOREIGN_PANEL_HOST_PORT}:2053",
        "-p", f"{FOREIGN_INBOUND_PORT}:{FOREIGN_INBOUND_PORT}", img], check_rc=True)
    sh(["docker", "run", "-d", "--name", RU_NAME, "--network", NETWORK,
        "--hostname", RU_NAME,
        "-p", f"{RU_PANEL_HOST_PORT}:2053",
        "-p", f"{RU_INBOUND_PORT}:{RU_INBOUND_PORT}", img], check_rc=True)


def wait_panel(base_url, name):
    deadline = time.time() + PROBE_TIMEOUT
    while time.time() < deadline:
        try:
            r = requests.get(base_url + "/", timeout=5, verify=False)
            if r.status_code < 500:
                print(f"  {name}: panel is up (HTTP {r.status_code})")
                return True
        except requests.RequestException:
            pass
        time.sleep(3)
    raise RuntimeError(f"{name}: panel did not come up in {PROBE_TIMEOUT}s")


def host_dns_is_fakeip():
    """Detect a fake-ip DNS environment (e.g. VPN client on the Docker host).

    198.18.0.0/15 is the fake-ip range used by sing-box/Hiddify etc. Docker
    Desktop forwards container DNS through the host, so ALL container DNS —
    including direct queries to public resolvers — can return fake IPs there.
    """
    try:
        ip = socket.gethostbyname("ip-api.com")
        return ip.startswith("198.18."), ip
    except OSError:
        return False, "unresolved"


# ---------------------------------------------------------------------------
# Idempotent configuration primitives (find-before-create)
# ---------------------------------------------------------------------------

def find_inbound(panel, tag):
    for inb in panel.list_inbounds():
        if inb.get("tag") == tag or inb.get("remark") == tag:
            return inb
    return None


def ensure_foreign_inbound(foreign, service_uuid):
    """Create inbound from-ru + service client ru-to-foreign-client if absent."""
    existing = find_inbound(foreign, FOREIGN_TAG)
    if existing:
        settings = as_dict(existing["settings"])
        emails = [c.get("email") for c in settings.get("clients", [])]
        if SERVICE_CLIENT_EMAIL in emails:
            print(f"  foreign: inbound '{FOREIGN_TAG}' + client exist — reuse")
            return False
        raise RuntimeError(
            f"conflict: inbound '{FOREIGN_TAG}' exists without service client — "
            "STOP, do not overwrite automatically")
    payload = {
        "up": 0, "down": 0, "total": 0, "remark": FOREIGN_TAG,
        "enable": True, "expiryTime": 0, "listen": "", "port": FOREIGN_INBOUND_PORT,
        "protocol": "vless", "tag": FOREIGN_TAG,
        # typed payload preferred (v3.x accepts it); legacy string serialization
        # is the fallback for older versions. tgId MUST be an int.
        "settings": {
            "clients": [{
                "id": service_uuid, "flow": "", "email": SERVICE_CLIENT_EMAIL,
                "limitIp": 0, "totalGB": 0, "expiryTime": 0, "enable": True,
                "tgId": 0, "subId": rand_subid(), "reset": 0,
            }],
            "decryption": "none", "fallbacks": [],
        },
        "streamSettings": {
            "network": "tcp", "security": "none", "externalProxy": [],
            "tcpSettings": {"acceptProxyProtocol": False,
                            "header": {"type": "none"}},
        },
        "sniffing": {"enabled": False, "destOverride": [],
                     "metadataOnly": False},
        "allocate": {"strategy": "always", "refresh": 5, "concurrency": 3},
    }
    resp = foreign.add_inbound(payload)
    if not resp.get("success"):
        raise RuntimeError(f"foreign: add inbound failed: {resp}")
    print(f"  foreign: created inbound '{FOREIGN_TAG}' (id={resp.get('obj', {}).get('id')})")
    return True


def ensure_foreign_egress(foreign):
    """Ensure foreign template egress resolves real IPs.

    In fake-ip DNS environments the freedom outbound would dial 198.18.x.x and
    Xray blackholes reserved-range targets. Explicit DNS servers +
    domainStrategy=UseIPv4 make egress deterministic. Idempotent.
    """
    tpl = foreign.get_saved_template()
    changed = False
    if not tpl.get("dns") or not tpl["dns"].get("servers"):
        tpl["dns"] = {"servers": ["8.8.8.8", "1.1.1.1"], "queryStrategy": "UseIPv4"}
        changed = True
    for o in tpl.get("outbounds", []):
        if o.get("protocol") == "freedom":
            if as_dict(o.get("settings")).get("domainStrategy") != "UseIPv4":
                o["settings"] = dict(as_dict(o.get("settings")), domainStrategy="UseIPv4")
                changed = True
    if changed:
        resp = foreign.update_template(tpl)
        if not resp.get("success"):
            raise RuntimeError(f"foreign: egress template update failed: {resp}")
        print("  foreign: egress DNS/domainStrategy configured (live apply)")
        time.sleep(3)
        return True
    print("  foreign: egress already configured — reuse")
    return False


def ensure_ru_inbound(ru):
    existing = find_inbound(ru, RU_TAG)
    if existing:
        print(f"  RU: inbound '{RU_TAG}' exists — reuse")
        return False
    payload = {
        "up": 0, "down": 0, "total": 0, "remark": RU_TAG,
        "enable": True, "expiryTime": 0, "listen": "", "port": RU_INBOUND_PORT,
        # http inbound = plain HTTP proxy, testable with curl/requests.
        # Validates routing + cascade flow; VLESS handshake is validated on the
        # RU->foreign hop. (SOCKS is not a standalone 3x-ui protocol.)
        "protocol": "http", "tag": RU_TAG,
        "settings": {"allowTransparent": False, "userLevel": 0},
        "streamSettings": {
            "network": "tcp", "security": "none", "externalProxy": [],
        },
        "sniffing": {"enabled": False, "destOverride": [],
                     "metadataOnly": False},
        "allocate": {"strategy": "always", "refresh": 5, "concurrency": 3},
    }
    resp = ru.add_inbound(payload)
    if not resp.get("success"):
        raise RuntimeError(f"RU: add inbound failed: {resp}")
    print(f"  RU: created inbound '{RU_TAG}' (id={resp.get('obj', {}).get('id')})")
    return True


def ensure_ru_outbound_and_routing(ru, service_uuid):
    """Add outbound to-foreign + routing rule into the saved template (idempotent)."""
    config = ru.get_saved_template()
    changed = False

    outbounds = config.setdefault("outbounds", [])
    if not any(o.get("tag") == RU_OUTBOUND_TAG for o in outbounds):
        outbounds.append({
            "tag": RU_OUTBOUND_TAG, "protocol": "vless",
            "settings": {"vnext": [{
                "address": FOREIGN_NAME, "port": FOREIGN_INBOUND_PORT,
                "users": [{"id": service_uuid, "encryption": "none", "flow": ""}],
            }]},
            "streamSettings": {"network": "tcp", "security": "none"},
        })
        changed = True
        print(f"  RU: added outbound '{RU_OUTBOUND_TAG}'")

    rules = config.setdefault("routing", {}).setdefault("rules", [])
    rule_present = any(
        RU_TAG in (r.get("inboundTag") or []) and r.get("outboundTag") == RU_OUTBOUND_TAG
        for r in rules
    )
    if not rule_present:
        # prepend: an earlier catch-all rule must not shadow the cascade route
        rules.insert(0, {"type": "field",
                         "inboundTag": [RU_TAG],
                         "outboundTag": RU_OUTBOUND_TAG})
        changed = True
        print(f"  RU: added routing rule {RU_TAG} -> {RU_OUTBOUND_TAG}")

    if changed:
        resp = ru.update_template(config)
        if not resp.get("success"):
            raise RuntimeError(f"RU: xray template update failed: {resp}")
        print(f"  RU: Xray template updated ({ru.api_mode} API, live apply)")
        time.sleep(5)
    else:
        print("  RU: outbound + routing already present — reuse")
    return changed


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def traffic_through_cascade(target=TEST_TARGET, timeout=25):
    try:
        r = requests.get(
            target,
            proxies={"http": f"http://127.0.0.1:{RU_INBOUND_PORT}"},
            timeout=timeout,
        )
        return r.status_code < 500, f"HTTP {r.status_code}, {len(r.content)} bytes"
    except requests.RequestException as e:
        return False, str(e)[:120]


def foreign_traffic_nonzero(foreign):
    inb = find_inbound(foreign, FOREIGN_TAG)
    if not inb:
        return False, "inbound disappeared"
    deadline = time.time() + 45
    total = inb.get("up", 0) + inb.get("down", 0)
    while time.time() < deadline and total == 0:
        time.sleep(5)
        body = foreign.get_inbound(inb["id"])
        if body.get("success") and body.get("obj"):
            inb = body["obj"]
            total = inb.get("up", 0) + inb.get("down", 0)
    return total > 0, f"up+down={total}"


def count_outbound_tag(config, tag):
    return sum(1 for o in config.get("outbounds", []) if o.get("tag") == tag)


def count_routing_rule(config):
    return sum(
        1 for r in config.get("routing", {}).get("rules", [])
        if RU_TAG in (r.get("inboundTag") or []) and r.get("outboundTag") == RU_OUTBOUND_TAG
    )


# ---------------------------------------------------------------------------
# Test phases
# ---------------------------------------------------------------------------

def main():
    print(f"3x-ui cascade Docker integration test — image {IMAGE}:{THREEXUI_VERSION}\n")
    docker_available = shutil.which("docker") is not None
    if not docker_available:
        print("Docker CLI not found — cannot run integration test.")
        return 2
    probe = subprocess.run(["docker", "ps"], capture_output=True, text=True)
    if probe.returncode != 0:
        print("Docker daemon not reachable — cannot run integration test.")
        print(probe.stderr.strip())
        return 2

    print("Phase 0: environment")
    docker_cleanup()
    docker_up()
    wait_panel(FOREIGN_URL, "foreign")
    wait_panel(RU_URL, "ru")
    fake, sample_ip = host_dns_is_fakeip()
    if fake:
        warn("Docker-host DNS is fake-ip (VPN client)",
             f"containers resolve domains to {sample_ip} (198.18.0.0/15); "
             "traffic test uses an IP target; foreign egress uses explicit DNS")

    print("\nPhase 1: API discovery + login (both panels)")
    foreign = Panel(FOREIGN_URL, "foreign")
    ru = Panel(RU_URL, "ru")
    foreign.login()
    ru.login()
    foreign.discover_api()
    ru.discover_api()
    check("API discovery modern/legacy", foreign.api_mode and ru.api_mode,
          f"foreign={foreign.api_mode}, ru={ru.api_mode}")

    service_uuid = str(uuidlib.uuid4())

    print("\nPhase 2: configure cascade (idempotent primitives)")
    ensure_foreign_inbound(foreign, service_uuid)
    ensure_foreign_egress(foreign)
    ensure_ru_inbound(ru)
    ensure_ru_outbound_and_routing(ru, service_uuid)

    print("\nPhase 3: end-to-end traffic")
    ok, detail = traffic_through_cascade()
    check("TCP end-to-end through cascade", ok, detail)
    ok2, detail2 = foreign_traffic_nonzero(foreign)
    check("Foreign inbound traffic counters > 0", ok2, detail2)
    if fake:
        okd, detaild = traffic_through_cascade("http://ip-api.com/json/")
        warn("Domain-target traffic (informational, poisoned-DNS env)",
             f"{'works' if okd else 'blocked by host fake-ip DNS, expected here'} ({detaild})")

    print("\nPhase 4: idempotency re-run")
    created_foreign = ensure_foreign_inbound(foreign, service_uuid)
    created_egress = ensure_foreign_egress(foreign)
    created_ru = ensure_ru_inbound(ru)
    created_tpl = ensure_ru_outbound_and_routing(ru, service_uuid)
    check("Re-run creates no duplicates",
          not (created_foreign or created_egress or created_ru or created_tpl),
          f"foreign_inbound={created_foreign}, egress={created_egress}, "
          f"ru_inbound={created_ru}, template={created_tpl}")

    inbounds_f = foreign.list_inbounds()
    inbounds_r = ru.list_inbounds()
    tag_f = [i["tag"] for i in inbounds_f]
    tag_r = [i["tag"] for i in inbounds_r]
    check("Single inbound per tag",
          tag_f.count(FOREIGN_TAG) == 1 and tag_r.count(RU_TAG) == 1,
          f"foreign tags={tag_f}, ru tags={tag_r}")

    fsettings = as_dict(find_inbound(foreign, FOREIGN_TAG)["settings"])
    clients = [c.get("email") for c in fsettings.get("clients", [])]
    check("Single service client", clients.count(SERVICE_CLIENT_EMAIL) == 1,
          f"clients={clients}")

    tpl = ru.get_saved_template()
    check("Single outbound + single routing rule",
          count_outbound_tag(tpl, RU_OUTBOUND_TAG) == 1 and count_routing_rule(tpl) == 1,
          f"outbounds={count_outbound_tag(tpl, RU_OUTBOUND_TAG)}, "
          f"rules={count_routing_rule(tpl)}")

    if not (created_foreign or created_egress or created_ru or created_tpl):
        print("  >>> NO CHANGES REQUIRED")

    print("\nPhase 5: negative probe (broken routing tag must not corrupt state)")
    tpl_bad = ru.get_saved_template()
    for r in tpl_bad.get("routing", {}).get("rules", []):
        if RU_TAG in (r.get("inboundTag") or []):
            r["outboundTag"] = "nonexistent-outbound"
    ru.update_template(tpl_bad)
    # restore the correct rule from the current saved state
    tpl_now = ru.get_saved_template()
    ok_restore = count_outbound_tag(tpl_now, RU_OUTBOUND_TAG) == 1
    for r in tpl_now.get("routing", {}).get("rules", []):
        if RU_TAG in (r.get("inboundTag") or []):
            r["outboundTag"] = RU_OUTBOUND_TAG
    ru.update_template(tpl_now)
    time.sleep(3)
    ok_after, _ = traffic_through_cascade()
    check("Broken tag does not destroy setup; restored + traffic works",
          ok_restore and ok_after, f"restored={ok_restore}, traffic={ok_after}")

    print("\n" + "=" * 60)
    failed = [r for r in results if r[1] == "FAIL"]
    for name, status, detail in results:
        print(f"  [{status:4}] {name}" + (f" — {detail}" if detail else ""))
    print("=" * 60)
    if failed:
        print(f"RESULT: FAIL ({len(failed)} failed of {len(results)})")
        rc = 1
    else:
        print(f"RESULT: PASS ({len(results)}/{len(results)})")
        rc = 0

    if not KEEP:
        print("\nCleaning up containers/network ...")
        docker_cleanup()
    else:
        print(f"\nKEEP_CONTAINERS=1 — containers left running "
              f"({RU_NAME}, {FOREIGN_NAME})")
    return rc


if __name__ == "__main__":
    sys.exit(main())
