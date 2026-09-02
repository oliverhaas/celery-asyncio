from collections.abc import Mapping, MutableMapping

from celery.app.utils import Settings, bugreport, filter_hidden_settings


class test_Settings:
    def test_is_mapping(self):
        """Settings should be a collections.Mapping"""
        assert issubclass(Settings, Mapping)

    def test_is_mutable_mapping(self):
        """Settings should be a collections.MutableMapping"""
        assert issubclass(Settings, MutableMapping)

    def test_find(self):
        assert self.app.conf.find_option("always_eager")

    def test_get_by_parts(self):
        self.app.conf.task_do_this_and_that = 303
        assert self.app.conf.get_by_parts("task", "do", "this", "and", "that") == 303

    def test_find_value_for_key(self):
        assert self.app.conf.find_value_for_key("always_eager") is False

    def test_table(self):
        assert self.app.conf.table(with_defaults=True)
        assert self.app.conf.table(with_defaults=False)
        assert self.app.conf.table(censored=False)
        assert self.app.conf.table(censored=True)


class test_filter_hidden_settings:
    def test_handles_non_string_keys(self):
        """filter_hidden_settings shouldn't raise an exception when handling
        mappings with non-string keys"""
        conf = {
            "STRING_KEY": "VALUE1",
            ("NON", "STRING", "KEY"): "VALUE2",
            "STRING_KEY2": {"STRING_KEY3": 1, ("NON", "STRING", "KEY", "2"): 2},
        }
        filter_hidden_settings(conf)

    def test_masks_the_password_in_a_broker_url(self):
        censored = filter_hidden_settings({"broker_url": "amqp://user:s3cret@host:5672//"})

        assert censored["broker_url"] == "amqp://user:********@host:5672//"

    def test_masks_a_url_of_a_transport_it_does_not_know(self):
        # Censoring through kombu.Connection made `celery report` raise on
        # the very configuration someone would be reporting.
        censored = filter_hidden_settings({"broker_url": "nosuchtransport://user:s3cret@host"})

        assert censored["broker_url"] == "nosuchtransport://user:********@host/"


class test_bugreport:
    def test_names_the_transport_without_connecting(self):
        self.app.conf.broker_url = "redis://localhost:6379/0"

        assert "transport:redis" in bugreport(self.app)

    def test_reports_a_broker_url_no_transport_can_serve(self):
        self.app.conf.broker_url = "nosuchtransport://localhost"

        assert "transport:unusable" in bugreport(self.app)
