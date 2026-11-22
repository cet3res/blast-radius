# The scorer builds this on linux/amd64 and runs it with no network. Same base
# image the commits were labeled in; the selector needs only the standard library.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
COPY selector.py fingerprint.py dependencies.json.gz keys.json.gz /app/
# Build time is not charged: import the pinned sqlglot once here and keep its
# fingerprinted import state and compiled modules. The selector checks they match
# this Python and these sources, and recomputes them at run time if not.
RUN chmod 0644 /app/selector.py /app/fingerprint.py /app/dependencies.json.gz /app/keys.json.gz \
    && (python -B /app/selector.py --prepare || echo "prepare failed; baseline will be computed at run time") \
    && chmod -R a+rX /app
ENV PYTHONDONTWRITEBYTECODE=1
CMD ["python", "-B", "/app/selector.py"]
