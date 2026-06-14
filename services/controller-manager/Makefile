.PHONY: build lint test clean

build:
	CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build -o bin/manager ./cmd/manager

lint:
	golangci-lint run ./...

test:
	go test ./...

clean:
	rm -f bin/manager
