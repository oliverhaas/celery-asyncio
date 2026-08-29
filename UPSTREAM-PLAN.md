# Tracking upstream kombu

This fork branched from [celery/kombu](https://github.com/celery/kombu) at `279b81f3`
(v5.6.2, 2025-12-29). Upstream is at `74f2d251` (2026-08-26), 109 non-merge commits later.

This file records every one of those commits that was looked at and what was decided, so the
next sweep starts from `74f2d251` and nothing gets re-triaged from scratch. It is not a list of
work to do. The open items are collected at the bottom.

## How to read a verdict

| Verdict | Meaning |
|---|---|
| **Ported** | Landed here, either verbatim or adapted. Commit named. |
| **Already handled** | The fork already does this, by its own route. Location named. |
| **Not applicable** | The code, subsystem or dependency does not exist here. |
| **Open** | Real and missing. Listed again under "Still open". |

The fork is a stripped async rewrite, so "not applicable" is the common answer. What is missing,
and therefore what whole classes of upstream fix cannot land:

- no `virtual/` package. `valkey_redis.Channel` is standalone, not a `virtual.Channel`
  subclass, and there is no `QoS`, no `FairCycle`, no `get_table`, no `_lookup`
- no `kombu/transport/redis.py`. The Redis transport here is a separate implementation with a
  ZSET-per-queue layout, Lua scripts and a visibility-timeout index, not ack emulation with an
  unacked key
- no `utils/eventio.py`, no fd poller, no `on_tick`, no `register_with_event_loop`
- no gevent or eventlet, no prefork
- no pyamqp / librabbitmq / SQS / SNS / GCP Pub/Sub / Azure Service Bus / Azure Storage Queue /
  etcd / MongoDB / Zookeeper / Consul transports
- no `Exchange.Message`
- AMQP is aio-pika, not py-amqp, so exception shapes differ

## Re-running the sweep

```
git -C ../../celery/kombu fetch origin
git -C ../../celery/kombu log --oneline --no-merges 74f2d251..origin/main
```

Then drop anything that only touches the transports, docs or CI listed above.

---

## Ported

| Upstream | Landed as | Note |
|---|---|---|
| `3a6f84f1` Add missing `reprkwargs` export | `36362104` | Verbatim. A test now asserts every name in `kombu.utils.__all__` imports. |
| `648bfd45` `ChannelPromise`: keep the `AttributeError` out of the traceback | `36362104` | Verbatim. |
| `900fd2d3` `Logwrapped` does not support the context-manager protocol | `36362104` | Adapted: forwards `__aenter__`/`__aexit__` too, since almost everything worth wrapping here is an async context manager. |
| `883a33cc` Use `compression.zstd` (PEP 784) | `36362104` | Verbatim, and the third-party `zstandard` dependency is gone. Its tests were `importorskip("zstandard")`, which had been skipping silently forever. |
| `77a5dee8` Defer `_reset_cycle` until after updating `_active_queue` | `2531da11` | Adapted. Same bug, different mechanism: a `basic_cancel` landing while a consume iteration is blocked in Redis pops a message nobody is listening for. It is now put back instead of left invisible until the visibility timeout, which would also have counted a redelivery against `delivery_limit`. |
| `9d096dd0` Preserve queue ordering in `active_queues` | `2531da11` | Adapted. The sweep deduped its queue list through a set, so it visited queues in hash order and which backlog got restored first under the batch limit differed between two identically configured workers. |
| `3b7bc66e` Ignore errors restoring a message that was acked | `db8b133d` | Adapted. The `close()` requeue loop iterates a snapshot across awaits while `basic_ack` runs on the same loop, so it now skips tags acked mid-drain rather than relying on the Lua script's missing-hash guard. |
| `9bece764` RabbitMQ 4.3.0 compatibility | `11c15244` | Adapted. Mailbox queues are exclusive by default. Two departures: the fork's own `queue_exclusive`/`queue_durable` conflict check means the default is a sentinel, so asking only for durability still works; and the 405 translation lives in `Node.listen()` rather than `Node.Consumer`, because nothing here touches the broker until `consume()` runs the declare. It matches on exception type via the new `Transport.resource_locked_errors`, because aiormq raises a class per reply code and carries no `code` attribute the way py-amqp does. |
| `9ee8595b` Make the Redis ack-emulation restore cadence configurable | `2a17dead` | Adapted. No ack emulation here, but the same complaint applied: the sweep ran on a hard-coded 60s that is also the grace margin on every visibility deadline, so `visibility_timeout` could not make a restore happen sooner. Now the `requeue_check_interval` transport option. |
| `92d2a5ce` Use logging instead of printing in `emergency_dump_state` | `ab6899e6` | Verbatim, plus a fork-only bug it uncovered: the pformat fallback wrote `str` to a binary file and raised `TypeError`, losing the state the dump exists to preserve. |

## Already handled

| Upstream | Where the fork handles it |
|---|---|
| `33d4eba8` Pass through Redis `credential_provider` from `connection_kwargs` | `valkey_redis.py` `_process_credential_provider()`, which also accepts a dotted import path. |
| `0a249e53` Deliver rotating `StreamingCredentialProvider` tokens to long-lived connections | Same. There are no long-lived `BRPOP`/pub-sub connections here: consume iterations are bounded by `block_timeout` and fanout is Redis Streams. |
| `9642b215` Use logger instead of print when restoring unacked messages | The restore paths already log. |
| `4a6c9371` Implement the missing `filesystem.Channel._delete()` | `filesystem.py` `queue_delete()` already strips the queue from every exchange's binding list and persists them. |
| `2c8372c6` Redis timer connection error | All three periodic loops (`_periodic_enqueue_due`, `_periodic_heartbeat`, `_periodic_refresh_expires`) already catch `Exception`, log and continue, and are cancelled on close. |
| `5cbdaf97` Support Redis queue expiration | Mostly present already, see PORT-PLAN item 8. `_expires`, the `MIN_QUEUE_EXPIRES` clamp, `PEXPIRE` on both `queue:{name}` and `messages_index:{name}`, and a refresh timer at TTL/2. Upstream's priority-fan-out `PEXPIRE` loop has no analogue: priority lives in the ZSET score here, not in separate keys. What is genuinely missing is tracked in PORT-PLAN. |
| `df369336` / `781576a3` / `b47b680f` dependency ceilings | The extras here declare floors with no upper bound (`redis>=7.1.0`, `valkey>=6.1.0`, bare `msgpack`), so newer majors are already allowed. Upstream's exact pins are a distribution choice this fork does not share. |

## Not applicable

Grouped by what is missing, so a future sweep can classify by inspection.

**No `virtual.Channel` / `QoS` / fd poller / `on_tick`**
`4281680e` `KeyError(<fileno>)` in `on_readable`/`handle_event` ·
`6a37089d` do not remove `on_poll_start` from `on_tick` on disconnect ·
`13d746b2` allow `OSError` to propagate to the errno handler in `unregister()` ·
`8f830add` RabbitMQ 4.x global QoS not supported on classic queues

**Transport does not exist here**
`2415b0cb`, `3c5c1bd8`, `6cc2228f`, `860e40a6`, `f94ccd41`, `6b503e11` (SQS/SNS) ·
`f20e26e2`, `41ea84a6`, `0cda6727`, `2c1670b5`, `339fb58d` (GCP Pub/Sub) ·
`f304ece1`, `fb80bd17`, `a731e629`, `2571fefe` (Azure Service Bus) ·
`817ebae4` prefixed native delayed delivery (py-amqp)

**No gevent/eventlet**
`82943304`, `156e003e`, `ec17b2b2`

**Feature absent by design**
`a96f06e7` honor `delivery_mode` in `Exchange.Message`. There is no `Exchange.Message` ·
`74f2d251` transport-aware batch publishing with Redis pipelines. There is no `Producer.batch()`
layer; `Producer.publish()` awaits `channel.publish()` directly. Worth revisiting as a feature,
but it is a new API, not a port ·
`243ee08b` callable passwords for credential refresh. aio-pika takes the URL and parses
credentials itself, so there is no interception point ·
`d5f16d59` cycle through host before calling errback. `Connection` here is one immutable URL,
with no multi-host `amqp://A;amqp://B` parsing and no `maybe_switch_next()` ·
`671edd3b` type annotation for `_transport_cache`. There is no transport cache; `transport/__init__.py`
resolves through `TRANSPORT_ALIASES` ·
`95d10c35` memoryview/bytearray in the YAML decoder. `yaml.safe_load` is registered directly
with no wrapper, and every path into it carries `bytes`

**Docs, CI, dependency bumps**
All remaining commits: `bb4c7755`, `1b286a12`, `7a83ebd7`, `3cbc3c93`, `e66e4c6e`, `324fb517`,
`3a414834`, `032513a9`, `36a0c667`, `a7b5ba88`, `57917550`, `06b98e10`, `28c6032a`, `52b6605a`,
`9bcdbcf3`, `76eef9c9`, `b9fc806d`, `e87dde58`, `9c11e3d8`, `67556eee`, `0689d4fe`, `92006e70`,
`383e4652`, `f3c88199`, `b3c3a575`, `827bbffc`, `6a9996fb`, `e1bf346c`, `2d512b77`.

---

## Still open

Nothing from this sweep is left unported. Two items it surfaced that are not upstream ports:

1. **`python_classes` hides 67 tests.** pytest's default is `Test*`, but the test suite inherited
   kombu's `class test_*` convention, so those groups are never collected: 261 tests collected
   against 328 with `-o python_classes="test_*"`. Enabling it gives 7 failures and 25 errors,
   mostly `TypeError: Can't instantiate abstract class` in `test_entity.py`, `test_message.py`
   and `test_common.py`. Needs its own pass; do not just flip the setting.

2. **Queue TTL is not refreshed on the consume path.** Upstream refreshes on every poll,
   including empty ones. Here the refresh timer lives on the `Channel`, so a queue only stays
   alive while some process holds the channel that declared it. Recorded as a design difference,
   not a defect, but it is the kind of thing that surprises people.
