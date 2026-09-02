# Changelog

## v6.0.0a6

A production-readiness audit of the whole package. Every module in `celery` and
`kombu` was read through; this release is what that turned up, plus the removal
of the code and settings nothing calls any more.

### Fixed

#### Worker and pool

- A broken broker socket ended the consumer loop quietly, and the worker then
  started a second Heart, Tasks and Evloop on top of the ones still running
  while the dead connection stayed open. The error now reaches the
  recoverable-error handler, which stops the running steps before starting them
  again
- Every task with an ETA raised the prefetch count and nothing ever lowered it
  again, so a worker that had scheduled a hundred ETA tasks over its lifetime
  was a hundred prefetch slots short. The consumer loop now sends a changed
  count to the broker once per pass
- A worker restarting for `worker_max_tasks_per_child` or
  `worker_max_memory_per_child` handed its prefetched tasks back to the broker
  while the pool was still going to run them, so a batch of tasks ran twice:
  once here and once on the worker that got the redelivery. The drain keeps them
  now and waits for the tasks in hand to finish
- An exception that escaped the async tracer, from message decoding, an
  unregistered task or the tracer itself, was reported to nobody: the pool
  logged nothing, the task stayed `PENDING` and `get()` blocked until it timed
  out. Such an error is now reported through the request's failure path, which
  stores `FAILURE`, sends `task_failure` and logs the traceback
- Revoking or cancelling a running async task did nothing. The pool handed the
  worker a result object with no handle on the coroutine, so `terminate()` and
  `cancel()` were silent no-ops and `terminate_job()` was not implemented at
  all. A revoked task ran to completion and its own success overwrote the
  `REVOKED` state with `SUCCESS`, and a task cancelled on connection loss kept
  running. The pool now keeps the asyncio task behind every async job and
  cancels it on its own loop thread
- A task's soft time limit was reported through the success path, which stores
  nothing, so the result stayed `PENDING` and no `task_failure` was sent. The
  soft limit now fails the task the way any other exception does, with the
  state, the signal and the log entry that go with it
- A hard time limit that followed a soft one was mistaken for the soft limit
  that had already fired, so `on_timeout` never ran and a task that swallowed
  its soft limit was never reported at all. The two limits now act on different
  tasks and cannot be confused
- The soft time limit for a sync task was thrown into a thread by id without
  checking that the thread was still running the task it was meant for, so a
  limit that fired late landed in an unrelated task, or in an idle pool thread
  where it was never delivered and stayed pending for whatever ran next. The
  injection is now bound to the run it belongs to, and any undelivered exception
  is cleared when the task ends
- `SystemExit` or `KeyboardInterrupt` raised by an async task, `WorkerShutdown`
  and `WorkerTerminate` included, was re-raised by asyncio into the loop that
  ran it and closed it. The loop worker thread died with every task on it, and
  every later task sent to that worker raised `Event loop is closed`. Both are
  now contained in the task that raised them and reported as `WorkerLostError`,
  and a shutdown request reaches the worker thread that can act on it
- The `cancel_consumer` control command reported a queue as cancelled while the
  worker kept consuming from it
- `celery worker --purge` died at startup with "Cannot block on the background
  loop from inside a running event loop"
- A cold shutdown joined the pool's threads on the worker's event loop, so a
  worker with several loop workers and a long-running sync task could spend a
  minute answering nothing: no heartbeats, no control commands, no task
  callbacks
- Acks and rejects sent from the consumer's synchronous callbacks were tasks the
  loop held only weakly, so handing a message back to the broker was at the
  mercy of a garbage collection, and a failure in one of them was never reported
- The event dispatcher's old connection was closed by a task nobody kept, so a
  worker that reconnected often left connections open on the broker
- Cancelling the task consumer, closing the broker connection and cancelling the
  pidbox consumer each caught every exception on the way out, two of them
  without a word in the log, so a shutdown that failed for a reason other than a
  broker that had already gone looked exactly like a clean one
- A failure raised inside the consumer's own `close()` was swallowed as if the
  worker had no consumer at all
- A second interrupt arriving after the consumer was torn down raised
  `AttributeError` from inside the signal handler instead of escalating the
  shutdown
- The memory limit could not fire on a platform without `/proc` where importing
  `resource` fails, because the reader reported 0 KiB for its own failure
- Every `MainProcess` check in the worker was false, because the single-process
  stand-in for `multiprocessing.current_process()` never set its name. Warm and
  cold shutdown sent no `worker_shutting_down` signal, printed no shutdown
  banner and installed no second-Ctrl+C cold shutdown handler. The stand-in now
  reports `MainProcess` with index 0, the way `multiprocessing` describes a
  program that never forked
- `celery worker --detach` died in the daemonized grandchild. Closing the
  inherited descriptors raised `AttributeError` on the list of descriptors to
  keep, which every caller passes, and the daemon context passes the standard
  streams themselves rather than their numbers. Both forms are accepted now,
  only `EBADF` is ignored while closing so a real failure is no longer hidden,
  and the null device duplicated over the standard streams is closed again
  instead of leaking
- The node-name substitutions `%i` and `%I` read a process attribute that did
  not exist, so `-n w%i@host` silently produced `w0@host` from a fallback rather
  than from the process index. There is one process, so `%i` is `0` and `%I` is
  empty by design, and that is now what they expand from
- `celery beat` hung on shutdown when the scheduler could not be built. A bad
  `--scheduler` or a failing `setup_schedule` escaped before the service marked
  itself stopped, so `stop()` waited forever and took worker shutdown with it.
  Closing no longer builds a second scheduler either, which used to mask the
  original error, and the embedded thread logs what went wrong before it dies
- The worker's SIGUSR1 stack dump printed a `LOCAL VARIABLES` heading and a
  separator with nothing under them. The frame locals are printed again

#### Tasks and canvas

- `self.request` inside a sync task body was blank: no `id`, no `retries`, and
  `called_directly` still true. The worker runs sync task bodies in a thread
  through `sync_to_async`, and the request stack was a `threading.local`, so
  nothing the trace pushed was visible there. Most visibly this made
  `self.retry()` take its "called directly" branch and re-raise without ever
  publishing the retry, so a retried task hung in the `RETRY` state forever. The
  stack is now backed by a `ContextVar`, which `sync_to_async` carries into the
  thread
