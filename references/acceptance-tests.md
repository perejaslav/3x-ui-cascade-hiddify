# Acceptance Tests

Mandatory verification for any cascade configuration run. The final report must contain the test table. A failed critical test forbids reporting success.

## Test table template

```text
TEST                                         RESULT

SSH RU                                       PASS/FAIL
SSH FOREIGN                                  PASS/FAIL
3x-ui RU                                     PASS/FAIL
3x-ui FOREIGN                                PASS/FAIL
API RU                                       PASS/FAIL
API FOREIGN                                  PASS/FAIL
Foreign inbound                              PASS/FAIL
Foreign service client                       PASS/FAIL
RU outbound → foreign                        PASS/FAIL
RU routing                                   PASS/FAIL
Hiddify profile target == RU                 PASS/FAIL
TCP end-to-end                               PASS/FAIL
External IPv4 == foreign                     PASS/FAIL
DNS                                          PASS/WARNING/FAIL
UDP                                          PASS/FAIL/N/A
IPv6                                         PASS/WARNING/N/A
Rollback capability                          PASS/FAIL
```

## Critical tests

These MUST be PASS before the configuration can be reported as successful:

```text
Foreign inbound operational
RU outbound reaches foreign
RU routing sends Hiddify traffic to foreign
Hiddify primary target == RU
External IPv4 == expected foreign server/network
Xray healthy on both servers
```

If any critical test fails: diagnose → fix, or roll back. Never finish with FAILED critical tests and a success status.

## Test procedures

### SSH + 3x-ui + API

```bash
ssh -p <PORT> <USER>@<HOST> 'whoami && hostname'
curl -s -o /dev/null -w '%{http_code}\n' "$PANEL_URL/"
# authenticated API probe: inbounds list must return 200 with valid JSON
```

### Foreign inbound operational

```bash
# on foreign
ss -lntup | grep <FOREIGN_PORT>
systemctl status x-ui --no-pager || docker ps
# from RU
nc -vz <FOREIGN_HOST> <FOREIGN_PORT>
```

### RU outbound reaches foreign

After a test request through the cascade: Xray log on RU shows an established connection to `<FOREIGN_HOST>:<FOREIGN_PORT>`, or the foreign server shows non-zero traffic counters on the `from-ru` inbound, or `ss -tnp | grep <FOREIGN_PORT>` on foreign shows connections from the RU server IP.

### RU routing

Routing rule `inboundTag: [hiddify-entry]` → `outboundTag: to-foreign` exists, is not shadowed by an earlier broader rule, and the Hiddify inbound tag matches the rule exactly.

### TCP end-to-end

From the Hiddify client (or the Docker test harness): fetch `http://ifconfig.me` through the proxy. Decisive test is the IP observed at the client; `curl` from the RU server itself only proves the RU server's own egress, not the client path.

### External IPv4

Client-observed IP == foreign server IP/network. If the client machine is available to the agent, run the check there; otherwise ask the user to open `https://ifconfig.me` / `https://ipinfo.io/ip` and report the result.

### DNS test (mandatory)

Goal: determine which resolver is actually used and detect DNS leaking directly through RU/local ISP when foreign egress is expected. Never conclude from a single `nslookup` — cross-check several probes.

**Step 1 — which resolver answers?** Use a resolver-echo service: the DNS name `whoami.akamai.net` resolves to the IP of the resolver that queried it. Query it through the tunnel and compare with direct:

```bash
# through the tunnel (client with proxy DNS, or on the exit server)
nslookup whoami.akamai.net
dig +short whoami.akamai.net

# baseline: the resolver seen without the tunnel (for comparison)
nslookup whoami.akamai.net 1.1.1.1
```

**Step 2 — is the resolved egress foreign?** Use an IP-geolocation echo domain (returns the location of the querying resolver):

```bash
dig +short o-o.myaddr.l.google.com @ns1.google.com   # TXT answer: resolver IP seen by Google
nslookup -type=TXT o-o.myaddr.l.google.com ns1.google.com
```

Compare the returned resolver IP/location with the foreign server location.

**Step 3 — behavioral probe (Windows client):** open a DNS-leak test page through the connected profile and check that no RU/local ISP resolvers appear:

```text
https://dnsleaktest.com        → Extended test
https://www.dnsleak.com
```

**Step 4 — consistency check:** repeat step 1 twice (stability) and confirm the resolver set does not include the user's ISP when the tunnel is up.

Decision matrix:

```text
PASS     resolver echoes foreign location (or user-approved local resolver);
         no ISP resolvers in the leak test; repeated probes stable
WARNING  resolver location unknown, mixed (some foreign, some local),
         or fake-ip client-side resolver detected (198.18.0.0/15 answers)
         — results unusable, redo the probe with real resolvers
FAIL     RU/local ISP resolver observed while foreign egress is expected (leak)
```

Note (fake-ip environments): if the client machine runs a VPN/fake-ip client, all DNS — even direct queries to public resolvers — may answer with `198.18.x.x`. Detect it first (any domain resolving into `198.18.0.0/15`), mark the probe WARNING, and re-test from a clean resolver context (e.g. query `whoami.akamai.net` directly against `1.1.1.1` from the RU server through the tunnel).

