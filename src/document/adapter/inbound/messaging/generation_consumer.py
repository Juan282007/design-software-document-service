"""AMQP consumer of document-generation-queue: acks a message only after it has been settled."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

import pika
from pika.exceptions import AMQPError

from document.application.usecase.handle_generation_message import GenerationMessageHandler

logger = logging.getLogger("pdf_renderer_worker")


class GenerationQueueConsumer:
    def __init__(
        self,
        handler: GenerationMessageHandler,
        amqp_url: str,
        queue: str,
        reconnect_delay_seconds: float = 5.0,
        on_queue_depth: Callable[[int], None] | None = None,
    ) -> None:
        self._handler = handler
        self._amqp_url = amqp_url
        self._queue = queue
        self._reconnect_delay = reconnect_delay_seconds
        self._on_queue_depth = on_queue_depth

    def run(self, stop: threading.Event | None = None, max_messages: int | None = None) -> int:
        """Consume until `stop` is set or `max_messages` were settled; returns how many were acked."""
        stop = stop or threading.Event()
        settled = 0
        while not stop.is_set() and (max_messages is None or settled < max_messages):
            try:
                settled += self._consume(stop, None if max_messages is None else max_messages - settled)
            except (AMQPError, OSError) as error:
                logger.error("broker connection lost, reconnecting", extra={"error": str(error)})
                stop.wait(self._reconnect_delay)
        return settled

    def _consume(self, stop: threading.Event, remaining: int | None) -> int:
        parameters = pika.URLParameters(self._amqp_url)
        parameters.socket_timeout = 10
        connection = pika.BlockingConnection(parameters)
        settled = 0
        try:
            channel = connection.channel()
            channel.queue_declare(queue=self._queue, durable=True)
            channel.basic_qos(prefetch_count=1)
            last_depth_report = 0.0
            for method, _properties, body in channel.consume(self._queue, inactivity_timeout=1.0):
                if self._on_queue_depth and time.monotonic() - last_depth_report >= 10:
                    depth = channel.queue_declare(queue=self._queue, durable=True, passive=True).method.message_count
                    self._on_queue_depth(depth)
                    last_depth_report = time.monotonic()
                if method is not None:
                    try:
                        # connection.sleep keeps answering heartbeats during long retry delays
                        self._handler.handle(body, sleep=connection.sleep)
                    except Exception:  # noqa: BLE001 - the message is not lost: it goes back to the queue
                        logger.exception("could not settle the message, requeueing it")
                        channel.basic_nack(method.delivery_tag, requeue=True)
                        stop.wait(1.0)
                    else:
                        channel.basic_ack(method.delivery_tag)
                        settled += 1
                if stop.is_set() or (remaining is not None and settled >= remaining):
                    break
            channel.cancel()
        finally:
            if connection.is_open:
                connection.close()
        return settled
