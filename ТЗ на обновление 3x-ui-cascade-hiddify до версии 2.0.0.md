# Техническое задание  
## Обновление `perejaslav/3x-ui-cascade-hiddify` с v1.2.0 до v2.0.0

## 1. Цель работы

Обновить Hermes Skill `3x-ui-cascade-hiddify` до актуального состояния 3x-ui и Hiddify на сентябрь 2026 года.

Основная топология должна сохраниться:

```text
Windows 11 + Hiddify
        ↓
Russian entry server
        ↓
Foreign exit server
        ↓
Internet
```

Ключевая архитектурная идея остается прежней:

- Hiddify подключается только к российскому серверу;
- российский сервер является entry point;
- зарубежный сервер является служебным exit point;
- пользовательский Hiddify-профиль создается на российском сервере;
- служебные credentials зарубежного сервера используются только российским outbound;
- зарубежный сервер не должен становиться основным target в Hiddify-профиле.

Цель версии 2.0.0 — сделать skill:

- актуальным для современных версий 3x-ui;
- version-aware;
- capability-aware;
- идемпотентным;
- безопасным для повторного запуска;
- устойчивым к изменениям API;
- пригодным для автономной работы агента;
- способным автоматически валидировать результат;
- способным безопасно откатывать неудачную конфигурацию.

---

# 2. Общий принцип переработки

Skill не должен исходить из предположения:

```text
"3x-ui всегда работает так, как описано в SKILL.md"
```

Новая модель:

```text
discover
↓
detect capabilities
↓
backup
↓
plan
↓
apply
↓
validate
↓
commit success
```

При ошибке:

```text
failure
↓
diagnose
↓
rollback
↓
verify restored state
```

---

# 3. Версия

Обновить metadata:

```yaml
version: 2.0.0
```

README также должен показывать:

```text
Current version: 2.0.0
```

Добавить краткий changelog.

---

# 4. Обновление API 3x-ui — P0

## Проблема

В текущей версии skill используются legacy endpoints:

```text
/panel/xray/*
```

Современные версии 3x-ui используют:

```text
/panel/api/xray/*
```

## Требование

Не делать тупую глобальную замену.

Реализовать capability detection.

Алгоритм:

```text
1. Определить версию 3x-ui.
2. Проверить современный API.
3. Если современный API существует — использовать его.
4. Legacy endpoints использовать только как fallback.
```

Пример:

```text
MODERN:
GET /panel/api/xray/getDefaultJsonConfig
POST /panel/api/xray/update
```

Legacy fallback:

```text
GET /panel/xray/getDefaultJsonConfig
POST /panel/xray/update
```

### Обязательное правило

Нельзя выбирать legacy endpoint только потому, что версия кажется старой.

Сначала должна выполняться фактическая проверка capability.

---

# 5. API discovery

Добавить в skill отдельный этап:

```text
3X-UI API DISCOVERY
```

Он должен определить:

- доступность `/panel/api/*`;
- доступность legacy API;
- поддерживаемые Xray endpoints;
- возможность получения конфигурации;
- возможность обновления конфигурации;
- возможность управления inbounds;
- способ аутентификации;
- наличие API token;
- необходимость CSRF;
- формат ответа API.

Результат фиксировать примерно так:

```text
3x-ui version: 3.x.x
API mode: modern
Authentication: API token
Xray API: available
Inbound API: available
Live apply: supported
Legacy fallback: not required
```

Не выводить секретные значения.

---

# 6. OpenAPI как source of truth — P0/P1

Если установленная версия 3x-ui предоставляет OpenAPI или встроенную API-документацию, использовать ее как предпочтительный источник схемы запросов.

Skill должен следовать принципу:

```text
current runtime API > documented assumptions inside SKILL.md
```

Добавить правило:

> Никогда не считать структуру endpoint или payload гарантированной только потому, что она описана в SKILL.md. Если установленная версия отличается от тестируемой, сначала проверить актуальную API-схему.

---

# 7. Typed JSON вместо JSON-encoded strings

Где современный API поддерживает nested JSON objects, использовать:

```json
{
  "settings": {
    "clients": []
  }
}
```

вместо:

```json
{
  "settings": "{\"clients\":[]}"
}
```

Legacy string serialization применять только при необходимости.