- `self.retry()`, `self.replace()` and `self.add_to_chord()` raised or published
  nothing from an async task body, and `autoretry_for` was ignored on async
  bodies entirely: wrapping the call to a coroutine function in `try/except`
  catches nothing, because calling it only builds the coroutine. Async bodies
  now retry, replace and extend a chord through the awaitable twins
- A `pydantic=True` task with an async body returned the coroutine instead of
  awaiting it
- `Task.send_event()` dropped the event when called from a task body, and from a
  sync body it blocked on the loop it was already standing on
- `Task.__call__` popped the request before the coroutine it returned had run,
  so an async body saw an empty `self.request`
- The async tracer ignored a task's custom `__call__`, reported a `RuntimeError`
  from the task as an internal error, and ran the body without the caller's
  context
- Task messages were published on whatever loop `async_to_sync` had just built,
  one connection per message, and the queue they were routed to was never
  declared, so a task published before the worker had ever run went to a queue
  the broker did not have. Messages now go out on the caller's loop, over that
  loop's connection, and declare their queue
- A connection cached for a loop that then closed left a socket nothing could
  close afterwards, one per `asyncio.run()`. A loop is now handed its connection
  back while it can still close it
- `send_task(connection=…)` and `send_task(producer=…)` were ignored on the sync
  path, which published on the app's own connection instead
- `group.apply_async(producer=…)` published with a producer of the app's
  choosing: `producer_or_acquire()` yielded `None` whatever it was given, and
  callers rebound their producer to its result. The async group ignored the
  argument outright
- A task with a `countdown` or an `eta` was published to the default queue on a
  quorum setup, and the eta rode along in a header no transport reads. Both now
  go out with the eta header on the queue the task routes to
- An `eta` header that is not a time was swallowed, and the task published for
  immediate delivery. It now raises where it is published
- A chord whose body was itself a chord never reported the header's failure. A
  chord's `task_id` option is its *header group's* id, since `freeze` assigns
  `self.id = self.tasks.id`, so `chord_error_from_stack` stored the error under
  a key nobody reads, while the result the caller was handed, the innermost
  body's, stayed `PENDING` and `get()` blocked until it timed out. The error now
  walks down to the body, and the inner header's members, which will never run
  either, are failed alongside it
