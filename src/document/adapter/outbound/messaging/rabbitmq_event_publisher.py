"""RabbitMQ adapters for the integration events (topic document-events) and the DLQ."""

from __future__ import annotations

import json
import os

import pika
from pika.exceptions import AMQPError

from document.domain.model.document import RenderError
from document.domain.model.events import DomainEvent

DEFAULT_EXCHANGE = "document-events"
DEFAULT_DLQ = "pdf-renderer.dlq"
DEFAULT_AMQP_URL = "amqp://guest:guest@localhost:5672/%2F"


def _parameters(amqp_url: str) -> pika.URLParameters:
    parameters = pika.URLParameters(amqp_url)
    parameters.socket_timeout = 5
    parameters.blocked_connection_timeout = 5
    return parameters


class RabbitMqEventPublisher:
    """Publishes the standard envelope to a durable topic exchange; the routing key is the event type."""

    def __init__(self, amqp_url: str | None = None, exchange: str | None = None) -> None:
        self._amqp_url = amqp_url or os.getenv("AMQP_URL", DEFAULT_AMQP_URL)
        self._exchange = exchange or os.getenv("EVENTS_EXCHANGE", DEFAULT_EXCHANGE)

    def publish(self, event: DomainEvent) -> None:
        body = json.dumps(event.to_envelope()).encode("utf-8")
        try:
            connection = pika.BlockingConnection(_parameters(self._amqp_url))
            try:
                channel = connection.channel()
                channel.exchange_declare(exchange=self._exchange, exchange_type="topic", durable=True)
                channel.confirm_delivery()
                channel.basic_publish(
                    exchange=self._exchange,
                    routing_key=event.event_type,
                    body=body,
                    properties=pika.BasicProperties(
                        content_type="application/json",
                        delivery_mode=pika.DeliveryMode.Persistent,
                        message_id=str(event.event_id),
                        correlation_id=event.correlation_id,
                        type=event.event_type,
                    ),
                )
            finally:
                connection.close()
        except (AMQPError, OSError) as error:
            raise RenderError(f"No fue posible publicar {event.event_type}") from error


class RabbitMqDeadLetterPublisher:
    """Stores the original message, untouched, in the pdf-renderer.dlq queue with the failure reason."""

    def __init__(self, amqp_url: str | None = None, queue: str | None = None) -> None:
        self._amqp_url = amqp_url or os.getenv("AMQP_URL", DEFAULT_AMQP_URL)
        self._queue = queue or os.getenv("DLQ_QUEUE", DEFAULT_DLQ)

    def send(self, body: bytes, reason: str, correlation_id: str | None) -> None:
        headers = {"x-failure-reason": reason[:500]}
        try:
            connection = pika.BlockingConnection(_parameters(self._amqp_url))
            try:
                channel = connection.channel()
                channel.queue_declare(queue=self._queue, durable=True)
                channel.confirm_delivery()
                channel.basic_publish(
                    exchange="",
                    routing_key=self._queue,
                    body=body,
                    properties=pika.BasicProperties(
                        content_type="application/json",
                        delivery_mode=pika.DeliveryMode.Persistent,
                        correlation_id=correlation_id,
                        headers=headers,
                    ),
                    mandatory=True,
                )
            finally:
                connection.close()
        except (AMQPError, OSError) as error:
            raise RenderError("No fue posible enviar el mensaje a la DLQ") from error
