---
name: 3x-ui-cascade-hiddify
description: Use when configuring two 3X-UI/Xray servers as a cascade where Hiddify on Windows connects to a Russian entry server, which forwards traffic to a foreign exit server. Version-aware, capability-aware, idempotent, transactional with backup and rollback.
version: 2.1.0
author: Hermes Agent
license: MIT
platforms: [linux, windows]
metadata:
  hermes:
    tags: [3x-ui, xray, hiddify, cascade, vpn, proxy, devops]
    related_skills: [remote-server-management, vps-administration, hermes-network-diagnostics]
---

# 3X-UI Cascade for Hiddify

## Overview

Use this skill to configure or diagnose a two-server 3X-UI/Xray cascade. Keep this file class-level and compact; session-specific implementation notes belong in `references/`.

Support files:

- `references/cascade-implementation-notes.md` — implementation notes, client-role split, protocol cautions, Docker simulation.
- `references/api-compatibility.md` — 3x-ui API endpoint matrix, capability detection, discovery procedure, curl examples.
- `references/acceptance-tests.md` — full acceptance test table, critical tests, negative tests, DNS/IPv6/UDP procedures.
- `references/security-and-rollback.md` — secret handling, backup procedure (SQLite WAL-safe, current-template snapshot), UNPLANNED CONFIG DRIFT protection, detailed rollback commands and rehearsal.
- `references/troubleshooting.md` — structured symptom → check → fix tables, MTU/MSS diagnostics, log collection.

Test suites (Docker, official pinned image): `tests/cascade_lib.py` (shared library: semantic diff, planned updates, byte-exact restore), `tests/cascade_docker_test.py`, `tests/rollback_rehearsal.py`, `tests/negative_tests.py`, `tests/reality_test.py`.

```text
Windows 11 + Hiddify → Russian 3X-UI server → foreign 3X-UI server → internet
```

The Russian server is the user-facing entry point. The foreign server is the final internet exit point. The Hiddify profile must point to the Russian server only; routing on the Russian server forwards client traffic to the foreign server via Xray outbound/routing.

Do not treat this as two independent proxy servers. The required shape is:

```text
RU inbound for Hiddify → RU outbound to foreign server → foreign inbound → foreign direct/freedom outbound
```

## Core Operating Model

The skill must never assume: "3x-ui always works as described in SKILL.md". Every run follows:

```text
discover → detect capabilities → backup → plan → apply → validate → commit success
```

On failure:

```text
failure → diagnose → rollback → verify restored state
```

Key principles:

1. **Runtime API > documented assumptions.** Never treat an endpoint or payload structure as guaranteed just because it is written in SKILL.md. If the installed 3x-ui version differs from the last tested one, probe the actual API first.
2. **Capability detection before version guessing.** Choose endpoints by probing what actually responds, not by assuming from the version number alone. A version is only a hint.
3. **Idempotency.** Every object is looked up before creation. A re-run on a correctly configured system must end with `NO CHANGES REQUIRED` or a minimal update, never duplicates.
4. **Transactional changes.** Backup before change, validate after change, roll back if critical tests fail.
5. **Fail-safe.** If the agent cannot confidently determine API schema, existing routing, or a rollback path, it must not make destructive changes. Read-only discovery, diagnostics, and a report are allowed.

## Capability Matrix

Internal decision table. Each capability is determined by probing, not assumed:

```text
Capability                         Preferred behavior

Modern /panel/api/* available      Use modern API
Typed payload supported            Use typed JSON (nested objects)
Live apply available               Apply without full restart
API token configured               Prefer token auth (no CSRF flow)
Legacy only                        Use legacy compatibility path
Unknown API                        Stop before destructive action
```

See `references/api-compatibility.md` for the detection procedure and endpoint matrix.

Discovery summary output (no secret values):

```text
3x-ui version: 3.x.x
API mode: modern
Authentication: API token
Xray API: available
Inbound API: available
Live apply: supported
Legacy fallback: not required
```

## Client Roles and Routing Identity

The cascade has two different client identities, and the skill must keep them separate.

### Foreign service client

This client is created on the foreign server and is used only by the Russian server.

Rules:

- it belongs to the foreign inbound for `RU → foreign`;
- its UUID/password/Reality/TLS parameters are consumed by the RU outbound;
- it is not the user-facing Hiddify profile;
- do not export it to the user as the main import link;
- in reports, describe it as a service client for the RU → foreign hop.

