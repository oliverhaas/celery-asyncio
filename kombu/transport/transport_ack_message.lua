-- Lua script for atomically acknowledging/removing messages.
-- Removes each delivery tag from the messages_index sorted set and the queue
-- sorted set, and deletes the per-message hash, in a single atomic operation.
-- This prevents orphaned message hashes from connection drops mid-pipeline.
--
-- The queue ZREM is not just about keeping ZCARD accurate. Once
-- enqueue_due_messages has restored a message while its original consumer was
-- still working on it, the tag is back in the queue. Without this ZREM the ack
-- cannot undo that restore, the duplicate stays poppable, and a second worker
-- runs the task. An ack that lands before another consumer pops now cancels the
-- restored copy outright.
--
-- Takes any number of messages, so the acks one pass of the event loop makes
-- cost one round trip between them.
--
-- KEYS: three per message, in the order of ARGV (global_keyprefix applied):
--       messages_index:{queue}, message:{tag}, queue:{queue}
-- ARGV: the delivery tag of each message (the member to ZREM from index and queue)

for i = 1, #ARGV do
    local k = (i - 1) * 3
    redis.call('ZREM', KEYS[k + 1], ARGV[i])
    redis.call('ZREM', KEYS[k + 3], ARGV[i])
    redis.call('DEL', KEYS[k + 2])
end
return 1
