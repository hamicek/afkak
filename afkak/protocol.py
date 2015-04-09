# -*- coding: utf-8 -*-
# Copyright (C) 2014 Cyan, Inc.
#
# PROPRIETARY NOTICE
# This Software consists of confidential information.  Trade secret law and
# copyright law protect this Software.  The above notice of copyright on this
# Software does not indicate any actual or intended publication of such
# Software.

from __future__ import absolute_import

import logging

from twisted.internet.error import ConnectionDone
from twisted.protocols.basic import Int32StringReceiver

log = logging.getLogger(__name__)


class KafkaProtocol(Int32StringReceiver):
    """
    Very thin wrapper around the Int32StringReceiver
    Simply knows to call its factory.handleResponse()
    method with the string received by stringReceived() and
    to cleanup the factory reference when the connection is lost
    """
    factory = None
    closing = False  # set by factory so we know to expect connectionLost
    MAX_LENGTH = 4 * 1024 * 1024

    def stringReceived(self, string):
        self.factory.handleResponse(string)

    def connectionLost(self, reason=ConnectionDone):
        if not (self.closing and reason.check(ConnectionDone)):
            log.warning("Lost Connection to Kafka Broker:%r", reason)
        self.factory = None

    def lengthLimitExceeded(self, length):
        log.error("KafkaProtocol Max Length:%d exceeded:%d",
                  self.MAX_LENGTH, length)
        self.transport.loseConnection()
