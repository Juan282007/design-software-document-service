# design-software-document-service (Python/FastAPI)

Esqueleto hexagonal (ADR-006): src/document/{domain,application,adapter} + apps/ por deployable
(document-api, template-api, pdf-renderer-worker, document-lifecycle-worker). Python usa inbound/outbound.

## template-api (HU-DOC-001 — Gestionar plantillas documentales)

Deployable: `apps/template_api/main.py` (`uvicorn apps.template_api.main:app`).

| Método | Ruta (`/api/v1`) | Permiso IAM |
|---|---|---|
| GET | `/document-templates` (filtros `output_type`, `code`, `is_active`; `page`, `page_size`, `sort=<campo>:<asc\|desc>`) | `DOC_TEMPLATE_VIEW` |
| GET | `/document-templates/{id o code}` | `DOC_TEMPLATE_VIEW` |
| POST | `/document-templates` (version inicial 1, `output_type` ∈ PDF, EXCEL, WORD) | `DOC_TEMPLATE_MANAGE` |
| PUT | `/document-templates/{id}` (incrementa `version`) | `DOC_TEMPLATE_MANAGE` |
| DELETE | `/document-templates/{id}` (desactiva, 204) | `DOC_TEMPLATE_MANAGE` |
| POST | `/document-templates/{id}/preview` (no crea registros en `document` ni `document_version`) | `DOC_TEMPLATE_MANAGE` |

- Motor de plantillas: Handlebars (`pybars3`). `code` es el identificador estable para los workers y es único en BD (`uq_document_template_code`).
- Paginación: `page`, `page_size`, `total_items`, `total_pages`. Errores: `{"error": {"code", "message", "details", "trace_id"}}`.
- Variables de entorno: `DB_URL` (o `DATABASE_URL`), `IAM_JWKS_URL`, `JWT_ISSUER` (JWT RS256 emitido por iam-service).

### Pruebas

```bash
pip install -e ".[dev]"
pytest                      # unitarias y JWT/JWKS; las de integración se omiten sin URL
TEST_POSTGRES_DB_URL=postgresql+psycopg://user:pass@host:5432/document_db TEST_AMQP_URL=amqp://user:pass@host:5672/%2F pytest   # incluye las de integración
```

La integración requiere aplicar antes el changelog de `design-software-document-db` (incluye el changeset `971`).

Alcance de la prueba JWT/JWKS: `tests/test_jwt_jwks.py` valida tokens RS256 contra un servidor JWKS simulado con un token de formato IAM; no se ha ejecutado contra un iam-service real.

## document-api (HU-DOC-002 — Solicitar generación asíncrona de documentos)

Deployable: `apps/document_api/main.py` (`uvicorn apps.document_api.main:app`).

| Método | Ruta (`/api/v1`) | Permiso IAM |
|---|---|---|
| POST | `/documents/generate` (responde 202 con `document_id` y `status = GENERATING`) | `DOC_CREATE` |
| GET | `/documents/{id}` (metadatos; nunca devuelve `storage_key` ni URL de descarga) | `DOC_VIEW` |

- `POST /documents/generate`: valida que exista una plantilla **activa** para `template_code` (si no, `404 TEMPLATE_NOT_FOUND` y no se encola), valida `domain` en SCHEDULE, FICHA, CERTIFICATE, ACTOR o REPORT, crea el registro en `document.document` con estado `GENERATING` y publica el mensaje en `document-generation-queue`.
- Mensaje publicado (JSON, persistente): `{"document_id", "template_code", "data_payload", "correlation_id"}`. El `correlation_id` se toma del header `X-Correlation-Id` (si no llega, se genera) y se devuelve en la respuesta y en las propiedades AMQP.
- Si el broker no acepta el mensaje: `503 GENERATION_QUEUE_UNAVAILABLE` y el documento queda en `GENERATION_FAILED`.
- Errores: `422 VALIDATION_ERROR` (payload inválido), `401 UNAUTHORIZED`, `403 FORBIDDEN`, `404 DOCUMENT_NOT_FOUND`.
- Variables de entorno: `DB_URL`, `IAM_JWKS_URL`, `JWT_ISSUER`, `AMQP_URL` (por defecto `amqp://guest:guest@localhost:5672/%2F`) y `GENERATION_QUEUE` (por defecto `document-generation-queue`).
- Requiere el changeset `972` de `design-software-document-db` (`storage_key` y `mime_type` dejan de ser obligatorios mientras el documento está en `GENERATING`).

