PY ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)
GOLANGCI_LINT ?= $(or $(shell command -v golangci-lint 2>/dev/null),$(abspath $(wildcard ../.aicf-work/gobin/golangci-lint)),golangci-lint)
COSIGN ?= $(or $(shell command -v cosign 2>/dev/null),$(abspath $(wildcard ../.aicf-work/bin/cosign)),cosign)

.PHONY: lint-go lint-py verify-dev verify-release-v1 test-go test-py proto build-cross
verify-dev: test-go test-py proto build-cross   ## checks that need no hardware (dev machine)

verify-release-v1: verify-dev lint-go lint-py   ## full release-candidate gate (tests, cross-builds, linters, SBOM signatures)
	COSIGN="$(COSIGN)" bash deploy/security/verify_sboms.sh

test-go:
	cd core/vram-estimator && gofmt -l . | (! grep .) && go vet ./... && go test -race -cover ./...
	cd apps/worker-guard && gofmt -l . | (! grep .) && go vet ./... && go test -race -cover ./...
	cd apps/site-gateway && gofmt -l . | (! grep .) && go vet ./... && go test -race -cover ./...
	cd apps/global-control-plane && gofmt -l . | (! grep .) && go vet ./... && go test -race -cover ./...
	cd contracts/gen/go && go vet ./... && go test ./...

lint-go:   ## golangci-lint (config in .golangci.yml); GOLANGCI_LINT=/path/to/golangci-lint
	@for m in core/vram-estimator apps/worker-guard apps/site-gateway apps/global-control-plane; do (cd $$m && "$(GOLANGCI_LINT)" run ./...) || exit 1; done

lint-py:
	$(PY) -m ruff check .
	$(PY) -m mypy

# Browser end-to-end (needs Chrome, a GPU and a model; NOT part of verify-dev):
#   cd tests/e2e && npm install && node dashboard_e2e.mjs --gateway ... --guard ... --server ... --model ... --out report.json

# Both OS targets must compile on every change. Compiling is NOT running: the Windows build has never
# been executed on a Windows machine (see plan B.8).
build-cross:
	cd apps/worker-guard && GOOS=linux GOARCH=amd64 go build -o /dev/null . && GOOS=windows GOARCH=amd64 go build -o /dev/null .
	cd apps/site-gateway && GOOS=linux GOARCH=amd64 go build -o /dev/null . && GOOS=windows GOARCH=amd64 go build -o /dev/null .
	cd apps/global-control-plane && GOOS=linux GOARCH=amd64 go build -o /dev/null ./cmd/gcp-server && GOOS=windows GOARCH=amd64 go build -o /dev/null ./cmd/gcp-server

test-py:
	$(PY) -m pytest -q

gen:   ## regenerate Python bindings (Go bindings are committed from the same protos with protoc-gen-go); then `cd contracts/gen/go && go test -update` only after an intended schema change
	protoc -Icontracts/protobuf --python_out=contracts/gen/python contracts/protobuf/aicf/v1/fabric.proto contracts/protobuf/aicf/v1/gateway.proto

proto:
	protoc -Icontracts/protobuf --descriptor_set_out=/dev/null contracts/protobuf/aicf/v1/fabric.proto contracts/protobuf/aicf/v1/gateway.proto
