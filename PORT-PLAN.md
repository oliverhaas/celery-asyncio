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
| 6 | yes | RabbitMQ naming and `delivery_limit` semantics | reject loops never stop | option, header, default |
| 7 | yes | Direct exchange with no bindings loses the message | silent drop | raises to publishers |

Features, deferred until the seven land: 8 to 12. Item 8 turned out to be mostly present
already; what is left of it, plus 10, are the open items.

| # | Done | Feature |
|---|---|---|
| 8 | partly | `queue_expires`: per-queue TTL is in, fanout streams and binding keys are not |
| 9 | yes | Sweep reporting |
| 10 | no | Fanout binding cleanup |
| 11 | n/a | Closed after investigation, nothing to port |
| 12 | n/a | Closed after investigation, nothing to port |

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

Corrections that come with the rename:

- **Header placement.** Does not apply here, unlike in celery-redis-plus. Both consume paths
  already wrote into the payload's top-level `headers`, which is what `_create_message` builds
  `Message.headers` from, so the header did reach consumers.
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

**Confirmed applicable**, though by a different route than in celery-redis-plus. This channel does
not subclass `virtual.Channel` and has no `_lookup`; the equivalent code is `_direct_publish`
(valkey_redis.py:570), which loops over an empty binding list and returns. Same outcome as kombu's
`virtual.Channel._lookup`, which yields `R = []` and discards the publish. kombu made that change
deliberately in PR #1404, because an empty table is the normal AMQP state for an exchange whose
queues were all unbound.

That reasoning holds for topic and fanout. It does not hold for direct, where the binding is
known to exist. It would stop holding entirely if binding keys ever carried a TTL, because then
an empty table would also be what an expired exchange looks like. They do not carry one today,
see item 8b.

Raise `InconsistencyError` in `_direct_publish`, not in `_load_bindings`: `queue_unbind` and
`queue_delete` also read the binding set and would throw during teardown. That is the same
reasoning behind celery-redis-plus raising in `_lookup` rather than `get_table`.
`InconsistencyError` subclasses kombu's `ConnectionError` and so is already covered by this
transport's `connection_errors`, which means `Connection.ensure` reconnects, redeclares and
retries. `Mailbox._publish_reply` (pidbox.py:300) already catches it, so pidbox is exempt for
free.

---

## Features, after the seven

### 8. `queue_expires`

The goal: abandoned queues, their index, their fanout streams and their bindings age out instead
of accumulating forever.

**8a. Per-queue TTL. Done, it predates this plan.** `x-expires` is read in `queue_declare`
(valkey_redis.py:428), clamped to `MIN_QUEUE_EXPIRES` (10s) with a once-per-process warning, and
`PEXPIRE`d onto both `queue:{name}` and `messages_index:{name}` on every publish (726) and from
`_periodic_refresh_expires`, which runs at half the smallest configured TTL (1427). `db8b133d`
fixed the one real defect: a redeclare with a changed or dropped TTL was ignored, so first
declare won forever.

celery-redis-plus's own version had the refresh timer never start, because it no-ops while the
loop is unset and nothing re-ran it after `register_with_event_loop`. That cannot happen here.
`_update_expires_task` (1444) is called from `queue_declare` whenever the value changes, and
there is a running loop by then.

**8b. Fanout streams and binding keys. Open.** `_fanout_publish` (636) bounds the stream with
`maxlen` only, so an exchange nobody consumes from keeps a trimmed stream alive indefinitely.
`_binding_key(exchange)` is a plain SET that only disappears in `exchange_delete` (412).
celery-redis-plus moves the binding table to a sorted set scored with the staleness deadline, and
migrates existing keys in place with a fifth Lua script (`transport_convert_bindings.lua`).

Two things to settle before porting that half: a binding table that expires changes what "no
bindings" means for item 7, which currently reads it as a misconfiguration and raises; and the
stream TTL has to outlive the longest consumer gap, not the queue TTL, since a fanout consumer
that reconnects after the stream expired silently loses its offset.

### 9. Sweep reporting

