from collections import defaultdict
from functools import partial
from itertools import count
import logging
import time

from kafka.common import TopicAndPartition
from kafka.exception import (
    ConnectionError, FailedPayloadsException, UnknownException
)
from kafka.connection import KafkaConnectionPool
from kafka.protocol import KafkaProtocol

log = logging.getLogger("kafka")


class KafkaClient(object):

    ID_GEN = count()

    def __init__(self, host, port, **kwargs):
        # We need one connection to bootstrap
        self.buffer_size = kwargs.get('buffer_size', 4096)
        self.client_id = kwargs.get('client_id', 'kafka-python')
        self.pool_size = kwargs.get('pool_size', 50)
        self.conns = {               # (host, port) -> KafkaConnection
            (host, port): KafkaConnectionPool(
                self.pool_size,
                host=host,
                port=port,
                buffer_size=self.buffer_size
            )
        }
        self.brokers = {}            # broker_id -> BrokerMetadata
        self.topics_to_brokers = {}  # topic_id -> broker_id
        self.topic_partitions = defaultdict(list)  # topic_id -> [0, 1, 2, ...]
        self._load_metadata_for_topics()

    ##################
    #   Private API  #
    ##################

    def _get_conn_for_broker(self, broker):
        """
        Get or create a connection to a broker
        """
        if (broker.host, broker.port) not in self.conns:
            self.conns[(broker.host, broker.port)] = KafkaConnectionPool(
                self.pool_size,
                host=broker.host,
                port=broker.port,
                buffer_size=self.buffer_size
            )

        return self.conns[(broker.host, broker.port)]

    def _get_leader_for_partition(self, topic, partition):
        key = TopicAndPartition(topic, partition)
        if key not in self.topics_to_brokers:
            self._load_metadata_for_topics(topic)

        if key not in self.topics_to_brokers:
            raise Exception("Partition does not exist: %s" % str(key))

        return self.topics_to_brokers[key]

    def _load_metadata_for_topics(self, *topics):
        """
        Discover brokers and metadata for a set of topics. This method will
        recurse in the event of a retry.
        """
        request_id = self._next_id()
        request = KafkaProtocol.encode_metadata_request(self.client_id,
                                                        request_id, topics)

        response = self._send_broker_unaware_request(request_id, request)
        if response is None:
            raise Exception("All servers failed to process request")

        (brokers, topics) = KafkaProtocol.decode_metadata_response(response)

        log.debug("Broker metadata: %s", brokers)
        log.debug("Topic metadata: %s", topics)

        self.brokers = brokers
        self.topics_to_brokers = {}

        for topic, partitions in topics.items():
            # Clear the list once before we add it. This removes stale entries
            # and avoids duplicates
            self.topic_partitions.pop(topic, None)

            if not partitions:
                log.info("Partition is unassigned, delay for 1s and retry")
                time.sleep(1)
                self._load_metadata_for_topics(topic)
                break

            for partition, meta in partitions.items():
                if meta.leader == -1:
                    log.info("Partition is unassigned, delay for 1s and retry")
                    time.sleep(1)
                    self._load_metadata_for_topics(topic)
                else:
                    topic_part = TopicAndPartition(topic, partition)
                    self.topics_to_brokers[topic_part] = brokers[meta.leader]
                    self.topic_partitions[topic].append(partition)

    @classmethod
    def _next_id(cls):
        """
        Generate a new correlation id
        """
        return KafkaClient.ID_GEN.next()

    def _send_broker_unaware_request(self, request_id, request):
        """
        Attempt to send a broker-agnostic request to one of the available
        brokers. Keep trying until you succeed.
        """
        for conn in self.conns.values():
            try:
                return conn.request(request_id, request)
            except BaseException as e:
                log.warning("Could not send request [%r] to server %s, "
                            "trying next server: %s" % (request, conn, e))
                continue

        return None

    def _group_request(self, payloads):
        """
        group the requests
        """
        original_keys = []
        payloads_by_broker = defaultdict(list)
        for payload in payloads:
            leader = self._get_leader_for_partition(
                payload.topic,
                payload.partition
            )
            payloads_by_broker[leader].append(payload)
            original_keys.append((payload.topic, payload.partition))
        return original_keys, payloads_by_broker

    def _send_broker_aware_request(self, payloads, encoder_fn, decoder_fn):
        """
        Group a list of request payloads by topic+partition and send them to
        the leader broker for that partition using the supplied encode/decode
        functions

        Params
        ======
        payloads: list of object-like entities with a topic and
                  partition attribute
        encode_fn: a method to encode the list of payloads to a request body,
                   must accept client_id, correlation_id, and payloads as
                   keyword arguments
        decode_fn: a method to decode a response body into response objects.
                   The response objects must be object-like and have topic
                   and partition attributes

        Return
        ======
        List of response objects in the same order as the supplied payloads
        """

        original_keys, payloads_by_broker = self._group_request(payloads)
        # Accumulate the responses in a dictionary
        acc = {}

        # keep a list of payloads that were failed to be sent to brokers
        failed_payloads = []

        # For each broker, send the list of request payloads
        for broker, payloads in payloads_by_broker.items():
            conn = self._get_conn_for_broker(broker)
            request_id = self._next_id()
            request = encoder_fn(client_id=self.client_id,
                                 correlation_id=request_id, payloads=payloads)

            # push or request
            try:
                if decoder_fn is None:
                    conn.push(request_id, request)
                else:
                    response = conn.request(request_id, request)
            except ConnectionError as e:  # ignore BufferUnderflow for now
                log.warning("Could not send request [%s] to server %s: %s" % (
                    request, conn, e))
                failed_payloads += payloads
                self.topics_to_brokers = {}  # reset metadata
                continue
            if not decoder_fn is None:
                for response in decoder_fn(response):
                    acc[(response.topic, response.partition)] = response

        if failed_payloads:
            raise FailedPayloadsException(failed_payloads)

        # Order the accumulated responses by the original key order
        return (acc[k] for k in original_keys) if acc else ()

    #################
    #   Public API  #
    #################

    def send_produce_request(self, payloads, acks=1, timeout=1000,
                             fail_on_error=True):
        """
        Encode and send some ProduceRequests

        ProduceRequests will be grouped by (topic, partition) and then
        sent to a specific broker. Output is a list of responses in the
        same order as the list of payloads specified

        Params
        ======
        payloads: list of ProduceRequest
        fail_on_error: boolean, should we raise an Exception if we
                       encounter an API error?
        Return
        ======
        list of ProduceResponse or callback(ProduceResponse), in the
        order of input payloads
        """

        encoder = partial(
            KafkaProtocol.encode_produce_request,
            acks=acks,
            timeout=timeout)

        if acks == 0:
            decoder = None
        else:
            decoder = KafkaProtocol.decode_produce_response

        resps = self._send_broker_aware_request(payloads, encoder, decoder)

        out = []
        for resp in resps:
            # Check for errors
            if fail_on_error is True and resp.error != 0:
                exception_cls = KafkaProtocol.ERROR_CODE_MAPPING.get(
                    resp.error, UnknownException
                )
                raise exception_cls(
                    "ProduceRequest for %s failed with errorcode=%d" %
                    (TopicAndPartition(resp.topic, resp.partition),
                     resp.error)
                )
            out.append(resp)
        return out

    def send_fetch_request(self, payloads, fail_on_error=True,
                           max_wait_time=100, min_bytes=4096):
        """
        Encode and send a FetchRequest

        Payloads are grouped by topic and partition so they can be pipelined
        to the same brokers.
        """
        encoder = partial(KafkaProtocol.encode_fetch_request,
                          max_wait_time=max_wait_time,
                          min_bytes=min_bytes)
        resps = self._send_broker_aware_request(
            payloads, encoder,
            KafkaProtocol.decode_fetch_response)

        out = []
        for resp in resps:
            # Check for errors
            if fail_on_error is True and resp.error != 0:
                exception_cls = KafkaProtocol.ERROR_CODE_MAPPING.get(
                    resp.error, UnknownException
                )
                raise exception_cls(
                    "FetchRequest for %s failed with errorcode=%d" %
                    (TopicAndPartition(resp.topic, resp.partition),
                        resp.error))

            out.append(resp)
        return out

    def send_offset_request(self, payloads, fail_on_error=True):
        resps = self._send_broker_aware_request(
            payloads,
            KafkaProtocol.encode_offset_request,
            KafkaProtocol.decode_offset_response)

        out = []
        for resp in resps:
            if fail_on_error is True and resp.error != 0:
                exception_cls = KafkaProtocol.ERROR_CODE_MAPPING.get(
                    resp.error, UnknownException
                )
                raise exception_cls("OffsetRequest failed with errorcode=%s",
                                    resp.error)
            out.append(resp)
        return out

    def send_offset_commit_request(self, group, payloads,
                                   fail_on_error=True, callback=None):
        encoder = partial(KafkaProtocol.encode_offset_commit_request,
                          group=group)
        decoder = KafkaProtocol.decode_offset_commit_response
        resps = self._send_broker_aware_request(payloads, encoder, decoder)

        out = []
        for resp in resps:
            if fail_on_error is True and resp.error != 0:
                exception_cls = KafkaProtocol.ERROR_CODE_MAPPING.get(
                    resp.error, UnknownException
                )
                raise exception_cls("OffsetCommitRequest failed with "
                                    "errorcode=%s", resp.error)
            out.append(resp)
        return out

    def send_offset_fetch_request(self, group, payloads,
                                  fail_on_error=True):

        encoder = partial(KafkaProtocol.encode_offset_fetch_request,
                          group=group)
        decoder = KafkaProtocol.decode_offset_fetch_response
        resps = self._send_broker_aware_request(payloads, encoder, decoder)

        out = []
        for resp in resps:
            if fail_on_error is True and resp.error != 0:
                exception_cls = KafkaProtocol.ERROR_CODE_MAPPING.get(
                    resp.error, UnknownException
                )
                raise exception_cls(
                    "OffsetCommitRequest failed with errorcode=%s",
                    resp.error)
            out.append(resp)
        return out
