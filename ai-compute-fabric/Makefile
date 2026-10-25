PY ?= python3

.PHONY: verify-dev test-go test-py proto
verify-dev: test-go test-py proto   ## checks that need no hardware (dev machine)

test-go:
	cd core/vram-estimator && gofmt -l . | (! grep .) && go vet ./... && go test -race -cover ./...

test-py:
	$(PY) -m pytest -q

proto:
	protoc -Icontracts/protobuf --descriptor_set_out=/dev/null contracts/protobuf/aicf/v1/fabric.proto

# NOT implemented yet (needs real PCs / Legal / network): the hardware gates in
# KE_HOACH...HF.md Parts C-D, `make verify-release-v1`, pilot SLA scripts.
