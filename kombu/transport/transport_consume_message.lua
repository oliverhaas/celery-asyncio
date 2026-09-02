-- Lua script for atomic message consumption from sorted set queues.
-- Pops the highest-priority message across multiple queues, refreshes the
-- messages_index score for visibility timeout tracking, and returns the
-- message data, all in a single atomic operation.
--
-- Handles expired message hashes (x-message-ttl) by cleaning up the index
-- entry and trying the next message.
--
-- KEYS: [queue:q1, queue:q2, ...] queue sorted set keys (with global_keyprefix applied)
-- ARGV: [1] = global_keyprefix, [2] = message_key_prefix,
--       [3] = new_queue_at (the visibility deadline, as string number),
--       [4] = messages_index_prefix, [5] = max messages to return,
--       [6..5+N] = queue_name_1, queue_name_2, ... (raw names, in KEYS order)
--       [6+N..5+2N] = no_ack flag ('1'/'0') per queue, same order as KEYS
-- Returns: a flat {queue_name, delivery_tag, payload, delivery_count, ...} of
-- up to `max messages` groups, or nil when nothing was available at all.

local global_keyprefix = ARGV[1]
local message_key_prefix = ARGV[2]
local new_queue_at = tonumber(ARGV[3])
local messages_index_prefix = ARGV[4]
local wanted = tonumber(ARGV[5])
local num_queues = #KEYS
local max_attempts = 100 + wanted
local out = {}
local found = 0

for _attempt = 1, max_attempts do
    -- Find the queue with the lowest minimum score (highest priority message)
    local best_score = nil
    local best_idx = nil
    for i = 1, num_queues do
        local peek = redis.call('ZRANGE', KEYS[i], 0, 0, 'WITHSCORES')
        if #peek > 0 then
            local score = tonumber(peek[2])
            if not best_score or score < best_score then
                best_score = score
                best_idx = i
            end
        end
    end

    if not best_idx then
        break  -- All queues empty
    end

    -- Atomically pop from the best queue
    local result = redis.call('ZPOPMIN', KEYS[best_idx], 1)
    if #result == 0 then
        break  -- Shouldn't happen inside Lua, but be safe
    end

    local tag = result[1]
    local queue_name = ARGV[5 + best_idx]

    -- Fetch message data
    local message_key = global_keyprefix .. message_key_prefix .. tag
    local fields = redis.call('HMGET', message_key, 'payload', 'delivery_count')

    if fields[1] then
        local index_key = global_keyprefix .. messages_index_prefix .. queue_name
        if ARGV[5 + num_queues + best_idx] == '1' then
            -- no_ack consumer: the message is finished the moment it is
            -- delivered. Dequeue it here instead of giving it a visibility
            -- deadline nobody will ever ack away, which would leak the index
            -- entry and redeliver the message on the next requeue sweep.
            redis.call('ZREM', index_key, tag)
            redis.call('DEL', message_key)
        else
            -- Valid message: set the messages_index score for visibility timeout.
            -- Not 'XX': an entry missing here would leave the message delivered with
            -- nothing tracking it, so a worker crash would lose it permanently. The
            -- guard bought nothing anyway, since this runs only after HMGET confirmed
            -- the hash exists and the whole script is atomic.
            redis.call('ZADD', index_key, new_queue_at, tag)
        end
        out[#out + 1] = queue_name
        out[#out + 1] = tag
        out[#out + 1] = fields[1]
        out[#out + 1] = fields[2] or '0'
        found = found + 1
        if found >= wanted then
            break
        end
    else
        -- Message hash expired (x-message-ttl): clean up index and try next
        local index_key = global_keyprefix .. messages_index_prefix .. queue_name
        redis.call('ZREM', index_key, tag)
    end
end

if found == 0 then
    return nil  -- Nothing available
end

return out
