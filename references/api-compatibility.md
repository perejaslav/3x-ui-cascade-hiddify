# API Compatibility

Source of truth for 3x-ui API behavior. Runtime API beats documented assumptions: before any write, probe the installed version. This reference documents the endpoint matrix, capability detection, authentication modes, and payload serialization rules.

## Version and capability matrix

| Component | Tested | Notes |
|---|---|---|
| 3x-ui | v3.7.0 (pinned Docker, acceptance tests) | modern `/panel/api/*` + legacy paths |
| 3x-ui | older releases (v2.x era) | legacy `/panel/xray/*` only — use fallback path |
| Xray-core | v26.3.27 (bundled with 3x-ui v3.7.0) | VLESS, Reality, xtls-rprx-vision |
| Hiddify Windows | v4.1.1 | VLESS+Reality, manual profile & share-link import |
| Ubuntu | 22.04, 24.04 | systemd install |
| Debian | 11, 12 | systemd install |
| Docker | ghcr.io/mhsanaei/3x-ui | test environment only |

Version numbers are hints only. A capability check always wins over a version check.

## Decision rule

```text
1. Determine 3x-ui version.
2. Probe modern API.
3. If modern API exists — use it.
4. Legacy endpoints only as fallback.
```

**Mandatory rule:** never pick a legacy endpoint only because the version "seems old". Probe first.

## Endpoint matrix

### Xray template (outbounds, routing, general settings)

| Purpose | Modern (preferred) | Legacy (fallback) |
|---|---|---|
| Read CURRENT saved template | `POST /panel/api/xray/` (getXraySetting) | `POST /panel/xray/` |
| Fetch DEFAULT template | `GET /panel/api/xray/getDefaultJsonConfig` | `GET /panel/xray/getDefaultJsonConfig` |
| Update Xray template | `POST /panel/api/xray/update` | `POST /panel/xray/update` |
| Test an outbound | `POST /panel/api/xray/testOutbound` (form: `outbound` JSON, `mode` = `tcp`/`real`, optional `allOutbounds` JSON) | — |
| Restart Xray core | `POST /panel/api/xray/restart` (may not exist) | panel UI button / `x-ui restart` / container restart |

**Critical read-modify-write rule (verified on v3.7.0):** `getDefaultJsonConfig` returns the **DEFAULT** template, not the current one. Using it as the read source overwrites existing outbounds/routing on update — and as a *backup source* it stores the wrong (default) configuration instead of the production state. Always read the CURRENT template via `POST /panel/api/xray/` (getXraySetting) → response `{"obj": "<json string>"}` → parse → `obj.xraySetting` → modify → `POST .../update` with form field `xraySetting`. Backups and rollback snapshots must come from this same current-template read (see `references/security-and-rollback.md`).

### Inbounds (database objects)

| Purpose | Endpoint |
|---|---|
| List inbounds | `GET /panel/api/inbounds/list` |
| Get inbound | `GET /panel/api/inbounds/get/:id` |
| Create inbound | `POST /panel/api/inbounds/add` |
| Update inbound | `POST /panel/api/inbounds/update/:id` |
| Delete inbound | `POST /panel/api/inbounds/del/:id` |
| Add client | `POST /panel/api/inbounds/addClient` |
| Update client | `POST /panel/api/inbounds/updateClient/:clientId` |
| Delete client | `POST /panel/api/inbounds/delClient/:clientId` |

The inbounds API lives under `/panel/api/inbounds/*` in both old and new versions; the Xray endpoints are the ones that moved from `/panel/xray/*` to `/panel/api/xray/*`.

## Authentication

### Option A — API token (preferred)