Pruebas de integración con broker: definir `TEST_POSTGRES_DB_URL` y `TEST_AMQP_URL` (RabbitMQ) además de las variables de la sección anterior.

## document-api: consulta y descarga (HU-DOC-003 — Consultar y descargar documentos generados)

| Método | Ruta (`/api/v1`) | Permiso IAM |
|---|---|---|
| GET | `/documents` (filtros `owner_service`, `owner_entity_id`, `domain`, `status`; `page`, `page_size`, `sort=<campo>:<asc|desc>`) | `DOC_VIEW` |
| GET | `/documents/{id}` | `DOC_VIEW` |
| GET | `/documents/{id}/versions` (arreglo con el historial, orden descendente por `version_number`) | `DOC_VIEW` |
| GET | `/documents/{id}/versions/{version_id}` | `DOC_VIEW` |
| GET | `/documents/{id}/download-url` (URL firmada S3/MinIO) | `DOC_DOWNLOAD` |

- **Acceso por alcance (scope):** el token de IAM trae pares `FEATURE:ALCANCE`. `GLOBAL` ve todos los documentos y `TRAINING_CENTER` solo los de su centro (claim `training_center_id` del token, que `POST /documents/generate` guarda en `document.training_center_id`). Cualquier otro alcance, o un feature sin alcance, responde `403 FORBIDDEN`. Un documento fuera del alcance responde igual que uno inexistente (`404 DOCUMENT_NOT_FOUND`), para no revelar su existencia. Los documentos sin `training_center_id` solo los ve `GLOBAL`.
- **Descarga:** solo si `status = AVAILABLE`; en cualquier otro estado responde `409 DOCUMENT_NOT_AVAILABLE`. Respuesta: `{"document_id", "url", "expires_in"}` con TTL por defecto de 300 s. Si el storage falla: `503 STORAGE_UNAVAILABLE`.
- **`storage_key` nunca se devuelve** en documentos, versiones ni en la descarga.
- Paginación del listado: `page`, `page_size` (máx. 100), `total_items`, `total_pages`. `sort` admite `created_at`, `updated_at`, `title`, `status`, `domain` y `owner_service` (por defecto `created_at:desc`); otro valor responde `400 INVALID_SORT`.
- Variables de entorno adicionales: `STORAGE_ENDPOINT`, `STORAGE_BUCKET`, `STORAGE_ACCESS_KEY`, `STORAGE_SECRET_KEY`, `STORAGE_REGION` (por defecto `us-east-1`) y `SIGNED_URL_TTL_SECONDS` (por defecto `300`).
- `STORAGE_ENDPOINT` debe ser alcanzable por el cliente que descarga: la URL firmada se construye con ese host.
- Requiere el changeset `973` de `design-software-document-db` (columna `training_center_id` e índices de los filtros).

Prueba de integración con object storage: definir `TEST_POSTGRES_DB_URL` y `TEST_S3_ENDPOINT` (y opcionalmente `TEST_S3_ACCESS_KEY` / `TEST_S3_SECRET_KEY`, por defecto `minioadmin`). Verifica contra un MinIO real que la URL firmada descarga el archivo, que una firma alterada se rechaza y que la URL expira.

## pdf-renderer-worker (HU-DOC-004 — Renderizar, versionar y publicar eventos de documento)

Deployable: `apps/pdf_renderer_worker/main.py` (`python -m apps.pdf_renderer_worker.main`).

**Flujo.** Consume `document-generation-queue` (el mensaje que publica `POST /documents/generate`: `{ document_id, template_code, data_payload, correlation_id }`) y para cada mensaje:

