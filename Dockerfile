# ---- bridge (Go + whatsmeow, cgo sqlite) ----
FROM golang:1.26-bookworm AS bridge
WORKDIR /b
COPY bridge/go.mod bridge/go.sum ./
RUN go mod download
COPY bridge/main.go ./
RUN CGO_ENABLED=1 go build -trimpath -o wabridge .

# ---- app (Python) ----
FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*
ARG GIT_SHA=unknown
ARG RAILWAY_GIT_COMMIT_SHA=
ENV GIT_SHA=${RAILWAY_GIT_COMMIT_SHA:-$GIT_SHA}
WORKDIR /srv
COPY app/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ ./app/
COPY --from=bridge /b/wabridge ./bridge/wabridge
COPY entrypoint.sh ./
RUN chmod +x entrypoint.sh ./bridge/wabridge
ENV STORE_DIR=/data/store BRIDGE_LOG=/data/bridge.log PORT=8080
EXPOSE 8080
CMD ["./entrypoint.sh"]