Configured in panel: Settings → Security → API Token. Send as a `Authorization: Bearer <token>` header (some versions also accept `Cookie` based token auth — probe). No CSRF flow needed.

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  http://HOST:PORT/panel/api/inbounds/list
```

### Option B — session login (fallback)

```bash
# 1. CSRF token (no auth needed)
CSRF=$(curl -s -c cookies.txt http://HOST:PORT/csrf-token | jq -r '.obj')

# 2. Login (field names are "username"/"password", NOT "user"/"passwd")
curl -s -b cookies.txt -c cookies.txt \
  -X POST http://HOST:PORT/login \
  -H "Content-Type: application/json" \
  -H "X-CSRF-Token: $CSRF" \
  -d '{"username":"...","password":"..."}'

# 3. Session cookie + CSRF header for subsequent calls
curl -s -b cookies.txt -H "X-CSRF-Token: $CSRF" \
  http://HOST:PORT/panel/api/inbounds/list
```

### Security requirements

Never: print the token or password, save credentials into the report, write credentials into shell history, commit credentials, keep cookies/token files after work unless needed. Use `curl -H @headerfile` or environment variables instead of inline secrets where possible; delete temp files after work.

## API discovery procedure

Run on each server before the first write. Read-only except the final report.

```bash
PANEL_URL="http://127.0.0.1:2053"   # adjust per server

# 0. Version from CLI if available
x-ui version 2>/dev/null || 3x-ui version 2>/dev/null || true

# 1. Panel reachable?
curl -s -o /dev/null -w '%{http_code}\n' "$PANEL_URL/"

# 2. Modern Xray API (probe before auth — 401/403 means endpoint exists, 404 means missing)
for ep in /panel/api/xray/getDefaultJsonConfig /panel/xray/getDefaultJsonConfig; do
  code=$(curl -s -o /dev/null -w '%{http_code}' "$PANEL_URL$ep")
  echo "$ep -> $code"
done

# 3. Inbound API
curl -s -o /dev/null -w '%{http_code}\n' "$PANEL_URL/panel/api/inbounds/list"

# 4. Auth: try token first, then session
#    (token configured in panel settings; if only session works, do CSRF+login)

# 5. Live apply support: after a benign template update, check whether
#    Xray picks it up without a panel restart (compare xray runtime config
#    or listener state). Record supported/not supported.
```

Interpretation:

- `404` on modern + `200/401/403` on legacy → API mode: legacy.
- `401/403` on modern → endpoint exists; authenticate and retry.
- Schema mismatch (unexpected JSON shape) → treat like discovery failure: re-run discovery, do not guess.

### Discovery summary format

```text
3x-ui version: 3.7.0
API mode: modern
Authentication: API token
Xray API: available
Inbound API: available
Live apply: supported
Legacy fallback: not required
```

Never include secret values (tokens, passwords, cookies, keys) in the summary.

## Typed JSON vs legacy string serialization

Prefer typed payloads when the installed API accepts them:

```json
{
  "settings": { "clients": [] }
}
```

Fallback (legacy string serialization) only when required:

```json
{
  "settings": "{\"clients\":[]}"
}
```

Helper logic:

```text
prefer typed payload
↓
fallback to legacy serialization
```

Practical rule: build the payload typed; if the API responds with a parse/validate error mentioning types or encoding, re-serialize the nested fields as JSON strings and retry once. Record which mode the server accepted in the discovery summary for the rest of the session.

## Xray template update

The update endpoint expects **form-encoded** body with field `xraySetting`, not a raw JSON body:

```python
import requests, json
s = requests.Session()
# ... auth (token header or CSRF+login) ...
# 1. READ the CURRENT template (NOT getDefaultJsonConfig — that is the default)
saved = s.post(f'{BASE}/panel/api/xray/', headers=auth_headers).json()
data = json.loads(saved['obj']) if isinstance(saved['obj'], str) else saved['obj']
config = data['xraySetting']
if isinstance(config, str):
    config = json.loads(config)
# 2. modify config['outbounds'], config['routing']['rules'], metrics, ...
# 3. write back
s.post(f'{BASE}/panel/api/xray/update',
       data={'xraySetting': json.dumps(config)},
       headers=auth_headers)
```

**Pitfalls:**
- JSON body or wrong field name → `"unexpected end of JSON input"`.
- Reading via `getDefaultJsonConfig` silently discards existing outbounds/routing on the next update.

## Inbound creation

```python
s.post(f'{BASE}/panel/api/inbounds/add', json={
  "up": 0, "down": 0, "total": 0, "remark": "from-ru",
  "enable": True, "expiryTime": 0, "listen": "", "port": 22220,
  "protocol": "vless",
  "settings": {
    "clients": [{
      "id": "<uuid>", "flow": "", "email": "ru-to-foreign-client",
      "limitIp": 0, "totalGB": 0, "expiryTime": 0, "enable": True,
      "tgId": 0, "subId": "<16-char-id>", "reset": 0
    }],
    "decryption": "none", "fallbacks": []
  },
  "streamSettings": {
    "network": "tcp", "security": "none", "externalProxy": [],
    "tcpSettings": {"acceptProxyProtocol": False, "header": {"type": "none"}}
  },
  "sniffing": {"enabled": True, "destOverride": ["http", "tls", "quic"],
               "metadataOnly": False, "routeOnly": False},
  "allocate": {"strategy": "always", "refresh": 5, "concurrency": 3}
}, headers=auth_headers)
```

Verified on v3.7.0:

- **Typed payload works**: the API accepts nested JSON objects for `settings`/`streamSettings`/`sniffing`/`allocate` (the typed-vs-string rule above), and also accepts legacy JSON-encoded strings. `list`/`get` responses return them as typed dicts on v3.7.0 — handle both shapes when parsing (`as_dict` helper in `tests/cascade_docker_test.py`).
- **`tgId` must be an int** (`0`), NOT a string — a string produces `json: cannot unmarshal string into Go struct field .0.tgId of type int64`.
- `subId` is an arbitrary string; `allocate` block is optional.
- Supported protocols: `vmess vless trojan shadowsocks wireguard hysteria http mixed tunnel` (verify against the installed version — the list changes between releases). SOCKS is NOT a standalone protocol — use `mixed` for HTTP+SOCKS.
- Client `created_at`/`updated_at` timestamps and extra inbound fields (`trafficReset`, `shareAddrStrategy`, `disableFlow`, …) are server-managed — do not send them.

## Live apply behavior

Verified on v3.7.0: after `POST /panel/api/xray/update`, Xray picks the change up **without a restart** — panel logs show `Xray config changes applied through the core API, no restart needed`. This is the preferred live-apply path; restart only if the runtime state diverges (see SKILL.md → Live Apply Rules).

## Future API resilience

```text
If a known endpoint returns 404/405 or schema mismatch:
do not immediately assume installation failure.
Re-run API discovery.
```

This matters for future 3x-ui releases: endpoints may move, form fields may change names, string serialization may become typed. The skill's behavior must be driven by probes, not by hardcoded expectations.