1. Resuelve la plantilla activa por `template_code` y renderiza el HTML con Handlebars (`pybars3`).
2. Convierte el HTML a PDF (`xhtml2pdf`, Python puro, sin dependencias del sistema) y lo sube al object storage con la clave `<domain>/<año>/<mes>/<owner_entity_id>/<document_id>-v<n>.pdf`.
3. En una sola transacción registra el `document_version` (con el `correlation_id`) y actualiza `document` (`storage_key`, `mime_type`, `size_bytes`, `status = AVAILABLE`, `row_version`), con el actor de sistema `00000000-0000-0000-0000-000000000000`.
4. Publica los eventos con el envelope estándar en el topic `document-events` (routing key = tipo de evento): `document.document.generated` (solo la primera versión) y `document.version.created` (cada versión).
5. Hace `ack` del mensaje solo cuando quedó resuelto (procesado, duplicado o enviado a la DLQ). Si no puede resolverlo (por ejemplo, la DLQ no responde), lo devuelve a la cola.

- **Nueva versión:** un mensaje para un documento que ya está `AVAILABLE` crea la versión siguiente y apunta `document.storage_key` a ella; solo publica `document.version.created`. Si esa regeneración falla, el documento conserva su versión vigente.
- **Idempotencia:** `document_version.correlation_id` (único por documento). Un mensaje repetido no renderiza ni versiona de nuevo; republica los eventos con el mismo `event_id` (entrega al menos una vez; los consumidores deben deduplicar por `event_id`).
- **Reintentos:** `RENDER_MAX_RETRIES` (por defecto 3) con espera exponencial desde `RENDER_BACKOFF_SECONDS` (por defecto 1 s, tope 30 s). Agotados, un documento que esperaba su primer archivo queda `GENERATION_FAILED`, el mensaje original va a la DLQ `pdf-renderer.dlq` (cabecera `x-failure-reason`) y no se publica `document.document.generated`.
- **Sin reintentos (DLQ directa):** mensaje inválido, documento inexistente o archivado/expirado, plantilla inexistente o inactiva, `output_type` distinto de PDF y plantilla que no produce contenido.
- **Seguridad del render:** la plantilla no puede cargar nada fuera del propio documento. El motor de PDF usa la política estricta de `xhtml2pdf` (`ResourceAccessPolicy.server`): sin acceso a la red (evita SSRF hacia servicios internos), sin lectura de archivos locales (credenciales, configuración) y con límites de tamaño y de tiempo. Solo se admiten recursos incrustados (`data:`); cualquier otro se omite del PDF y queda en los logs.
- **Métricas Prometheus** en `:METRICS_PORT/metrics`: `document_render_messages_total{outcome}`, `document_render_duration_seconds`, `document_render_retries_total`, `document_render_dead_lettered_total` y `document_generation_queue_depth`. Logs JSON con `document_id`, `correlation_id`, `outcome` y `reason`.
- Variables de entorno: `DB_URL`, `AMQP_URL`, `GENERATION_QUEUE`, `DLQ_QUEUE` (`pdf-renderer.dlq`), `EVENTS_EXCHANGE` (`document-events`), `STORAGE_ENDPOINT`, `STORAGE_BUCKET`, `STORAGE_ACCESS_KEY`, `STORAGE_SECRET_KEY`, `RENDER_MAX_RETRIES`, `RENDER_BACKOFF_SECONDS`, `METRICS_PORT` (9108) y `LOG_LEVEL`. El bucket debe existir.
- Requiere el changeset `974` de `design-software-document-db` (`document_version.correlation_id`).
- Alcance: no incluye el `document-lifecycle-worker`.

Prueba de punta a punta (API de generación, worker, MinIO, PostgreSQL, eventos y descarga firmada): `tests/test_pdf_renderer_worker_integration.py`, con `TEST_POSTGRES_DB_URL`, `TEST_AMQP_URL` y `TEST_S3_ENDPOINT`.