Output:

```text
DNS STATUS: PASS / WARNING / FAIL
```

### IPv6 test (mandatory)

A foreign IPv4 does not prove IPv6 routing. Test both:

```bash
curl -4 https://ifconfig.me
curl -6 https://ifconfig.me
```

States:

```text
IPv4 foreign / IPv6 disabled (or unreachable)  → PASS
IPv4 foreign / IPv6 foreign                    → PASS
IPv4 foreign / IPv6 RU/local                   → FAIL or WARNING (per user requirements)
```

### UDP test

Run only if the chosen protocol or user traffic implies UDP (QUIC/H3, Hysteria2, gaming, VoIP). Do not open UDP firewall ports just to test.

```text
UDP: PASS  — UDP datagrams traverse the cascade
UDP: FAIL  — UDP blocked or broken
UDP: N/A   — protocol is TCP-only or user does not need UDP
```

### Rollback capability

PASS requires: backup exists on both servers, rollback commands were derived from the actual state during planning, and (in Docker tests) a rollback rehearsal restored the prior state. Without a verified rollback path the run must not modify configuration.

## Negative tests (Docker/CI)

The following failure modes are **automated** in `tests/negative_tests.py` (runs in CI, blocking, against the pinned official image). Each scenario asserts: clear failure, no destructive overwrite, no unwanted duplicates, no unrelated config changes, and rollback/recovery where applicable:

```text
occupied foreign port          — pre-check detects listener; stray inbound cleaned up; Xray healthy
occupied RU port               — pre-check detects listener
invalid foreign UUID           — update rejected or breakage detected; rollback recovers
foreign unreachable            — bogus hostname; clear failure; rollback recovers
wrong API endpoint             — unknown paths return 404; config untouched
CSRF/auth failure              — unauthenticated writes rejected; nothing applied
wrong credentials              — login rejected; no state change
conflicting existing inbound   — conflict STOP (unit) + live duplicate-port rejection; cleanup works
invalid xraySetting            — malformed JSON / wrong shape; config not corrupted; recovery works
broken routing tag             — traffic fails; rollback restores; traffic works
```

Docker-scope limits still apply (see below): provider firewall, Reality in a real network, Hiddify on Windows, real DNS leaks and UDP over the production path are NOT covered by these automated tests and must be verified manually on real VPS during acceptance.

## Re-run / idempotency test (mandatory)

Run the skill a second time on an already correctly configured system. Expected:

```text
no duplicate inbound
no duplicate client
no duplicate outbound
no duplicate routing rule
no unnecessary firewall entries
no unnecessary restart
```

Final result must be:

```text
NO CHANGES REQUIRED
```

or a minimal update. Any duplicate creation is a test failure.

## Upgrade compatibility test

Scenario:

```text
3x-ui older supported version → skill works
→ upgrade 3x-ui (in-place)
→ skill rerun
```

Expected: skill re-runs discovery, detects new capabilities (e.g. modern `/panel/api/xray/*`), switches endpoints without touching the working cascade objects, ends with `NO CHANGES REQUIRED`.

## Docker test matrix

Two modes:

### Pinned stable (default, reproducible)

```bash
THREEXUI_VERSION=v3.7.0
docker pull ghcr.io/mhsanaei/3x-ui:$THREEXUI_VERSION
```

### Latest compatibility (separate, non-blocking)

```bash
docker pull ghcr.io/mhsanaei/3x-ui:latest
```

Used to catch future breaking changes early. Never use `latest` as the only CI target.

## Docker integration test scope

The CI harness (`tests/cascade_docker_test.py`) automatically:

```text
create Docker network
start foreign 3x-ui (pinned official image)
start RU 3x-ui (pinned official image)
configure foreign inbound (from-ru) + service client
configure RU outbound (to-foreign) + routing rule
send test traffic through the RU proxy
verify exit path (foreign inbound traffic counters > 0)
re-run the configuration phase
verify no duplicates are created
```

## Docker tests are NOT production validation

Docker proves:

- Xray config validity;
- protocol handshake;
- routing;
- service-client architecture;
- TCP flow.

Docker does NOT prove:

- provider firewall / cloud security groups;
- ISP routing;
- MTU;
- public IPv4 / IPv6;
- Reality behavior in a real network;
- SNI/CDN behavior;
- Hiddify on Windows;
- real DNS leaks;
- UDP through the production path.

A passing Docker test never replaces the real VPS acceptance table.

## Hiddify smoke test checklist (for the user)

When the agent cannot control the client machine, give the user this minimum:

```text
1. Временно отключить другие VPN/proxy.
2. Открыть Hiddify и импортировать профиль (или добавить вручную).
3. Убедиться, что адрес профиля — российский сервер.
4. Подключиться.
5. Открыть https://ifconfig.me и https://ipinfo.io/ip —
   ожидается IP зарубежного сервера.
6. Открыть несколько обычных сайтов — убедиться, что страницы грузятся целиком.
7. (опционально) Проверить IPv6: https://test-ipv6.com
8. (опционально) Сообщить, нужен ли UDP (игры, звонки) — проверить отдельно.
```
