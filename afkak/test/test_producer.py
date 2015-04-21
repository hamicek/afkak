import logging
import uuid

from mock import Mock, ANY, patch, call

from twisted.python.failure import Failure
from twisted.internet.defer import (
    setDebugging, Deferred, fail, succeed,
    CancelledError as tid_CancelledError
    )
from twisted.internet.base import DelayedCall
from twisted.internet.task import LoopingCall
from twisted.test.proto_helpers import MemoryReactorClock
from twisted.trial import unittest

from afkak.producer import (Producer)
import afkak.producer as aProducer

from afkak.common import (
    ProduceRequest,
    ProduceResponse,
    UnsupportedCodecError,
    UnknownTopicOrPartitionError,
    OffsetOutOfRangeError,
    BrokerNotAvailableError,
    LeaderNotAvailableError,
    NoResponseError,
    FailedPayloadsError,
    CancelledError,
    PRODUCER_ACK_NOT_REQUIRED,
    )

from afkak.kafkacodec import (create_message_set)
from testutil import (random_string)

log = logging.getLogger(__name__)
logging.basicConfig(level=1, format='%(asctime)s %(levelname)s: %(message)s')

DEBUGGING = True
setDebugging(DEBUGGING)
DelayedCall.debug = DEBUGGING


def dummyProc(messages):
    return None


