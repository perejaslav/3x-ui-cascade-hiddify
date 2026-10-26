#!/usr/bin/env python3
"""Shared library for 3x-ui cascade Docker tests (skill v2.0.1).

Provides:
  - Panel: authenticated 3x-ui API client with modern/legacy capability
    discovery, CURRENT-template read (getXraySetting), template update,
    planned_update() with semantic pre/post diff and UNPLANNED CONFIG DRIFT
    detection, byte-exact backup/restore.
  - semantic_template_diff / deep helpers.
  - Docker helpers: up/cleanup/wait, port-blocker listeners.
  - Result/check helpers shared by all test scripts.

API facts verified on 3x-ui v3.7.0 (see references/api-compatibility.md):
  - modern Xray API under /panel/api/xray/*, legacy /panel/xray/*
  - CURRENT template: POST <xray_base>/  -> {"obj": "<json string>"}
    -> {"xraySetting": <template>};  getDefaultJsonConfig returns the DEFAULT.
  - update: POST form field `xraySetting`; panel applies via Xray core API
    live (no restart needed).
  - inbounds: typed payloads accepted; tgId must be int; delete via
    POST /panel/api/inbounds/del/{id}.
"""

import copy
import json
import subprocess
import time
import random
import string
import os
import secrets

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

THREEXUI_VERSION_DEFAULT = "v3.11.0"
IMAGE = "ghcr.io/mhsanaei/3x-ui"
PANEL_USER = "admin"   # Docker-test-only credentials (fresh container)
PANEL_PASS = secrets.token_urlsafe(24)  # unique per isolated test run

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

TEST_TARGET = "http://1.1.1.1/"  # IP target: independent of host DNS env
TEST_TIMEOUT = 25


# ---------------------------------------------------------------------------
# Results helpers
# ---------------------------------------------------------------------------

class Results:
    def __init__(self):
        self.items = []

    def check(self, name, ok, detail=""):
        status = "PASS" if ok else "FAIL"
        self.items.append((name, status, detail))
        print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))
        return ok

    def warn(self, name, detail=""):
        self.items.append((name, "WARN", detail))
        print(f"  [WARN] {name}" + (f" — {detail}" if detail else ""))
        return True

    def summary(self):
        print("\n" + "=" * 60)
        failed = [i for i in self.items if i[1] == "FAIL"]
        for name, status, detail in self.items:
            print(f"  [{status:4}] {name}" + (f" — {detail}" if detail else ""))
        print("=" * 60)
        if failed:
            print(f"RESULT: FAIL ({len(failed)} failed of {len(self.items)})")
            return 1
        print(f"RESULT: PASS ({len(self.items)}/{len(self.items)})")
        return 0


def sh(cmd, check_rc=False):
    print(f"  $ {' '.join(cmd)}")
    r = subprocess.run(cmd, capture_output=True, text=True, shell=False)
    if check_rc and r.returncode != 0:
        raise RuntimeError(f"command failed: {' '.join(cmd)}\n{r.stdout}\n{r.stderr}")
    return r


def as_dict(value):
    """API may return nested settings as dict (typed, v3.x) or JSON string
    (legacy). Normalize to dict."""
    if isinstance(value, dict):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return {}


def rand_subid():
    return "".join(random.choices(string.ascii_letters + string.digits, k=16))