Добавить helper-логику:

```text
prefer typed payload
↓
fallback to legacy serialization
```

---

# 8. Аутентификация

Обновить раздел 3x-ui API authentication.

Поддерживать минимум:

### Вариант A — API token

Предпочтительный способ.

```text
API token
↓
без CSRF session flow
```

### Вариант B — session login

Если token недоступен:

```text
fetch CSRF
↓
login
↓
save cookies
↓
perform API requests
```

### Требования безопасности

Никогда не:

- выводить API token;
- выводить password;
- сохранять credentials в итоговый отчет;
- записывать credentials в shell history;
- коммитить credentials;
- сохранять cookies/API token после окончания работы без необходимости.

---

# 9. Version + capability matrix

Добавить внутреннюю таблицу логики.

Пример:

```text
Capability                         Preferred behavior

Modern /panel/api/* available      Use modern API
Typed payload supported            Use typed JSON
Live apply available               Apply without full restart
API token configured               Prefer token auth
Legacy only                        Use legacy compatibility path
Unknown API                        Stop before destructive action
```

Версию использовать как дополнительный hint, но не как единственный критерий.

---

# 10. Live Apply — P0

Текущий skill слишком часто предполагает обязательный restart Xray.

Это изменить.

Новая логика:

```text
change config
↓
attempt live apply
↓
validate
```

Restart делать только если:

- live apply отсутствует;
- live apply вернул ошибку;
- Xray требует restart для конкретного изменения;
- runtime state не соответствует конфигурации;
- агент явно доказал необходимость restart.

Перед restart:

- сообщить причину;
- проверить backup;
- оценить влияние на активные подключения.

---

# 11. Идемпотентность — P0/P1

Skill должен безопасно запускаться повторно.

Перед созданием каждого объекта искать существующий.

Минимум проверять:

```text
inbound tag
inbound remark
port
protocol
client email/name
client UUID
outbound tag
routing rule tag/match
```

Алгоритм:

```text
find existing object
```

Если объект отсутствует:

```text
create
```

Если существует и совместим:

```text
reuse
```

Если существует, но требует небольшого изменения:

```text
update
```

Если конфликтует с требуемой архитектурой:

```text
STOP
report conflict
do not overwrite automatically
```

---

# 12. Стандартные имена объектов

Использовать предсказуемые идентификаторы.

Foreign:

```text
inbound tag:
from-ru

service client:
ru-to-foreign-client
```

RU:

```text
Hiddify inbound:
hiddify-entry

Hiddify client:
windows-hiddify-client

foreign outbound:
to-foreign
```

Routing:

```text
hiddify-entry
    ↓
to-foreign
```

Если существующая конфигурация использует другие имена, не переименовывать ее без необходимости.

---

# 13. Transactional workflow — P0

Перестроить основной workflow.

## Phase 1 — Discovery

С обоих серверов собрать:

```text
OS
architecture
kernel
3x-ui version
Xray version
running services
listening ports
firewall
Docker/systemd install mode
panel availability
API capabilities
current inbounds
current outbounds
routing
```

---

## Phase 2 — Backup

Сделать snapshot текущего состояния.

Backup должен включать по возможности:

```text
3x-ui database
3x-ui configuration
Xray template
inbounds
outbounds
routing
firewall rules
service state
port listeners
```

SQLite backup делать корректно с учетом WAL.

Не копировать работающую SQLite БД неконсистентным способом, если активен WAL.

Использовать безопасный backup-механизм.

---

## Phase 3 — Plan

До применения изменений построить внутренний change plan:

```text
Foreign:
+ inbound from-ru
+ service client
+ firewall port

RU:
+ Hiddify inbound
+ outbound to-foreign
+ routing rule
+ Hiddify client
```

При наличии существующих объектов:

```text
reuse/update
```

---

## Phase 4 — Configure foreign

Сначала настроить foreign exit.

После изменения выполнить локальную проверку.

Не переходить к RU, если foreign inbound не работает.

---

## Phase 5 — Validate foreign

Проверить:

```text
Xray running
port listening
firewall allows required traffic
service client exists
inbound active
configuration valid
```

---

## Phase 6 — Configure RU

Создать или обновить:

```text
Hiddify-facing inbound
to-foreign outbound
routing rule
Hiddify client
```

