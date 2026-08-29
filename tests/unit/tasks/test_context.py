from celery.app.task import Context


# Retrieve the values of all context attributes as a
# dictionary in an implementation-agnostic manner.
def get_context_as_dict(ctx, getter=getattr):
    defaults = {}
    for attr_name in dir(ctx):
        if attr_name.startswith("_"):
            continue  # Ignore pseudo-private attributes
        attr = getter(ctx, attr_name)
        if callable(attr):
            continue  # Ignore methods and other non-trivial types
        defaults[attr_name] = attr
    return defaults


default_context = get_context_as_dict(Context())


class test_Context:
    def test_default_context(self):
        # A bit of a tautological test, since it uses the same
        # initializer as the default_context constructor.
        defaults = dict(default_context, children=[])
        assert get_context_as_dict(Context()) == defaults

    def test_updated_context(self):
        expected = dict(default_context)
        changes = {"id": "unique id", "args": ["some", 1], "wibble": "wobble"}
        ctx = Context()
        expected.update(changes)
        ctx.update(changes)
        assert get_context_as_dict(ctx) == expected
        assert get_context_as_dict(Context()) == default_context

    def test_modified_context(self):
        expected = dict(default_context)
        ctx = Context()
        expected["id"] = "unique id"
        expected["args"] = ["some", 1]
        ctx.id = "unique id"
        ctx.args = ["some", 1]
        assert get_context_as_dict(ctx) == expected
        assert get_context_as_dict(Context()) == default_context

    def test_cleared_context(self):
        changes = {"id": "unique id", "args": ["some", 1], "wibble": "wobble"}
        ctx = Context()
        ctx.update(changes)
        ctx.clear()
        defaults = dict(default_context, children=[])
        assert get_context_as_dict(ctx) == defaults
        assert get_context_as_dict(Context()) == defaults

    def test_context_get(self):
        expected = dict(default_context)
        changes = {"id": "unique id", "args": ["some", 1], "wibble": "wobble"}
        ctx = Context()
        expected.update(changes)
        ctx.update(changes)
        ctx_dict = get_context_as_dict(ctx, getter=Context.get)
        assert ctx_dict == expected
        assert get_context_as_dict(Context()) == default_context

    def test_timelimit_is_unpacked_into_time_limit_and_soft_time_limit(self):
        # `timelimit` is the wire format. A task body reading self.request
        # wants the two named attributes, not a positional pair.
        ctx = Context({"timelimit": [30, 10]})
        assert ctx.time_limit == 30
        assert ctx.soft_time_limit == 10

        ctx = Context({"timelimit": (30, 10)})
        assert ctx.time_limit == 30
        assert ctx.soft_time_limit == 10

    def test_timelimit_absent_leaves_the_attributes_unset(self):
        ctx = Context({"id": "x"})
        assert ctx.time_limit is None
        assert ctx.soft_time_limit is None

    def test_timelimit_set_to_none_clears_a_previous_pair(self):
        ctx = Context({"timelimit": [30, 10]})
        ctx.update({"timelimit": None})
        assert ctx.time_limit is None
        assert ctx.soft_time_limit is None

    def test_an_update_without_timelimit_keeps_the_previous_pair(self):
        ctx = Context({"timelimit": [30, 10]})
        ctx.update({"id": "x"})
        assert ctx.time_limit == 30
        assert ctx.soft_time_limit == 10

    def test_extract_headers(self):
        # Should extract custom headers from the request dict
        request = {
            "task": "test.test_task",
            "id": "e16eeaee-1172-49bb-9098-5437a509ffd9",
            "custom-header": "custom-value",
        }
        ctx = Context(request)
        assert ctx.headers == {"custom-header": "custom-value"}

    def test_dont_override_headers(self):
        # Should not override headers if defined in the request
        request = {
            "task": "test.test_task",
            "id": "e16eeaee-1172-49bb-9098-5437a509ffd9",
            "headers": {"custom-header": "custom-value"},
            "custom-header-2": "custom-value-2",
        }
        ctx = Context(request)
        assert ctx.headers == {"custom-header": "custom-value"}
