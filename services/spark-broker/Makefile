.PHONY: build test clean

build:
	CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build -o bin/spark-broker ./cmd/spark-broker

test:
	go test -race ./...

clean:
	rm -f bin/spark-broker
