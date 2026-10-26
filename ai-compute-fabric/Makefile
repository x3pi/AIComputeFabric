PY ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)

.PHONY: lint-go lint-py verify-dev test-go test-py proto build-cross
verify-dev: test-go test-py proto build-cross   ## checks that need no hardware (dev machine)

test-go:
	cd core/vram-estimator && gofmt -l . | (! grep .) && go vet ./... && go test -race -cover ./...
	cd apps/worker-guard && gofmt -l . | (! grep .) && go vet ./... && go test -race -cover ./...
	cd apps/site-gateway && gofmt -l . | (! grep .) && go vet ./... && go test -race -cover ./...
	cd apps/global-control-plane && gofmt -l . | (! grep .) && go vet ./... && go test -race -cover ./...

lint-go:   ## golangci-lint (config in .golangci.yml); GOLANGCI_LINT=/path/to/golangci-lint
	@for m in core/vram-estimator apps/worker-guard apps/site-gateway apps/global-control-plane; do (cd $$m && $${GOLANGCI_LINT:-golangci-lint} run ./...) || exit 1; done

lint-py:
	$(PY) -m ruff check .

# Browser end-to-end (needs Chrome, a GPU and a model; NOT part of verify-dev):
#   cd tests/e2e && npm install && node dashboard_e2e.mjs --gateway ... --guard ... --server ... --model ... --out report.json

# Both OS targets must compile on every change. Compiling is NOT running: the Windows build has never
# been executed on a Windows machine (see plan B.8).
build-cross:
	cd apps/worker-guard && GOOS=linux GOARCH=amd64 go build -o /dev/null . && GOOS=windows GOARCH=amd64 go build -o /dev/null .
	cd apps/site-gateway && GOOS=linux GOARCH=amd64 go build -o /dev/null . && GOOS=windows GOARCH=amd64 go build -o /dev/null .

test-py:
	$(PY) -m pytest -q

proto:
	protoc -Icontracts/protobuf --descriptor_set_out=/dev/null contracts/protobuf/aicf/v1/fabric.proto contracts/protobuf/aicf/v1/gateway.proto

# NOT implemented yet (needs real PCs / Legal / network): the hardware gates in
# KE_HOACH...HF.md Parts C-D, `make verify-release-v1`, pilot SLA scripts.
