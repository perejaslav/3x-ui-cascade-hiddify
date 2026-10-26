# 3x-ui-cascade-hiddify

Hermes Skill для настройки каскадной схемы из двух серверов с 3X-UI/Xray:

**Windows 11 + Hiddify → российский сервер → зарубежный сервер → интернет**

## Что это и для чего

Скилл для агента (Hermes/ZCode), который настраивает и проверяет каскад: пользователь в Hiddify подключается только к российскому серверу (entry), а весь интернет-трафик выходит через зарубежный сервер (exit). Схема:

```text
Windows 11 + Hiddify → российский сервер → зарубежный сервер → интернет
```

- профиль Hiddify создаётся на российском сервере и указывает только на него;
- служебные credentials зарубежного сервера используются только исходящим соединением (outbound) российского сервера;
- зарубежный сервер не становится основным target в Hiddify-профиле.

## Что делает скилл

- сбор данных перед началом работ (OS, сервисы, порты, firewall — включая провайдерский);
- определение версии 3x-ui и **обнаружение возможностей API** (modern `/panel/api/*` vs legacy `/panel/xray/*`, способ аутентификации, формат payload);
- проверка SSH-доступа по уже настроенному ключу;
- preflight-проверки;
- резервное копирование перед изменениями (WAL-безопасная копия SQLite);
- построение плана изменений (create / reuse / update для каждого объекта) — **идемпотентно**, с поиском существующих объектов и проверкой коллизий портов;
- проверка или установка 3X-UI на обоих серверах (только официальный источник, без переустановки работающего);
- настройка inbound/outbound/routing;
- **live apply** вместо перезапуска там, где это возможно;
- создание отдельного служебного клиента на зарубежном сервере;
- создание пользовательского профиля для Hiddify на российском сервере + валидация полей профиля;
- end-to-end проверки: TCP-путь, внешний IP, **DNS**, **IPv6**, **UDP**;
- при провале критических проверок — **откат** к исходному состоянию с верификацией;
- итоговый отчёт с таблицей acceptance-тестов и статусом `HEALTHY / PARTIALLY HEALTHY / FAILED / ROLLED BACK`;
- диагностика типовых ошибок (symptom → check → fix);
- Docker-интеграционный тест каскада на одной машине + тест идемпотентности повторным запуском.

## Что скилл НЕ делает

- не переустанавливает работающий 3x-ui без доказанной причины;
- не удаляет существующие объекты и не перезаписывает рабочую конфигурацию без необходимости;
- не отключает firewall и не открывает порты сверх необходимого;
- не редактирует runtime `config.json` напрямую (3x-ui регенерирует его из БД);
- не предполагает дефолтных credentials (`admin/admin`) в production;
- не публикует секреты (UUID, пароли, приватные ключи, токены, ссылки подписки);
- не обещает гарантированную анонимность и устойчивость к блокировкам;
- Docker-тест не считается production-валидацией (firewall провайдера, ISP, MTU, Reality в реальной сети, Hiddify, DNS-лики не проверяются).

## Структура репозитория

```text
README.md                                  — этот файл
SKILL.md                                   — основной файл скилла (алгоритм, правила, safety)
references/
├── cascade-implementation-notes.md        — заметки по внедрению, протоколы, Docker-симуляция
├── api-compatibility.md                   — матрица endpoints, capability detection, auth, payload
├── troubleshooting.md                     — structured troubleshooting, MTU/MSS, логи
├── acceptance-tests.md                    — таблица тестов, critical tests, DNS/IPv6/UDP, negative tests
└── security-and-rollback.md               — секреты, WAL-safe backup, drift-защита, алгоритм отката
tests/
├── cascade_lib.py                         — общая библиотека: semantic diff, planned updates, restore
├── cascade_docker_test.py                 — интеграционный тест + drift + backup/rollback exact-restore
├── rollback_rehearsal.py                  — полный rollback rehearsal (Snapshot A → damage → rollback)
├── negative_tests.py                      — матрица из 10 негативных сценариев
└── reality_test.py                        — Reality compatibility (обе ноги VLESS+Reality, vision)
.github/workflows/ci.yml                   — CI: metadata/markdown + integration + negative (blocking);
                                             Reality + latest probe (еженедельно, отдельно)
```

