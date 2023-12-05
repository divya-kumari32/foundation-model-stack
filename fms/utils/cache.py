import collections.abc
from typing import Tuple, List, Dict, Optional, Union
from vllm import cache_ops, attention_ops
import torch
from dataclasses import dataclass

lib = torch.library.Library("paged_attention", "FRAGMENT")

lib.define(
    "reshape_and_cache(Tensor key, Tensor value, Tensor key_cache, Tensor value_cache, Tensor slot_mapping) -> None"
)
lib.impl("reshape_and_cache", cache_ops.reshape_and_cache, "CUDA")

# needed for compile
# @torch.library.impl(lib, "reshape_and_cache", "Meta")
# def _reshape_and_cache_meta(key, value, key_cache, value_cache, slot_mapping):
#     return key.contiguous(), value.contiguous(), key_cache.contiguous(), value_cache.contiguous(), slot_mapping.contiguous()
#
# @torch.library.impl(lib, "reshape_and_cache", "CUDA")
# def _reshape_and_cache(key, value, key_cache, value_cache, slot_mapping):
#     cache_ops.reshape_and_cache(key, value, key_cache, value_cache, slot_mapping)
#     return key, value, key_cache, value_cache, slot_mapping


lib.define(
    "paged_attention_v1(Tensor out, Tensor query, Tensor key_cache, Tensor value_cache, Tensor head_mapping, float scale, Tensor block_tables, Tensor context_lens, int block_size, int max_context_len, Tensor? alibi_slopes) -> None"
)
lib.impl("paged_attention_v1", attention_ops.paged_attention_v1, "CUDA")

# needed for compile
# @torch.library.impl(lib, "paged_attention_v1", "Meta")
# def _paged_attention_v1_meta(out, query, key_cache, value_cache, head_mapping, scale, block_tables, context_lens, block_size, max_context_len, alibi_slopes):
#     return None


KVCache = Tuple[torch.Tensor, torch.Tensor]  # (key cache, value cache)


def get_cache_block_size(block_size, head_size, num_heads, num_layers, dtype) -> int:
    kv_cache_block_size = block_size * num_heads * head_size * 2  # 2 for k and v

    total_size = num_layers * kv_cache_block_size
    dtype_size = torch.tensor([], dtype=dtype).element_size()
    return dtype_size * total_size


def get_max_gpu_blocks_available(
    block_size: int,
    emb_dim: int,
    nheads: int,
    nlayers: int,
    gpu_memory_utilization: float,
    dtype,
) -> int:

    # Calculate the number of blocks that can be allocated with the
    # profiled peak memory.
    torch.cuda.synchronize()
    peak_memory = torch.cuda.max_memory_allocated()
    total_gpu_memory = torch.cuda.get_device_properties("cuda").total_memory
    cache_block_size = get_cache_block_size(
        block_size, emb_dim // nheads, nheads, nlayers, dtype
    )
    num_gpu_blocks = int(
        (total_gpu_memory * gpu_memory_utilization - peak_memory) // cache_block_size
    )
    num_gpu_blocks = max(num_gpu_blocks, 0)
    torch.cuda.empty_cache()
    return num_gpu_blocks


class CacheBlock:
    def __init__(
        self,
        block_number: int,
        block_size: int,
    ):
        self.block_number = block_number
        self.block_size = block_size
        self.num_tokens = 0

    def num_available_slots(self) -> int:
        return self.block_size - self.num_tokens

    def is_full(self) -> bool:
        return self.num_available_slots() == 0

    def append_num_tokens(self, num_tokens: int):
        # todo: we need some way of differentiating number of tokens stored in the cache vs num allocated
        self.num_tokens += num_tokens


