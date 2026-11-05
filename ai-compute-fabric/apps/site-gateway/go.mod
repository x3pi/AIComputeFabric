module github.com/aicf/ai-compute-fabric/apps/site-gateway

go 1.26.0

toolchain go1.26.10

require (
	github.com/aicf/ai-compute-fabric/contracts/gen/go v0.0.0-00010101000000-000000000000
	github.com/aicf/ai-compute-fabric/core/vram-estimator v0.0.0
	google.golang.org/grpc v1.83.2
)

require (
	golang.org/x/crypto v0.57.0 // indirect
	golang.org/x/net v0.60.0 // indirect
	golang.org/x/sys v0.48.0 // indirect
	golang.org/x/text v0.42.0 // indirect
	google.golang.org/genproto/googleapis/rpc v0.0.0-20260526163538-3dc84a4a5aaa // indirect
	google.golang.org/protobuf v1.36.11 // indirect
)

replace github.com/aicf/ai-compute-fabric/contracts/gen/go => ../../contracts/gen/go

replace github.com/aicf/ai-compute-fabric/core/vram-estimator => ../../core/vram-estimator
