module github.com/aicf/ai-compute-fabric/apps/worker-guard

go 1.22

require (
	github.com/aicf/ai-compute-fabric/contracts/gen/go v0.0.0
	github.com/aicf/ai-compute-fabric/core/vram-estimator v0.0.0
	golang.org/x/sys v0.26.0
	google.golang.org/grpc v1.69.2
)

require (
	golang.org/x/net v0.30.0 // indirect
	golang.org/x/text v0.19.0 // indirect
	google.golang.org/genproto/googleapis/rpc v0.0.0-20241015192408-796eee8c2d53 // indirect
	google.golang.org/protobuf v1.36.6 // indirect
)

replace github.com/aicf/ai-compute-fabric/core/vram-estimator => ../../core/vram-estimator

replace github.com/aicf/ai-compute-fabric/contracts/gen/go => ../../contracts/gen/go
