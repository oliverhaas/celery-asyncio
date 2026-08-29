# Porting the celery-redis-plus transport fixes into valkey_redis

Source: [celery-redis-plus](https://github.com/oliverhaas/celery-redis-plus), the sync
Redis/Valkey transport this one shares a design with. The two diverged around 2026-02-15;
everything below landed there between then and `6cf029a` (2026-08-22).

celery-redis-plus carries its own `PORT-PLAN.md` for seven of these, written when they were
ported into it from a vendored copy. Each was verified against real code there. This plan
re-verifies them against `kombu/transport/valkey_redis.py` and the four Lua scripts here, and
adds the items that only exist in celery-redis-plus's later history.

## Verification status

Every item marked **Confirmed** was checked by reading both sides. Line numbers are from `main`
at `ac52b681`.

## Order of work

Correctness fixes that change no public API go first. Item 5 depends on 3; item 6 depends on 3,
4 and 5, and must not land before 3 or a slow queue eats its own backlog.

| # | Done | Fix | Consequence today | API change |
|---|---|---|---|---|
| 1 | yes | Ack does not remove the queue entry | duplicate execution | KEYS arity |
| 2 | yes | Delivery with no visibility deadline | silent message loss | none |
| 3 | yes | Backlog counted as a redelivery | wrong counter, feeds 5 and 6 | none |
| 4 | yes | `no_ack` deliveries stay in the index | spurious redelivery of pidbox/reply | ARGV arity |
| 5 | yes | `redelivered` written but never read | celery never sees redeliveries | hash field, headers |
| 6 | no | RabbitMQ naming and `delivery_limit` semantics | reject loops never stop | option, header, default |
| 7 | no | Direct exchange with no bindings loses the message | silent drop | raises to publishers |

Features, deferred until the seven land: 8 to 12.

---

## 1. Acking does not remove the queue entry

**Confirmed.** `transport_ack_message.lua:9-10` does `ZREM` on the index and `DEL` on the hash,
and never touches `queue:{name}`. celery-redis-plus passes the queue key as `KEYS[3]` and `ZREM`s
it there (`transport_ack_message.lua:19`).

The inflated `ZCARD` is not the point. Once `enqueue_due_messages` has restored a message while
its original consumer is still working on it, the tag is back in the queue, the ack cannot undo
that restore, the duplicate stays poppable, and a second worker runs the task.

KEYS arity goes 2 to 3. Check every caller and every test that invokes the script directly.

## 2. A delivery can end up with no visibility deadline

**Confirmed.** `transport_consume_message.lua:58` uses `ZADD ... 'XX'`. When the index entry is
missing, the message is delivered with nothing tracking it: out of the queue, out of the index,
so a worker crash loses it permanently.

The guard buys nothing. The `ZADD` runs only after `HMGET` confirmed the hash exists, and the
script is atomic. celery-redis-plus dropped it at `transport_consume_message.lua:71`.

The slow path (`_slow_consume`, valkey_redis.py:930) needs the same check during the port, where
the argument differs: its `ZADD` shares a non-transactional pipeline with the `HMGET`, so a plain
`ZADD` can write an entry for a message acked a moment earlier. That self-corrects only if the
missing-payload branch actually `ZREM`s it again. Confirm before relying on it.

## 3. A queue backlog is counted as a redelivery

**Confirmed.** `transport_enqueue_due_messages.lua:64-65` increments the counter for every tag
whose deadline passed, without checking whether the tag is still sitting in the queue. A message
that merely waits in a backlog gets counted every cycle.

The `ZADD queue_key 'NX'` on line 86 already reports this: it returns 1 only when the tag was
absent. Move that write above the counter branch and gate the increment, the drop check and
`total_enqueued` on its result, as celery-redis-plus does at
`transport_enqueue_due_messages.lua:73`.

Two things to watch: moving the `ZADD` above the drop check means a message about to be dropped is
added to the queue and then `ZREM`d again, so keep the ordering deliberate; and `total_enqueued`
stops counting backlog re-checks, which existing tests may assert on.

## 4. `no_ack` deliveries are never dequeued

**Confirmed.** `transport_consume_message.lua` has no `no_ack` handling: it always writes a
visibility deadline into the index. Nothing ever acks a `no_ack` message, so the entry leaks and
the next sweep redelivers it. Pidbox and reply queues are the ones that suffer.

celery-redis-plus passes a per-queue flag in a second ARGV block and, when set, `ZREM`s the index
entry and `DEL`s the hash inside the pop (`transport_consume_message.lua:58-64`). The channel
already knows which consumers are `no_ack` (`valkey_redis.py:208`, `693`), so the flag is
available; it has to be threaded through in the same order as `KEYS`.

## 5. `redelivered` is a stored field that nothing reads

**Confirmed write-only.** Written by `_put` (valkey_redis.py:649),
`transport_enqueue_due_messages.lua:79` and `transport_requeue_message.lua:29`. Nothing reads it
back, and the two writers disagree: the enqueue script sets it alongside the counter, the requeue
script sets it alone and never touches the counter.

Celery reads `delivery_info['redelivered']`, and gates `worker_deduplicate_successful_tasks` on
it. It cannot currently see a redelivery from this transport.

- Remove `redelivered` from the `_put` hash mapping.
- `transport_requeue_message.lua`: `HSET redelivered 1` becomes `HINCRBY delivery_count 1`, so
  reject-with-requeue counts like a visibility timeout restore.
- Both consume paths (valkey_redis.py:891, 1412) derive the AMQP flag from the counter via one
  shared helper rather than each doing its own thing.

## 6. RabbitMQ naming and semantics for the delivery limit

Renames:

| Before | After |
|---|---|
| `restore_count` (hash field, Lua local) | `delivery_count` |
| `x-restore-count` (header) | `x-delivery-count` |
| `max_restore_count` (transport option) | `delivery_limit` |

The concept maps onto RabbitMQ quorum queues' `x-delivery-count` header and `delivery-limit`
policy: RabbitMQ increments on a message returned to the queue because its consumer went away,
and on `basic.nack`/`reject` with `requeue=true`. Those are exactly the two Lua scripts here, and
after item 5 both increment.

Three corrections come with the rename:

- **Header placement.** `valkey_redis.py:897` and `:986` write `x-restore-count` into
  `properties["headers"]`. kombu rebuilds `Message.headers` from the payload's top-level
  `headers`, so nothing ever sees it. It has to move.
- **Off by one.** `transport_enqueue_due_messages.lua:68` compares `restore_count >
  max_restore_count`, so a limit of 3 allows four attempts. Move to `>=` so the limit counts
  attempts, as in RabbitMQ.
- **Default.** Currently `None`, so the drop branch is dead. RabbitMQ quorum queues have applied
  `20` since 4.0.

The default change is the one item here that is a decision, not a port: it makes a dead branch
live and deletes messages. It must not land before item 3.

`transport_requeue_message.lua` also has to enforce the limit itself
(celery-redis-plus `transport_requeue_message.lua:68`). The sweep cannot catch a live reject
loop, because every consume re-stamps the index deadline and the entry never comes due there.
Without it a consumer rejecting in a tight loop spins forever.

## 7. Publishing to a direct exchange with no bindings loses the message

**Confirmed applicable.** There is no `_lookup` override here, so kombu's
`virtual.Channel._lookup` applies: an empty table yields `R = []` and the publish is discarded.
kombu made that change deliberately in PR #1404, because an empty table is the normal AMQP state
for an exchange whose queues were all unbound.

That reasoning holds for topic and fanout. It does not hold for direct, where the binding is
known to exist, and it stops holding entirely once binding keys carry a TTL (item 8).

Raise `InconsistencyError` in `_lookup`, not in `get_table`: `exchange_delete`, `queue_unbind`
and `list_bindings` also call `get_table` and would throw during teardown. `InconsistencyError`
is already in this transport's `connection_errors` (valkey_redis.py:1523), so kombu's
`Connection.ensure` reconnects, redeclares and retries, and `Mailbox._publish_reply` already
catches it, so pidbox is exempt for free.

---

## Features, after the seven

### 8. `queue_expires`

Global queue TTL: abandoned queues, their index and their fanout streams age out instead of
accumulating. Needs the binding table moved from a plain SET to a sorted set scored with the
staleness deadline, which is what celery-redis-plus's fifth Lua script
(`transport_convert_bindings.lua`) migrates in place. Needs an async refresh timer here.

Note the bug celery-redis-plus found in its own version: the refresh timer never started, because
it no-ops while the loop is unset and nothing re-ran it after `register_with_event_loop`. Do not
reproduce that.

### 9. Sweep reporting

`enqueue_due_messages` returns `{enqueued, dropped}` (valkey_redis.py:1308). celery-redis-plus
returns a five-element `SweepStats`: enqueued, dropped, redelivered, orphaned, and the payloads
of dropped messages, since the `DEL` is their last trace and they are worth logging.

### 10. Fanout binding cleanup

Fanout bindings are already kept out of the binding table here (valkey_redis.py:437). Missing is
the cleanup celery-redis-plus does on first declare, which deletes binding keys left by older
versions.

### 11. The queue a message was consumed from

celery-redis-plus stamps the popped queue into `delivery_info["queue"]` and resolves ack, requeue
and heartbeat against it, because `routing_key` is the publish-time key and only names the queue
under default direct routing.

Here the Lua scripts read `routing_key` out of the message hash and build the queue key from it
(`transport_requeue_message.lua:51`, `transport_enqueue_due_messages.lua:85`), so the shape of the
problem differs. **Investigate before porting**, since this may or may not be reachable here.

### 12. Per-channel `blocking_timeout` snapshot

celery-redis-plus `c2b5f1d`. Low priority; this transport uses a transport-level block timeout,
so it may not apply.

---

## Release notes to write

- `delivery_limit` defaults to `20` and deletes the message when reached. Previously unlimited.
  Old behaviour is `delivery_limit: None`.
- Hash field `restore_count` became `delivery_count`. A message published by the old version and
  consumed by the new one reads `nil` and restarts at 0, so the limit is not retroactive across
  an upgrade.
- The `redelivered` hash field is no longer written. Leftovers on old messages are ignored.
- Header `x-restore-count` became `x-delivery-count`, and moved from `properties["headers"]` to
  the payload's top-level `headers`, where it is actually visible.
- `delivery_info['redelivered']` is now set on redeliveries, so
  `worker_deduplicate_successful_tasks` can work.
- `transport_ack_message.lua` takes three KEYS instead of two.
- `_lookup` can raise `InconsistencyError` to publishers that name a direct exchange.