### RU Hiddify client

This client is created on the Russian server and is the only profile the user imports into Hiddify on Windows 11.

Rules:

- it belongs to the Hiddify-facing inbound on the RU server;
- its host/address must be the Russian server only;
- after connection, the external IP must resolve to the foreign server;
- if the generated Hiddify link points to the foreign server as the primary address, stop and report a configuration error;
- never present the foreign service client as the main Hiddify profile unless the user explicitly asks for that separate service object.

Control rule:

```text
The Hiddify import link must point to the Russian entry server only.
The foreign server client credentials are service credentials for RU outbound only.
```

If the user explicitly asks for a direct foreign-only profile, treat it as a separate bypass profile and keep it clearly outside the cascade's main Hiddify import path.

## Standard Object Names

Use predictable identifiers for objects this skill creates. If existing configuration uses different names, do not rename it without need — adopt the existing names and record them in the plan.

```text
Foreign inbound tag:        from-ru
Foreign service client:     ru-to-foreign-client
RU Hiddify inbound tag:     hiddify-entry
RU Hiddify client:          windows-hiddify-client
RU foreign outbound tag:    to-foreign
Routing rule:               hiddify-entry → to-foreign
```

## When to Use

Use when the user asks to:

- set up a two-hop/cascade proxy with 3X-UI;
- make Russian server accept Hiddify traffic and foreign server be the exit IP;
- configure 3X-UI panel-to-panel / outbound tunneling;
- create a Hiddify-compatible client profile for Windows 11;
- verify that traffic exits through the foreign server;
- diagnose a broken RU → foreign 3X-UI cascade.

Do not use for:

- a single-server proxy/VPN setup;
- Tor/I2P/anonymity network setup;
- requests involving illegal activity;
- promising guaranteed anonymity, censorship bypass, or unblockability.

## Confirmation Workflow and approved_scope

Do not interrogate the user step by step. Ask for the access details once, then ask for a single approval covering the whole scope:

> Правильно ли я понял, что нужно настроить два сервера с 3X-UI в каскадную схему, где российский сервер принимает трафик от Hiddify на Windows 11, а зарубежный сервер является конечной точкой выхода в интернет? Разрешаете по ходу работы устанавливать недостающее ПО, открывать необходимые порты, изменять конфигурацию 3X-UI/Xray и перезапускать сервисы?

If the user approves this scope (`approved_scope`), proceed through all routine steps without re-asking: discovery, backup, creating/updating cascade objects, opening required firewall ports, live apply, service restart, validation.

Additional explicit confirmation is required ONLY for:

- destructive deletion of existing objects;
- deleting unknown existing objects;
- changing SSH server configuration;
- disabling the firewall;
- replacing an existing working inbound;
- server reboot;
- actions that may cut remote access;
- rollback that would discard newly applied working changes.

## Data to Request Before Starting

Ask for both servers separately. Keep it to one message.

### Russian entry server

- IP address or domain.
- SSH port, username, auth method (key, password, other).
- Whether the user has root or sudo.
- Whether 3X-UI is already installed; if installed: panel URL/port and whether panel access works.
- Which port may be opened for the Hiddify client.
- Firewall type: ufw, firewalld, iptables/nftables, provider firewall; provider firewall/security group state.
- Whether a domain is available; TLS/Reality/certificate requirements.
- Existing inbounds/outbounds that must not be touched.

### Foreign exit server

Same fields as RU, plus country/location and which port may be opened for the RU → foreign connection.

### Hiddify/client choices

- Desired protocol: VLESS, VLESS+Reality, Trojan, Shadowsocks, VMess, Hysteria2, or other. If not chosen, propose the default (see Protocol Selection) and get one confirmation.
- Import link/subscription vs manual profile.
- Domain/TLS vs IP-only.
- Number of client profiles needed.

## SSH Credential Safety Rules