---

## Phase 7 — RU → Foreign verification

До проверки Hiddify убедиться, что российский Xray способен установить соединение с foreign inbound.

---

## Phase 8 — End-to-end verification

Проверить полный путь.

---

# 14. Rollback как обязательная часть транзакции — P0

Rollback должен быть не просто текстовым советом.

Он должен являться частью алгоритма.

Если после применения конфигурации критические acceptance tests провалены:

```text
rollback
```

Rollback должен восстанавливать:

```text
previous 3x-ui state
previous Xray settings
previous inbounds
previous outbounds
previous routing
previous firewall
service state
```

После rollback провести:

```text
rollback verification
```

И вывести:

```text
ROLLBACK STATUS: SUCCESS
```

или:

```text
ROLLBACK STATUS: PARTIAL FAILURE
```

При partial failure обязательно перечислить, что осталось измененным.

---

# 15. Docker testing — P0

Удалить:

```bash
bigbugcc/3x-ui:latest
```

Использовать официальный image:

```text
ghcr.io/mhsanaei/3x-ui
```

Тестируемую версию фиксировать.

Например:

```bash
THREEXUI_VERSION=v3.7.0
docker pull ghcr.io/mhsanaei/3x-ui:$THREEXUI_VERSION
```

Не использовать `latest` в acceptance tests по умолчанию.

Разрешить отдельный compatibility test с `latest`.

---

# 16. Docker test matrix

Минимум два режима:

## Pinned stable

```text
official image
known tested version
```

Использовать для reproducible CI.

## Latest compatibility

```text
official latest image
```

Использовать для проверки будущих breaking changes.

---

# 17. Docker tests не считать production validation

В документации явно разделить:

```text
integration test
```

и:

```text
real VPS acceptance test
```

Docker проверяет:

- Xray config;
- protocol handshake;
- routing;
- service-client architecture;
- TCP flow.

Docker НЕ доказывает корректность:

- provider firewall;
- cloud security groups;
- ISP routing;
- MTU;
- public IPv4;
- IPv6;
- Reality behavior в реальной сети;
- SNI/CDN;
- Hiddify Windows;
- реальные DNS leaks;
- UDP через production path.

---

# 18. Hiddify integration — P1

Расширить Hiddify-раздел.

Skill должен валидировать итоговый профиль.

Проверить минимум:

```text
scheme
host/address
port
UUID/password
security
Reality/TLS
SNI
fingerprint
flow
transport
ALPN if relevant
```

Ключевое правило:

```text
host/address == RU entry
```

Если primary host совпадает с foreign server:

```text
FAIL
```

---

# 19. Hiddify smoke test

Если клиентская машина доступна агенту:

```text
1. Generate/import profile.
2. Start connection.
3. Verify connection established.
4. Verify external IPv4.
5. Verify DNS.
6. Verify UDP where applicable.
7. Verify IPv6 behavior.
```

Если агент не может управлять Hiddify напрямую, выдать пользователю минимальный checklist.

---

# 20. DNS testing

Добавить обязательную DNS-проверку.

Нужно определить:

```text
какой resolver реально используется
```

Проверить отсутствие нежелательного выхода DNS напрямую через RU/local ISP при ожидаемом foreign egress.

Вывести:

```text
DNS STATUS
PASS / WARNING / FAIL
```

---

# 21. IPv6 testing

Не считать наличие иностранного IPv4 доказательством полной маршрутизации.

Проверить:

```text
IPv4
IPv6
```

Возможные состояния:

```text
IPv4 foreign / IPv6 disabled
PASS

IPv4 foreign / IPv6 foreign
PASS

IPv4 foreign / IPv6 RU/local
FAIL or WARNING depending on user requirements
```

---

# 22. UDP testing

Если используемый протокол или пользовательский трафик предполагает UDP, провести UDP validation.

Не открывать UDP firewall без необходимости.

Статус:

```text
UDP:
PASS
FAIL
N/A
```

---

# 23. MTU/MSS diagnostics

Добавить troubleshooting для случаев:

```text
connection established
but sites partially load
large downloads hang
QUIC fails
```

Проверять при необходимости:

```text
MTU
PMTU
MSS
fragmentation
```

Не изменять MTU автоматически без доказанной причины.

