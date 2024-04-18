import collections
import os
import re
from collections import ChainMap
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Callable, List, Mapping, MutableMapping, Optional, Tuple, Union

import torch
from packaging.version import Version

from fms.modules.tp import TPModule


__adapters: MutableMapping[str, MutableMapping[str, Callable[[Mapping], Mapping]]] = {}


def register_adapter(
    architecture: str,
    source: str,
    adapter: Callable[[Mapping], Mapping],
):
    """
    Registers a state dict adapter to be available to the (de) serialization
    API.

    Args:
    architecture: The name of the model architecture, e.g. 'llama'
    source: A label representing the format of the weights to be converted.
            E.g. 'hf'
    adapter: the class of the adapter. The class must accept one constructor
                parameter, which will be a state dict (`OrderedDict`)
    """
    sources: MutableMapping[str, Callable[[Mapping], Mapping]] = {}
    if architecture in __adapters:
        sources = __adapters[architecture]

    if source in sources:
        raise KeyError(
            f"Variant {source} already registered for architecture {architecture}"
        )

    sources[source] = adapter
    __adapters[architecture] = sources


def list_sources(architecture: str):
    """
    Lists available sources (attribute formats) of a model architecture.
    E.g. `models.list_variants('llama')` -> ['meta', 'fms', 'hf']
    Args:
    architecture: one of the registered architectures returned by
                    `models.list_models()`.
    """
    if architecture not in __adapters:
        return []
    return list(__adapters[architecture].keys())


def _legacy_attn_unfused_to_fused_weight_conversion(
    name: str, orig_sd: Mapping
) -> Optional[Tuple[str, torch.Tensor]]:
    """
    function which converts unfused fms weights to fused fms weights in the case the model was using the older unfused
    weights (version 0.0.3)

    Args:
        name: str
            current name to convert
        orig_sd: Mapping
            a mapping from a name to a param, in most cases will be a singleton, however when

    Returns:
    Optional[Tuple[str, torch.Tensor]]
        if query/key/value all exist in the given state dict, a tuple of the new fused name as well as weights will be
        returned from this function
        if one of query, key, or value exists in state dict (not all together), a FusableWeightsMissingError will be
        raised with the weights that are missing
        otherwise this function will return None signifying that no preprocessing needed to be done for this name param
    """
    # if we find query/key/value, this means the weights are unfused and therefore need to be fused
    if "attn.query" in name or "attn.key" in name or "attn.value" in name:
        weight_type = name.split(".")[-1]

        unfused_weights = [
            re.sub(
                rf"attn.(query|key|value).{weight_type}",
                f"attn.query.{weight_type}",
                name,
            ),
            re.sub(
                rf"attn.(query|key|value).{weight_type}",
                f"attn.key.{weight_type}",
                name,
            ),
            re.sub(
                rf"attn.(query|key|value).{weight_type}",
                f"attn.value.{weight_type}",
                name,
            ),
        ]

        result = (
            re.sub(
                rf"attn.(query|key|value).{weight_type}",
                f"attn.in_proj.qkv_fused.{weight_type}",
                name,
            ),
            torch.cat([orig_sd[w] for w in unfused_weights], dim=0),
        )

        return result
    return None


def _legacy_mlp_glu_unfused_to_fused_weight_conversion(
    name: str, orig_sd: Mapping
) -> Optional[Tuple[str, torch.Tensor]]:
    """
    function which converts unfused fms GLU weights to fused fms weights in the case the model was using the older
    unfused weights (version 0.0.3)

    Args:
        name: str
            current name to convert
        orig_sd: Mapping
            a mapping from a name to a param, in most cases will be a singleton, however when

    Returns:
    Optional[Tuple[str, torch.Tensor]]
        if wg/w1 all exist in the given state dict, a tuple of the new fused name as well as weights will be
        returned from this function
        if one of wg or w1 exists in state dict (not all together), a FusableWeightsMissingError will be
        raised with the weights that are missing
        otherwise this function will return None signifying that no preprocessing needed to be done for this name param
    """
    if (
        "ff_sub_layer.wg_fused" not in name
        and "ff_sub_layer.wg" in name
        or "ff_sub_layer.w1" in name
    ):
        weight_type = name.split(".")[-1]

        unfused_weights = [
            re.sub(
                rf"ff_sub_layer.(wg|w1).{weight_type}",
                f"ff_sub_layer.wg.{weight_type}",
                name,
            ),
            re.sub(
                rf"ff_sub_layer.(wg|w1).{weight_type}",
                f"ff_sub_layer.w1.{weight_type}",
                name,
            ),
        ]

        result = (
            re.sub(
                rf"ff_sub_layer.(w1|wg).{weight_type}",
                f"ff_sub_layer.wg_fused.{weight_type}",
                name,
            ),
            torch.cat([orig_sd[w] for w in unfused_weights], dim=0),
        )
        return result
    return None


