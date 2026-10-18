"""RabbitMQ (AMQP) adapter that publishes to document-generation-queue."""

from __future__ import annotations

import json
import os

import pika
from pika.exceptions import AMQPError

from document.domain.model.document import GenerationQueueUnavailableError, GenerationRequest

DEFAULT_QUEUE = "document-generation-queue"


class RabbitMqDocumentGenerationPublisher:
    def __init__(self, amqp_url: str | None = None, queue: str | None = None) -> None:
        self._amqp_url = amqp_url or os.getenv("AMQP_URL", "amqp://guest:guest@localhost:5672/%2F")
        self._queue = queue or os.getenv("GENERATION_QUEUE", DEFAULT_QUEUE)

    def publish(self, request: GenerationRequest) -> None:
        body = json.dumps(
            {
                "document_id": str(request.document_id),
                "template_code": request.template_code,
                "data_payload": request.data_payload,
                "correlation_id": request.correlation_id,
            }
        ).encode("utf-8")
        parameters = pika.URLParameters(self._amqp_url)
        parameters.socket_timeout = 5
        parameters.blocked_connection_timeout = 5
        try:
            connection = pika.BlockingConnection(parameters)
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
                        correlation_id=request.correlation_id,
                    ),
                    mandatory=True,
                )
            finally:
                connection.close()
        except (AMQPError, OSError) as error:
            raise GenerationQueueUnavailableError("No fue posible publicar en document-generation-queue") from error
