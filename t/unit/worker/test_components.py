from unittest.mock import Mock

import t.skip
from celery.worker.components import Pool, Timer


class test_Timer:

    def test_create(self):
        w = Mock(name='w')
        Timer(w).create(w)
        assert hasattr(w, 'timer')


class test_Pool:

    def test_close_terminate(self):
        w = Mock()
        comp = Pool(w)
        pool = w.pool = Mock()
        comp.close(w)
        pool.close.assert_called_with()

        w.pool = None
        comp.close(w)

    def test_create_calls_instantiate_with_max_memory(self):
        w = Mock()
        comp = Pool(w)
        comp.instantiate = Mock()
        w.max_memory_per_child = 32

        comp.create(w)

        assert comp.instantiate.call_args[1]['max_memory_per_child'] == 32

    def test_create_passes_pool_params(self):
        w = Mock()
        comp = Pool(w)
        comp.instantiate = Mock()
        w.loop_workers = 3
        w.loop_concurrency = 20
        w.sync_workers = 4

        comp.create(w)

        kwargs = comp.instantiate.call_args[1]
        assert kwargs['loop_workers'] == 3
        assert kwargs['loop_concurrency'] == 20
        assert kwargs['sync_workers'] == 4
