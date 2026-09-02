from kombu.utils.encoding import bytes_to_str, ensure_bytes, safe_repr, safe_str, str_to_bytes


class newbytes(bytes):
    """Mock class to simulate python-future newbytes class"""

    def __repr__(self):
        return "b" + super().__repr__()

    def __str__(self):
        return "b" + f"'{super().__str__()}'"


class newstr(str):
    """Mock class to simulate python-future newstr class"""

    __slots__ = ()

    def encode(self, encoding=None, errors=None):
        return newbytes(super().encode(encoding, errors))


class test_conversions:
    def test_str_to_bytes(self):
        assert str_to_bytes("foo") == b"foo"
        assert str_to_bytes(b"foo") == b"foo"

    def test_bytes_to_str(self):
        assert bytes_to_str(b"foo") == "foo"
        assert bytes_to_str("foo") == "foo"

    def test_bytes_to_str_replaces_undecodable_bytes(self):
        assert bytes_to_str(b"fo\xffo") == "fo�o"

    def test_ensure_bytes(self):
        assert ensure_bytes("foo") == b"foo"
        assert ensure_bytes(b"foo") == b"foo"


class test_safe_str:
    def test_when_bytes(self):
        assert safe_str(b"foo") == "foo"

    def test_when_newstr(self):
        """Simulates using python-future package under 2.7"""
        assert str(safe_str(newstr("foo"))) == "foo"

    def test_when_unicode(self):
        assert isinstance(safe_str("foo"), str)

    def test_when_containing_high_chars(self):
        s = "The quiæk fåx jømps øver the lazy dåg"
        res = safe_str(s)
        assert isinstance(res, str)
        assert len(s) == len(res)

    def test_when_not_string(self):
        o = object()
        assert safe_str(o) == repr(o)

    def test_when_unrepresentable(self):
        class UnrepresentableObject:
            def __repr__(self):
                raise KeyError("foo")

        assert "<Unrepresentable" in safe_str(UnrepresentableObject())


class test_safe_repr:
    def test_repr(self):
        assert safe_repr("foo") == "'foo'"
        assert safe_repr([1, 2]) == "[1, 2]"

    def test_when_repr_raises(self):
        class UnrepresentableObject:
            def __repr__(self):
                raise KeyError("foo")

        assert "<Unrepresentable" in safe_repr(UnrepresentableObject())
