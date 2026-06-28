FROM golang:1.23-alpine AS builder
WORKDIR /src
COPY go.mod .
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -o /gen-queue ./cmd/gen-queue

FROM scratch
COPY --from=builder /gen-queue /gen-queue
ENTRYPOINT ["/gen-queue"]