def simple_mapping(
    orig_sd: Mapping,
    mappers: List[Callable[[str, Mapping], Optional[Tuple[str, torch.Tensor]]]],
) -> Mapping:
    new_sd = {}
    for name, param in orig_sd.items():
        for mapper in mappers:
            mapper_out = mapper(name, orig_sd)

            if mapper_out is not None:
                name, param = mapper_out
                break

        new_sd[name] = param

    return new_sd


def _get_adapter(
    architecture: str, source: Optional[str]
) -> Callable[[Mapping[str, Any]], Mapping[str, Any]]:
    if source is None:
        source = "fms"

    version_pattern = re.compile(
        f"{source}(\.v[0-9]+((\.[0-9]+\.[0-9]+)|(\.[0-9]+)))?$"
    )
    if (
        source == "fms"
        or architecture not in __adapters
        or all(
            re.match(version_pattern, k) is None
            for k in __adapters[architecture].keys()
        )
    ):
        # if no adapter is registered, assume the attributes are already in proper fms format
        return lambda x: x
    elif source in __adapters[architecture]:
        # if a standard source without a version is an adapter, just use it and ignore versioning
        return __adapters[architecture][source]
    else:
        # use versioning with source
        versions = []
        for k in __adapters[architecture].keys():
            matched_pattern = re.match(version_pattern, k)
            if matched_pattern is not None and k != source:
                versions.append(k[len(source) + 2 :])

        versions.sort(key=Version)

        def iter_adapter_func(sd):
            result_sd = sd
            for version in versions:
                result_sd = __adapters[architecture][f"{source}.v{version}"](result_sd)
            return result_sd

        return iter_adapter_func


def get_adapted(
    architecture: str, source: Optional[str], state_dict: Mapping[str, Any]
) -> Mapping[str, Any]:
    """
    Convert a state dict to FMS format, using an adapter specified by name.

    Args:
    architecture: one of the architectures from `models.list_models()`.
                    E.g. llama.
    source: A reference to an attribute format
    state_dict: the model.state_dict() to be converted/adapted.
    """
    # sometimes we only load onto rank 0 so may not have a state_dict here.
    if not len(state_dict):
        return state_dict
    adapter = _get_adapter(architecture, source)
    adapted = adapter(state_dict)
    return adapted


# `models` imports each model class, causing models and adapters to be registered.
# down here to avoid circular dependencies.
from fms import models


def _get_safetensors_item(key, file: Path, device: torch.device) -> torch.Tensor:
    from safetensors import safe_open  # type: ignore[import-untyped]

    with torch.no_grad():
        with safe_open(
            file, framework="pt", device=str(device)
        ) as model_weights:  # type: ignore[attr-defined]
            return model_weights.get_tensor(key)


class LazySafetensorsDict(collections.UserDict):
    def set_lazy_tensor(self, key, file, device):
        super().__setitem__(key, lambda: _get_safetensors_item(key, file, device))

    def __getitem__(self, key):
        lazy_tensor = super().__getitem__(key)
        if callable(lazy_tensor):
            lazy_tensor = lazy_tensor()
            super().__setitem__(key, lazy_tensor)
        return lazy_tensor


