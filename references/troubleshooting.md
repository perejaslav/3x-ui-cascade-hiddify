# Troubleshooting

Structured diagnostics. Work layer by layer: fix the first failing layer only, do not change multiple layers blindly. Redact secrets from any log output before showing it.

## Symptom: Hiddify connects but internet unavailable

```text
CHECK:
1. RU inbound active and listening (ss -lntup, API list)
2. RU routing rule hiddify-entry → to-foreign present and not shadowed
3. RU outbound to-foreign points to the foreign host/port with correct credentials
4. Foreign inbound from-ru active
5. Foreign service-client credentials match the RU outbound exactly
   (UUID/password, flow, Reality params)
6. Foreign freedom/direct outbound exists
7. DNS — client may resolve but egress/DNS is broken (see DNS test below)
```

Layer order: client → RU inbound → RU routing → RU outbound → foreign inbound → foreign outbound → DNS.

## Symptom: Hiddify connects and IP is RU

```text
LIKELY CAUSE:
traffic not routed to to-foreign outbound
```

Check:

- routing rule exists and `inboundTag` matches the actual Hiddify inbound tag exactly;
- rule order (an earlier rule with a broader match can shadow it);
- no earlier rule sends traffic to a RU direct/freedom outbound;
- Hiddify is using the intended profile (not an old direct-RU one).

Fix the routing rule; do not rebuild the whole setup.

## Symptom: foreign inbound sees no connection

```text
CHECK:
1. firewall (OS) — ufw/firewalld/iptables/nft
2. provider firewall / security group  → if unverifiable: "Provider firewall status: UNKNOWN",
   ask the user to check the cloud console; do NOT claim reachability from `ufw allow` alone
3. wrong port / wrong protocol (TCP vs UDP)
4. wrong Reality parameters (SNI, shortId, public key)
5. Xray actually listening on 0.0.0.0 (not 127.0.0.1)
```

## Symptom: cascade established, requests blackholed / domains resolve to 198.18.x.x

Signature (Xray debug log on the exit server):

```text
proxy/freedom: dialing to tcp:198.18.3.195:80
proxy/freedom: blocked target: tcp:198.18.3.195:80, blackholing connection
```

Cause: a fake-ip VPN/proxy client on the path (Hiddify/sing-box fake-ip mode, VPN filter drivers on the Docker host) answers DNS with fake IPs from `198.18.0.0/15` — a reserved range. Xray treats reserved-range targets as blocked and blackholes them. Docker Desktop makes this worse by forwarding container DNS through the host, so even direct queries to `8.8.8.8` from inside containers return fake IPs.

Checks:

```bash
# inside the affected server/container
nslookup ip-api.com        # 198.18.x.x = fake-ip poisoning
nslookup ip-api.com 8.8.8.8  # if still 198.18.x.x, DNS is intercepted upstream
```

Fixes:

- on an exit server in a fake-ip environment, configure explicit Xray DNS (`"dns": {"servers": ["8.8.8.8", "1.1.1.1"], "queryStrategy": "UseIPv4"}`) and set the freedom outbound to `"settings": {"domainStrategy": "UseIPv4"}` so Xray resolves real IPs itself;
- in Docker test environments, prefer IP-based test targets (e.g. `http://1.1.1.1/`) — see `tests/cascade_docker_test.py`;
- on a real VPS this usually means a local VPN/fake-ip client is running on the machine — find and account for it before diagnosing further.

Related: Xray-core (v26.x era) blackholes reserved-range targets by design — this is not a bug.

## Symptom: port closed locally but open in config

```bash
ss -lntup | grep <PORT>
ufw status verbose || true
firewall-cmd --list-all || true
iptables -S || true
nft list ruleset || true
nc -vz <HOST> <PORT>          # from the other server
```

Likely causes: inbound disabled, Xray not listening, bound to `127.0.0.1`, local firewall, provider firewall, wrong protocol.

## Port collision

Before creating any inbound:

```bash
ss -lntup
```

Check: existing services, Xray listeners, 3x-ui panel, metrics listeners, SSH, web servers, Docker proxies.

If the port is occupied: **do not kill the process.** Choose another port or stop and report the conflict.

### Metrics listener conflict (dynamic detection)

Do not assume metrics listens on `127.0.0.1:11111`. Detect dynamically:

- parse the Xray template for `metrics` / `api` / `stats` blocks and their listen addresses/ports;
- cross-check with `ss -lntup` for loopback listeners owned by Xray.

If the desired inbound port collides with the metrics listener, change `metrics.listen` in the template to a free port (e.g. `127.0.0.1:62790`) before creating the inbound. Xray error signature:

