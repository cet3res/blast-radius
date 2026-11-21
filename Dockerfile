# The scorer builds this on linux/amd64 and runs it with no network. Same base
# image the commits were labeled in; the selector needs only the standard library.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
COPY selector.py fingerprint.py dependencies.json.gz /app/
RUN chmod 0644 /app/selector.py /app/fingerprint.py /app/dependencies.json.gz \
    && python -c "import ast, pathlib; [ast.parse(pathlib.Path(p).read_text()) for p in ('/app/selector.py', '/app/fingerprint.py')]"
ENV PYTHONDONTWRITEBYTECODE=1
CMD ["python", "-B", "/app/selector.py"]
