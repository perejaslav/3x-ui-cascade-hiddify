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

Determine which resolver is actually used:

```powershell
# Windows client
nslookup whoami.akamai.net          # resolver identity
Resolve-DnsName whoami.cloudflare.esk
```

Or use a test domain that echoes the resolver's egress location. Then check for leaks: when foreign egress is expected, DNS must not resolve directly through RU/local ISP resolvers.

Output:

```text
DNS STATUS: PASS / WARNING / FAIL
```

- PASS — DNS egress follows the foreign path (or local-by-design and user approved).
- WARNING — resolver location unknown or mixed.
- FAIL — DNS goes through RU/local ISP while traffic goes foreign (leak).

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

The Docker/CI acceptance suite must exercise failure modes and verify the skill degrades safely with clear diagnostics and without destroying the existing setup:

```text
occupied foreign port
occupied RU port
invalid foreign UUID
broken routing tag (routing → nonexistent outbound)
foreign unreachable (foreign container stopped)
wrong API endpoint (404 path)
CSRF required (no token/cookie sent)
wrong credentials
existing conflicting inbound (same port/tag created beforehand)
Xray config invalid (malformed xraySetting)
```

Expected behavior for each: readable error, no partial objects left behind, existing objects untouched, no auto-deletion.

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