---

# 24. Firewall improvements

Проверять не только локальный firewall.

Разделить:

```text
OS firewall
provider firewall/security group
```

При невозможности проверить provider firewall явно писать:

```text
Provider firewall status: UNKNOWN
```

Не считать порт доступным только потому, что `ufw allow` выполнен.

---

# 25. Port collision detection

Перед созданием inbound:

```bash
ss -lntup
```

Проверить:

- существующие сервисы;
- Xray listeners;
- 3x-ui panel;
- metrics;
- SSH;
- web server;
- Docker proxy.

Если порт занят:

```text
do not kill process
```

Выбрать другой порт или остановиться.

---

# 26. Metrics conflict

Сохранить текущий warning про `127.0.0.1:11111`.

Но переписать его как универсальную проверку:

```text
detect metrics listener dynamically
```

Не считать, что metrics всегда слушает 11111.

---

# 27. Protocol selection

Текущий default сохранить примерно таким:

```text
Hiddify → RU:
VLESS + Reality

RU → foreign:
VLESS
```

Но перед созданием проверить:

- поддержку текущим Xray;
- поддержку Hiddify;
- существующий firewall;
- наличие domain;
- TLS/Reality requirements;
- UDP requirements.

Не использовать протокол только потому, что он перечислен в SKILL.md.

---

# 28. Reality validation

При Reality проверять:

```text
serverName
publicKey
privateKey presence on correct side
shortId
fingerprint
flow
target/dest
```

Не выводить private key.

Добавить sanity checks:

```text
public/private key role mismatch
invalid shortId
wrong SNI
wrong fingerprint
```

---

# 29. Secret handling

Усилить существующие правила.

Redact:

```text
UUID
password
Reality private key
API token
session cookie
subscription URL
full share URI
```

Допускается показывать безопасные части:

```text
host
port
protocol
tag
masked UUID
```

Например:

```text
UUID:
8f23****91ab
```

---

# 30. Не использовать дефолт admin/admin как рабочее предположение

Если где-либо сейчас есть ожидание:

```text
admin/admin
```

оставить это максимум как историческую или Docker-test информацию.

Production workflow не должен предполагать стандартные credentials.

---

# 31. Confirmation workflow

Убрать избыточные остановки.

Ввести понятие:

```text
approved_scope
```

Если пользователь дал разрешение:

```text
настроить каскад полностью
установить недостающее ПО
открыть необходимые порты
изменить конфигурацию
перезапустить сервис при необходимости
```

не спрашивать разрешение повторно для каждого штатного шага.

Дополнительное подтверждение требуется только для:

- destructive deletion;
- удаления неизвестных existing objects;
- изменения SSH;
- отключения firewall;
- замены существующего working inbound;
- reboot;
- потери удаленного доступа;
- rollback с потенциальной потерей новых изменений.

---

# 32. Existing installation preservation

Если 3x-ui уже установлен:

```text
DO NOT reinstall
```

Без доказанной причины.

Сначала:

```text
inspect
reuse
minimal modifications
```

---

# 33. Installation source

Если установка требуется, использовать только официальный источник проекта 3x-ui.

Никаких случайных mirrors и сторонних install scripts.

Перед запуском:

```text
show source
verify repository/project identity
```

---

# 34. Health status model

Ввести итоговые состояния.

```text
HEALTHY
PARTIALLY HEALTHY
FAILED
ROLLED BACK
```

### HEALTHY

Все обязательные tests успешны.

### PARTIALLY HEALTHY

Основной TCP path работает, но, например:

```text
UDP FAIL
IPv6 WARNING
DNS UNKNOWN
```

### FAILED

Основной cascade path не работает.

### ROLLED BACK

Новая конфигурация не прошла validation, старое состояние восстановлено.

---

# 35. Acceptance tests

Перед завершением агент обязан сформировать таблицу.

Пример:

```text
TEST                                         RESULT

SSH RU                                       PASS
SSH FOREIGN                                  PASS
3x-ui RU                                     PASS
3x-ui FOREIGN                                PASS
API RU                                       PASS
API FOREIGN                                  PASS
Foreign inbound                              PASS
Foreign service client                       PASS
RU outbound → foreign                        PASS
RU routing                                   PASS
Hiddify profile target == RU                 PASS
TCP end-to-end                               PASS
External IPv4 == foreign                     PASS
DNS                                          PASS
UDP                                          PASS
IPv6                                         N/A
Rollback capability                          PASS
```

