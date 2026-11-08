import os,sys,time,torch,torch.distributed as dist
rank=int(sys.argv[1]); ws=2
dist.init_process_group("gloo",init_method="tcp://10.42.0.1:29602",rank=rank,world_size=ws)
dev=torch.device("cuda",0); torch.cuda.set_device(dev)
print(rank,"init ok",torch.cuda.get_device_name(0),flush=True)
for mb in (16,64,128):
    n=mb*1024*1024//2
    shard=torch.randn(n//2,dtype=torch.bfloat16,device=dev)   # shard = mb/2
    out=torch.empty(n,dtype=torch.bfloat16,device=dev)
    for kind in ("all_gather_into_tensor","all_gather_list"):
        try:
            dist.barrier(); torch.cuda.synchronize(); t=time.time()
            if kind=="all_gather_into_tensor": dist.all_gather_into_tensor(out,shard)
            else:
                l=[torch.empty_like(shard) for _ in range(ws)]; dist.all_gather(l,shard)
            torch.cuda.synchronize(); dt=time.time()-t
            recv=mb/2
            print(rank,f"{kind} cuda {mb}MB total: {dt:.3f}s recv {recv/dt:.1f} MB/s",flush=True)
        except Exception as e: print(rank,kind,"FAIL",repr(e)[:200],flush=True)
try:
    from torch.distributed.fsdp import fully_shard
    print(rank,"fully_shard import ok",flush=True)
except Exception as e: print(rank,"FSDP2 import FAIL",e)
dist.destroy_process_group()