- Default assumption for this user: SSH access is performed with an SSH key already configured on the current Windows computer.
- Ask for host, SSH port, username, and which local SSH key/profile should be used if multiple keys exist.
- Do not ask for passwords, private keys, tokens, UUIDs, subscription links, or client configs in open text if a safer method is available.
- Prefer the already configured local SSH key. If that is not available, prefer a temporary SSH public key, temporary sudo user, or secure out-of-band transfer.
- Never write secrets to reports, logs, final answers, or persistent memory.
- Do not put secrets directly into shell commands where they may enter history.
- If a private key must be stored temporarily, use a temporary file, `chmod 600`, and delete it after work.
- Never print private keys, passwords, tokens, subscription links, or full client configs unless explicitly necessary and approved.
- Do not disable firewall globally. Open only required ports.
- Do not remove existing services or 3X-UI objects without explicit permission.
- Do not overwrite full Xray configs without backup.

## Transactional Workflow

### Phase 1 — Discovery (read-only, both servers)

Collect and record:

```text
OS, architecture, kernel          cat /etc/os-release; uname -m; uname -a
3x-ui version                     x-ui version / 3x-ui version / /panel/API endpoint
Xray version                      xray version
running services                  systemctl status x-ui / 3x-ui; ps aux
listening ports                   ss -tulpen
firewall (OS + provider)          ufw/firewalld/iptables/nft + provider state marked UNKNOWN if unverifiable
install mode                      systemd vs Docker
panel availability                HTTP probe of the panel URL
API capabilities                  see 3X-UI API Discovery below
current inbounds/outbounds/routing
```

Provider firewall is a separate layer from the OS firewall. If the provider firewall/security group cannot be verified, record `Provider firewall status: UNKNOWN` and do not claim the port is reachable just because `ufw allow` succeeded.

### 3X-UI API Discovery

Before any write, run API discovery on each server. The procedure and endpoint matrix live in `references/api-compatibility.md`; the mandatory checks:

1. Determine the 3x-ui version (CLI or panel).
2. Probe the modern Xray API: `GET /panel/api/xray/getDefaultJsonConfig`.
3. If modern responds — use it; probe legacy `/panel/xray/getDefaultJsonConfig` only as fallback.
4. Probe inbound API: `GET /panel/api/inbounds/list`.
5. Determine authentication: API token if configured, else CSRF session flow.
6. Determine live apply support (does the Xray config update apply without a panel restart?).
7. Fix the result as the discovery summary (see Capability Matrix) and use it for all subsequent calls.

Read-modify-write rule for the Xray template: the CURRENT saved template is read via `POST /panel/api/xray/` (getXraySetting). `GET getDefaultJsonConfig` returns the DEFAULT template — using it as the read source wipes existing outbounds/routing on the next update (verified on v3.7.0; details in `references/api-compatibility.md`).

If a known endpoint later returns 404/405 or a schema mismatch, do not immediately assume installation failure — re-run API discovery.

Never choose a legacy endpoint merely because the version "looks old": probe first.

### Phase 2 — Backup

Before modifying 3X-UI, Xray, routing, or firewall state, create a dated backup directory on each server. Detailed procedure (including SQLite WAL-safe copy) is in `references/security-and-rollback.md`.

Minimum backup scope:

- OS and host identity, service state, port listeners;
- firewall state;
- 3x-ui database (WAL-safe: do not copy a live SQLite DB inconsistently when WAL is active — use the built-in backup command or checkpoint first);
- 3x-ui settings / Xray template;
- exported inbounds, outbounds, routing (via API export when available);
- never dump secrets into chat or the final report.

If backup cannot be created, stop and ask the user whether to continue without backup. Default answer is no.

### Phase 3 — Plan

Before applying anything, build an internal change plan distinguishing new objects from reused/updated ones:

```text
Foreign:
+ inbound from-ru            (create / reuse: <found tag>)
+ service client             (create / reuse: <found email>)
+ firewall port              (create / already present)

RU:
+ Hiddify inbound            (create / reuse: <found tag>)
+ outbound to-foreign        (create / reuse)
+ routing rule               (create / reuse)
+ Hiddify client             (create / reuse: <found email>)
```

### Idempotency Rules

Before creating each object, search for an existing one. Check at minimum: inbound tag, inbound remark, port, protocol, client email/name, client UUID, outbound tag, routing rule tag/match.

```text
not found                      → create
exists and compatible          → reuse
exists, minor change needed    → update
conflicts with the architecture → STOP, report conflict, do not overwrite automatically
```

Port collision detection before creating any inbound:

```bash
ss -lntup
```