## Безопасность

Скилл запрещает:

- публиковать секреты в отчётах и логах (redaction UUID/паролей/ключей/токенов);
- отключать firewall полностью;
- перезаписывать конфигурации без backup;
- выдавать служебный foreign-клиент как основной Hiddify-профиль;
- выполнять restart без доказанной необходимости.

## Версия

Текущая версия: `2.1.0`

## Changelog

### 2.1.0 (hardening)

- Docker test resources are scoped to the current run; port publications bind only to localhost.
- Version selection honors `THREEXUI_VERSION`; pinned CI target updated to 3x-ui v3.11.0 (bundling Xray-core v26.10.21). Compatibility must be confirmed by a successful CI run.
- Planned template update now rejects incomplete application and verifies rollback outcome.
- Reality test redacts sensitive X25519 command output on parse errors.
- Separate deterministic regression tests for semantic diff, version selection and safe cleanup.
- Hiddify Windows GUI, live VPS, DNS/IPv6/UDP leak tests remain manual acceptance gates.

### 2.0.1

- **P0 — backup текущего шаблона:** backup Xray template исправлен — читается **текущий сохранённый** шаблон через `POST /panel/api/xray/` (getXraySetting) c извлечением `xraySetting`, а не `getDefaultJsonConfig` (тот возвращает дефолт). Rollback использует именно этот snapshot. Тест «current config → backup → modification → rollback → exact restore» — PASS.
- **P0/P1 — защита от незапланированных изменений:** перед каждым `POST .../xray/update` снимается semantic snapshot; после обновления — semantic diff; изменения вне change plan (routing rules, outbounds, DNS, balancers, policy, другие секции) → `UNPLANNED CONFIG DRIFT` и автоматический rollback. Реализация: `tests/cascade_lib.py` (`planned_update`, `semantic_template_diff`), покрыта тестами.
- **P1 — настоящий rollback integration test:** `tests/rollback_rehearsal.py`: Snapshot A → configure cascade → intentional damage → rollback → сравнение со Snapshot A. PASS только при побайтовом восстановлении шаблонов, удалении созданных объектов, неизменности старых и работоспособном baseline.
- **P1 — negative-матрица автоматизирована:** `tests/negative_tests.py` — 10 сценариев (occupied ports, invalid UUID, foreign unreachable, wrong endpoint, CSRF/auth, wrong credentials, conflicting inbound, invalid xraySetting, broken routing tag) с проверками «clear failure / no destructive overwrite / no duplicates / recovery». CI — blocking.
- **P2 — исправлен DNS acceptance test:** убран ошибочный `whoami.cloudflare.esk`; добавлена воспроизводимая процедура (resolver-echo `whoami.akamai.net`, `o-o.myaddr.l.google.com`, leak-test страницы, детекция fake-ip) с матрицей PASS / WARNING / FAIL и запретом вывода по одному nslookup.
- **P2 — Reality compatibility test:** `tests/reality_test.py` — полный прод-сценарий client → VLESS+Reality → RU → VLESS+Reality → Foreign → Internet: x25519-пары per-server, role-check приватных ключей, shortId/SNI/fingerprint/`xtls-rprx-vision`, валидация share URI (host == RU), сквозной трафик, счётчики foreign. Отдельный CI job (scheduled/manual).
- **CI:** pinned `v3.7.0` — blocking (integration + rollback rehearsal + negative matrix); `latest` — отдельный non-blocking job; недельный schedule (пн 03:00 UTC) прогоняет pinned + latest + Reality.

### 2.0.0

