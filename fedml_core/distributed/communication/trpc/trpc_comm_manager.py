import csv
import decimal
import logging
import os
import threading
import time
from typing import List

import torch
import torch.distributed as dist
import torch.distributed.rpc as rpc
import torch.multiprocessing as mp
from FedML.fedml_api.distributed.utils.gpu_mapping import get_gpu_util_map
from torch.distributed import rpc

from .trpc_server import TRPCCOMMServicer
from ...communication.base_com_manager import BaseCommunicationManager
from ...communication.message import Message
from ...communication.observer import Observer
from ...communication.utils import log_communication_tick

lock = threading.Lock()


WORKER = "worker{}"


class TRPCCommManager(BaseCommunicationManager):
    def __init__(
        self,
        trpc_master_config_path,
        process_id=0,
        world_size=0,
        enable_cuda_rpc=False,
        gpu_util_file=None,
        gpu_util_key=None,
    ):
        logging.info("using TRPC backend")
        with open(trpc_master_config_path, newline="") as csv_file:
            csv_reader = csv.reader(csv_file)
            # skip header line
            next(csv_reader)
            master_address, master_port = next(csv_reader)
        self.master_address = master_address
        self.master_port = master_port
        self.process_id = process_id
        self.world_size = world_size
        self._observers: List[Observer] = []

        if process_id == 0:
            self.node_type = "server"
        else:
            self.node_type = "client"

        print(f"Worker rank {process_id} initializing RPC")

        self.trpc_servicer = TRPCCOMMServicer(master_address, master_port, world_size, process_id)
        logging.info(os.getcwd())

        os.environ["MASTER_ADDR"] = self.master_address
        os.environ["MASTER_PORT"] = self.master_port

        self._init_torch_rpc_tp(
            master_address, master_port, process_id, world_size, enable_cuda_rpc, gpu_util_file, gpu_util_key
        )

        self.is_running = True
        print("server started. master address: " + str(master_address))

    def _init_torch_rpc_tp(
        self, master_addr, master_port, worker_idx, worker_num, enable_cuda_rpc, gpu_util_file, gpu_util_key
    ):
        # https://github.com/pytorch/pytorch/issues/55615
        # [BC-Breaking][RFC] Retire ProcessGroup Backend for RPC #55615
        str_init_method = "tcp://" + str(master_addr) + ":" + str(master_port)
        logging.info("str_init_method = {}".format(str_init_method))
        if enable_cuda_rpc:
            options = rpc.TensorPipeRpcBackendOptions(
                num_worker_threads=16,
                rpc_timeout=1800,
                init_method=str_init_method,
                _transports=["shm", "uv"],
                _channels=["cma", "basic", "cuda_xth", "cuda_ipc", "cuda_basic", "cuda_gdr"],
            )
        else:
            options = rpc.TensorPipeRpcBackendOptions(
                num_worker_threads=16,
                rpc_timeout=1800,
                init_method=str_init_method,
                _transports=["shm", "uv"],
                _channels=["cma", "basic", "cuda_xth", "cuda_ipc", "cuda_basic", "cuda_gdr"],
            )
        if enable_cuda_rpc and gpu_util_file:
            trpc_gpu_mapping = self.get_trpc_gpu_mapping(worker_idx, gpu_util_file, gpu_util_key)
            logging.info(trpc_gpu_mapping)
            for key in trpc_gpu_mapping:
                options.set_device_map(key, trpc_gpu_mapping[key])
        else:
            options.set_device_map("worker1", {5: 5})
        rpc.init_rpc(
            WORKER.format(worker_idx),
            backend=rpc.BackendType.TENSORPIPE,
            rank=worker_idx,
            world_size=worker_num,
            rpc_backend_options=options,
        )
        logging.info("_init_torch_rpc_tp finished.")

    def send_message(self, msg: Message):
        receiver_id = msg.get_receiver_id()

        logging.info("sending message to {}".format(receiver_id))

        log_communication_tick(self.process_id, receiver_id)
        # Should I wait?
        rpc.rpc_sync(WORKER.format(receiver_id), TRPCCOMMServicer.sendMessage, args=(self.process_id, msg))

        logging.debug("sent")

    def add_observer(self, observer: Observer):
        self._observers.append(observer)

    def remove_observer(self, observer: Observer):
        self._observers.remove(observer)

    def handle_receive_message(self):
        thread = threading.Thread(target=self.message_handling_subroutine)
        thread.start()

    def message_handling_subroutine(self):
        while self.is_running:
            if self.trpc_servicer.message_q.qsize() > 0:
                lock.acquire()
                msg = self.trpc_servicer.message_q.get()
                self.notify(msg)
                lock.release()
        return

    def stop_receive_message(self):
        rpc.shutdown()
        self.is_running = False

    def notify(self, message: Message):
        msg_type = message.get_type()
        for observer in self._observers:
            observer.receive_message(msg_type, message)

    def get_trpc_gpu_mapping(self, process_id, gpu_util_file, gpu_util_key):
        gpu_util_map, _ = get_gpu_util_map(process_id, gpu_util_file, gpu_util_key)
        trpc_gpu_mapping = {}
        proccess_gpu = gpu_util_map[process_id][1]
        for key in gpu_util_map:
            if key != process_id:
                trpc_gpu_mapping[WORKER.format(key)] = {proccess_gpu: gpu_util_map[key][1]}
        return trpc_gpu_mapping