Check existing services, Xray listeners, 3x-ui panel, metrics listeners, SSH, web servers, Docker proxies. If the port is occupied: do not kill the process. Choose another port or stop and report.

Metrics conflict: do not assume metrics always listens on `127.0.0.1:11111` — detect any metrics listener dynamically (parse the Xray template for `metrics` / `api` blocks and cross-check `ss -lntup`).

### Existing installation preservation

If 3x-ui is already installed: `DO NOT reinstall` without a proven reason. First inspect, reuse, make minimal modifications.

If installation is required, use only the official 3x-ui install source. Show the source and verify repository identity before running anything. No random mirrors or third-party install scripts.

### Phase 4 — Configure foreign

Create or update (per plan): foreign inbound `from-ru`, service client `ru-to-foreign-client`, firewall port.

Open only the required port. For TCP-based protocols, open TCP only. For Hysteria2/TUIC/QUIC-based transports, open UDP as required. Do not open both TCP and UDP unless the selected protocol explicitly needs both.

After the change, run local validation (Phase 5). Do not proceed to RU if the foreign inbound does not work.

### Phase 5 — Validate foreign

Check:

```text
Xray running                    systemctl status / container state
port listening                  ss -lntup
firewall allows traffic         OS firewall + provider firewall state recorded
service client exists           API/inbound export
inbound active                  API list
configuration valid             Xray log has no config errors
```

### Phase 6 — Configure RU

Create or update (per plan): Hiddify-facing inbound `hiddify-entry`, client `windows-hiddify-client`, outbound `to-foreign`, routing rule `hiddify-entry → to-foreign`, firewall port.

Required routing state:

- Hiddify-facing inbound has tag `hiddify-entry` (or the pre-existing equivalent);
- outbound to foreign has tag `to-foreign`;
- routing rule maps `inboundTag: [hiddify-entry]` → `outboundTag: to-foreign`;
- client traffic must not fall through to the RU direct/freedom outbound.

Logical structure (adapt to the actual API/config shape):

```json
{
  "routing": {
    "rules": [
      {
        "type": "field",
        "inboundTag": ["hiddify-entry"],
        "outboundTag": "to-foreign"
      }
    ]
  }
}
```

If 3x-ui manages Xray configuration, do not manually rewrite the whole Xray JSON unless there is no safer option and the user approved that method. Never edit runtime `config.json` directly — 3x-ui regenerates it from the database on restart.

### Live Apply Rules

Default to live apply; restart is the exception:

```text
change config → attempt live apply → validate
```

**Planned-update discipline (mandatory for every Xray template update):** before each `POST /panel/api/xray/update`, snapshot the current template semantically; after the update, re-read and compute a semantic diff against the pre-update state. Every actual change must match the change plan. If routing rules, outbounds, DNS, balancers, policy, or any other Xray section changed or disappeared outside the plan:

```text
UNPLANNED CONFIG DRIFT
```

→ immediately restore the pre-update snapshot and re-verify. Reference implementation: `tests/cascade_lib.py` (`planned_update`, `restore_template`); exercised in `tests/cascade_docker_test.py` and `tests/rollback_rehearsal.py`.

Restart only if:

- live apply is unavailable;
- live apply returned an error;
- Xray requires a restart for that specific change;
- runtime state does not match configuration;
- the agent has proven the necessity.

Before restarting: state the reason, verify the backup exists, and note the impact on active connections. Never restart "just in case".

### Phase 7 — RU → Foreign verification

Before testing Hiddify, prove the Russian Xray can reach the foreign inbound:

```bash
# from RU server
nc -vz <FOREIGN_HOST> <FOREIGN_INBOUND_PORT>
# fallback without nc
timeout 5 bash -c '</dev/tcp/<FOREIGN_HOST>/<FOREIGN_INBOUND_PORT>' && echo open || echo closed
```

Then verify the RU outbound actually connects (Xray log shows an established outbound connection or the API reports non-zero traffic on the `to-foreign` outbound after a test request).

### Phase 8 — End-to-end verification

Run the acceptance tests in `references/acceptance-tests.md`. Critical tests that MUST be PASS before reporting success:

```text
Foreign inbound operational
RU outbound reaches foreign
RU routing sends Hiddify traffic to foreign
Hiddify primary target == RU
External IPv4 == expected foreign server/network
Xray healthy on both servers
```