- **API:** поддержка modern `/panel/api/xray/*` с legacy `/panel/xray/*` как fallback; обязательный этап API discovery с capability detection (modern/legacy, auth, live apply, serialization); правило «runtime API > описанное в SKILL.md»; re-discovery при 404/405 и несовпадении схемы.
- **Проверено на реальном 3x-ui v3.7.0 (Docker-интеграционный тест):** typed JSON payload принимается и возвращается; `tgId` клиента — только int; `getDefaultJsonConfig` возвращает дефолтный шаблон, текущий читается через `POST /panel/api/xray/`; live apply подтверждён (`applied through the core API, no restart needed`); fake-ip DNS окружение (198.18.0.0/15) вызывает blackhole freedom-outbound — детектируется и обходится явным DNS + `domainStrategy: UseIPv4`.
- **Payloads:** typed JSON вместо JSON-encoded строк там, где API поддерживает; legacy-сериализация только как fallback.
- **Auth:** API token (предпочтительно) и session+CSRF flow; строгие правила обращения с секретами (redaction UUID/ключей/токенов/cookie, маскирование).
- **Workflow:** транзакционная схема discover → detect capabilities → backup → plan → apply → validate → commit; при провале — diagnose → rollback → verify.
- **Идемпотентность:** поиск существующих объектов перед созданием (tags, ports, client emails/UUID, routing), reuse/update/STOP при конфликте; повторный запуск не создаёт дубликатов.
- **Live apply:** по умолчанию без перезапуска; restart только с доказанной необходимостью.
- **Откат:** обязательная часть алгоритма с `ROLLBACK STATUS: SUCCESS / PARTIAL FAILURE` и верификацией восстановленного состояния; WAL-безопасный backup SQLite.
- **Тестирование:** официальный образ `ghcr.io/mhsanaei/3x-ui` с pinned версией (вместо `bigbugcc/3x-ui:latest`); Docker-матрица pinned stable + latest compatibility; negative tests; re-run тест; upgrade compatibility сценарий.
- **Валидация:** таблица acceptance-тестов; критические тесты; DNS / IPv6 / UDP проверки; валидация Hiddify-профиля (scheme/host/port/UUID/security/Reality/SNI/flow/transport); port collision detection; динамическое обнаружение metrics-listener.
- **Статусы:** модель `HEALTHY / PARTIALLY HEALTHY / FAILED / ROLLED BACK`; провал критического теста запрещает статус успеха.
- **Документация:** SKILL.md разгружен в 5 reference-файлов; README переписан; compatibility matrix; structured troubleshooting.
- **CI:** GitHub Actions — markdown/metadata validation + Docker integration test (cascade + idempotency).

### 1.2.0

- Docker testing, 3X-UI API pitfalls (CSRF, `xraySetting`, конфликт портов, SQLite WAL), two-step config process.

### 1.1.1

- Начальный релиз скилла.

## Compatibility

Tested against конкретные версии (не «latest»):

| Component | Tested | Notes |
|---|---|---|
| 3x-ui | v3.11.0 | Docker cascade, rollback, negative tests, VLESS+Reality (2026-10-26) |
| Xray-core | v26.10.21 | bundled in 3x-ui v3.11.0; confirmed in container logs |
| Hiddify Windows | v4.1.1 | VLESS+Reality, ручной профиль и share-link |
| Ubuntu | 22.04, 24.04 | systemd install |
| Debian | 11, 12 | systemd install |
| Docker | 24+ / ghcr.io/mhsanaei/3x-ui | test environment |

## Docker testing

Быстрый прогон на одной машине (официальный образ, pinned версия):

```bash
THREEXUI_VERSION=v3.11.0   # текущая закреплённая версия (environment override)
python tests/cascade_docker_test.py      # каскад + drift + backup/rollback
python tests/rollback_rehearsal.py       # полный rehearsal
python tests/negative_tests.py           # негативная матрица
python tests/reality_test.py             # Reality compatibility (обе ноги)
```

Подробности — `references/cascade-implementation-notes.md`. CI (GitHub Actions): на каждый push — metadata/markdown, интеграция, rehearsal и negative-матрица (blocking, pinned); еженедельно по понедельникам дополнительно Reality compatibility и `latest` probe.