- A chord header built from a generator was unrolled completely before any of
  its tasks were published, so the header could not be produced incrementally
  (upstream #3021). `_apply_tasks` had materialised the header to write
  `set_chord_size` before the first dispatch. It now looks one task ahead and
  writes the size before the last dispatch instead, so the final part return
  still always sees the size, without draining the generator
- A group nested inside another group froze its children in reverse, so a chord
  body received `group(a, group(b, c))` as `[a, c, b]`
- `group.apply_async(task_id="X")` returned a `GroupResult` with a random id
  under `task_always_eager`; the eager branch forwarded already-prepared options
  to `apply()`, which prepared them a second time and invented a new group id
- Group callbacks never ran. `promise(fun, weak=True)` wrapped bound methods in
  a plain weak reference, which a bound method cannot survive, so the callback
  was dead before first use; `barrier` marked itself ready on the first arrival
  and fired on `finalize()` having received none; a promise built with default
  arguments ignored them when called with none, so `ResultSet.on_ready` handed
  its callbacks `None` instead of the result set; and a result set only fired
  `on_ready` for async backends, so an eager group's `then()` callbacks never
  ran at all
- Exceptions raised inside a promise callback were dropped without a trace when
  no error handler was registered. They are logged now

#### Result backends

- Every async task failed outright when the result backend was disabled.
  `DisabledBackend` overrode the sync storage hooks but not their async twins or
  the abstract ones, so `astore_result` fell through to the base class and
  raised `AttributeError` from inside the tracer instead of quietly discarding
  the result. Reading a result or a group without a backend now reports the same
  "no result backend" message the sync path gives
- The async Redis backend saved and restored groups, and built its client,
  through the synchronous client library. A `rediss://` or `socket://` URL put a
  blocking `SSLConnection` or `UnixDomainSocketConnection` into an async pool,
  which connected with blocking socket code and then failed on its first
  command. The connection class is now swapped for its async twin, and a missing
  twin is reported as a configuration error rather than a crash
- `RedisBackend.on_task_call` returned `None` where the caller unpacks the
  result with `**`, so `Signature.election()` raised `TypeError`
- The async Redis wait ignored `timeout` and `on_message` and could overshoot a
  deadline by a full poll interval. It now trims the last sleep to whatever is
  left of the timeout
- `_astore_result` refused to overwrite a stored `FAILURE` or `REVOKED`, so a
  task that succeeded on retry stayed failed. Only `SUCCESS` is sticky now,
  decided in Python and enforced in one round trip by a Lua script, and a
  dropped duplicate write is logged with both states
- The filesystem backend computed its expiry cutoff from a naive epoch, so with
  the app on any timezone but UTC the subtraction shifted by the offset and
  deleted results that were still fresh
- `wait_for_pending()` and `iter_native()` took an `interval` argument and threw
  it away, always polling at `timeout / 20`. `iter_native()` also raised
  `TimeoutError` after it had already collected every result
- A revoke that arrived after the task had finished overwrote the result with
  `REVOKED`. `Control.revoke` reaches the worker over the broker, so the write
  lands whenever it lands; a chord header failure with a group body revoked and
  failed the body tasks in one breath, and `get()` raised `TaskRevokedError`
  instead of the error that had actually stopped the chord. A write of
  `REVOKED` now loses to any finished state, in the same check-then-set that
  already made `SUCCESS` sticky
- `EagerResult` overrode `forget()` and `revoke()` but inherited `aforget()` and
  `arevoke()`, which assigned to a read-only property and reached for an app an
  eager result does not have. Both raised `AttributeError`, which broke every
  await of them under `task_always_eager`

#### Broker connections

- `Mailbox._collect` waited for replies until `drain_events` timed out, which on
  a channel shared with a busy consumer it never does: it returns for as long as
  messages keep arriving. With no reply limit nothing ended the loop, so
  `inspect` and any other `broadcast(reply=True)` blocked on a set of replies
  that was already complete. The `timeout` is now the window it says it is
- The shared loop was stopped and closed with whatever was still running on it,
  so a transport's background tasks, among them consumer iterations, heartbeats
  and expiry refreshes, were reported as `Task was destroyed but it is pending!`
  at interpreter exit, sometimes trailed by a `no running event loop` traceback
  from the coroutine's next `await`. It now performs the same shutdown
  `asyncio.run` does
- `Connection.connection_errors`, `channel_errors` and `resource_locked_errors`
  answered with a generic default until something had been connected, which is
  exactly when they are asked, since the caller is usually about to connect or
  has just been disconnected. Against an unreachable AMQP broker the worker's
  recoverable-error handler therefore never saw aiormq's `AMQPConnectionError`
  and died with an unhandled traceback instead of the shutdown it was supposed
  to report. The tuples now come from the transport class, which does not
  require an instance
- `with Connection(...)` opened the connection on a throwaway event loop and
  closed it on another, so leaving the block raised `RuntimeError: Event loop is
  closed`. The sync context manager, `Control.purge()`, `Control.broadcast()`
  and `app.events.default_dispatcher()` now share one long-lived loop, which
  also unbreaks Flower and `celery -A app worker --purge`
- `with Connection(...)` raised from inside a running event loop, which is where
  Flower's tornado request handlers call it, so Flower's pages 500'd
- The sync `Connection` context manager entered from a coroutine running on the
  shared loop deadlocked instead of saying so
- `ensure_connection` retried on any exception at all, so a transport option
  that does not exist, which raises `TypeError` before a socket is opened, was
  retried forever behind one warning per attempt instead of being reported. It
  now retries what the transport calls a connection error, plus `OSError` for
  the socket and DNS failures underneath, and raises everything else
- Two concurrent first callers of `Connection.default_channel` each opened a
  channel, and one was left registered on the broker with nothing able to reach
  it. A `close` that failed left the connection marked closed and unrecoverable
- `Consumer.consume()` called again after `add_queue` re-consumed the queues it
  was already consuming and never declared the new one, so a queue added at
  runtime received nothing and the old ones delivered twice
- `cancel_by_queue` forgot the queue locally but left the broker consumer
  running, so messages kept arriving from a queue the caller had cancelled
- A consumer's `accept` list was not applied to the message it handed to
  callbacks, so a body in a content type the consumer had refused was decoded
  anyway
- `Producer.publish(retry=True)` ignored the retry policy and published once,
  and a `prefetch_count` was applied after the first consumer was registered,
  where the broker no longer applies it to that consumer
- A failed `declare` during `publish` was swallowed, so a queue or exchange that
  did not match the one on the broker looked like a successful publish into
  nothing
- Declaring a queue declared the queue alone, so a message published to its
  exchange was routed nowhere until something else declared the exchange and the
  binding
- A message body that would not decode was reported as a decode failure only if
  the transport happened to pass it on, and `Consumer.on_decode_error` was
  stored and never called
- `SimpleQueue(accept=[...])` applied the restriction only to `get(block=False)`,
  so a blocking `get` decoded content types the caller had refused
- `ConsumerMixin` passed its connection error handler where kombu expected a
  between-retries callback, so a broker that was down turned every retry into a
  `TypeError` instead of the handler's log line
- `ConsumerMixin.run()` caught every exception and started over, so a bug in
  `get_consumers` looped forever instead of reaching the caller
- A `broadcast(pattern=..., matcher=...)` did not carry the pattern to the
  nodes, so every worker ran a command meant for a few
- A pidbox reply ignored the serializer the mailbox was configured with and
  always went out as JSON
- A pidbox command handler defined as a coroutine function was never awaited:
  its coroutine object was sent back as the reply
- `Mailbox._collect` resolved the channel it was given and then built its reply
  consumer without it
- A mailbox read from two threads handed out two different reply queues, because
  its identity mixes in the calling thread while the reply queue it caches does
  not, so the collecting thread waited on a queue no reply was routed to

#### Valkey and Redis transport

- The transport rejected `block_timeout`, `queue_expires` and
  `requeue_check_interval`, three options its own documentation advertises. They
  were passed straight through to the client library, which raised `TypeError`
  before the first command ran. They are consumed by the transport now
- Every blocking consume ended in a read timeout. The client libraries read
  replies under a socket timeout of their own, five seconds by default, and pass
  no separate deadline for a blocking command, so a ten second `block_timeout`
  never survived. An idle worker churned a connection every five seconds. The
  socket timeout is now derived from `block_timeout`, and one configured shorter
  than the block is refused with an explanation
- `drain_events` ignored the timeout it was given and blocked for the full
  `block_timeout` instead. An idle worker fired ETA tasks up to ten seconds
  late, `SimpleQueue.get(timeout=0.5)` reported an empty queue after ten
  seconds, and `inspect ping --timeout 1` waited ten seconds with no workers
  running. The call now returns within the time it was given, and a timeout of
  zero polls without blocking
- A fanout message published while no consumer was blocked on the stream was
  dropped. The read asked for the end of the stream each time it ran rather than
  resuming from where the queue subscribed, so anything published between two
  drains was skipped: control commands and events vanished, and a mailbox call,
  which publishes before it drains, never saw its reply. The reader now resumes
  from the last message it saw, starting at the position the queue was bound at
- A delayed message went onto its queue up to a full `requeue_check_interval`
  before its eta, so with default settings an eta could fire a minute early. The
  sweep that moves due messages looks one interval ahead so that nothing falls
  due between two runs, and the deadline written for a delayed message now
  carries the same margin. The deadline written when the sweep restores a timed
  out message had the same gap and counted an early redelivery against
  `delivery_limit`
- A consumer callback that raised put its message back without counting the
  attempt, and said so only at debug level, so a message that breaks its
  callback every time circulated forever and `delivery_limit` could never act on
  it. The failure is now reported at error level with its traceback and the
  redelivery is counted. The path that drains expired messages skipped the same
  bookkeeping entirely and lost a message outright when the consumer never acked
  it
- An outage was reported as an empty queue. The consume script call, the
  blocking pop, the stream read and `get()` each caught every exception, logged
  one debug line and answered "nothing there", so a worker kept polling a dead
  socket and never saw a reason to reconnect, and `SimpleQueue.get_nowait()`
  raised `Empty` instead of the connection error. Broker failures now reach the
  caller
- A `drain_events` call could be cancelled although nobody had cancelled it,
  because it re-raised the cancellation of a consumer iteration that `close()`
  had stopped
- A binding pattern or routing key containing a regular expression metacharacter
  broke topic routing: `a(b` raised out of `publish()`, and `a+b` matched
  `aab`. Words are escaped individually now, which also fixes a `#` between two
  words, which could not match its zero word expansion
- A failed second ping left a connected pool behind with nothing referencing it,
  and two callers racing for the first channel orphaned a whole pair of clients.
  A `close()` cancelled while draining a channel left both sockets open although
  the connection had already marked itself closed. Neither path can leak a
  client now
- Closing a connection detached its clients before it drained its channels, so
  the requeue of what a channel held unacked, the restore of its prefetch buffer
  and the deletion of its auto-delete queues each ran against nothing and came
  out as a warning. The queue was left behind on the broker and its messages sat
  out the visibility timeout
- Cancelling one of two consumers on the same fanout queue stopped the stream
  read for both, so the remaining consumer went silent
- `Channel.get()` accepted a set of content types and ignored it, so a message
  came back willing to deserialise any content type at all
- A binding entry that cannot be parsed is now reported instead of skipped in
  silence, where it used to send a queue's whole traffic nowhere while the
  exchange looked correctly configured
- Celery opens a channel per unit of work on some paths, and the transport held
  every one of them until it closed. Channels now leave the list as they close

#### AMQP transport

- The transport reported a healthy connection after the broker had closed it.
  aio-pika only resolves its "closed" future for a close this side asked for, so
  a broker restart or a server-side close left `connect()` returning at once,
  `is_connected` True and `drain_events` waiting on a buffer nothing could fill.
  A worker sat in RUN consuming nothing, and `ensure_connection()` reported
  success on a dead socket. The transport now follows aio-pika's connected event
  and its close callbacks: every channel of a lost connection reports the loss
  to its next caller as a `connection_errors` member and then moves itself to
  the replacement, with its queues, bindings and consumers restored, and a
  channel the broker closed on its own is reopened the same way
- `drain_events(timeout=0)` reported an empty buffer even with messages waiting,
  because `asyncio.wait_for` cancels the get before it has run at a zero
  timeout. That cost the worker a pass through its outer loop per message
- `apply_async` with `task_serializer="pickle"` or `"msgpack"` raised
  `LookupError: unknown encoding: binary`. The producer base64-wraps a binary
  payload to fit it into the JSON envelope and labels the content encoding
  "binary", which is not a Python codec. The wrapper is consumed on the way out
  and the serializer's own bytes are published unchanged, which is also what
  lets a compressed body reach the consumer intact
- A message fetched with `basic.get` decoded any content type the registry knew,
  including pickle, because the `accept` set was dropped on the way
- A multiple ack dropped only the tag it named from the channel's bookkeeping,
  so acking a lower tag afterwards sent the broker a tag it had already
  forgotten and got PRECONDITION_FAILED and a closed channel back
- The incoming buffer was fixed at a thousand messages whatever the prefetch, so
  it neither followed the credit window nor pushed back; with the default
  unlimited prefetch a deep queue put one parked task in memory per message on
  it. The buffer is sized from the prefetch now
- A prefetch count set while consumers were running changed nothing, since
  RabbitMQ fixes a consumer's credit when the consumer is registered. The
  running consumers are registered again
- The documented `heartbeat` transport option was dropped on the way to the
  broker, and `connection_timeout` and `ssl` were never forwarded at all.
  aio-pika ignores its keyword arguments when it is handed a URL, and aiormq
  reads the heartbeat, the timeout and the TLS files from the URL query alone,
  so they are folded into the URL
- A delivery whose buffering failed disappeared without a word, because aiormq
  runs that callback in a task per delivery and never looks at the result. It is
  logged and handed back to the broker now, and a body that cannot be decoded is
  reported rather than reaching the consumer as if nothing happened
- `get`, `purge`, `bind` and `unbind` on a queue the channel had not declared
  itself did nothing and said nothing: `get` answered None, which is what an
  empty queue looks like, and `purge` answered zero. A queue another channel
  declared is an ordinary thing to address, so these reach the broker, and a
  name that really does not exist comes back as its 404
- A message whose consumer was cancelled between the delivery and the drain was
  dropped and left unacknowledged until the channel closed
- Two consumers on one queue shared the first one's callback, so the second
  never ran and the first saw messages it had not asked for. The consumer tag on
  the delivery picks the callback
- A channel the broker closed replayed its buffered deliveries on top of the
  redeliveries the broker was already sending, running the same message twice

#### In-memory and filesystem transports

- The in-memory transport kept its queues as `asyncio.Queue` objects on the
  channel class, and each bound itself to the first event loop that blocked on
  it, so every later loop got "bound to a different event loop". A second
  `asyncio.run()` in the same process, or a suite with function-scoped loops,
  failed unless it cleared the class attributes in between. The queues are plain
  deques now: a drain that has to wait registers a future on its own running
  loop and a publish resolves those futures through the loop each came from, so
  a producer and a consumer on different loops or threads still exchange
  messages
- Cancelling a drain on the memory transport ate the next message. Each drain
  left a pending queue read per consumer behind, and the orphan took the
  following message and dropped it, which is what happened to every inspect
  reply after a `broadcast(reply=True)` timed out. Nothing leaves a queue now
  until there is a consumer to hand it to
- `drain_events(timeout=0)` blocked for a full second on the memory and
  filesystem transports instead of polling, and a timeout longer than a second
  came back early as a spurious timeout. The worker's batch loop drains with a
  zero timeout after each message, so ETA and shutdown checks were held up by a
  second per batch. Both transports honour the timeout now: zero polls and
  returns, a positive timeout returns within it, and no timeout waits
- A JSON payload that was not an object crashed the memory and filesystem
  channels with an `AttributeError` raised after the message had already been
  taken off the queue, so the message was gone and the drain loop broke. A
  payload that is not an envelope, and an envelope whose body cannot be decoded,
  is logged and delivered as opaque `application/data` bytes that a consumer can
  reject
- The filesystem transport deleted a message file while reading it and recorded
  nothing as unacknowledged, so requeue, reject with requeue, recover and
  closing a channel had nothing to put back and the message was lost. A message
  is claimed into an in-flight directory now and stays there until it is
  acknowledged or rejected
- Two workers binding queues to the same filesystem fanout exchange each erased
  the other's bindings, because a bind wrote the process's own view over the
  control file without reading it and a process that already knew the exchange
  never re-read the file. The control file is the record now: a bind reads it,
  changes it and writes it back under a lock, and the replacement is atomic
- A control file the filesystem transport could not parse was treated as no
  bindings at all, so a fanout publish went nowhere and said nothing.
  Unparsable content, text or binary, raises a channel error now
- The filesystem transport matched a queue name anywhere at the end of a
  filename, so queue `a` consumed the messages of queue `b.a`, and its
  millisecond timestamps let two messages published in the same millisecond come
  back out of order. Names are matched exactly and timestamps are nanosecond
- A `basic_cancel` from another task while a drain was in progress raised
  "dictionary changed size during iteration". Drains work from a snapshot of the
  consumers
- Acknowledging a delivery tag that was not outstanding with `multiple=True`
  acknowledged and destroyed every message in flight on the channel. An unknown
  tag acknowledges nothing now
- A drain always started at the first consumer, so a queue that always had
  messages starved the others and a control command queued behind a saturated
  task queue was never delivered. Consumers are served in turn
- A body the serializer could not decode was handed to the consumer as raw bytes
  with the exception swallowed, leaving no trace of why. The failure is logged,
  and only decoding errors are caught
- The filesystem transport's documentation had the incoming and outgoing data
  folders the wrong way round, and its example configured two directories that
  could never see each other's messages

#### Command line and control

- `celery list bindings` always ended in `AttributeError: 'Connection' object
  has no attribute 'manager'`. It now says which transports can list bindings
  and exits with an error on the ones that cannot
- `celery status -q` printed the full report anyway, and `celery status --json`
  put the node count after the JSON document, so nothing could parse its output.
  Both options now produce what they promise
- `celery inspect` and `celery control` repeated the last positional argument,
  so a command given two arguments received three
- `celery purge` reported success while purging nothing. Two separate faults: a
  queue the broker does not have raised a channel error that was swallowed, and
  because the broker closes the channel it raised on, every queue after it
  silently failed too; and underneath, the AMQP transport only purged queues
  that had been declared through the same channel, which the freshly opened
  channel of the command never had. Missing queues are now reported one by one
  on stderr, the rest are still purged, and the count at the end is the real one
- `celery worker --detach` passed the value of `--uid` and `--gid` on to the
  detached process as a stray positional argument after dropping the flag itself
- `--time-limit`, `--soft-time-limit`, `--max-tasks-per-child` and
  `--max-memory-per-child` were accepted, validated, and then dropped into a
  pool attribute nothing reads. They now set the settings the worker takes those
  limits from
- Options an app declares through `app.user_options` were added to the CLI
  commands for good rather than for the invocation, so a second app in the same
  process was offered options it knows nothing about and its command was then
  called with a keyword argument it has no parameter for
- `Inspect.scheduled()` and `reserved()` took `safe` and dropped it, so a caller
  asking for censored arguments got the arguments. Both ends now pass it to
  `Request.info()`, as `active` already did
- `celery report` printed an empty transport and an empty driver line for every
  app: it read `conn.transport.driver_name`, which is `None` until something
  connects, and `conn.transport_cls`, which no longer exists, and swallowed the
  `AttributeError`
- `conf.humanize()` and `celery report` raised on a broker URL no transport can
  serve, which is the configuration most worth reporting. Censoring no longer
  builds a `Connection` to mask the password
- `Celery.connection()` took `userid`, `password`, `virtual_host`, `port`,
  `ssl`, `transport`, `login_method` and `failover_strategy` and passed none of
  them on, so a caller spelling out its credentials got an anonymous connection
  instead of an error

#### Events, monitoring and Django

- Events were never buffered while the dispatcher was offline. The publish is a
  coroutine, so the failure it was watching for could not surface where it was
  looked for. Events that fail to publish are now buffered, logged at warning
  level, and flushed on the next successful publish
- A dispatcher constructed with an explicit channel handed that channel to the
  producer where its connection belongs, so the first publish asked the channel
  for a channel of its own
- `State.tasks_by_worker()` raised `AttributeError` on any task that had not
  been assigned to a worker yet
- The Django fixup closed the raw file descriptor of every open database
  connection when the worker pool started. That is what a forked child has to do
  with inherited descriptors, and nothing forks here, so Django went on using a
  socket whose number the operating system was free to hand out again
- Importing `celery.contrib.django.task` pulled in `django.db` at import time,
  which fails before Django is configured. The import now happens when the
  on-commit helpers run
- Two handlers bound to different instances of the same class collided in the
  signal dispatcher, and the second `connect()` was dropped without a word. A
  second app in a process with `DJANGO_SETTINGS_MODULE` set therefore received
  none of its Django fixup handlers. In the same dispatcher, reconnecting an
  already connected receiver leaked one finalizer per call, and a signal with
  caching enabled raised `TypeError` for `sender=None` or for the hostname
  strings the worker sends with
- `start_worker()` ran the embedded test worker under its own `asyncio.run()`
  while the test published through the process-wide loop, so the two shared
  transports across two loops. It now runs on the shared loop, which un-skips
  the `celery.contrib.testing` tests
- `start_worker()` hung forever when the embedded worker failed to start,
  instead of raising what went wrong
- `start_worker(logfile=None)` passed `""` on to the logging setup, which took
  it for a filename and opened the working directory

#### Serialization and utilities

- `accept` was matched against content types only, so the serializer names the
  same setting takes everywhere else (`accept=["json"]`) were refused with
  `ContentDisallowed`. Names and content types are now both accepted, and an
  unknown name raises `SerializerNotInstalled` instead of silently disallowing
  everything
- `Producer.publish(compression=...)`, `Producer(compression=...)` and celery's
  `task_compression` setting were accepted and ignored: the body went out
  uncompressed, even though messages carrying a `compression` header have always
  been decompressed on receipt. A headers mapping passed to `publish` is no
  longer written into, so a caller reusing one dict does not publish the
  previous message's envelope keys
- `enable_insecure_serializers` and `disable_insecure_serializers` raised a bare
  `KeyError` for a serializer name, and `disable_insecure_serializers` resolved
  names while it was already disabling, so a typo left the registry with
  everything disabled and nothing re-enabled. Names are resolved before any
  state changes
- Deserializing a type that is no longer registered reported `<class 'type'>`
  instead of the name recorded in the payload
- Sorting a heap holding a single event raised `IndexError`, and comparing a
  `timetuple` with a plain tuple recursed until the stack ran out, because the
  reflected comparison landed back in the same method. Both comparison
  directions are now computed directly, and an operand that is not an event
  returns `NotImplemented`
- `as_url` took a query string and threw it away, so the broker URL in the
  startup banner lost `ssl_cert_reqs` and every other option. Values keep their
  slashes, so certificate paths stay readable
- Overwriting an existing key in an `LRUCache` that was at its limit evicted the
  oldest entry, so a cache of N keys could hold N-1
- Re-adding an item to a `LimitedSet` refreshed its expiry everywhere except the
  eviction heap, so a task id revoked twice still expired at the time of the
  first revoke and was dropped first under length pressure. Eviction now skips
  heap entries that no longer match the item's current one
- Comparing a `LimitedSet` with anything that was not a `LimitedSet` raised
  `AttributeError`, so even a membership test against a plain list blew up. Such
  a comparison is simply false now
- A `regen` wrapping anything re-iterable other than a generator handed out its
  already consumed elements a second time, because every lookahead called
  `iter()` on the source afresh and restarted it. The source is bound once now,
  and exhausting it marks the sequence complete on the first lookahead rather
  than on the one that overshoots
- `matcher.match` used `glob` whatever `_set_default_matcher` had been given,
  and reported an unregistered matcher as a `KeyError`
- `setup_logging(loglevel=None)`, documented as attaching the handler without
  touching the level, raised `TypeError`
- `draw_node(obj)` and `draw_edge(a, b)` on a graph formatter raised `dict()
  argument after ** must be a mapping, not NoneType`, so drawing a graph without
  explicit attributes was impossible
- The repr of a beat schedule entry never closed its angle bracket
- `imgcat` accepted and discarded arbitrary keyword arguments, so asking for a
  width or a height returned a plain inline image and no error. Unknown options
  are rejected
- The fast-forward date delta accepted and discarded arbitrary keyword
  arguments, so a misspelled field such as `minutes=` produced a delta that
  moved nothing. Unknown fields are rejected

### Changed

- Moved the shared loop runner to `kombu.utils.eventloop`, so kombu and celery
  drive a connection from the same loop; `celery.utils.eventloop` re-exports it
- `Message`, `Queue`, `SimpleQueue`, `Connection`, `Producer` and `Consumer` no
  longer end their signatures in `**kwargs`. Options they do not implement,
  upstream kombu's `bindings` and `auto_declare` and the broker settings this
  fork takes in the URL among them, were accepted and dropped; they are now a
  `TypeError`. `Consumer(on_decode_error=...)` is a named argument
- `Producer.publish` gained `ensure` and `revive`: a retried publish reconnects
  and starts over on a fresh channel
- `Celery.connection()`, `connection_for_read()` and `connection_for_write()`
  take `(url, transport_options, heartbeat)`. The URL carries the credentials,
  the virtual host and the port; passing them separately is now a `TypeError`
- `broker_heartbeat` is read again: it becomes a transport option for `amqp` and
  `amqps` URLs, where the transport turns it into a protocol-level heartbeat.
  Other transports are left alone, since Redis hands options it does not
  recognise to redis-py
- Every remote control command has an awaitable twin, `Control.apurge()` among
  them, and `Consumer.cancel_task_queue` is a coroutine
- The eta a transport delays delivery by is set where the message is published,
  not by a signal receiver registered once per process, so `before_task_publish`
  receivers see the properties that go on the wire
- `worker_process_init` is now sent from each loop worker thread as it comes up,
  rather than once from the thread that starts the pool, and
  `worker_process_shutdown` is sent when a loop worker stops. Nothing was ever
  sent at shutdown, so listeners that release per-worker resources never ran
- `terminate_job()` takes the id of the job to stop instead of a process id. The
  asyncio pool runs tasks in threads and has no per-task pid to address
- A blueprint's `on_start` callback may be a coroutine function, and the
  worker's is one
- `block_timeout`, `visibility_timeout` and `requeue_check_interval` are checked
  when a Valkey or Redis channel is created. Zero turned the consumer wait and
  the visibility sweep into busy loops and a negative value put every deadline
  in the past, both without complaint; `requeue_check_interval` used to warn and
  fall back to its default rather than telling the caller its setting was wrong
- `store_processed` on the filesystem transport now decides what happens when a
  message is acknowledged, keeping the file in the processed folder instead of
  deleting it. It used to decide whether a copy was kept while the message was
  being read. Unacknowledged messages live in an `inflight` subdirectory of the
  incoming data folder until they are acknowledged, rejected or the channel
  closes
- `basic_recover` on the AMQP transport reaches the aiormq channel through
  `get_underlay_channel()` instead of aio-pika's deprecated `channel` property,
  which warns and raises on a channel that is not open
- `parse_iso8601` is gone. Nothing called it and it was wrong: it passed the raw
  fraction digits to the microsecond argument, so a half-second parsed as five
  microseconds, and a year-only string raised `TypeError` instead of
  `ValueError`. `maybe_iso8601` parses both correctly and is what the codebase
  uses
- The `flower` extra no longer installs Flower itself, only Flower's other
  dependencies. Flower requires upstream `celery` from PyPI, which installed
  over this package for anyone who was not resolving inside this workspace.
  Install it with `pip install "celery-asyncio[flower]" && pip install flower
  --no-deps`
- Dropped the `[tool.uv] override-dependencies` entry that neutralised upstream
  `celery`; nothing in the project pulls it in any more
- The `amqp` extra takes `aio-pika>=9.5.0,<11`, and the development pin moves to
  10.0.1, which the AMQP suite is verified against
- Raised the minimum `asgiref` version to 3.8.0
- The `3.14t` CI job ran with the GIL re-enabled. msgpack 1.2.2 declares
  free-threading support, but `_brotli` and `ephem._libastro` do not, and either
  one turns the GIL back on for the whole process, so the job sets `PYTHON_GIL=0`
- Bumped the dev dependencies to ty 0.0.77, msgpack 1.2.2 and requests 2.34.2,
  moved `pre-commit-hooks` to v6.0.0, and refreshed 28 locked transitive
  packages including click 8.5.0 and mkdocstrings-python 2.0.8
- `ephem` and `tblib` join the dev group, so the solar schedule tests and the
  remote-traceback test run instead of being skipped
- Merged the `kombu-asyncio` package into this repository; `kombu` now ships as
  a top-level package of `celery-asyncio` instead of a separate install
- Restored the per-rule reasons on the ruff `ignore` list, lost in the merge

### Added

- A Broker API (kombu) section in the docs nav: Connection, producers and
  consumers, exchanges and queues, and the simple interface
- A testing page in the user guide: how to enable the `celery.contrib.pytest`
  fixtures with `pytest_plugins`, what each fixture does, and how an `async def`
  task behaves called directly versus through a worker
- Documentation for `task_acks_on_failure`, `task_acks_on_timeout`,
  `worker_prefetch_multiplier`, `worker_enable_prefetch_count_reduction`,
  `worker_cancel_long_running_tasks_on_connection_loss`,
  `worker_deduplicate_successful_tasks`, the two soft-shutdown settings, and the
  Valkey/Redis backend's `additional_connection_errors` transport option
- CI runs the celery integration suite, which nothing had ever run
- Each pytest-xdist worker runs the integration suite against a Redis database
  of its own, so parallel workers no longer share broker queues, fanout channels
  or the keys the test tasks write to. Without it `inspect` saw every worker's
  embedded worker. Both integration suites stay in databases 10 to 15 and leave
  the low ones, database 0 above all, to whatever else is on the machine
- A `global_pubsub` marker for the three tests that assert on the set of active
  Redis PUBSUB channels. Redis reports those per server rather than per
  database, so CI runs them in a step of their own

### Removed

- Autoscaling: the `--autoscale` option, the `worker_autoscaler` setting, the
  `pool_grow`, `pool_shrink` and `autoscale` control commands and their client
  methods. The bootstep was never in the worker blueprint, its `create()`
  returned `None`, and the asyncio pool has no `grow()` or `shrink()`, so the
  commands raised `AttributeError` and `--autoscale` only pinned concurrency to
  the low end of the range
- 20 settings that nothing read: `broker_failover_strategy`,
  `broker_login_method`, `broker_native_delayed_delivery_queue_type`,
  `broker_pool_limit`, `broker_port`, `broker_user`, `broker_password`,
  `broker_vhost`, `result_compression`, `result_exchange`,
  `result_exchange_type`, `worker_agent`, `worker_detect_quorum_queues`,
  `worker_disable_prefetch`, `worker_eta_task_limit`, `worker_lost_wait`,
  `worker_pool_putlocks`, `worker_proc_alive_timeout`, `worker_timer` and
  `worker_timer_precision`, along with the worker attributes that carried the
  prefork ones
- The worker options `-O` / `--optimization`, `--disable-prefetch` and
  `--autoscale`. The only optimization profile described how the prefork pool
  handed work to its children, and prefetching in the asyncio pool is bounded by
  its semaphore
- `app.producer_pool`, which only raised, and `app.producer_or_acquire()`, which
  acquired nothing: producers are passed as ordinary arguments. `app.pool`,
  `app._acquire_connection()` and `app.connection_or_acquire()` go with them
- The sync `amqp.send_task_message`; use `asend_task_message`
- `store_errors` from `build_tracer()` and `build_async_tracer()`, which never
  read it, `create_missing` from `Router`, which routed by it nowhere, `eager`
  from `chord.run()` and `chord.arun()`, which neither read nor forwarded it,
  and `TaskRegistry.regular()`, `periodic()` and `filter_types()`
- `BaseResultConsumer` and the Valkey/Redis `ResultConsumer`. Result fetching
  polls, so both were no-op stubs nothing called, as were the `_pending_results`
  and `_pending_messages` maps the backends built for them, along with
  `Backend.prepare_persistent`, `Backend.subpolling_interval`,
  `MESSAGE_BUFFER_MAX`, `pending_results_t` and both `_iter_meta`
  implementations
- `Consumer.recover`, `Consumer.iterate` and the consumer's async iterator
  protocol, `ConsumerMixin.maybe_conn_error`, `ConsumerProducerMixin`,
  `Connection.qos_semantics_matches_spec`, `Mailbox.producer_pool` and the
  transport alias table
- `kombu.common.drain_consumer`, `itermessages`, `collect_replies`, `send_reply`
  and `ignore_errors`. Draining is what `Connection.drain_events` and
  `kombu.common.eventloop` are for
- `kombu.utils.scheduling`, `kombu.utils.collections`, `kombu.utils.div` and
  `kombu.utils.time`, none of which had a caller left
- The unused half of `kombu.log` (`LogMixin`, `Log`, `setup_logging`,
  `get_loglevel`, `safeify_format`, `naive_format_parts`, `DISABLE_TRACEBACKS`),
  of `kombu.utils.encoding` (`from_utf8`, `default_encode`, `default_encoding`
  and the encoding-file globals), of `kombu.utils.compat` (`coro`,
  `detect_environment`, `register_after_fork`), of `kombu.utils.text`
  (`escape_regex`, `fmatch_iter`, `fmatch_best`), of `kombu.utils.functional`
  (`ChannelPromise`, `shufflecycle`, `fxrangemax`, `accepts_argument`, and the
  `promise`/`maybe_promise` aliases), and `Logwrapped` from `kombu.utils.debug`
- `parse_url`, `parse_ssl_cert_reqs` and the `ssl_available` flag: connections
  parse URLs through `url_to_parts`, and the Valkey backend builds its own ssl
  options
- The kombu exceptions nothing raises: `VersionMismatch`, `LimitExceeded`,
  `ConnectionLimitExceeded`, `ChannelLimitExceeded`, `ResourceError`,
  `NotBoundError` and `HttpError`
- The deprecated re-exports from `kombu.utils`; import from the module that
  defines the name. `symbol_by_name` stays. `safe_str`, `safe_repr` and
  `_safe_str` no longer take an `errors` argument they never used
- `Hub`, `get_event_loop`, `set_event_loop` and `LaxBoundedSemaphore` from
  `celery.utils.scheduling`, and `default_socket_timeout` from
  `celery.utils.threads`. Nothing in celery, kombu, the tests or the examples
  called them; the worker schedules through `Timer` and asyncio directly
- The embedded beat service no longer takes a `thread` flag or a
  `max_interval`. Neither did anything: there is no process variant left, and
  the thread always runs on a one second interval so that stopping does not wait
  out a long sleep. Passing either is now an error instead of a silent no-op
- The compat-module machinery in `celery.local`. Its module list was an empty
  literal, so nothing it fed could ever run; `create_module` loses the two
  arguments only that path passed
- `Signal.send_robust`, which was a plain alias of `send`, the alarm helpers on
  the signal wrapper, `CeleryOption.default_value_from_context`,
  `EventReceiver.itercapture`, the synchronous `wakeup_workers`,
  `State.tasks_by_timestamp`, and `BaseLoader.init_worker_process` /
  `on_worker_process_init`
- `Blueprint.connect_with`, `CycleError`, `fill_paragraphs`,
  `load_extension_classes`, `iter_open_logger_fds`, the process-aware logger
  patch, the deprecated `Callable` alias and the base64 wrappers in the
  serialization utilities, none of which had a reference outside their own
  definition and tests
- The billiard-era `MP_MAIN_FILE` branch in task naming. A task defined in
  `__main__` is still named after the app's main module
- Unused Valkey/Redis transport state: the `supports_native_delayed_delivery`
  flag no caller consults, a default health check interval nothing read, a
  resolved exception tuple and the helper behind it, an exchange to queue map,
  an in memory mirror of the binding table that routing never consulted, and a
  connection id stored by both the transport and every channel. The memory and
  filesystem channels lose the same kind of unread state, along with the unused
  pattern field of the filesystem binding tuple
- The blanket `except` around an AMQP `basic.get`, which turned a real broker or
  channel error into "the queue is empty", and two unreachable "cannot recover"
  branches in `basic_recover`
- The `zstd` extra. PEP 784 put zstd in the stdlib as of 3.14, this package's
  floor, so `zstandard` was already unused
- The unused `unit` and `integration` pytest markers, and the vestigial
  `UV_NO_SOURCES` from the workflows: there is no `[tool.uv.sources]` table
- Requirements no longer list `kombu-asyncio` as a dependency
- `UPSTREAM-PLAN.md`. The sweep it planned has landed and git history keeps the
  document

## v6.0.0a5

### Fixed

- Receiving an AMQP message with a TTL raised `AttributeError` (kombu-asyncio 6.0.0a5)

### Changed

- Every re-raise now names its cause, so a traceback points at the original error
  instead of stopping at the exception celery raised in its place

## v6.0.0a4

Ported every applicable fix from a sweep of upstream Celery and Kombu, and
fixed the fork-only defects the sweep turned up along the way.

### Added

- `AsyncResult.exists()` and its async twin
- `time_limit` and `soft_time_limit` on `task.request`
- Separate `acks_on_failure` and `acks_on_timeout` task options
- `additional_connection_errors` transport option for the Valkey/Redis backend
- Warning when routing is declared as a task attribute

### Fixed

- Publishing opened a broker connection per task
- Chord counter double-counted on redelivery
- A failing step in a chain never reached the chord body
- Chains of chords were nested instead of flat; empty groups broke `as_tuple`
- Eager chains kept running past `Ignore` and `Reject`; `EagerResult` had no `aget()`
- Crontabs matched against the wrong timezone, and DST gaps were skipped
- Beat kept a broker connection it never used, and lost entries that asked to wait
- `send_task` by name ignored the task's own options
- PEP 649 lazy annotations broke task registration
- Revoked tasks were not marked `REVOKED` in the backend
- Cold-shutdown terminations were reported as task failures
- Reconnects lost in-flight bookkeeping, and broken connections were reused
- The event dispatcher dropped and leaked buffered events
- `autoretry_for` was ignored on async tasks
- Passwords in failover URLs were logged in the clear
- `redis_backend_use_ssl` was ignored on `redis://`, and sentinel ACL credentials were dropped
- Routing-only queues were added to the consumer set
- Control and event queues are now exclusive by default
- Django connections were opened only to be closed again

### Fixed (production readiness audit)

- `LoopWorker.start()` returned before its event loop was running
- `LoopWorker.stop()` left the loop unclosed, leaking its self-pipe on every pool restart
- The hard-timeout timer for sync tasks was never cancelled, parking a thread per task
- `CancelledError` was swallowed in the async task path instead of propagating
- The timer scheduled on the wall clock, so a clock step shifted or stampeded pending entries
- A deep backlog could starve the `max_tasks_per_child` and `max_memory_per_child` checks
- `celery-asyncio[hiredis]` and `[libvalkey]` installed nothing
- A local `uv build` swept vendored virtualenvs into the sdist

## v6.0.0a3

- Dual Valkey/Redis support; `redis` backend module renamed to `valkey_redis`
- API reference (mkdocstrings) and migration guide
- Flower packaged as a `[flower]` extra
- Soft time limits for sync tasks in the thread pool
- Redis PUBSUB replaced with polling in the result backend
- Django 6.0 Tasks backend removed in favour of
  [django-tasks-celery](https://github.com/oliverhaas/django-tasks-celery)
- Memcached backend, Python 2 compat shims, and dead code removed

## v6.0.0a2

Initial alpha of celery-asyncio.

### What works

- Async worker with hybrid asyncio + thread pool
- `async def` and regular `def` tasks in the same worker
- Valkey/Redis transport (with sorted-set priority queues, Lua scripts, fanout)
- AMQP transport (via aio-pika, RabbitMQ)
- Full CLI (`celery worker`, `celery inspect`, `celery control`, `celery result`)
- Task events and Celery Flower monitoring
- Worker restart (max tasks, max memory, stuck threads)
- Task timeouts (soft and hard, async and sync)
- Django 6.0 Tasks support via [django-tasks-celery](https://github.com/oliverhaas/django-tasks-celery)
- Delayed/scheduled tasks (countdown, eta)
- Task priority
- Task retries

### What's not yet tested

- Multi-worker deployments
- Rate limiting and autoscaling

### Breaking changes from upstream Celery

- Requires Python 3.14+
- Requires kombu-asyncio (not upstream kombu)
- Removed eventlet, gevent, and prefork pool backends
- Removed billiard dependency
- Default pool is `asyncio` (not `prefork`)
- Bootsteps are async (`async def start/stop`)