```text
listen tcp 127.0.0.1:11111: bind: address already in use
```

## Symptom: connection established, but sites partially load / large downloads hang / QUIC fails

MTU/MSS diagnostics:

```bash
# on the servers
ip link show eth0 | grep mtu
# path PMTU probe (do not fragment)
ping -M do -s 1472 <HOST>       # decrease 1472 until it passes; +28 = effective MTU
tracepath <HOST>                # shows PMTU along the path
```

- Symptoms match: TCP connects, small pages load, large transfers stall, QUIC/UDP flaky.
- Do **not** change MTU automatically without a proven cause. If PMTU blackhole is proven, options: lower interface MTU on the affected hop, or clamp MSS on TCP — and report what was changed and why.

## DNS checks

Determine the resolver actually used and compare its egress location with expectations (see `acceptance-tests.md` → DNS test):

```powershell
# Windows client
nslookup whoami.akamai.net
ipconfig /displaydns   # diagnostic only
```

```bash
# servers
getent hosts <DOMAIN>
dig <DOMAIN> || true
nslookup <DOMAIN> || true
```

TLS/SNI check:

```bash
openssl s_client -connect <DOMAIN>:443 -servername <DOMAIN>
```

Likely causes: wrong A/AAAA records, wrong SNI, certificate mismatch, provider firewall, IPv6 pointing elsewhere, client using ISP DNS while expecting foreign DNS.

## Symptom: 3X-UI not working / Xray fails to start

```bash
systemctl status x-ui --no-pager || true
systemctl status 3x-ui --no-pager || true
journalctl -u x-ui -n 200 --no-pager || true
journalctl -u 3x-ui -n 200 --no-pager || true
ps aux | grep -Ei 'x-ui|3x-ui|xray' | grep -v grep || true
ss -tulpen
```

Likely causes: port conflict (incl. metrics listener), invalid Xray config (malformed `xraySetting`), wrong Reality/TLS fields, occupied panel port, disk full, permissions.

Recovery order: back up config first → roll back the last inbound/outbound/routing change → validate → only then try other fixes. If the API returned `unexpected end of JSON input` on a template update, see `api-compatibility.md` (form field `xraySetting`).

## Symptom: API endpoint returns 404/405 or schema mismatch

```text
Do not assume installation failure.
Re-run API discovery (see api-compatibility.md):
- probe modern /panel/api/xray/* and legacy /panel/xray/*
- re-check auth mode (token vs CSRF session)
- re-check payload serialization (typed JSON vs legacy string)
```

## Symptom: Hiddify does not connect

Check client-side parameters:

```text
RU address/domain, RU port, UUID/password, protocol, transport,
SNI, ALPN, fingerprint, Reality public key, short ID, flow,
TLS mode, client/server clock skew (Reality is time-sensitive)
```

Server checks:

```bash
ss -tulpen | grep <RU_CLIENT_PORT>
journalctl -u x-ui -n 100 --no-pager || true
journalctl -u 3x-ui -n 100 --no-pager || true
```

Common mistake: the profile points to the foreign server directly instead of the Russian entry server. Validate the profile (see SKILL.md → Hiddify Profile Validation).

## Symptom: no SSH access

Symptoms: `Permission denied`, `Connection timed out`, `No route to host`, `Host key verification failed`.

```bash
ssh -vvv -p <SSH_PORT> <USER>@<HOST>
```

Likely causes: wrong host/port/user, missing key, root login disabled, firewall/provider firewall, password auth disabled, changed host key. Do not brute-force passwords; ask for corrected access or a temporary public key setup.

## Wrong inbound/outbound pairing (layer check)

```text
1. Does Hiddify reach RU inbound?
2. Does RU inbound have the expected tag?
3. Does routing map that tag to the foreign outbound?
4. Does RU outbound connect to the foreign inbound?
5. Does foreign inbound accept RU?
6. Does foreign outbound use direct/freedom?
7. Does client-observed IP equal the foreign IP?
```

Fix the first failing layer only.

## Log collection (with redaction)

Collect during diagnosis:

```bash
journalctl -u x-ui -n 200 --no-pager || true
journalctl -u 3x-ui -n 200 --no-pager || true
docker logs --tail 200 <container> 2>&1 || true
```

Before showing: strip UUIDs, passwords, Reality private keys, tokens, cookies, share URIs. Prefer error levels and short error excerpts over full dumps.

## Restart policy during troubleshooting

Restart Xray/3x-ui only when: live apply is unavailable/failed, the runtime state does not match the config, or the specific change requires it. State the reason, confirm the backup exists, and note the impact on active connections before restarting. Never restart "just in case" — repeated restarts hide the real failure layer.