def load_state_dict(
    model_path: Union[str, Path],
    *,
    source: Optional[str] = None,
    distributed_strategy: Optional[str] = None,
    checkpoint_sharding: Optional[str] = None,
    initial_device: torch.device = torch.device("cpu"),
    rank: int = 0,
    world_size: int = 1,
) -> MutableMapping[str, Any]:
    """
    Validates that the file(s) found at a checkpoint path are compatible with
    the intended (possibly distributed) use-case, and returns a lazy loading
    state dict if possible (some formats may not support that).

    If model_path is a directory, it'll try to load models based on the source
    (e.g. .bin for HF, .pth for Meta), and, if no source is specified or hasn't
    been registered, it'll try .safetensors, .pth, and .bin.

    Args:
    model_path: the path to find the weights. If not set, return None.
    source: If the weights in the state dict didn't come from an FMS model,
            `source` specifies which conversion function might be needed.
            See `serialization.list_sources(architecture)`
    distributed_strategy: the kind of possibly-distributed model in which we
            intend to load these weights. E.g. tp, fsdp, None. Used for
            validation.
    checkpoint_sharding: the sharding format of the checkpoint.
            E.g. layer, tp, fsdp.
    initial_device: where the state dict will be loaded if not lazy.
            If meta, return empty dict.
    """
    if model_path is None or initial_device.type == "meta":
        return {}
    if checkpoint_sharding == "fsdp" and distributed_strategy not in ["fsdp", "hsdp"]:
        raise ValueError(f"FSDP checkpoints can only be loaded into an FSDP model")
    if checkpoint_sharding == "tp" and distributed_strategy != "tp":
        raise ValueError("TP checkpoints can only be loaded into a TP model")

    # Before creating the Path object, check if model_path has a glob pattern
    if isinstance(model_path, str):
        model_path, sep, glob_pattern = model_path.partition("*")
    else:
        sep = ""
        glob_pattern = ""
    glob_pattern = sep + glob_pattern

    model_path = Path(os.path.expanduser(model_path))

    checkpoints = []

    if model_path.is_dir():
        if glob_pattern != "":
            glob_pattern_list = [glob_pattern]
        elif source == "meta":
            glob_pattern_list = ["*.pth", "*.safetensors"]
        elif source == "hf":
            glob_pattern_list = ["*.bin", "*.safetensors"]
        else:
            glob_pattern_list = ["*.safetensors", "*.pth", "*.bin"]
        for glob_pattern_possibility in glob_pattern_list:
            file_list = list(model_path.glob(glob_pattern_possibility))
            if len(file_list) > 0:
                checkpoints = sorted(file_list)
                break

    if model_path.is_file():
        checkpoints = [model_path]

    # Check if we found some files
    assert (
        len(checkpoints) > 0
    ), f"Can't find the requested checkpoint data at {model_path}"

    if checkpoint_sharding is not None and checkpoint_sharding != "layer":
        assert world_size == len(
            checkpoints
        ), f"Loading a {checkpoint_sharding}-sharded checkpoint with len={len(checkpoints)} but world size is {world_size}"

        checkpoints = [checkpoints[rank]]

    # if there's only one checkpoint for fsdp/hsdp, load it only into rank zero
    # and it will be distributed by the FSDP `sync_module_states` parameter
    if checkpoint_sharding is None and distributed_strategy in {"hsdp", "fsdp"}:
        if rank == 0:
            checkpoints = [checkpoints[0]]
        else:
            return {}

    checkpoint_sds = []
    if checkpoints[0].suffix == ".safetensors":
        for ckp in checkpoints:
            checkpoint_sds.append(
                _load_safetensors_state_dict(
                    ckp,
                    initial_device,
                )
            )
    else:
        with torch.no_grad():
            checkpoint_sds = [
                torch.load(str(ckpt_path), mmap=True) for ckpt_path in checkpoints
            ]
    return ChainMap(*checkpoint_sds)


def _load_safetensors_state_dict(
    checkpoint: Path,
    device: torch.device,
):
    sd = LazySafetensorsDict()

    from safetensors import safe_open

    with safe_open(checkpoint, framework="pt", device=str(device)) as model_weights:  # type: ignore[attr-defined]
        sd_keys = list(model_weights.keys())
        for key in sd_keys:
            sd.set_lazy_tensor(key, checkpoint, device)
    return sd


def load_state_dict_into_model(
    model: torch.nn.Module,
    state_dict: MutableMapping[str, Any],
    architecture: str,
    source: str,
    distributed_strategy: Optional[str] = None,
    checkpoint_sharding: Optional[str] = None,
    initial_device: torch.device = torch.device("cpu"),
    rank: int = 0,
    world_size: int = 0,
) -> None:
    """
    This function loads state_dict into model in the most efficient way possible,
    and it removes all weights that have been used in model from state_dict
    in order to conserve memory.

    Args:
    model: The model where the weights are being loaded.
    state_dict: The dictionary with all the weights. If it has been mmaped
            (for torch.load) or it is an instance of LazySafetensorsDict,
            the weights are loaded lazily from disk.
    architecture: the model architecture, e.g. llama. See `models.list_models()`.
    source: If the weights in the state dict didn't come from an FMS model,
            `source` specifies which conversion function might be needed.
            See `serialization.list_sources(architecture)`
    distributed_strategy: the kind of possibly-distributed model in which we
            intend to load these weights. E.g. tp, fsdp, None. Used for weight
            sharding.
    checkpoint_sharding: the sharding format of the checkpoint.
            E.g. layer, tp, fsdp. Used for weight sharding.
    initial_device: where the weights will be loaded from disk.
    """

    # 1. Get the adapter from checkpoint sd to fms sd
    adapter = _get_adapter(architecture, source)

    # 2. Decide if model needs sharding and how (for now only TP)
    needs_tp_sharding = checkpoint_sharding != "tp" and distributed_strategy == "tp"

    # 3. Adapt the model weights and load the weights into the model
    with torch.no_grad():
        adapted_state_dict = adapter(state_dict)
        _load_partial_state_dict(
            model, adapted_state_dict, needs_tp_sharding, rank, world_size
        )


