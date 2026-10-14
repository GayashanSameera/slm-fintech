"""One place that lists every Lambda entry point.

All seven functions are deployed from the SAME container image; each function's
CloudFormation definition sets `ImageConfig.Command` to one of these names
(see infra/cloudformation/app.yaml):

    authorizer         API Gateway REQUEST authorizer     -> API key to tenant
    api                API Gateway proxy integration      -> FastAPI app
    worker             SQS jobs-interactive / background  -> extraction pipeline
    intake             EventBridge (S3, GuardDuty)        -> upload to queued job
    webhook            SQS webhooks                       -> signed delivery
    retention_cleaner  DynamoDB stream (TTL deletes)      -> delete S3 objects
    scan_sweeper       EventBridge Scheduler (5 min)      -> expire / time out waiting jobs
"""

from __future__ import annotations

from app.auth.authorizer import handler as authorizer  # noqa: F401
from app.intake import handle_event as intake  # noqa: F401
from app.intake import sweep as scan_sweeper  # noqa: F401
from app.main import handler as api  # noqa: F401
from app.modules.extraction.worker import handle_sqs_event as worker  # noqa: F401
from app.retention import handle_stream as retention_cleaner  # noqa: F401
from app.webhooks import handle_sqs_event as webhook  # noqa: F401
