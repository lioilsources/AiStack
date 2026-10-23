.PHONY: build sparkctl test clean

# broker běží na SPARKu (linux/arm64); sparkctl i na Macu (darwin/arm64)
build: sparkctl
	CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build -o bin/spark-broker ./cmd/spark-broker

sparkctl:
	CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build -o bin/sparkctl-linux-arm64 ./cmd/sparkctl
	CGO_ENABLED=0 GOOS=darwin GOARCH=arm64 go build -o bin/sparkctl-darwin-arm64 ./cmd/sparkctl

test:
	go test -race ./...

clean:
	rm -rf bin
