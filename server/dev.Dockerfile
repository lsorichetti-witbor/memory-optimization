FROM python:3.12

WORKDIR /app

# Local TLS-interception roots (corporate proxy or antivirus that re-signs HTTPS).
# server/certs/ ships empty, so this is a no-op on a normal network. When a .crt
# is present, pip/curl would otherwise fail with CERTIFICATE_VERIFY_FAILED and no
# hint as to why. See server/certs/README.md.
COPY server/certs/ /usr/local/share/ca-certificates/local/
RUN update-ca-certificates
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    PIP_CERT=/etc/ssl/certs/ca-certificates.crt

# Install Poetry
RUN curl -sSL https://install.python-poetry.org | python3 -
ENV PATH="/root/.local/bin:$PATH"

# Copy requirements first for better caching
COPY server/requirements.txt .
RUN pip install -r requirements.txt

# Install mem0 in editable mode using Poetry
WORKDIR /app/packages
COPY pyproject.toml .
COPY poetry.lock .
COPY README.md .
COPY mem0 ./mem0
RUN pip install -e .[graph]

# Return to app directory and copy server code
WORKDIR /app
COPY server .

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
