import torch
import torch.distributed.autograd as autograd
import torch.distributed.rpc as rpc
import torch.multiprocessing as mp
import torch.nn as nn

import os
import time
from mpi4py import MPI


class MyModule(nn.Module):
    def __init__(self, device, comm_mode):
        super().__init__()
        self.device = device
        self.linear = nn.Linear(5000, 5000).to(device)
        self.comm_mode = comm_mode

    def forward(self, x):
        # x.to() is a no-op if x is already on self.device
        y = self.linear(x.to(self.device))
        return y.cpu() if self.comm_mode == "cpu" else y

    def parameter_rrefs(self):
        return [rpc.RRef(p) for p in self.parameters()]


def measure(comm_mode):
    # local module on "worker0/cuda:0"
    lm = MyModule("cuda:5", comm_mode)
    # remote module on "worker1/cuda:1"
    rm = rpc.remote("worker1", MyModule, args=("cuda:5", comm_mode))
    # prepare random inputs
    x = torch.randn(5000, 5000).cuda(5)

    tik = time.time()
    for iteration_idx in range(10):
        print("iteration_idx = {}".format(iteration_idx))
        with autograd.context() as ctx:
            y = rm.rpc_sync().forward(lm(x))
            autograd.backward(ctx, [y.sum()])
    # synchronize on "cuda:0" to make sure that all pending CUDA ops are
    # included in the measurements
    torch.cuda.current_stream("cuda:5").synchronize()
    tok = time.time()
    print(f"{comm_mode} RPC total execution time: {tok - tik}")


def run_worker(rank):
    os.environ["MASTER_ADDR"] = "192.168.11.1"
    os.environ["MASTER_PORT"] = "29500"
    options = rpc.TensorPipeRpcBackendOptions(
        num_worker_threads=128,
        _transports=["shm", "uv"],
        _channels=["cma", "basic", "cuda_xth", "cuda_ipc", "cuda_basic", "cuda_gdr"],
    )

    if rank == 0:
        options.set_device_map("worker1", {5: 5})
        rpc.init_rpc(f"worker{rank}", rank=rank, world_size=2, rpc_backend_options=options)
        # measure(comm_mode="cpu")
        measure(comm_mode="cuda")
    else:
        rpc.init_rpc(f"worker{rank}", rank=rank, world_size=2, rpc_backend_options=options)

    # block until all rpcs finish
    rpc.shutdown()


if __name__ == "__main__":
    comm = MPI.COMM_WORLD
    rank = comm.Get_rank()
    world_size = 2
    run_worker(rank)
    # mp.spawn(run_worker, nprocs=world_size, join=True)