class CacheBlockGroup(List[CacheBlock]):
    def __init__(self, block_size: int):
        super().__init__()
        self.block_size = block_size
        self._is_generating = False
        self._is_initialized_with_prompt = False

    def __getitem__(self, key):
        return super(CacheBlockGroup, self).__getitem__(key)

    def is_initialized_with_prompt(self):
        return self._is_initialized_with_prompt

    def is_generating(self):
        return self._is_generating

    def last_cache_block_is_full(self):
        return self[-1].is_full()

    def get_sequence_length(self):
        if len(self) == 0:
            return 0
        else:
            return sum([cb.num_tokens for cb in self])

    def get_cache_block(self, position: int):
        return self[position // self.block_size]

    def get_slot_mapping(self, position: Optional[int] = None) -> List[int]:
        slot_mapping = []
        start = position if position else 0
        for position_i in range(start, self.get_sequence_length()):
            block_number = self.get_cache_block(position_i).block_number
            block_offset = position_i % self.block_size
            slot = block_number * self.block_size + block_offset
            slot_mapping.append(slot)
        return slot_mapping

    def get_block_mapping(self):
        return [cb.block_number for cb in self]


SequenceIDsInput = Union[Dict, List[int], int]
SlotMappingInput = Union[Dict, torch.Tensor]


def sequence_id_input(inner_f):
    def wrapper(self, sequence_ids: SequenceIDsInput, *args, **kwargs):
        if isinstance(sequence_ids, Dict):
            result = sequence_ids["sequence_ids"]
        elif isinstance(sequence_ids, List):
            result = sequence_ids
        else:
            result = [sequence_ids]
        return inner_f(self, result, *args, **kwargs)

    return wrapper


def slot_mapping_input(inner_f):
    def wrapper(self, slot_mapping: SlotMappingInput, *args, **kwargs):
        if isinstance(slot_mapping, Dict):
            result = slot_mapping["slot_mapping"]
        elif isinstance(slot_mapping, List):
            result = slot_mapping
        else:
            result = [slot_mapping]
        return inner_f(self, result, *args, **kwargs)

    return wrapper


class PagedKVCache:
    def __init__(
        self,
        num_layers: int,
        num_heads: int,
        emb_dim: int,
        total_num_gpu_blocks: Optional[int] = None,
        block_size: int = 16,
        dtype: torch.dtype = torch.float32,
    ):
        self.block_size = block_size
        self.cache: List[KVCache] = []
        element_size = torch.tensor([], dtype=dtype).element_size()

        if not total_num_gpu_blocks:
            total_num_gpu_blocks = get_max_gpu_blocks_available(
                block_size, emb_dim, num_heads, num_layers, 0.7, dtype
            )
        self.total_num_gpu_blocks = total_num_gpu_blocks

        head_size = emb_dim // num_heads

        x = self.block_size // element_size
        key_block_shape = (
            num_heads,
            head_size // x,
            block_size,
            x,
        )
        value_block_shape = (
            num_heads,
            head_size,
            block_size,
        )
        for _ in range(num_layers):
            key_blocks = torch.empty(
                size=(total_num_gpu_blocks, *key_block_shape),
                dtype=dtype,
                device="cuda",
            )
            value_blocks = torch.empty(
                size=(total_num_gpu_blocks, *value_block_shape),
                dtype=dtype,
                device="cuda",
            )
            self.cache.append((key_blocks, value_blocks))

        self.free_blocks: List[CacheBlock] = []

        for i in range(total_num_gpu_blocks):
            self.free_blocks.append(CacheBlock(i, block_size))

        # each sequence will be mapped to a cache block group
        # for now this will just assume we always have the same sequences in batch
        self.block_table_map: Dict[int, CacheBlockGroup] = {}

    def get_max_sequence_length(
        self, sequence_ids_or_cache_metadata: SequenceIDsInput
    ) -> int:
        max_sequence_length = None
        sequence_ids = sequence_ids_or_cache_metadata
        if isinstance(sequence_ids_or_cache_metadata, dict):
            sequence_ids = sequence_ids_or_cache_metadata["sequence_ids"]
            max_sequence_length = sequence_ids_or_cache_metadata.get(
                "max_sequence_length", None
            )

        if max_sequence_length is None:
            max_sequence_length = max(
                [
                    self.block_table_map[seq_id].get_sequence_length()
                    for seq_id in sequence_ids
                ]
            )
        return max_sequence_length

    def _allocate_block(self) -> CacheBlock:
        return self.free_blocks.pop()

    @staticmethod
    def __pad_to_max_left(x: List[int], max_len: int, pad: int) -> List[int]:
        return [pad] * (max_len - len(x)) + x

    @staticmethod
    def __pad_to_max_right(x: List[int], max_len: int, pad: int) -> List[int]:
        return x + [pad] * (max_len - len(x))

    @sequence_id_input
    def is_generating(self, sequence_ids: SequenceIDsInput):
        for sequence_id in sequence_ids:
            if (
                sequence_id not in self.block_table_map
                or not self.block_table_map[sequence_id].is_generating()
            ):
                return False
        return True

    @sequence_id_input
    def is_initialized_with_prompt(self, sequence_ids: SequenceIDsInput):
        for sequence_id in sequence_ids:
            if (
                sequence_id not in self.block_table_map
                or not self.block_table_map[sequence_id].is_initialized_with_prompt()
            ):
                return False
        return True

    def free(self, sequence_id: int):
        if sequence_id not in self.block_table_map:
            return
        cbg = self.block_table_map[sequence_id]
        for cb in cbg:
            cb.num_tokens = 0
            self.free_blocks.append(cb)
        del self.block_table_map[sequence_id]

    @sequence_id_input
    def free_sequences(self, sequence_ids: SequenceIDsInput):
        for seq_id in sequence_ids:
            self.free(seq_id)

    def get_unassigned_sequence_ids(self, prompt_tensor: torch.Tensor) -> List[int]:
        # todo: there are better ways to do this, but this is fine for now
        result = []
        batch_size = prompt_tensor.size(0)
        seq_id = 0
        while len(result) < batch_size:
            if seq_id not in self.block_table_map:
                result.append(seq_id)
            seq_id += 1
        return result

    def _get_cache_metadata(self, sequence_ids: List[int], is_prompt: bool) -> dict:
        slot_mapping = []
        block_tables = []
        context_lengths = []
        max_sequence_length = self.get_max_sequence_length(sequence_ids)
        max_num_blocks = max_sequence_length // self.block_size
        if max_sequence_length % self.block_size != 0:
            max_num_blocks += 1
        for sequence_id in sequence_ids:
            cbg = self.block_table_map[sequence_id]

            context_length = cbg.get_sequence_length()
            if is_prompt:
                slot = cbg.get_slot_mapping()
                slot = self.__pad_to_max_left(slot, max_sequence_length, -1)
            else:
                slot = cbg.get_slot_mapping(context_length - 1)

            block_mapping = cbg.get_block_mapping()
            block_mapping = self.__pad_to_max_right(block_mapping, max_num_blocks, 0)
            max_num_blocks = max(max_num_blocks, len(block_mapping))

            slot_mapping.append(slot)
            block_tables.append(block_mapping)
            context_lengths.append(context_length)

        slot_mapping = torch.tensor(slot_mapping, dtype=torch.long, device="cuda").view(
            -1
        )
        block_tables = torch.tensor(
            block_tables,
            dtype=torch.int,
            device="cuda"
        )
        context_lengths = torch.tensor(context_lengths, dtype=torch.int, device="cuda")
        return {
            "sequence_ids": sequence_ids,
            "context_lengths": context_lengths,
            "max_sequence_length": max_sequence_length,
            "position_offset": 0 if is_prompt else max_sequence_length - 1,
            "slot_mapping": slot_mapping,
            "block_tables": block_tables,
            "type": "paged_attention",
            "is_generating": not is_prompt,
            "block_size": self.block_size
        }

    def allocate_initial_prompt(
        self, prompt_tensor: torch.Tensor, sequence_ids: Optional[List[int]] = None
    ) -> dict:
        if not sequence_ids:
            sequence_ids = self.get_unassigned_sequence_ids(prompt_tensor)

        prompt_list = prompt_tensor.tolist()
        for seq_id, prompt_ids in zip(sequence_ids, prompt_list):
            self._allocate_prompt_sequence(seq_id, prompt_ids)

        return self._get_cache_metadata(sequence_ids, is_prompt=True)

    @sequence_id_input
    def allocate_generated_token(self, sequence_ids: SequenceIDsInput) -> dict:
        for seq_id in sequence_ids:
            cache_block_group = self.block_table_map[seq_id]
            cache_block_group._is_generating = True

            if cache_block_group.last_cache_block_is_full():
                last_block = self._allocate_block()
                last_block.append_num_tokens(1)
                cache_block_group.append(last_block)
            else:
                cache_block_group[-1].append_num_tokens(1)

        return self._get_cache_metadata(sequence_ids, is_prompt=False)

    def _allocate_prompt_sequence(self, seq_id: int, tokens: List[int]):
        tokens = [x for x in tokens if x != 0]
        cache_block_group: CacheBlockGroup = CacheBlockGroup(self.block_size)

        # one block allocation will happen automatically as the group always starts empty
        last_cache_block = self._allocate_block()

        cursor = 0
        while cursor < len(tokens):
            tokens_to_append = (
                min(len(tokens), cursor + last_cache_block.num_available_slots())
                - cursor
            )
            last_cache_block.append_num_tokens(tokens_to_append)
            cursor += tokens_to_append

            if cursor >= len(tokens):
                # we are done, so we need to append but not allocate
                cache_block_group.append(last_cache_block)
            elif last_cache_block.is_full():
                # if the block is full we can append it
                cache_block_group.append(last_cache_block)
                # because the other condition did not hold, we can allocate a new block
                last_cache_block = self._allocate_block()

        cache_block_group._is_initialized_with_prompt = True
        self.block_table_map[seq_id] = cache_block_group