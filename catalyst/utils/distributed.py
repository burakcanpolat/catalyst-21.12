from typing import Any, Callable, List, Optional
from collections import OrderedDict
import os
import pickle
import random
import socket
import subprocess

import torch
from torch import nn
import torch.distributed as dist

from catalyst.settings import SETTINGS

if SETTINGS.xla_required:
    import torch_xla.core.xla_env_vars as xenv
    import torch_xla.core.xla_model as xm


def _is_torch_distributed_initialized() -> bool:
    """Checks if torch.distributed is available and initialized."""
    return dist.is_available() and dist.is_initialized()


def _is_xla_distributed_initialized() -> bool:
    return SETTINGS.xla_required and os.environ.get(xenv.TORCH_DIST_ROOT, None) is not None


def _is_slurm_available():
    """Checks if slurm is available."""
    return "SLURM_JOB_NUM_NODES" in os.environ and "SLURM_NODEID" in os.environ


def _is_ddp_wrapped(model: nn.Module) -> bool:
    """Checks whether model is wrapped with DataParallel/DistributedDataParallel."""
    parallel_wrappers = nn.DataParallel, nn.parallel.DistributedDataParallel

    # Check whether Apex is installed and if it is,
    # add Apex's DistributedDataParallel to list of checked types
    if SETTINGS.apex_required:
        from apex.parallel import DistributedDataParallel as apex_DDP

        parallel_wrappers = parallel_wrappers + (apex_DDP,)

    if SETTINGS.fairscale_required:
        from fairscale.nn.data_parallel import FullyShardedDataParallel, ShardedDataParallel

        parallel_wrappers = parallel_wrappers + (ShardedDataParallel, FullyShardedDataParallel)

    if SETTINGS.deepspeed_required:
        from deepspeed import DeepSpeedEngine, PipelineEngine

        parallel_wrappers = parallel_wrappers + (DeepSpeedEngine, PipelineEngine)

    return isinstance(model, parallel_wrappers)


def get_nn_from_ddp_module(model: nn.Module) -> nn.Module:
    """
    Return a real model from a torch.nn.DataParallel,
    torch.nn.parallel.DistributedDataParallel, or
    apex.parallel.DistributedDataParallel.

    Args:
        model: A model, or DataParallel wrapper.

    Returns:
        A model
    """
    if _is_ddp_wrapped(model):
        model = model.module
    return model


def get_backend() -> Optional[str]:
    """Returns the backend for distributed training."""
    if _is_xla_distributed_initialized():
        return "xla"
    elif _is_torch_distributed_initialized():
        return "ddp"
    else:
        return None


def get_rank() -> int:
    """
    Returns the rank of the current worker.

    Returns:
        int: ``rank`` if torch.distributed is initialized, otherwise ``-1``
    """
    if _is_xla_distributed_initialized():
        return xm.get_ordinal()
    elif _is_torch_distributed_initialized():
        return dist.get_rank()
    else:
        return -1


# def get_local_rank() -> int:
#     pass


def get_world_size() -> int:
    """Returns the world size for distributed training."""
    if _is_xla_distributed_initialized():
        return xm.xrt_world_size()
    elif _is_torch_distributed_initialized():
        return dist.get_world_size()
    else:
        return 1


# def get_num_nodes() -> int:
#     pass
#
#
# def get_num_proc_per_nodes() -> int:
#     pass
#
#
# def get_node_rank() -> int:
#     pass


# TODO: rename
# TODO: remove, restore? deprecated part
def _get_slurm_params():
    """Return slurm params for experiment run.

    Returns:
        tuple with current node index, number of nodes, master node
            and master port
    """
    cmd = "scontrol show hostnames '%s'" % os.environ["SLURM_JOB_NODELIST"]
    nodes = subprocess.getoutput(cmd).split()
    num_nodes = int(os.environ["SLURM_JOB_NUM_NODES"])
    current_node = os.environ["SLURMD_NODENAME"]
    master_node = socket.gethostbyname(nodes[0])
    cur_node_idx = nodes.index(current_node)
    job_id = os.environ["SLURM_JOB_ID"]
    master_port = str(5 * 10 ** 4 + int(job_id) % 10 ** 4)
    return cur_node_idx, num_nodes, master_node, master_port


