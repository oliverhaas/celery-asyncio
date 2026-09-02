import pytest

from kombu.matcher import MatcherNotInstalled, fnmatch, match, register, registry, rematch, unregister


@pytest.fixture
def restore_default_matcher():
    saved = registry._default_matcher_name
    yield
    registry._set_default_matcher(saved)


class test_Matcher:
    def test_register_match_unregister_matcher(self, restore_default_matcher):
        register("test_matcher", rematch)
        registry.matcher_pattern_first.append("test_matcher")
        try:
            assert registry._matchers["test_matcher"] == rematch
            assert match("data", r"d.*", "test_matcher") is not None
            assert registry._default_matcher_name == "glob"
            registry._set_default_matcher("test_matcher")
            assert registry._default_matcher_name == "test_matcher"
        finally:
            registry._set_default_matcher("glob")
            registry.matcher_pattern_first.remove("test_matcher")
        unregister("test_matcher")
        assert "test_matcher" not in registry._matchers

    def test_match_uses_the_default_matcher(self, restore_default_matcher):
        # `match` hard-coded "glob" and ignored _set_default_matcher, so a
        # regex pattern was matched with fnmatch and quietly did not match.
        assert match("data", r"d.*") is False
        assert fnmatch("data", r"d.*") is False

        registry._set_default_matcher("pcre")
        assert match("data", r"d.*") is not None
        assert match("data", r"nope") is None

    def test_set_default_matcher_not_registered(self):
        with pytest.raises(MatcherNotInstalled):
            registry._set_default_matcher("notinstalled")
        assert registry._default_matcher_name == "glob"

    def test_unregister_matcher_not_registered(self):
        with pytest.raises(MatcherNotInstalled):
            unregister("notinstalled")

    def test_match_using_unregistered_matcher(self):
        with pytest.raises(MatcherNotInstalled):
            match("data", r"d.*", "notinstalled")
