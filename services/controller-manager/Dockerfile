FROM golang:1.23-alpine AS builder
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -o /bin/manager ./cmd/manager

FROM alpine:3.20
# docker CLI needed to run docker compose as subprocess
RUN apk add --no-cache docker-cli
COPY --from=builder /bin/manager /usr/local/bin/manager
ENTRYPOINT ["/usr/local/bin/manager"]