# TODO: rename
# TODO: remove, restore? deprecated part
def get_distributed_params():
    """Returns distributed params for experiment run.

    Returns:
        dictionary with distributed params
    """
    master_port = str(random.randint(5 * 10 ** 4, 6 * 10 ** 4))
    master_addr = "127.0.0.1"
    cur_node, num_nodes = 0, 1
    if _is_slurm_available():
        cur_node, num_nodes, master_addr, master_port = _get_slurm_params()

    os.environ["MASTER_ADDR"] = os.getenv("MASTER_ADDR", master_addr)
    os.environ["MASTER_PORT"] = os.getenv("MASTER_PORT", master_port)

    workers_per_node = torch.cuda.device_count()
    start_rank = cur_node * workers_per_node
    world_size = num_nodes * workers_per_node

    local_rank = os.getenv("LOCAL_RANK", None)
    rank = os.getenv("RANK", None)
    local_rank, rank = [v and int(v) for v in [local_rank, rank]]
    world_size = int(os.getenv("WORLD_SIZE", world_size))

    output = OrderedDict(
        local_rank=local_rank,
        start_rank=start_rank,
        rank=rank,
        world_size=world_size,
        master_addr=os.environ["MASTER_ADDR"],
        master_port=os.environ["MASTER_PORT"],
    )

    return output


def sum_reduce(tensor: torch.Tensor) -> torch.Tensor:
    """Reduce tensor to all processes and compute total (sum) value.

    Args:
        tensor: tensor to reduce.

    Returns:
        reduced tensor
    """
    cloned = tensor.clone()
    dist.all_reduce(cloned, dist.ReduceOp.SUM)
    return cloned


def mean_reduce(tensor: torch.Tensor, world_size: int) -> torch.Tensor:
    """Reduce tensor to all processes and compute mean value.

    Args:
        tensor: tensor to reduce.
        world_size: number of processes in DDP setup.

    Returns:
        reduced tensor
    """
    # TODO: fix division operator for int/long tensors
    reduced = sum_reduce(tensor) / world_size
    return reduced


def all_gather(data: Any) -> List[Any]:
    """Run all_gather on arbitrary picklable data (not necessarily tensors).

    .. note::
        if data on different devices then data in resulted list will be on the same devices.
        Source: https://github.com/facebookresearch/detr/blob/master/util/misc.py#L88-L128

    Args:
        data: any picklable object

    Returns:
        list of data gathered from each process.

    """
    if not dist.is_available() or not dist.is_initialized():
        world_size = 1
    else:
        world_size = dist.get_world_size()

    if world_size == 1:
        return [data]

    # serialized to a Tensor
    buffer = pickle.dumps(data)
    storage = torch.ByteStorage.from_buffer(buffer)
    tensor = torch.ByteTensor(storage).to("cuda")

    # obtain Tensor size of each rank
    local_size = torch.tensor([tensor.numel()], device="cuda")
    size_list = [torch.tensor([0], device="cuda") for _ in range(world_size)]
    dist.all_gather(size_list, local_size)
    size_list = [int(size.item()) for size in size_list]
    max_size = max(size_list)

    # receiving Tensor from all ranks
    # we pad the tensor because torch all_gather does not support
    # gathering tensors of different shapes
    tensor_list = []
    for _ in size_list:
        tensor_list.append(torch.empty((max_size,), dtype=torch.uint8, device="cuda"))

    if local_size != max_size:
        padding = torch.empty(size=(max_size - local_size,), dtype=torch.uint8, device="cuda")
        tensor = torch.cat((tensor, padding), dim=0)
    dist.all_gather(tensor_list, tensor)

    data_list = []
    for size, tensor in zip(size_list, tensor_list):
        buffer = tensor.cpu().numpy().tobytes()[:size]
        data_list.append(pickle.loads(buffer))

    return data_list


def ddp_reduce(tensor: torch.Tensor, mode: str, world_size: int):
    """Syncs ``tensor`` over ``world_size`` in distributed mode.

    Args:
        tensor: tensor to sync across the processes.
        mode: tensor synchronization type, should be one of 'sum', 'mean' or 'all'.
        world_size: world size

    Returns:
        torch.Tensor with synchronized values.

    Raises:
        ValueError: if mode is out of ``sum``, ``mean``, ``all``.
    """
    if mode not in {"sum", "mean", "all"}:
        raise ValueError(f"Unknown sync_type '{mode}'")
    if mode == "sum":
        return sum_reduce(tensor)
    elif mode == "mean":
        return mean_reduce(tensor, world_size)
    else:
        return all_gather(tensor)


def ddp_sync_run(function: Callable):
    """Runs function in a synchronous way: 0-rank first and all other processes after.

    Args:
        function: callable function
    """
    rank = get_rank()
    if rank > 0:
        dist.barrier()
    function()
    if rank == 0:
        dist.barrier()


__all__ = [
    "get_backend",
    "get_rank",
    "get_world_size",
    "get_distributed_params",
    "get_nn_from_ddp_module",
    "sum_reduce",
    "mean_reduce",
    "all_gather",
    "ddp_reduce",
    "ddp_sync_run",
]
