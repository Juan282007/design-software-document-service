import uuid

import pytest

from document.adapter.outbound.messaging.document_generation_publisher import RabbitMqDocumentGenerationPublisher
from document.domain.model.document import GenerationQueueUnavailableError, GenerationRequest


def _request() -> GenerationRequest:
    return GenerationRequest(uuid.uuid4(), "SCHEDULE_CERTIFICATE", {"name": "Ana"}, "corr-1")


@pytest.mark.parametrize(
    "amqp_url",
    [
        "amqp://guest:guest@127.0.0.1:1/%2F",
        "amqp://guest:guest@no-such-host.invalid:5672/%2F",
    ],
)
def test_unreachable_broker_raises_queue_unavailable(amqp_url: str) -> None:
    publisher = RabbitMqDocumentGenerationPublisher(amqp_url=amqp_url)

    with pytest.raises(GenerationQueueUnavailableError):
        publisher.publish(_request())
