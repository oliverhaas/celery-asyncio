from kombu import Queue

from celery.utils.nodenames import host_format, node_format, worker_direct


class test_worker_direct:
    def test_returns_if_queue(self):
        q = Queue("foo")
        assert worker_direct(q) is q


class test_process_index_expansion:
    def test_i_expands_to_the_only_process(self):
        assert node_format("w%i@%h", "worker@example.com") == "w0@example.com"

    def test_I_expands_to_nothing(self):
        assert node_format("w%I@%h", "worker@example.com") == "w@example.com"

    def test_host_format_expands_both(self):
        assert host_format("%i%I", host="example.com") == "0"
