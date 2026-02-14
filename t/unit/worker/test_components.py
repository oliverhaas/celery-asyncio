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