---

# 36. Обязательные critical acceptance tests

Следующие проверки обязаны быть PASS:

```text
Foreign inbound operational
RU outbound reaches foreign
RU routing sends Hiddify traffic to foreign
Hiddify primary target == RU
External IPv4 == expected foreign server/network
Xray healthy on both servers
```

Если любой critical test FAIL:

```text
configuration must not be reported as successful
```

---

# 37. Negative tests

Docker/CI acceptance test должен проверять не только happy path.

Добавить минимум:

```text
occupied foreign port
occupied RU port
invalid foreign UUID
broken routing tag
foreign unreachable
wrong API endpoint
CSRF required
wrong credentials
existing conflicting inbound
Xray config invalid
```

Skill должен выдавать понятную диагностику и не разрушать existing setup.

---

# 38. Re-run test

Обязательный acceptance criterion:

Запустить skill второй раз на уже правильно настроенной системе.

Ожидаемый результат:

```text
no duplicate inbound
no duplicate client
no duplicate outbound
no duplicate routing rule
no unnecessary firewall entries
no unnecessary restart
```

Итог:

```text
NO CHANGES REQUIRED
```

или минимальный update.

---

# 39. Upgrade compatibility test

Проверить сценарий:

```text
3x-ui older supported version
↓
skill works
↓
upgrade 3x-ui
↓
skill rerun
```

Skill должен обнаружить новые capabilities и не ломать рабочий cascade.

---

# 40. Repository structure

Оставить:

```text
README.md
SKILL.md
references/
```

Но расширить references.

Рекомендуемая структура:

```text
README.md
SKILL.md

references/
├── cascade-implementation-notes.md
├── api-compatibility.md
├── troubleshooting.md
├── acceptance-tests.md
└── security-and-rollback.md
```

---

# 41. Что вынести из SKILL.md

SKILL.md не должен разрастаться бесконечно.

В нем оставить:

- core topology;
- operating algorithm;
- safety rules;
- discovery;
- transaction flow;
- acceptance requirements;
- high-level API logic.

В references вынести:

```text
API endpoint matrix
curl examples
Docker commands
long troubleshooting tables
detailed rollback commands
protocol-specific notes
```

---

# 42. README

README переписать так, чтобы новый пользователь за минуту понял:

```text
что это
для чего
какая схема
что требуется
что skill меняет
что skill не меняет
какие версии тестировались
```

Добавить:

```text
Tested with:
3x-ui: ...
Xray: ...
Hiddify: ...
```

Важно:

Не писать:

```text
supports latest
```

без версии.

Писать:

```text
tested against X.Y.Z
```

---

# 43. Compatibility section

Добавить таблицу:

| Component | Tested | Notes |
|---|---|---|
| 3x-ui | версия | modern API |
| Xray-core | версия | tested |
| Hiddify Windows | версия | tested |
| Ubuntu | версии | tested |
| Debian | версии | tested |
| Docker | версия | test environment |

---

# 44. Troubleshooting

Добавить structured troubleshooting.

Например:

```text
SYMPTOM:
Hiddify connects but internet unavailable

CHECK:
RU inbound
RU routing
RU outbound
foreign inbound
foreign service-client credentials
foreign freedom outbound
DNS
```

---

```text
SYMPTOM:
Hiddify connects and IP is RU

LIKELY CAUSE:
traffic not routed to to-foreign outbound
```

---

```text
SYMPTOM:
foreign inbound sees no connection

CHECK:
firewall
provider ACL
wrong port
wrong protocol
wrong Reality parameters
```

---

# 45. Logs

При диагностике собирать:

```text
3x-ui service logs
Xray logs
journalctl
Docker logs
```

Но перед выводом автоматически redaction secrets.

---

# 46. CI / automated tests

Если возможно, добавить GitHub Actions.

Минимальный CI:

```text
Markdown validation
metadata validation
Docker integration test
modern 3x-ui API smoke test
cascade TCP smoke test
rerun/idempotency test
```

Не хранить credentials в repository.

---

# 47. Docker integration test