class TestAfkakProducer(unittest.TestCase):
    _messages = {}
    topic = None

    def msgs(self, iterable):
        return [self.msg(x) for x in iterable]

    def msg(self, s):
        if s not in self._messages:
            self._messages[s] = '%s-%s-%s' % (s, self.id(), str(uuid.uuid4()))
        return self._messages[s]

    def setUp(self):
        super(unittest.TestCase, self).setUp()
        if not self.topic:
            self.topic = "%s-%s" % (
                self.id()[self.id().rindex(".") + 1:], random_string(10))

    def test_producer_init_simplest(self):
        producer = Producer(Mock())
        self.assertEqual(
            producer.__repr__(),
            "<Producer <class 'afkak.partitioner.RoundRobinPartitioner'>:"
            "Unbatched:1:1000>")
        d = producer.stop()
        self.successResultOf(d)

    def test_producer_init_batch(self):
        producer = Producer(Mock(), batch_send=True)
        looper = producer.sendLooper
        self.assertEqual(type(looper), LoopingCall)
        self.assertTrue(looper.running)
        d = producer.stop()
        self.successResultOf(d)
        self.assertFalse(looper.running)
        self.assertEqual(
            producer.__repr__(),
            "<Producer <class 'afkak.partitioner.RoundRobinPartitioner'>:"
            "10cnt/32768bytes/30secs:1:1000>")

    def test_producer_bad_codec_value(self):
        with self.assertRaises(UnsupportedCodecError):
            p = Producer(Mock(), codec=99)
            p.__repr__()  # STFU pyflakes

    def test_producer_bad_codec_type(self):
        with self.assertRaises(TypeError):
            p = Producer(Mock(), codec='bogus')
            p.__repr__()  # STFU pyflakes

    def test_producer_send_empty_messages(self):
        client = Mock()
        producer = Producer(client)
        d = producer.send_messages(self.topic)
        self.failureResultOf(d, ValueError)
        d = producer.stop()
        self.successResultOf(d)

    def test_producer_send_messages(self):
        first_part = 23
        client = Mock()
        ret = Deferred()
        client.send_produce_request.return_value = ret
        client.topic_partitions = {self.topic: [first_part, 101, 102, 103]}
        msgs = [self.msg("one"), self.msg("two")]
        ack_timeout = 5

        producer = Producer(client, ack_timeout=ack_timeout)
        d = producer.send_messages(self.topic, msgs=msgs)
        # Check the expected request was sent
        msgSet = create_message_set(msgs, producer.codec)
        req = ProduceRequest(self.topic, first_part, msgSet)
        client.send_produce_request.assert_called_once_with(
            [req], acks=producer.req_acks, timeout=ack_timeout,
            fail_on_error=False)
        # Check results when "response" fires
        self.assertNoResult(d)
        resp = [ProduceResponse(self.topic, first_part, 0, 10L)]
        ret.callback(resp)
        result = self.successResultOf(d)
        self.assertEqual(result, resp[0])
        d = producer.stop()
        self.successResultOf(d)

    def test_producer_send_messages_no_acks(self):
        first_part = 19
        client = Mock()
        ret = Deferred()
        client.send_produce_request.return_value = ret
        client.topic_partitions = {self.topic: [first_part, 101, 102, 103]}
        msgs = [self.msg("one"), self.msg("two")]
        ack_timeout = 5

        producer = Producer(client, ack_timeout=ack_timeout,
                            req_acks=PRODUCER_ACK_NOT_REQUIRED)
        d = producer.send_messages(self.topic, msgs=msgs)
        # Check the expected request was sent
        msgSet = create_message_set(msgs, producer.codec)
        req = ProduceRequest(self.topic, first_part, msgSet)
        client.send_produce_request.assert_called_once_with(
            [req], acks=producer.req_acks, timeout=ack_timeout,
            fail_on_error=False)
        # Check results when "response" fires
        self.assertNoResult(d)
        ret.callback([])
        result = self.successResultOf(d)
        self.assertEqual(result, None)
        d = producer.stop()
        self.successResultOf(d)

    def test_producer_send_messages_no_retry_fail(self):
        client = Mock()
        f = Failure(BrokerNotAvailableError())
        client.send_produce_request.side_effect = [fail(f)]
        client.topic_partitions = {self.topic: [0, 1, 2, 3]}
        msgs = [self.msg("one"), self.msg("two")]

        producer = Producer(client, max_req_attempts=1)
        d = producer.send_messages(self.topic, msgs=msgs)
        # Check the expected request was sent
        msgSet = create_message_set(msgs, producer.codec)
        req = ProduceRequest(self.topic, 0, msgSet)
        client.send_produce_request.assert_called_once_with(
            [req], acks=producer.req_acks, timeout=producer.ack_timeout,
            fail_on_error=False)
        self.failureResultOf(d, BrokerNotAvailableError)

        d = producer.stop()
        self.successResultOf(d)

    def test_producer_send_messages_unexpected_err(self):
        client = Mock()
        f = Failure(TypeError())
        client.send_produce_request.side_effect = [fail(f)]
        client.topic_partitions = {self.topic: [0, 1, 2, 3]}
        msgs = [self.msg("one"), self.msg("two")]

        producer = Producer(client)
        with patch.object(aProducer, 'log') as klog:
            d = producer.send_messages(self.topic, msgs=msgs)
            klog.error.assert_called_once_with(
                'Unexpected failure: %r in _handle_send_response', f)
        self.failureResultOf(d, TypeError)

        d = producer.stop()
        self.successResultOf(d)

    def test_producer_send_messages_batched(self):
        client = Mock()
        f = Failure(BrokerNotAvailableError())
        ret = [fail(f), succeed([ProduceResponse(self.topic, 0, 0, 10L)])]
        client.send_produce_request.side_effect = ret
        client.topic_partitions = {self.topic: [0, 1, 2, 3]}
        msgs = [self.msg("one"), self.msg("two")]
        clock = MemoryReactorClock()
        batch_n = 2

        producer = Producer(client, batch_every_n=batch_n, batch_send=True,
                            clock=clock)
        d = producer.send_messages(self.topic, msgs=msgs)
        # Check the expected request was sent
        msgSet = create_message_set(msgs, producer.codec)
        req = ProduceRequest(self.topic, ANY, msgSet)
        client.send_produce_request.assert_called_once_with(
            [req], acks=producer.req_acks, timeout=producer.ack_timeout,
            fail_on_error=False)
        # At first, there's no result. Have to retry due to first failure
        self.assertNoResult(d)
        clock.advance(producer._retry_interval)
        self.successResultOf(d)

        d = producer.stop()
        self.successResultOf(d)

    def test_producer_send_messages_batched_partial_success(self):
        """test_producer_send_messages_batched_partial_success
        This tests the complexity of the error handling for a single batch
        request.
        Scenario: The producer's caller sends 5 requests to two (total) topics
                  The client's metadata is such that the producer will produce
                    requests to post msgs to 5 separate topic/partition tuples
                  The batch size is reached, so the producer sends the request
                  The caller then cancels one of the requests
                  The (mock) client returns partial success in the form of a
                    FailedPayloadsError.
                  The Producer then should return the successful results and
                    retry the failed.
                  The (mock) client then "succeeds" the remaining results.
        """
        client = Mock()
        topic2 = 'tpsmbps_two'
        client.topic_partitions = {self.topic: [0, 1, 2, 3], topic2: [4, 5, 6]}

        init_resp = [ProduceResponse(self.topic, 0, 0, 10L),
                     ProduceResponse(self.topic, 1, 6, 20L),
                     ProduceResponse(topic2, 5, 0, 30L),
                     ]
        next_resp = [ProduceResponse(self.topic, 2, 0, 10L),
                     ProduceResponse(self.topic, 1, 0, 20L),
                     ProduceResponse(topic2, 4, 0, 30L),
                     ]
        failed_payloads = [(ProduceRequest(self.topic, ANY, ANY),
                            BrokerNotAvailableError()),
                           (ProduceRequest(topic2, ANY, ANY),
                            BrokerNotAvailableError()),
                           ]

        f = Failure(FailedPayloadsError(init_resp, failed_payloads))
        ret = [fail(f), succeed(next_resp)]
        client.send_produce_request.side_effect = ret

        msgs = self.msgs(range(10))
        results = []
        clock = MemoryReactorClock()

        producer = Producer(client, batch_send=True, batch_every_t=0,
                            clock=clock)
        # Send 5 total requests: 4 here, one after we make sure we didn't
        # send early
        results.append(producer.send_messages(self.topic, msgs=msgs[0:3]))
        results.append(producer.send_messages(topic2, msgs=msgs[3:5]))
        results.append(producer.send_messages(self.topic, msgs=msgs[5:8]))
        results.append(producer.send_messages(topic2, msgs=msgs[8:9]))
        # No call yet, not enough messages
        self.assertFalse(client.send_produce_request.called)
        # Enough messages to start the request
        results.append(producer.send_messages(self.topic, msgs=msgs[9:10]))
        # Before the retry, there should be some results
        self.assertEqual(init_resp[0], self.successResultOf(results[0]))
        self.assertEqual(init_resp[2], self.successResultOf(results[3]))
        # Advance the clock
        clock.advance(producer._retry_interval)
        # Check the otehr results came in
        self.assertEqual(next_resp[0], self.successResultOf(results[4]))
        self.assertEqual(next_resp[1], self.successResultOf(results[2]))
        self.assertEqual(next_resp[2], self.successResultOf(results[1]))

        d = producer.stop()
        self.successResultOf(d)

    def test_producer_send_messages_batched_fail(self):
        client = Mock()
        ret = [Deferred(), Deferred(), Deferred()]
        client.send_produce_request.side_effect = ret
        client.topic_partitions = {self.topic: [0, 1, 2, 3]}
        msgs = [self.msg("one"), self.msg("two")]
        batch_t = 5
        clock = MemoryReactorClock()

        producer = Producer(client, batch_every_t=batch_t, batch_send=True,
                            clock=clock, max_req_attempts=3)
        # Advance the clock to ensure when no messages to send no error
        clock.advance(batch_t)
        d = producer.send_messages(self.topic, msgs=msgs)
        # Check no request was yet sent
        self.assertFalse(client.send_produce_request.called)
        # Advance the clock
        clock.advance(batch_t)
        # Check the expected request was sent
        msgSet = create_message_set(msgs, producer.codec)
        req = ProduceRequest(self.topic, 0, msgSet)
        produce_request_call = call([req], acks=producer.req_acks,
                                    timeout=producer.ack_timeout,
                                    fail_on_error=False)
        produce_request_calls = [produce_request_call]
        client.send_produce_request.assert_has_calls(produce_request_calls)
        self.assertNoResult(d)
        # Fire the failure from the first request to the client
        ret[0].errback(OffsetOutOfRangeError(
            'test_producer_send_messages_batched_fail'))
        # Still no result, producer should retry first
        self.assertNoResult(d)
        # Check retry wasn't immediate
        self.assertEqual(client.send_produce_request.call_count, 1)
        # Advance the clock by the retry delay
        clock.advance(producer._retry_interval)
        # Check 2nd send_produce_request (1st retry) was sent
        produce_request_calls.append(produce_request_call)
        client.send_produce_request.assert_has_calls(produce_request_calls)
        # Fire the failure from the 2nd request to the client
        ret[1].errback(BrokerNotAvailableError(
            'test_producer_send_messages_batched_fail_2'))
        # Still no result, producer should retry one more time
        self.assertNoResult(d)
        # Advance the clock by the retry delay
        clock.advance(producer._retry_interval * 1.1)
        # Check 3nd send_produce_request (2st retry) was sent
        produce_request_calls.append(produce_request_call)
        client.send_produce_request.assert_has_calls(produce_request_calls)
        # Fire the failure from the 2nd request to the client
        ret[2].errback(LeaderNotAvailableError(
            'test_producer_send_messages_batched_fail_3'))

        self.failureResultOf(d, LeaderNotAvailableError)

        d = producer.stop()
        self.successResultOf(d)

    def test_producer_cancel_request_in_batch(self):
        # Test cancelling a request before it's begun to be processed
        client = Mock()
        client.topic_partitions = {self.topic: [0, 1, 2, 3]}
        msgs = [self.msg("one"), self.msg("two")]
        msgs2 = [self.msg("three"), self.msg("four")]
        batch_n = 3

        producer = Producer(client, batch_every_n=batch_n, batch_send=True)
        d1 = producer.send_messages(self.topic, msgs=msgs)
        # Check that no request was sent
        self.assertFalse(client.send_produce_request.called)
        d1.cancel()
        self.failureResultOf(d1, CancelledError)
        d2 = producer.send_messages(self.topic, msgs=msgs2)
        # Check that still no request was sent
        self.assertFalse(client.send_produce_request.called)
        self.assertNoResult(d2)

        d = producer.stop()
        self.successResultOf(d)

    def test_producer_cancel_request_getting_topic(self):
        # Test cancelling a request after it's begun to be processed
        client = Mock()
        client.topic_partitions = {}
        ret = Deferred()
        client.load_metadata_for_topics.return_value = ret
        msgs = [self.msg("one"), self.msg("two")]
        msgs2 = [self.msg("three"), self.msg("four")]
        batch_n = 4

        producer = Producer(client, batch_every_n=batch_n, batch_send=True)
        d1 = producer.send_messages(self.topic, msgs=msgs)
        # Check that no request was sent
        self.assertFalse(client.send_produce_request.called)
        # This will trigger the metadata lookup
        d2 = producer.send_messages(self.topic, msgs=msgs2)
        d1.cancel()
        self.failureResultOf(d1, CancelledError)
        # Check that still no request was sent
        self.assertFalse(client.send_produce_request.called)
        self.assertNoResult(d2)
        # Setup the client's topics and trigger the metadata deferred
        client.topic_partitions = {self.topic: [0, 1, 2, 3]}
        ret.callback(None)
        # Expect that only the msgs2 messages were sent
        msgSet = create_message_set(msgs2, producer.codec)
        req = ProduceRequest(self.topic, 1, msgSet)
        client.send_produce_request.assert_called_once_with(
            [req], acks=producer.req_acks, timeout=producer.ack_timeout,
            fail_on_error=False)

        d = producer.stop()
        self.successResultOf(d)

    def test_producer_stop_during_request(self):
        # Test stopping producer while it's waiting for reply from client
        client = Mock()
        f = Failure(BrokerNotAvailableError())
        ret = [fail(f), Deferred()]
        client.send_produce_request.side_effect = ret
        client.topic_partitions = {self.topic: [0, 1, 2, 3]}
        msgs = [self.msg("one"), self.msg("two")]
        clock = MemoryReactorClock()
        batch_n = 2

        producer = Producer(client, batch_every_n=batch_n, batch_send=True,
                            clock=clock)
        d = producer.send_messages(self.topic, msgs=msgs)
        # At first, there's no result. Have to retry due to first failure
        self.assertNoResult(d)
        clock.advance(producer._retry_interval)

        d2 = producer.stop()
        self.failureResultOf(d, tid_CancelledError)
        self.successResultOf(d2)

    def test_producer_stop_waiting_to_retry(self):
        # Test stopping producer while it's waiting to retry a request
        client = Mock()
        f = Failure(BrokerNotAvailableError())
        ret = [fail(f)]
        client.send_produce_request.side_effect = ret
        client.topic_partitions = {self.topic: [0, 1, 2, 3]}
        msgs = [self.msg("one"), self.msg("two")]
        clock = MemoryReactorClock()
        batch_n = 2

        producer = Producer(client, batch_every_n=batch_n, batch_send=True,
                            clock=clock)
        d = producer.send_messages(self.topic, msgs=msgs)
        # At first, there's no result. Have to retry due to first failure
        self.assertNoResult(d)
        clock.advance(producer._retry_interval / 2)

        d2 = producer.stop()
        self.failureResultOf(d, tid_CancelledError)
        self.successResultOf(d2)

    def test_producer_send_messages_unknown_topic(self):
        client = Mock()
        ds = [Deferred()]
        client.load_metadata_for_topics.return_value = ds[0]
        client.metadata_error_for_topic.return_value = 3
        client.topic_partitions = {}
        msgs = [self.msg("one"), self.msg("two")]
        ack_timeout = 5

        producer = Producer(client, ack_timeout=ack_timeout)
        d = producer.send_messages(self.topic, msgs=msgs)
        # d is waiting on result from ds[0] for load_metadata_for_topics
        # Check results when "response" fires
        self.assertNoResult(d)
        # fire it with client still reporting no metadata for topic
        ds[0].callback(None)
        self.failureResultOf(d, UnknownTopicOrPartitionError)
        self.assertFalse(client.send_produce_request.called)

        d = producer.stop()
        self.successResultOf(d)

    def test_producer_send_messages_bad_response(self):
        first_part = 68
        client = Mock()
        ret = Deferred()
        client.send_produce_request.return_value = ret
        client.topic_partitions = {self.topic: [first_part, 101, 102, 103]}
        msgs = [self.msg("one"), self.msg("two")]
        ack_timeout = 5

        producer = Producer(client, ack_timeout=ack_timeout)
        d = producer.send_messages(self.topic, msgs=msgs)
        # Check the expected request was sent
        msgSet = create_message_set(msgs, producer.codec)
        req = ProduceRequest(self.topic, first_part, msgSet)
        client.send_produce_request.assert_called_once_with(
            [req], acks=producer.req_acks, timeout=ack_timeout,
            fail_on_error=False)
        # Check results when "response" fires
        self.assertNoResult(d)
        ret.callback([])
        self.failureResultOf(d, NoResponseError)
        d = producer.stop()
        self.successResultOf(d)

    def test_producer_send_timer_failed(self):
        """test_producer_send_timer_failed
        Test that the looping call is restarted when _send_batch errs
        Somewhat artificial test to confirm that when failures occur in
        _send_batch (which cause the looping call to terminate) that the
        looping call is restarted.
        """
        client = Mock()
        client.topic_partitions = {self.topic: [0, 1, 2, 3]}
        batch_t = 5
        clock = MemoryReactorClock()

        with patch.object(aProducer, 'log') as klog:
            producer = Producer(client, batch_send=True, batch_every_t=batch_t,
                                clock=clock)
            msgs = [self.msg("one"), self.msg("two")]
            d = producer.send_messages(self.topic, msgs=msgs)
            # Check no request was yet sent
            self.assertFalse(client.send_produce_request.called)
            # Patch Producer's Deferred to throw an exception
            with patch.object(aProducer, 'Deferred') as d:
                d.side_effect = ValueError(
                    "test_producer_send_timer_failed induced failure")
                # Advance the clock
                clock.advance(batch_t)
            # Check the expected message was logged by the looping call restart
            klog.warning.assert_called_once_with('_send_timer_failed:%r: %s',
                                                 ANY, ANY)
        # Check that the looping call was restarted
        self.assertTrue(producer.sendLooper.running)

        d = producer.stop()
        self.successResultOf(d)

    def test_producer_send_timer_stopped_error(self):
        # Purely for coverage
        client = Mock()
        producer = Producer(client, batch_send=True)
        with patch.object(aProducer, 'log') as klog:
            producer._send_timer_stopped('Borg')
            klog.warning.assert_called_once_with(
                'commitTimerStopped with wrong timer:%s not:%s', 'Borg',
                producer.sendLooper)

        d = producer.stop()
        self.successResultOf(d)

    def test_producer_non_integral_batch_every_n(self):
        client = Mock()
        with self.assertRaises(TypeError):
            producer = Producer(client, batch_send=True, batch_every_n="10")
            producer.__repr__()  # STFU Pyflakes

    def test_producer_non_integral_batch_every_b(self):
        client = Mock()
        with self.assertRaises(TypeError):
            producer = Producer(client, batch_send=True, batch_every_b="10")
            producer.__repr__()  # STFU Pyflakes