def run_worker(rank, world_size):
    r"""
    A wrapper function that initializes RPC, calls the function, and shuts down
    RPC.
    """
    if rank == 1:
        com_manager_client = TRPCCommManager("./trpc_master_config.csv", rank, world_size)
        start = time.time()
        tensor = torch.ones(1000, 1000)
        message = Message(type="test", sender_id=rank, receiver_id="1")
        message.add_params("THE_TENSOR", tensor)
        TRPCCOMMServicer.sendMessage("worker0", message)
        message_values = []
        message = Message(type="test", sender_id=rank, receiver_id="1")
        message2 = Message(type="test", sender_id=rank, receiver_id="1")
        message.add_params("THE_TENSOR", tensor)
        for i in range(100):
            print("###############################")
            print("Measuring for Single Message")
            for size in [100, 1000, 10000]:

                # for size in [100, 1000]:
                print(f"======= size = {size} =====")
                tensor = torch.ones(size, size)
                start = time.time()
                TRPCCOMMServicer.sendMessageTest1("worker0", message)
                end = time.time()
                duration = end - start
                message_values.append(duration)
                # print(f"Message tensor size={size} duration={str(duration)}", flush=True)

            print("###############################")
            print("Measuring for Message with separate Tensor")
            sinle_tensor_values = []
            start = time.time()
            for size in [100, 1000, 10000]:

                # for size in [100, 1000]:
                print(f"======= size = {size} =====")
                tensor = torch.ones(size, size)
                # message = Message(type="test", sender_id=rank, receiver_id="1")
                # message.add_params("THE_TENSOR", tensor)
                start = time.time()
                TRPCCOMMServicer.sendMessageTest2("worker0", message2.get_params(), tensor)
                end = time.time()
                duration = end - start
                # print(f"Single tensor size={size} duration={str(duration)}", flush=True)
                sinle_tensor_values.append(duration)

        print("mean message: " + str(decimal.Decimal(sum(message_values) / len(message_values))))
        print("mean single tensor: " + str(decimal.Decimal(sum(sinle_tensor_values) / len(sinle_tensor_values))))
        # ret = rpc.rpc_sync("worker1", TRPCCOMMServicer., args=(torch.ones(2), torch.ones(2)))
    else:
        # parameter server does nothing
        com_manager_client = TRPCCommManager("./trpc_master_config.csv", rank, world_size)

    rpc.shutdown()


if __name__ == "__main__":
    world_size = 2
    # run_worker(0,1)
    mp.spawn(run_worker, args=(world_size,), nprocs=world_size, join=True)
