# ------------------------------------------------------------------------------
# ONE container image for all 7 Lambda functions (implementation guide §7.3).
# Each function chooses its entry point with ImageConfig.Command in
# infra/cloudformation/app.yaml, e.g. ["app.lambda_handlers.worker"].
#
# Why an image instead of a zip: pypdfium2, pdfplumber and Pillow ship native
# libraries; an image packages them reliably (and allows up to 10 GB).
#
# Build for Graviton (arm64), which is what the functions run on:
#   docker build --platform linux/arm64 -t slm:<git-sha> .
# ------------------------------------------------------------------------------
FROM public.ecr.aws/lambda/python:3.12

# uv only exists in the build step; it reads uv.lock so the image gets EXACTLY
# the dependency versions we tested with.
COPY --from=ghcr.io/astral-sh/uv:0.12.13 /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock ./
RUN uv export --frozen --no-dev --no-hashes --no-emit-project -o /tmp/requirements.txt \
 && uv pip install --no-cache --python python3.12 --target "${LAMBDA_TASK_ROOT}" -r /tmp/requirements.txt \
 && rm /tmp/requirements.txt /usr/local/bin/uv

# Application code only: no tests, docs, infra or local data (see .dockerignore).
COPY app/ ${LAMBDA_TASK_ROOT}/app/

# Default entry point; every function overrides it in CloudFormation.
CMD ["app.lambda_handlers.api"]
