# Copyright 2013-2014 DataStax, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

try:
    import unittest2 as unittest
except ImportError:
    import unittest # noqa

from mock import Mock, NonCallableMagicMock
from threading import Thread, Event

from cassandra.cluster import Session
from cassandra.connection import Connection, MAX_STREAM_PER_CONNECTION
from cassandra.pool import Host, HostConnectionPool, NoConnectionsAvailable
from cassandra.policies import HostDistance, SimpleConvictionPolicy


class HostConnectionPoolTests(unittest.TestCase):

    def make_session(self):
        session = NonCallableMagicMock(spec=Session, keyspace='foobarkeyspace')
        session.cluster.get_core_connections_per_host.return_value = 1
        session.cluster.get_max_requests_per_connection.return_value = 1
        session.cluster.get_max_connections_per_host.return_value = 1
        return session

    def test_borrow_and_return(self):
        host = Mock(spec=Host, address='ip1')
        session = self.make_session()
        conn = NonCallableMagicMock(spec=Connection, in_flight=0, is_defunct=False, is_closed=False)
        session.cluster.connection_factory.return_value = conn

        pool = HostConnectionPool(host, HostDistance.LOCAL, session)
        session.cluster.connection_factory.assert_called_once_with(host.address)

        c = pool.borrow_connection(timeout=0.01)
        self.assertIs(c, conn)
        self.assertEqual(1, conn.in_flight)
        conn.set_keyspace_blocking.assert_called_once_with('foobarkeyspace')

        pool.return_connection(conn)
        self.assertEqual(0, conn.in_flight)
        self.assertNotIn(conn, pool._trash)

    def test_failed_wait_for_connection(self):
        host = Mock(spec=Host, address='ip1')
        session = self.make_session()
        conn = NonCallableMagicMock(spec=Connection, in_flight=0, is_defunct=False, is_closed=False)
        session.cluster.connection_factory.return_value = conn

        pool = HostConnectionPool(host, HostDistance.LOCAL, session)
        session.cluster.connection_factory.assert_called_once_with(host.address)

        pool.borrow_connection(timeout=0.01)
        self.assertEqual(1, conn.in_flight)

        conn.in_flight = MAX_STREAM_PER_CONNECTION

        # we're already at the max number of requests for this connection,
        # so we this should fail
        self.assertRaises(NoConnectionsAvailable, pool.borrow_connection, 0)

    def test_successful_wait_for_connection(self):
        host = Mock(spec=Host, address='ip1')
        session = self.make_session()
        conn = NonCallableMagicMock(spec=Connection, in_flight=0, is_defunct=False, is_closed=False)
        session.cluster.connection_factory.return_value = conn

        pool = HostConnectionPool(host, HostDistance.LOCAL, session)
        session.cluster.connection_factory.assert_called_once_with(host.address)

        pool.borrow_connection(timeout=0.01)
        self.assertEqual(1, conn.in_flight)

        def get_second_conn():
            c = pool.borrow_connection(1.0)
            self.assertIs(conn, c)
            pool.return_connection(c)

        t = Thread(target=get_second_conn)
        t.start()

        pool.return_connection(conn)
        t.join()
        self.assertEqual(0, conn.in_flight)

    def test_all_connections_trashed(self):
        host = Mock(spec=Host, address='ip1')
        session = self.make_session()
        conn = NonCallableMagicMock(spec=Connection, in_flight=0, is_defunct=False, is_closed=False)
        session.cluster.connection_factory.return_value = conn
        session.cluster.get_core_connections_per_host.return_value = 1

        # manipulate the core connection setting so that we can
        # trash the only connection
        pool = HostConnectionPool(host, HostDistance.LOCAL, session)
        session.cluster.get_core_connections_per_host.return_value = 0
        pool._maybe_trash_connection(conn)
        session.cluster.get_core_connections_per_host.return_value = 1

        submit_called = Event()

        def fire_event(*args, **kwargs):
            submit_called.set()

        session.submit.side_effect = fire_event

        def get_conn():
            conn.reset_mock()
            c = pool.borrow_connection(1.0)
            self.assertIs(conn, c)
            self.assertEqual(1, conn.in_flight)
            conn.set_keyspace_blocking.assert_called_once_with('foobarkeyspace')
            pool.return_connection(c)

        t = Thread(target=get_conn)
        t.start()

        submit_called.wait()
        self.assertEqual(1, pool._scheduled_for_creation)
        session.submit.assert_called_once_with(pool._create_new_connection)

        # now run the create_new_connection call
        pool._create_new_connection()

        t.join()
        self.assertEqual(0, conn.in_flight)

    def test_spawn_when_at_max(self):
        host = Mock(spec=Host, address='ip1')
        session = self.make_session()
        conn = NonCallableMagicMock(spec=Connection, in_flight=0, is_defunct=False, is_closed=False)
        session.cluster.connection_factory.return_value = conn

        # core conns = 1, max conns = 2
        session.cluster.get_max_connections_per_host.return_value = 2

        pool = HostConnectionPool(host, HostDistance.LOCAL, session)
        session.cluster.connection_factory.assert_called_once_with(host.address)

        pool.borrow_connection(timeout=0.01)
        self.assertEqual(1, conn.in_flight)

        # make this conn full
        conn.in_flight = MAX_STREAM_PER_CONNECTION

        # we don't care about making this borrow_connection call succeed for the
        # purposes of this test, as long as it results in a new connection
        # creation being scheduled
        self.assertRaises(NoConnectionsAvailable, pool.borrow_connection, 0)
        session.submit.assert_called_once_with(pool._create_new_connection)

    def test_return_defunct_connection(self):
        host = Mock(spec=Host, address='ip1')
        session = self.make_session()
        conn = NonCallableMagicMock(spec=Connection, in_flight=0, is_defunct=False, is_closed=False)
        session.cluster.connection_factory.return_value = conn

        pool = HostConnectionPool(host, HostDistance.LOCAL, session)
        session.cluster.connection_factory.assert_called_once_with(host.address)

        pool.borrow_connection(timeout=0.01)
        conn.is_defunct = True
        session.cluster.signal_connection_failure.return_value = False
        pool.return_connection(conn)

        # the connection should be closed a new creation scheduled
        conn.close.assert_called_once()
        session.submit.assert_called_once()
        self.assertFalse(pool.is_shutdown)

    def test_return_defunct_connection_on_down_host(self):
        host = Mock(spec=Host, address='ip1')
        session = self.make_session()
        conn = NonCallableMagicMock(spec=Connection, in_flight=0, is_defunct=False, is_closed=False)
        session.cluster.connection_factory.return_value = conn

        pool = HostConnectionPool(host, HostDistance.LOCAL, session)
        session.cluster.connection_factory.assert_called_once_with(host.address)

        pool.borrow_connection(timeout=0.01)
        conn.is_defunct = True
        session.cluster.signal_connection_failure.return_value = True
        pool.return_connection(conn)

        # the connection should be closed a new creation scheduled
        session.cluster.signal_connection_failure.assert_called_once()
        conn.close.assert_called_once()
        self.assertFalse(session.submit.called)
        self.assertTrue(pool.is_shutdown)

    def test_return_closed_connection(self):
        host = Mock(spec=Host, address='ip1')
        session = self.make_session()
        conn = NonCallableMagicMock(spec=Connection, in_flight=0, is_defunct=False, is_closed=True)
        session.cluster.connection_factory.return_value = conn

        pool = HostConnectionPool(host, HostDistance.LOCAL, session)
        session.cluster.connection_factory.assert_called_once_with(host.address)

        pool.borrow_connection(timeout=0.01)
        conn.is_closed = True
        session.cluster.signal_connection_failure.return_value = False
        pool.return_connection(conn)

        # a new creation should be scheduled
        session.submit.assert_called_once()
        self.assertFalse(pool.is_shutdown)

    def test_host_instantiations(self):
        """
        Ensure Host fails if not initialized properly
        """

        self.assertRaises(ValueError, Host, None, None)
        self.assertRaises(ValueError, Host, '127.0.0.1', None)
        self.assertRaises(ValueError, Host, None, SimpleConvictionPolicy)

    def test_host_equality(self):
        """
        Test host equality has correct logic
        """

        a = Host('127.0.0.1', SimpleConvictionPolicy)
        b = Host('127.0.0.1', SimpleConvictionPolicy)
        c = Host('127.0.0.2', SimpleConvictionPolicy)

        self.assertEqual(a, b, 'Two Host instances should be equal when sharing.')
        self.assertNotEqual(a, c, 'Two Host instances should NOT be equal when using two different addresses.')
        self.assertNotEqual(b, c, 'Two Host instances should NOT be equal when using two different addresses.')