def canonical(tpl):
    return json.dumps(tpl, sort_keys=True, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Semantic diff of Xray templates
# ---------------------------------------------------------------------------

def _walk(prefix, a, b, out):
    """Collect JSON pointer paths where a and b differ (a=old, b=new)."""
    if type(a) is not type(b) and not (isinstance(a, (int, float)) and isinstance(b, (int, float))):
        out.append(prefix)
        return
    if isinstance(a, dict):
        for k in sorted(set(a) | set(b)):
            p = f"{prefix}/{k}"
            if k not in a:
                out.append(p)
            elif k not in b:
                out.append(p)
            else:
                _walk(p, a[k], b[k], out)
    elif isinstance(a, list):
        if len(a) != len(b):
            out.append(prefix)
            return
        for i, (x, y) in enumerate(zip(a, b)):
            _walk(f"{prefix}/{i}", x, y, out)
    else:
        if a != b:
            out.append(prefix)


def semantic_template_diff(old, new):
    """Return sorted list of JSON-pointer paths whose values differ."""
    out = []
    _walk("", old, new, out)
    return sorted(p for p in out if p)  # skip root equality marker


def _get_path(tpl, path):
    cur = tpl
    for part in path.lstrip("/").split("/"):
        if isinstance(cur, list):
            cur = cur[int(part)]
        else:
            cur = cur.get(part)
    return cur


def _set_path(tpl, path, value):
    parts = path.lstrip("/").split("/")
    cur = tpl
    for part in parts[:-1]:
        cur = cur[int(part)] if isinstance(cur, list) else cur.setdefault(part, {})
    last = parts[-1]
    if isinstance(cur, list):
        cur[int(last)] = value
    else:
        cur[last] = value


def subtemplate_at(tpl, paths):
    sub = {}
    for p in paths:
        _set_path(sub, p, _get_path(tpl, p))
    return sub


def _rule_key(rule):
    return canonical({k: rule.get(k) for k in sorted(rule)})


def _normalize_rule_list(rules):
    """Compare rule lists order-insensitively (the panel may reorder rules
    on save; e.g. it keeps the api rule first)."""
    return sorted((_rule_key(r) for r in rules))


def assert_changes_planned(before, after, plan):
    """Every semantic change between before and after must match `plan`.

    Returns (ok, unplanned_paths, detail). All actual changed paths must have
    the same value in `after` as in `plan` — otherwise the update touched
    things outside the change plan (UNPLANNED CONFIG DRIFT).

    Lists of dicts under routing.rules are compared as multisets (order
    ignored); everything else is compared positionally.
    """
    changed = semantic_template_diff(before, after)
    # 3x-ui may place its API rule first when persisting the template.
    # Compare the rule membership separately; all other paths stay strict.
    def normalize(tpl):
        result = copy.deepcopy(tpl)
        rules = result.get("routing", {}).get("rules")
        if isinstance(rules, list):
            result["routing"]["rules"] = _normalize_rule_list(rules)
        return result
    # A successful write must reach the intended state, not merely avoid
    # touching unrelated keys. Otherwise a no-op could incorrectly PASS.
    missing = semantic_template_diff(normalize(after), normalize(plan))
    if missing:
        return False, missing, f"planned state not reached: {missing}"

    def value_matches(path):
        a, b, p = _get_path(before, path), _get_path(after, path), _get_path(plan, path)
        if path == "/routing/rules" or path.startswith("/routing/rules/"):
            if isinstance(a, list) and isinstance(b, list) and isinstance(p, list):
                return _normalize_rule_list(b) == _normalize_rule_list(p)
        return b == p

    unplanned = [p for p in changed if not value_matches(p)]
    if not unplanned:
        return True, [], f"{len(changed)} planned path(s) applied"
    return False, unplanned, f"unplanned paths: {unplanned}"


# ---------------------------------------------------------------------------
# Panel client
# ---------------------------------------------------------------------------

class Panel:
    def __init__(self, base_url, name):
        self.base = base_url.rstrip("/")
        self.name = name
        self.s = requests.Session()
        self.csrf = None
        self.api_mode = None  # "modern" | "legacy"
        self.xray_base = None
        self.tls = False

    # -- auth --

    def login(self, username=PANEL_USER, password=PANEL_PASS):
        r = self.s.get(f"{self.base}/csrf-token", timeout=10)
        r.raise_for_status()
        self.csrf = r.json()["obj"]
        for extra in ({"X-CSRF-Token": self.csrf}, {}):
            r = self.s.post(
                f"{self.base}/login",
                json={"username": username, "password": password},
                headers=extra, timeout=10,
            )
            if r.status_code != 403:
                break
        data = r.json()
        if not data.get("success"):
            raise RuntimeError(f"{self.name}: login failed: {data}")

    def try_login(self, username, password):
        """Attempt login, return (ok, msg) without raising."""
        try:
            self.login(username, password)
            return True, "ok"
        except RuntimeError as e:
            return False, str(e)[:160]
        except requests.RequestException as e:
            return False, str(e)[:160]

    def auth_headers(self):
        h = {}
        if self.csrf:
            h["X-CSRF-Token"] = self.csrf
        return h

    # -- capability discovery --

    def discover_api(self):
        """Probe modern /panel/api/xray/* before legacy /panel/xray/*.

        An endpoint counts as present on HTTP 200 with success=true; 401/403
        also proves presence but auth must be fixed first (not expected here).
        """
        for mode, path in (
            ("modern", "/panel/api/xray/getDefaultJsonConfig"),
            ("legacy", "/panel/xray/getDefaultJsonConfig"),
        ):
            r = self.s.get(f"{self.base}{path}", headers=self.auth_headers(), timeout=10)
            if r.status_code == 200:
                try:
                    body = r.json()
                except ValueError:
                    continue
                if body.get("success"):
                    self.api_mode = mode
                    self.xray_base = path.rsplit("/", 1)[0]
                    print(f"  {self.name}: API mode = {mode}")
                    return mode
            else:
                print(f"  {self.name}: probe {path} -> HTTP {r.status_code}")
        raise RuntimeError(f"{self.name}: no working Xray API endpoint found")

    # -- Xray template --

    def get_saved_template(self):
        """Read the CURRENT saved Xray template via POST <xray_base>/.

        getDefaultJsonConfig returns the DEFAULT template — must not be used
        as the read source for read-modify-write cycles.
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

    def backup_template(self):
        """Semantic snapshot (dict) of the current saved template."""
        return self.get_saved_template()

    def restore_template(self, snapshot):
        """Restore template from snapshot. Returns True only if the re-read
        current template is byte-identical (canonical JSON) to the snapshot."""
        resp = self.update_template(snapshot)
        if not resp.get("success"):
            print(f"  {self.name}: restore update returned: {resp}")
            return False
        time.sleep(2)
        try:
            current = self.get_saved_template()
            return canonical(current) == canonical(snapshot)
        except Exception as e:
            print(f"  {self.name}: restore verification failed: {e}")
            return False

    def planned_update(self, plan, mutate, label=""):
        """Planned template change with UNPLANNED CONFIG DRIFT detection.

        plan:   the intended full template state after the change;
        mutate: callable applied to a deep copy of the current template to
                build the payload actually sent (normally produces `plan`).

        If the current template already matches the plan (idempotent re-run),
        nothing is sent and (None, None) is returned.
        """
        before = self.get_saved_template()
        if canonical(before) == canonical(plan):
            print(f"  {self.name}: '{label}' already applied — reuse")
            return None, None
        payload = mutate(copy.deepcopy(before))
        resp = self.update_template(payload)
        if not resp.get("success"):
            raise RuntimeError(f"{self.name}: template update failed: {resp}")
        time.sleep(2)
        after = self.get_saved_template()
        ok, unplanned, detail = assert_changes_planned(before, after, plan)
        if not ok:
            # UNPLANNED CONFIG DRIFT: roll back to the pre-update snapshot
            restored = self.restore_template(before)
            raise RuntimeError(
                f"{self.name}: UNPLANNED CONFIG DRIFT after '{label}': {detail} "
                f"(rollback {'verified' if restored else 'FAILED'})")
        print(f"  {self.name}: planned update '{label}' applied "
              f"({detail}, no drift)")
        return before, after

    # -- inbounds --

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

    def del_inbound(self, inbound_id):
        r = self.s.post(f"{self.base}/panel/api/inbounds/del/{inbound_id}",
                        headers=self.auth_headers(), timeout=10)
        try:
            return r.json()
        except ValueError:
            return {"success": False, "msg": f"HTTP {r.status_code}: {r.text[:200]}"}

    def inbound_state(self):
        """Semantic snapshot of all inbounds (for pre/post comparisons)."""
        state = []
        for inb in self.list_inbounds():
            state.append({
                "tag": inb.get("tag"),
                "port": inb.get("port"),
                "protocol": inb.get("protocol"),
                "settings": as_dict(inb.get("settings")),
            })
        state.sort(key=lambda x: (x["tag"], x["port"]))
        return state

    def find_inbound(self, tag):
        for inb in self.list_inbounds():
            if inb.get("tag") == tag or inb.get("remark") == tag:
                return inb
        return None

    def del_inbound_by_tag(self, tag):
        inb = self.find_inbound(tag)
        if not inb:
            return False, "not found"
        resp = self.del_inbound(inb["id"])
        return bool(resp.get("success")), resp.get("msg", "")[:80]


# ---------------------------------------------------------------------------
# Traffic helpers
# ---------------------------------------------------------------------------

def host_dns_is_fakeip():
    """Detect a fake-ip DNS environment (e.g. VPN client on the Docker host).

    198.18.0.0/15 is the fake-ip range used by sing-box/Hiddify etc. Docker
    Desktop forwards container DNS through the host, so ALL container DNS —
    including direct queries to public resolvers — can return fake IPs there.
    """
    import socket
    try:
        ip = socket.gethostbyname("ip-api.com")
        return ip.startswith("198.18."), ip
    except OSError:
        return False, "unresolved"


def traffic_through_cascade(port=RU_INBOUND_PORT, target=TEST_TARGET,
                            timeout=TEST_TIMEOUT):
    try:
        r = requests.get(
            target,
            proxies={"http": f"http://127.0.0.1:{port}"},
            timeout=timeout,
        )
        return r.status_code < 500, f"HTTP {r.status_code}, {len(r.content)} bytes"
    except requests.RequestException as e:
        return False, str(e)[:120]


def foreign_traffic_nonzero(foreign):
    inb = foreign.find_inbound(FOREIGN_TAG)
    if not inb:
        return False, "inbound disappeared"
    deadline = time.time() + 90
    total = inb.get("up", 0) + inb.get("down", 0)
    while time.time() < deadline and total == 0:
        time.sleep(5)
        # keep generating traffic so the stats collector sees the connection
        try:
            requests.get(TEST_TARGET,
                         proxies={"http": f"http://127.0.0.1:{RU_INBOUND_PORT}"},
                         timeout=15)
        except requests.RequestException:
            pass
        body = foreign.get_inbound(inb["id"])
        if body.get("success") and body.get("obj"):
            inb = body["obj"]
            total = inb.get("up", 0) + inb.get("down", 0)
    return total > 0, f"up+down={total}"


# ---------------------------------------------------------------------------
# Docker helpers
# ---------------------------------------------------------------------------

def docker_available():
    if not shutil_which("docker"):
        print("Docker CLI not found — cannot run integration test.")
        return False
    probe = subprocess.run(["docker", "ps"], capture_output=True, text=True)
    if probe.returncode != 0:
        print("Docker daemon not reachable — cannot run integration test.")
        print(probe.stderr.strip())
        return False
    return True


def shutil_which(name):
    import shutil
    return shutil.which(name)


def docker_cleanup(names=(RU_NAME, FOREIGN_NAME), network=NETWORK):
    # Only resources explicitly created by this test invocation are removed.
    for name in names:
        if name in _created_containers:
            sh(["docker", "rm", "-f", name], check_rc=True)
            _created_containers.remove(name)
    if network in _created_networks:
        sh(["docker", "network", "rm", network], check_rc=True)
        _created_networks.remove(network)


_created_containers = set()
_created_networks = set()


def docker_run_owned(args, name):
    sh(["docker", "run", "-d", "--name", name, *args], check_rc=True)
    _created_containers.add(name)


def provision_test_login(name):
    """Set ephemeral credentials without printing them or relying on image defaults."""
    result = subprocess.run(["docker", "exec", name, "/app/x-ui", "setting",
                             "-username", PANEL_USER, "-password", PANEL_PASS],
                            capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f"Cannot provision isolated test login in {name} (output redacted)")
    sh(["docker", "restart", name], check_rc=True)


def docker_up(version=None, extra_ports=None):
    version = version or os.getenv("THREEXUI_VERSION", THREEXUI_VERSION_DEFAULT)
    img = f"{IMAGE}:{version}"
    sh(["docker", "network", "create", NETWORK], check_rc=True)
    _created_networks.add(NETWORK)
    print(f"  pulling {img} ...")
    sh(["docker", "pull", img], check_rc=True)
    docker_run_owned(["--network", NETWORK,
        "--hostname", FOREIGN_NAME,
        "-p", f"127.0.0.1:{FOREIGN_PANEL_HOST_PORT}:2053",
        "-p", f"127.0.0.1:{FOREIGN_INBOUND_PORT}:{FOREIGN_INBOUND_PORT}", img], FOREIGN_NAME)
    docker_run_owned(["--network", NETWORK,
        "--hostname", RU_NAME,
        "-p", f"127.0.0.1:{RU_PANEL_HOST_PORT}:2053",
        "-p", f"127.0.0.1:{RU_INBOUND_PORT}:{RU_INBOUND_PORT}", img], RU_NAME)
    provision_test_login(FOREIGN_NAME)
    provision_test_login(RU_NAME)


def wait_panel(base_url, name, timeout=180):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = requests.get(base_url + "/", timeout=5, verify=False)
            if r.status_code < 500:
                print(f"  {name}: panel is up (HTTP {r.status_code})")
                return True
        except requests.RequestException:
            pass
        time.sleep(3)
    raise RuntimeError(f"{name}: panel did not come up in {timeout}s")


def occupy_port(container, port):
    """Start a dummy TCP listener inside a container (busybox nc)."""
    sh(["docker", "exec", "-d", container,
        "sh", "-c", f"nc -l -p {port} >/dev/null 2>&1"])
    time.sleep(1)
    r = sh(["docker", "exec", container, "sh", "-c", "netstat -ltn 2>/dev/null"])
    return f":{port}" in r.stdout or f" {port} " in r.stdout


def connect_panels():
    foreign = Panel(FOREIGN_URL, "foreign")
    ru = Panel(RU_URL, "ru")
    foreign.login()
    ru.login()
    foreign.discover_api()
    ru.discover_api()
    return foreign, ru


# ---------------------------------------------------------------------------
# Cascade configuration primitives (idempotent; shared by all tests)
# ---------------------------------------------------------------------------

def ensure_foreign_inbound(foreign, service_uuid, port=FOREIGN_INBOUND_PORT,
                           stream_settings=None, client_flow=""):
    existing = foreign.find_inbound(FOREIGN_TAG)
    if existing:
        if existing.get("port") != port or existing.get("protocol") != "vless":
            raise RuntimeError("foreign inbound tag conflict: unexpected port/protocol — STOP")
        settings = as_dict(existing["settings"])
        matches = [c for c in settings.get("clients", []) if c.get("email") == SERVICE_CLIENT_EMAIL]
        if len(matches) == 1 and matches[0].get("id") == service_uuid:
            print(f"  foreign: inbound '{FOREIGN_TAG}' + client exist — reuse")
            return False
        raise RuntimeError(
            f"conflict: inbound '{FOREIGN_TAG}' service client missing or credentials mismatch — "
            "STOP, do not overwrite automatically")
    stream = stream_settings or {
        "network": "tcp", "security": "none", "externalProxy": [],
        "tcpSettings": {"acceptProxyProtocol": False, "header": {"type": "none"}},
    }
    payload = {
        "up": 0, "down": 0, "total": 0, "remark": FOREIGN_TAG,
        "enable": True, "expiryTime": 0, "listen": "", "port": port,
        "protocol": "vless", "tag": FOREIGN_TAG,
        "settings": {
            "clients": [{
                "id": service_uuid, "flow": client_flow,
                "email": SERVICE_CLIENT_EMAIL,
                "limitIp": 0, "totalGB": 0, "expiryTime": 0, "enable": True,
                "tgId": 0, "subId": rand_subid(), "reset": 0,
            }],
            "decryption": "none", "fallbacks": [],
        },
        "streamSettings": stream,
        "sniffing": {"enabled": False, "destOverride": [], "metadataOnly": False},
        "allocate": {"strategy": "always", "refresh": 5, "concurrency": 3},
    }
    resp = foreign.add_inbound(payload)
    if not resp.get("success"):
        raise RuntimeError(f"foreign: add inbound failed: {resp}")
    print(f"  foreign: created inbound '{FOREIGN_TAG}' "
          f"(id={resp.get('obj', {}).get('id')})")
    return True


def ensure_foreign_egress(foreign):
    """Explicit DNS + freedom domainStrategy=UseIPv4 — deterministic egress
    even in fake-ip DNS environments (idempotent)."""
    tpl = foreign.get_saved_template()
    before = copy.deepcopy(tpl)
    changed = False
    if not tpl.get("dns") or not tpl["dns"].get("servers"):
        tpl["dns"] = {"servers": ["8.8.8.8", "1.1.1.1"], "queryStrategy": "UseIPv4"}
        changed = True
    for o in tpl.get("outbounds", []):
        if o.get("protocol") == "freedom":
            if as_dict(o.get("settings")).get("domainStrategy") != "UseIPv4":
                o["settings"] = dict(as_dict(o.get("settings")),
                                     domainStrategy="UseIPv4")
                changed = True
    if changed:
        foreign.planned_update(tpl, lambda _: copy.deepcopy(tpl), label="foreign DNS egress")
        print("  foreign: egress DNS/domainStrategy configured (live apply)")
        time.sleep(3)
        return True
    print("  foreign: egress already configured — reuse")
    return False


def ensure_ru_inbound(ru, port=RU_INBOUND_PORT, stream_settings=None,
                      client_flow=""):
    existing = ru.find_inbound(RU_TAG)
    if existing:
        if existing.get("port") != port or existing.get("protocol") != "http":
            raise RuntimeError("RU inbound tag conflict: unexpected port/protocol — STOP")
        print(f"  RU: inbound '{RU_TAG}' exists — reuse")
        return False
    stream = stream_settings or {
        "network": "tcp", "security": "none", "externalProxy": [],
    }
    payload = {
        "up": 0, "down": 0, "total": 0, "remark": RU_TAG,
        "enable": True, "expiryTime": 0, "listen": "", "port": port,
        # http inbound = plain HTTP proxy (testable with requests);
        # the VLESS/Reality handshake is validated on the RU->foreign leg
        # and by tests/reality_test.py for the client leg.
        "protocol": "http", "tag": RU_TAG,
        "settings": {"allowTransparent": False, "userLevel": 0},
        "streamSettings": stream,
        "sniffing": {"enabled": False, "destOverride": [], "metadataOnly": False},
        "allocate": {"strategy": "always", "refresh": 5, "concurrency": 3},
    }
    if client_flow:
        # only meaningful for VLESS inbounds; http inbound ignores flow
        payload["settings"]["userLevel"] = 0
    resp = ru.add_inbound(payload)
    if not resp.get("success"):
        raise RuntimeError(f"RU: add inbound failed: {resp}")
    print(f"  RU: created inbound '{RU_TAG}' (id={resp.get('obj', {}).get('id')})")
    return True


def ensure_ru_outbound_and_routing(ru, service_uuid,
                                   foreign_address=FOREIGN_NAME,
                                   port=FOREIGN_INBOUND_PORT,
                                   stream_settings=None, client_flow=""):
    """Planned template change: add to-foreign outbound + routing rule, with
    UNPLANNED CONFIG DRIFT detection. Returns True only when something changed."""
    stream = stream_settings or {"network": "tcp", "security": "none"}

    def mutate(config):
        outbounds = config.setdefault("outbounds", [])
        if not any(o.get("tag") == RU_OUTBOUND_TAG for o in outbounds):
            outbounds.append({
                "tag": RU_OUTBOUND_TAG, "protocol": "vless",
                "settings": {"vnext": [{
                    "address": foreign_address, "port": port,
                    "users": [{"id": service_uuid, "encryption": "none",
                               "flow": client_flow}],
                }]},
                "streamSettings": stream,
            })
        rules = config.setdefault("routing", {}).setdefault("rules", [])
        if not any(RU_TAG in (r.get("inboundTag") or [])
                   and r.get("outboundTag") == RU_OUTBOUND_TAG for r in rules):
            # prepend so no earlier catch-all rule shadows the cascade route
            rules.insert(0, {"type": "field", "inboundTag": [RU_TAG],
                             "outboundTag": RU_OUTBOUND_TAG})
        return config

    plan = mutate(copy.deepcopy(ru.get_saved_template()))
    before, after = ru.planned_update(plan, mutate, label="cascade outbound+routing")
    return after is not None


def count_outbound_tag(config, tag):
    return sum(1 for o in config.get("outbounds", []) if o.get("tag") == tag)


def count_routing_rule(config):
    return sum(
        1 for r in config.get("routing", {}).get("rules", [])
        if RU_TAG in (r.get("inboundTag") or [])
        and r.get("outboundTag") == RU_OUTBOUND_TAG
    )
