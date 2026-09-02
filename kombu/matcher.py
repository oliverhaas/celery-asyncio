# Originally from Kombu by Ask Solem & contributors (BSD-3-Clause)
# https://github.com/celery/kombu
"""Pattern matching registry."""

from collections.abc import Callable
from fnmatch import fnmatch
from re import match as rematch
from typing import cast

from .utils.compat import entrypoints
from .utils.encoding import bytes_to_str

MatcherFunction = Callable[[str, str], bool]


class MatcherNotInstalled(Exception):
    """Matcher not installed/found."""


class MatcherRegistry:
    """Pattern matching function registry."""

    MatcherNotInstalled = MatcherNotInstalled
    matcher_pattern_first = ["pcre"]

    def __init__(self) -> None:
        self._matchers: dict[str, MatcherFunction] = {}
        self._default_matcher_name: str | None = None

    def register(self, name: str, matcher: MatcherFunction) -> None:
        """Add matcher by name to the registry."""
        self._matchers[name] = matcher

    def unregister(self, name: str) -> None:
        """Remove matcher by name from the registry."""
        try:
            self._matchers.pop(name)
        except KeyError:
            raise self.MatcherNotInstalled(f"No matcher installed for {name}") from None

    def _set_default_matcher(self, name: str) -> None:
        """Set the default matching method.

        :param name: The name of the registered matching method.
            For example, `glob` (default), `pcre`, or any custom
            methods registered using :meth:`register`.

        :raises MatcherNotInstalled: If the matching method requested
            is not available.
        """
        if name not in self._matchers:
            raise self.MatcherNotInstalled(f"No matcher installed for {name}")
        self._default_matcher_name = name

    def match(
        self,
        data: str | bytes,
        pattern: str | bytes,
        matcher: str | None = None,
        matcher_kwargs: dict[str, str] | None = None,
    ) -> bool:
        """Match `data` by `pattern` using `matcher`.

        :param data: The data that should be matched.
        :param pattern: The pattern that should be applied.
        :keyword matcher: An optional string representing the matching
            method (for example, `glob` or `pcre`). The default matcher
            is used when it is :const:`None`.
        :keyword matcher_kwargs: Additional keyword arguments that will be
            passed to the specified `matcher`.
        :returns: :const:`True` if `data` matches pattern,
            :const:`False` otherwise.

        :raises MatcherNotInstalled: If the matching method requested is not
            available.
        """
        name = matcher or self._default_matcher_name
        if name is None:
            raise self.MatcherNotInstalled("No default matcher installed")
        try:
            match_func = self._matchers[name]
        except KeyError:
            raise self.MatcherNotInstalled(f"No matcher installed for {name}") from None
        if name in self.matcher_pattern_first:
            first_arg = bytes_to_str(pattern)
            second_arg = bytes_to_str(data)
        else:
            first_arg = bytes_to_str(data)
            second_arg = bytes_to_str(pattern)
        return match_func(first_arg, second_arg, **matcher_kwargs or {})


#: Global registry of matchers.
registry = MatcherRegistry()

match = registry.match
register = registry.register
unregister = registry.unregister


def register_glob() -> None:
    """Register glob into default registry."""
    registry.register("glob", fnmatch)


def register_pcre() -> None:
    """Register pcre into default registry."""
    registry.register("pcre", cast("MatcherFunction", rematch))


# Register the base matching methods.
register_glob()
register_pcre()

# Default matching method is 'glob'
registry._set_default_matcher("glob")

# Load entrypoints from installed extensions
for ep, args in entrypoints("kombu.matchers"):
    register(ep.name, *args)