CI должен автоматически:

```text
create Docker network
start foreign 3x-ui
start RU 3x-ui
configure foreign inbound
configure RU outbound
configure routing
send test traffic
verify exit path
```

Затем повторить configuration phase и убедиться, что дубликаты не создаются.

---

# 48. Future API resilience

Добавить правило:

```text
If a known endpoint returns 404/405 or schema mismatch:
do not immediately assume installation failure.
Re-run API discovery.
```

Это важно для будущих версий 3x-ui.

---

# 49. Fail-safe behavior

Если агент не может уверенно определить:

```text
API schema
existing routing
rollback path
```

он не должен производить destructive изменения.

Разрешены:

```text
read-only discovery
diagnostics
report
```

---

# 50. Definition of Done

Версия `2.0.0` считается готовой только если выполнены все пункты:

- modern `/panel/api/*` поддерживается;
- legacy fallback реализован;
- API capability detection работает;
- официальный Docker image используется;
- Docker version pinned;
- идемпотентность реализована;
- повторный запуск не создает дубликаты;
- backup создается до изменения;
- rollback проверен;
- live apply используется при возможности;
- restart не выполняется без необходимости;
- Hiddify target проверяется;
- foreign exit IP проверяется;
- DNS test присутствует;
- IPv6 test присутствует;
- UDP test присутствует или корректно помечается N/A;
- port collision detection работает;
- existing config не перезаписывается без необходимости;
- secrets redacted;
- acceptance test table формируется;
- failed critical test не может завершиться статусом SUCCESS;
- README обновлен;
- version повышена до `2.0.0`;
- changelog добавлен;
- Docker integration test проходит;
- idempotency test проходит.

---

# 51. Финальный отчет агента

После работы агент должен вывести кратко:

```text
VERSION:
2.0.0

FILES CHANGED:
...

MAIN CHANGES:
...

API COMPATIBILITY:
...

TESTED AGAINST:
...

AUTOMATED TESTS:
PASS/FAIL

IDEMPOTENCY:
PASS/FAIL

ROLLBACK TEST:
PASS/FAIL

KNOWN LIMITATIONS:
...

FINAL STATUS:
READY / NOT READY
```

---

# 52. Запреты

Не:

- удалять существующие inbounds ради удобства;
- переустанавливать рабочий 3x-ui без причины;
- отключать firewall полностью;
- редактировать runtime Xray config напрямую;
- использовать случайные Docker images;
- использовать `latest` как единственный CI target;
- публиковать credentials;
- менять SSH-конфигурацию без необходимости;
- объявлять cascade рабочим только по факту успешного запуска Xray;
- считать foreign IPv4 достаточной проверкой DNS/IPv6;
- выполнять restart просто «на всякий случай».

---

# 53. Рекомендуемая последовательность реализации

## P0

1. API capability detection.
2. `/panel/api/xray/*`.
3. официальный Docker image.
4. pinned Docker version.
5. transactional workflow.
6. rollback.
7. idempotency.
8. live apply.
9. critical acceptance tests.

## P1

10. typed API payloads.
11. Hiddify profile validation.
12. DNS testing.
13. IPv6 testing.
14. UDP testing.
15. OpenAPI-aware behavior.
16. provider firewall awareness.

## P2

17. CI.
18. negative tests.
19. compatibility matrix.
20. advanced troubleshooting.
21. MTU/MSS diagnostics.

---

# Ожидаемый результат

После обновления пользователь должен иметь возможность дать Hermes задачу примерно такого уровня:

```text
Настрой два моих сервера в каскад 3x-ui так, чтобы Hiddify подключался к российскому серверу, а весь интернет-трафик выходил через зарубежный.
```

И агент должен самостоятельно:

```text
обследовать обе системы
↓
определить версию и API 3x-ui
↓
найти существующие объекты
↓
создать backup
↓
спланировать минимальные изменения
↓
настроить foreign
↓
проверить foreign
↓
настроить RU
↓
проверить RU → foreign
↓
создать/проверить Hiddify profile
↓
провести end-to-end tests
↓
выдать health report
```

При повторном запуске:

```text
Existing cascade detected.
Configuration matches desired state.
No destructive changes required.
```

Именно это считать целевым поведением версии `2.0.0`.