**Ported.** `_enqueue_due_messages` used to return `{enqueued, dropped}` and log two lines about
the totals. It now returns a `SweepStats` of enqueued, dropped, redelivered and orphaned, and the
Lua script hands back the payloads of up to `DROPPED_REPORT_LIMIT` dropped messages so the error
log can name the tasks: the `DEL` is their last trace, and "5 messages dropped" on its own does
not say which. Every line is per queue now rather than per sweep, so a queue with a poison
message is identifiable. A queue whose script call raises is logged and skipped instead of
costing the remaining queues their sweep.

### 10. Fanout binding cleanup

Fanout bindings are already kept out of the binding table here (valkey_redis.py:437). Missing is
the cleanup celery-redis-plus does on first declare, which deletes binding keys left by older
versions.

### 11. The queue a message was consumed from (closed, no port needed)

celery-redis-plus stamps the popped queue into `delivery_info["queue"]` and resolves ack, requeue
and heartbeat against it, because `routing_key` is the publish-time key and only names the queue
under default direct routing.

**Investigated: not reachable here, nothing to port.** Two independent reasons.

`_put_message` stores `"routing_key": queue` (valkey_redis.py:663), and its three callers all pass
a real queue name: `_direct_publish` and `_topic_publish` pass the queue resolved from the binding
table, and the default-exchange branch passes the routing key, which under default direct routing
*is* the queue name. So the hash field the Lua scripts read is the queue, not the publish-time key,
despite its name.

Separately, the Python paths never consult `delivery_info` for a queue. `basic_ack` (1205),
`basic_reject` (1229), `_update_messages_index` (1418) and `close` (1527) all read
`self._delivered[tag]`, which was stamped with the popped queue at consume time.

The `routing_key` hash field is misnamed for what it holds. Renaming it to `queue` would be
honest, but it is a stored-format change for no behaviour gain, so not now.

### 12. Per-channel `blocking_timeout` snapshot (closed, no port needed)

**Investigated: does not apply, nothing to port.** celery-redis-plus `c2b5f1d` fixed two things:
`self.connection.blocking_timeout or 1` coerced a configured `0` to `1`, and `close()` drained a
pending poll that, with `0` meaning block-forever, never returned.

Neither exists here. `_block_timeout` is read once per channel from transport options
(valkey_redis.py:265) with no `or` coercion, and `close()` bounds its drain with
`asyncio.wait_for` and cancels on expiry (1490, 1508) rather than issuing another blocking read.

One difference worth recording, though it is not celery-redis-plus's bug: `0` means something
else here. `_consume_regular` treats `timeout == 0` as "FAST poll only, do not fall through to
BZMPOP" (valkey_redis.py:869), so `block_timeout: 0` makes the consumer loop spin instead of
blocking forever, and the error backoff `sleep(min(self._block_timeout, 0.1))` becomes `sleep(0)`.
There is no way to express block-forever. Either reject `0` at channel init or document it.

---

## Release notes to write

- `delivery_limit` defaults to `20` and deletes the message when reached. Previously unlimited.
  Old behaviour is `delivery_limit: None`.
- A reject with `requeue=True` now counts against `delivery_limit` and drops the message when it
  is reached. Previously a consumer rejecting in a tight loop spun forever.
- Hash field `restore_count` became `delivery_count`. A message published by the old version and
  consumed by the new one reads `nil` and restarts at 0, so the limit is not retroactive across
  an upgrade.
- The `redelivered` hash field is no longer written. Leftovers on old messages are ignored.
- Header `x-restore-count` became `x-delivery-count`. Its placement did not change.
- `delivery_info['redelivered']` is now set on every delivery, `True` on redeliveries, so
  `worker_deduplicate_successful_tasks` can work.
- `transport_ack_message.lua` takes three KEYS instead of two, and
  `transport_consume_message.lua` takes a second ARGV block of per-queue `no_ack` flags.
- A `no_ack` delivery no longer leaves an index entry or a message hash behind, so pidbox and
  reply queues stop seeing spurious redeliveries.
- Publishing to a named direct exchange with no bindings raises `InconsistencyError` instead of
  silently discarding the message. Topic and fanout are unchanged.
