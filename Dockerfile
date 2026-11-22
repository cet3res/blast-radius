# The scorer builds this on linux/amd64 (with network, 10 minutes) and runs it with no
# network. Same base image the commits were labeled in.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
# Running selected tests while deciding (README: allowed, paid in CPU) needs sqlglot's
# tests and fixtures at the pinned commit and the packages they import, at the locked
# versions. If either step fails the selector still works, without running tests.
COPY requirements-tests.txt /app/requirements-tests.txt
RUN pip install --no-cache-dir --disable-pip-version-check --no-deps -r /app/requirements-tests.txt \
    || echo "test dependencies unavailable"
RUN (python -c "import urllib.request; urllib.request.urlretrieve('https://github.com/tobymao/sqlglot/archive/eb11aec4be8cb2122afe713a4816662c3f0fb2fa.tar.gz', '/tmp/sqlglot.tar.gz')" \
     && mkdir -p /app/sqlglot-src \
     && tar -xzf /tmp/sqlglot.tar.gz -C /app/sqlglot-src --strip-components=1 \
     && rm -f /tmp/sqlglot.tar.gz \
     && python -m compileall -q /app/sqlglot-src/tests) \
    || echo "sqlglot checkout unavailable: selected tests will not be run"
COPY selector.py fingerprint.py dependencies.json.gz keys.json.gz values.json.gz /app/
# Build time is not charged: import the pinned sqlglot once here and keep its
# fingerprinted import state, compiled modules, TPC step results and test costs. The
# selector checks they match this Python and these sources, and recomputes the state
# at run time if not.
RUN chmod 0644 /app/selector.py /app/fingerprint.py /app/dependencies.json.gz /app/keys.json.gz /app/values.json.gz \
    && (python -B /app/selector.py --prepare || echo "prepare failed; baseline will be computed at run time") \
    && chmod -R a+rX /app
ENV PYTHONDONTWRITEBYTECODE=1
CMD ["python", "-B", "/app/selector.py"]