def _copy_colwise(param: torch.nn.Parameter, tensor_value, is_bias, rank, world_size):
    """
    This function copies the correct shard of the weights for a colwise-TP'd module
    according to the rank of the process and the world_size.

    Args
    ====
    param: torch.nn.Parameter
        Parameter that has had TP applied
    tensor_value: torch.Tensor
        tensor that needs sharding
    rank: int
        Rank of the current process
    world_size: int
        Total number of TP processes
    """
    # Divide the weight matrix along the first dimension.
    output_size_per_partition = param.shape[0]
    if not is_bias:
        tensor = tensor_value[
            (rank * output_size_per_partition) : (
                (rank + 1) * output_size_per_partition
            ),
            :,
        ]
    else:
        tensor = tensor_value[
            (rank * output_size_per_partition) : (
                (rank + 1) * output_size_per_partition
            )
        ]
    param.copy_(tensor, non_blocking=True)


def _copy_rowwise(param: torch.nn.Parameter, tensor_value, is_bias, rank, world_size):
    """
    This function copies the correct shard of the weights for a rowwise-TP'd module
    according to the rank of the process and the world_size.

    Args
    ====
    param: torch.nn.Parameter
        Parameter that has had TP applied
    tensor_value: torch.Tensor
        tensor that needs sharding
    rank: int
        Rank of the current process
    world_size: int
        Total number of TP processes
    """
    # Divide the weight matrix along the last dimension.
    if not is_bias:
        output_size_per_partition = param.shape[1]
        tensor = tensor_value[
            :,
            (rank * output_size_per_partition) : (
                (rank + 1) * output_size_per_partition
            ),
        ]
        param.copy_(tensor, non_blocking=True)
    else:
        if rank == 0:
            _copy_if_present(param, tensor_value)
        else:
            param.zero_()


def _copy_embedding(param: torch.nn.Parameter, tensor_value, rank, world_size):
    """
    This function copies the correct shard of the weights for a TP'd embedding module
    according to the rank of the process and the world_size.

    Args
    ====
    param: torch.nn.Parameter
        Parameter that has had TP applied
    tensor_value: torch.Tensor
        tensor that needs sharding
    rank: int
        Rank of the current process
    world_size: int
        Total number of TP processes
    """
    # Divide the weight matrix along the last dimension.
    output_size_per_partition = param.shape[1]
    tensor = tensor_value[
        :,
        (rank * output_size_per_partition) : ((rank + 1) * output_size_per_partition),
    ]
    param.copy_(tensor, non_blocking=True)


def _copy_if_present(parameter, tensor_value):
    parameter.copy_(tensor_value, non_blocking=True)


def _load_partial_state_dict(
    model: torch.nn.Module,
    state_dict,
    needs_tp_sharding: bool,
    rank=0,
    world_size=1,
):
    unused_params = []
    for key, tensor_value in state_dict.items():
        target_module = model
        # Find where to put the weight and decide whether it needs TP'ing
        key_steps = key.split(".")
        prefix = ""
        key_step = 0
        tp_module = None
        # Navigate the model tree to find the module where the parameter is
        # located and whether there is a TPModule in the way in case the
        # parameter requires sharding
        while key_step < len(key_steps) - 1:
            try:
                target_module = getattr(target_module, key_steps[key_step])
                if key_step > 0:
                    prefix += "."
                prefix += key_steps[key_step]
                key_step += 1
                if isinstance(target_module, Iterable):
                    target_module = target_module[int(key_steps[key_step])]  # type: ignore[index]
                    prefix += "." + key_steps[key_step]
                    key_step += 1
                if isinstance(target_module, TPModule):
                    tp_module = target_module
            except AttributeError:
                unused_params.append(key)
                break

        # Check if target_module has the Parameter/buffer
        try:
            param = getattr(target_module, key_steps[-1])

            # If TP sharding is not needed, copy the parameter
            # into the model
            if not needs_tp_sharding or tp_module is None:
                _copy_if_present(param, tensor_value)
            elif tp_module is not None:
                # Handle TP sharding
                if key_steps[-2] in tp_module.colwise_param_names():
                    _copy_colwise(
                        param,
                        tensor_value,
                        key_steps[-1] == "bias",
                        rank,
                        world_size,
                    )
                if key_steps[-2] in tp_module.rowwise_param_names():
                    _copy_rowwise(
                        param,
                        tensor_value,
                        key_steps[-1] == "bias",
                        rank,
                        world_size,
                    )
                if key_steps[-2] in tp_module.embedding_param_names():
                    _copy_embedding(
                        param,
                        tensor_value,
                        rank,
                        world_size,
                    )
        except AttributeError:
            unused_params.append(key)