If any critical test fails, the configuration must not be reported as successful. If critical tests fail after apply, roll back (see Rollback).

### Protocol Selection

Keep this default unless validation says otherwise:

```text
Hiddify → RU: VLESS + Reality
RU → foreign: VLESS
foreign → internet: freedom/direct
```

Before committing to a protocol, verify against the actual environment:

- support by the installed Xray (probe, don't assume);
- support by Hiddify;
- existing firewall and provider firewall;
- domain availability;
- TLS/Reality requirements;
- UDP requirements.

Do not use a protocol just because it is listed in SKILL.md.

Reality is a VLESS-family option. Do not pair Reality with non-VLESS protocols.

### Reality Validation

When Reality is used, verify on the correct sides:

```text
serverName (SNI)        matches between inbound and client profile
publicKey               present in the client profile / RU outbound
privateKey              present only on the server side (never print it)
shortId                 present and hex
fingerprint             set (e.g. chrome)
flow                    consistent (e.g. xtls-rprx-vision)
target/dest             reachable from the server
```

Sanity checks: public/private key role mismatch (private key in a client profile = config error), invalid shortId characters, wrong SNI, wrong fingerprint.

### Hiddify Profile Validation

The Hiddify profile must point to the Russian server, not the foreign server. Before giving the profile to the user, validate:

```text
scheme                  matches the chosen protocol
host/address            == RU entry (if == foreign server → FAIL, stop)
port                    matches the RU inbound
UUID/password           present, matches the RU inbound client
security                none/TLS/Reality as chosen
Reality/TLS             publicKey/shortId/fingerprint/SNI present when Reality
SNI, fingerprint, flow  consistent with the inbound
transport               TCP/ws/grpc/httpQUIC as chosen
ALPN                    if relevant to the chosen transport
```

### Hiddify Smoke Test

If the client machine is accessible to the agent:

```text
1. Generate/import profile.
2. Start connection.
3. Verify connection established.
4. Verify external IPv4 == foreign server.
5. Verify DNS (see acceptance tests).
6. Verify UDP where applicable.
7. Verify IPv6 behavior.
```

If the agent cannot control Hiddify directly, give the user the minimal checklist from `references/acceptance-tests.md`.

### DNS, IPv6, UDP Testing

Mandatory parts of end-to-end verification (detailed procedures in `references/acceptance-tests.md`):

- **DNS:** determine which resolver is actually used; detect DNS leaking directly via RU/local ISP when foreign egress is expected. Output `DNS STATUS: PASS / WARNING / FAIL`.
- **IPv6:** test both IPv4 and IPv6. `IPv4 foreign / IPv6 disabled` → PASS; `IPv4 foreign / IPv6 foreign` → PASS; `IPv4 foreign / IPv6 RU/local` → FAIL or WARNING depending on user requirements. A foreign IPv4 alone does not prove full routing.
- **UDP:** if the protocol or user traffic implies UDP, validate it. Do not open UDP firewall ports without need. Output `UDP: PASS / FAIL / N/A`.

### MTU/MSS Diagnostics

For "connection established but sites partially load / large downloads hang / QUIC fails": check MTU, PMTU, MSS, fragmentation. Do not change MTU automatically without a proven cause. See `references/troubleshooting.md`.

## Docker Testing (no real servers)

When no real VPS servers are available, the cascade can be simulated with two Docker containers on one machine. Full procedure: `references/cascade-implementation-notes.md` → "Docker testing approach"; CI script: `tests/cascade_docker_test.py`.

Key constraints:

- Use the official image `ghcr.io/mhsanaei/3x-ui` with a **pinned version** for acceptance tests (e.g. `THREEXUI_VERSION=v3.7.0`). Never `latest` as the default; a separate compatibility run with `latest` is allowed (weekly CI job, non-blocking).
- Automated suites: `tests/cascade_docker_test.py` (cascade + drift detection + backup/restore), `tests/rollback_rehearsal.py` (full rollback rehearsal), `tests/negative_tests.py` (10-scenario negative matrix), `tests/reality_test.py` (Reality compatibility, scheduled).
- 3x-ui API has CSRF protection — get token from `/csrf-token` first, login with `username`/`password` fields (not `user`/`passwd`); API token (Settings → Security) bypasses CSRF when available.
- Never edit `config.json` directly — 3x-ui regenerates it from SQLite on restart.
- Outbound connections and routing rules are NOT in the database — they live in the Xray template (update via the Xray API).
- Default metrics listener may conflict with inbound ports — detect the metrics listener dynamically and move it via `metrics.listen` in the template before adding an inbound on that port.

Docker tests are an integration test, not production validation. Docker proves: Xray config, protocol handshake, routing, service-client architecture, TCP flow. Docker does NOT prove: provider firewall, cloud security groups, ISP routing, MTU, public IPv4/IPv6, Reality behavior in a real network, SNI/CDN, Hiddify on Windows, real DNS leaks, UDP through the production path.

## Logs and Secret Redaction

When diagnosing, collect 3x-ui service logs, Xray logs, `journalctl`, Docker logs — then redact before showing:

Always redact: UUID, password, Reality private key, API token, session cookie, subscription URL, full share URI. Safe to show: host, port, protocol, tag, masked UUID (`8f23****91ab`).

## Health Status Model

Final report uses exactly one of:

```text
HEALTHY              all mandatory tests pass
PARTIALLY HEALTHY    main TCP path works, but e.g. UDP FAIL / IPv6 WARNING / DNS UNKNOWN
FAILED               main cascade path does not work
ROLLED BACK          new configuration failed validation, previous state restored
```

If any critical acceptance test fails, the result is FAILED or ROLLED BACK — never a success status.

## Rollback

Rollback is part of the algorithm, not advice. If critical acceptance tests fail after applying changes: diagnose → roll back → verify restored state.

Rollback must restore: previous 3x-ui state, previous Xray settings, previous inbounds, outbounds, routing, previous firewall, service state. After rollback, run rollback verification and output:

```text
ROLLBACK STATUS: SUCCESS
```

or

```text
ROLLBACK STATUS: PARTIAL FAILURE
```

With `PARTIAL FAILURE`, list explicitly what remains changed. Detailed commands, semantic-snapshot discipline, and the rollback rehearsal requirement (Snapshot A → damage → rollback → compare): `references/security-and-rollback.md`. Automated rehearsal: `tests/rollback_rehearsal.py`.

## Final Report Format

Use a short safe report with the health status and acceptance table (`references/acceptance-tests.md`), e.g.:

```text
Готово. Статус: HEALTHY

Схема:
Windows 11 + Hiddify → российский сервер → зарубежный сервер → интернет

Acceptance tests: (таблица PASS/FAIL/N/A — см. references/acceptance-tests.md)

Что настроено:
- Российский сервер: входная точка для Hiddify (inbound hiddify-entry).
- Зарубежный сервер: конечная точка выхода (inbound from-ru, service client ru-to-foreign-client).
- На российском сервере трафик клиентского inbound направлен в outbound to-foreign.
- На зарубежном сервере трафик выпускается через direct/freedom.
- Служебный клиент на зарубежном сервере создан только для RU → foreign и не выдан пользователю.

Протоколы:
- Hiddify → RU: <protocol>
- RU → foreign: <protocol>

Открытые порты:
- RU: <port>/tcp или udp
- Foreign: <port>/tcp или udp

Проверки:
- SSH к обоим серверам: OK/failed
- 3X-UI на обоих серверах: OK/failed
- RU видит foreign inbound: OK/failed
- Внешний IP через Hiddify: foreign / not verified / failed
- DNS: PASS/WARNING/FAIL · UDP: PASS/FAIL/N/A · IPv6: PASS/WARNING/N/A

Как добавить и проверить в Hiddify на Windows 11:
1. Временно отключить другие VPN/proxy, чтобы не исказить проверку.
2. Открыть Hiddify.
3. Добавить профиль импортом или вручную.
4. Убедиться, что профиль указывает на российский сервер.
5. Подключиться.
6. Проверить IP на ifconfig.me или ipinfo.io/ip.
7. Ожидаемый результат: внешний IP соответствует зарубежному серверу.

Секретные параметры не включены в отчёт.
```

## Completion Checklist

- [ ] User explicitly approved the work (approved_scope captured).
- [ ] SSH access uses the already configured local SSH key where possible.
- [ ] SSH access to both servers verified.
- [ ] Discovery completed on both servers (system, firewall, 3x-ui version, API capabilities).
- [ ] 3X-UI API discovery run; API mode (modern/legacy) recorded; auth method recorded.
- [ ] Backup created before touching 3X-UI, Xray, routing, or firewall (WAL-safe DB copy).
- [ ] Change plan built with reuse/update/create per object before applying.
- [ ] Port collision check run before creating inbounds; metrics listener detected dynamically.
- [ ] Existing 3x-ui installation not reinstalled and existing objects not overwritten without need.
- [ ] Rollback path is known and can be explained.
- [ ] Foreign server configured as exit and validated locally before RU changes.
- [ ] RU outbound points to foreign inbound; RU routing sends Hiddify inbound to foreign outbound.
- [ ] Live apply attempted first; restart done only with a stated reason.
- [ ] Semantic pre/post diff checked on every template update; UNPLANNED CONFIG DRIFT would trigger rollback.
- [ ] Backup created from the CURRENT saved template (getXraySetting), never from getDefaultJsonConfig.
- [ ] Hiddify profile points to RU server; foreign host/IP is not the primary target.
- [ ] Hiddify profile fields validated (scheme/host/port/UUID/security/Reality/SNI/fingerprint/flow/transport).
- [ ] Client-side verification on Windows 11 was performed or explicitly delegated to the user.
- [ ] TCP end-to-end verified; external IP verified as foreign.
- [ ] DNS test run (PASS/WARNING/FAIL); IPv6 test run; UDP test run or marked N/A.
- [ ] Acceptance test table produced; all critical tests PASS or rollback executed.
- [ ] Final report contains no secrets unless explicitly approved.
- [ ] No claims of guaranteed anonymity or universal blocking resistance.

## 3X-UI API Quick Notes

Full endpoint matrix, discovery curls, and payload examples: `references/api-compatibility.md`.

### CSRF Protection (session auth)

All session-based API calls require:

1. `GET /csrf-token` → extract `obj` field as CSRF token.
2. `POST /login` with `{"username":"...","password":"..."}` + `X-CSRF-Token` header.
3. All subsequent requests need the `X-CSRF-Token` header + session cookie.

API token (Settings → Security → API Token) bypasses CSRF entirely — prefer it when configured. Never assume default credentials (`admin/admin` is only for fresh Docker test environments, and even there change it after first login); production workflow must always ask for credentials.

### Xray Config Update

The Xray template update endpoint expects **form-encoded** data with field name `xraySetting` (modern path: `/panel/api/xray/update`, legacy: `/panel/xray/update`). Sending a JSON body or a wrong field name returns `"unexpected end of JSON input"`. Use typed nested JSON when the API accepts it; fall back to string-serialized JSON only when required. Inbound client field `tgId` must be an int (`0`), not a string.

### Two-Step Configuration Process

3x-ui separates inbound management (database) from outbound/routing (Xray template):

**Step A — inbounds** via the inbounds API (JSON body). Both the Hiddify inbound (RU) and the service inbound (foreign) are created this way.

**Step B — outbound + routing** via the Xray template update (form field `xraySetting`): fetch the current template, add outbound and routing rule, POST back. Without this step Xray has no outbound to foreign and no routing rule.

**Step C — apply.** Prefer live apply; restart the service/container only if live apply is unavailable or fails.

### Inbound Creation

JSON body works for inbounds. Supported protocols include: `vmess vless trojan shadowsocks wireguard hysteria http mixed tunnel` (verify against the installed version — the list changes between releases). SOCKS is NOT a standalone protocol — use `mixed` for HTTP+SOCKS.

### Config Persistence

3x-ui stores the Xray template in SQLite `settings` table (key `xraySetting`). Inbounds are in the `inbounds` table. On restart, `config.json` is regenerated from the database. Direct file edits are overwritten on restart.

### SQLite WAL Mode

The database uses WAL mode. To copy it safely from outside the container: install sqlite in the container (`apk add --no-cache sqlite`), checkpoint WAL (`sqlite3 /etc/x-ui/x-ui.db "PRAGMA wal_checkpoint(TRUNCATE);"`), then `docker cp container:/etc/x-ui/x-ui.db /tmp/db.sqlite` — or prefer 3x-ui's built-in backup mechanism when available. Never copy a live WAL-active DB as a single plain file copy.
