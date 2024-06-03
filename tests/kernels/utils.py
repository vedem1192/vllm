"""Kernel test utils"""

import itertools
import random
from typing import List, Optional, Union

import pytest
import torch

from vllm.attention.backends.abstract import (AttentionBackend,
                                              AttentionMetadata, AttentionType)
from vllm.attention.backends.xformers import XFormersBackend
from vllm.utils import (make_tensor_with_pad, maybe_make_int_tensor,
                        maybe_make_long_tensor, maybe_max)

STR_BACKEND_ENV_VAR: str = "VLLM_ATTENTION_BACKEND"
STR_FLASH_ATTN_VAL: str = "FLASH_ATTN"
STR_INVALID_VAL: str = "INVALID"


def override_backend_env_variable(mpatch: pytest.MonkeyPatch,
                                  backend_name: str) -> None:
    '''
    Override the environment variable indicating the vLLM backend temporarily,
    using pytest monkeypatch to ensure that the env vars get
    reset once the test context exits.

    Arguments:

    * mpatch: pytest monkeypatch instance
    * backend_name: attention backend name to force
    '''
    mpatch.setenv(STR_BACKEND_ENV_VAR, backend_name)


def ref_masked_attention(query: torch.Tensor,
                         key: torch.Tensor,
                         value: torch.Tensor,
                         scale: float,
                         custom_mask: Optional[torch.Tensor] = None,
                         q_seq_lens: Optional[List] = None,
                         kv_seq_lens: Optional[List] = None) -> torch.Tensor:
    '''
    "Golden" masked attention reference. Supports two types of masking:

    * Basic attention mask, utilizing {q,kv}_seq_lens args to mask out
      padding elements
    * Custom attention mask, which can force an arbitrary mask tensor, i.e.
      causal

    Arguments:

    * query: batch_size x q_padded_seq_len x num_heads x head_size
    * key: batch_size x kv_padded_seq_len x num_heads x head_size
    * value: batch_size x kv_padded_seq_len x num_heads x head_size
    * scale: Attention scale factor
    * Custom mask: custom attention mask; good place to inject a causal
      attention mask
    * q_seq_lens: list of unpadded query seq_lens for each batch index
    * kv_seq_lens: list of unpadded key/value seq_lens for each batch index

    Returns:

    * Attention result, batch_size x q_padded_seq_len x num_heads x head_size
    '''

    batch_size = query.shape[0]
    assert (len(q_seq_lens) == batch_size)
    assert (len(kv_seq_lens) == batch_size)

    attn_weights = scale * torch.einsum("bqhd,bkhd->bhqk", query, key).float()

    # Basic attention mask, derived from seq lens
    if (q_seq_lens is not None) or (kv_seq_lens is not None):
        attn_mask = torch.zeros_like(attn_weights)
        if q_seq_lens is not None:
            for bdx, plen in enumerate(q_seq_lens):
                attn_mask[bdx, :, plen:, :] = -torch.inf
        if kv_seq_lens is not None:
            for bdx, plen in enumerate(kv_seq_lens):
                attn_mask[bdx, :, :, plen:] = -torch.inf

        attn_weights = attn_weights + attn_mask.float()

    # Custom attention mask
    if custom_mask is not None:
        attn_weights = attn_weights + custom_mask.float()

    attn_weights = torch.softmax(attn_weights, dim=-1).to(value.dtype)
    out = torch.einsum("bhqk,bkhd->bqhd", attn_weights, value)
    return out


def make_qkv(
    batch_size: int,
    max_q_seq_len: int,
    max_kv_seq_len: int,
    num_heads: int,
    head_size: int,
    device: Union[torch.device, str],
    attn_type: AttentionType = AttentionType.ENCODER_DECODER,
    force_max_len: bool = False,
) -> tuple:
    '''
    Construct QKV test tensors for self- and cross-attention.

    Generates three query/key/value triplets:

    * "Baseline" query/key/value (for input to reference attention function)
    * "Prefill" query/key/value (last sequence offset zero'd out, for use as
      input to prefill kernel)
    * "Decode" query/key/value (only the last sequence offset  from baseline,
      for use as input to decode kernel)

    Each Q/K/V triplet is associated with a list of q seqlens and a list of k/v
    seqlens

    Arguments:

    * batch_size
    * max_q_seq_len: max query seq len
    * max_kv_seq_len: max key/value seq len
    * num_heads
    * head_size
    * is_encoder_decoder_attn: if True, query seqlen may differ from 
      key/value seqlen (as is often the case for cross-attention); 
      o/w, query/key/value seqlens match at each batch index 
      (max_kv_seq_len is unused)
    * force_max_len: if True, all query seqlens are max_q_seq_len; o/w query
      seqlens are random in [2,max_q_seq_lens]. Same for key/value seqlens
      and max_kv_seq_len, unless forced by is_encoder_decoder_attn=False
    * device: CPU or CUDA device

    Returns:

    * query: "baseline" query; batch_size x max_q_seq_len x num_heads x
      head_size
    * key: "baseline" key; batch_size x max_kv_seq_len x num_heads x
      head_size
    * value: "baseline" value; batch_size x max_kv_seq_len x num_heads x
      head_size
    * prefill_query: batch_size x (max_q_seq_len-1) x num_heads x head_size
    * prefill_key: batch_size x (max_kv_seq_len-1) x num_heads x head_size
    * prefill_value: batch_size x (max_kv_seq_len-1) x num_heads x head_size
    * decode_query: batch_size x 1 x num_heads x head_size
    * decode_key: batch_size x 1 x num_heads x head_size
    * decode_value: batch_size x 1 x num_heads x head_size
    * q_seq_lens: "baseline" query seqlen list
    * kv_seq_lens: "baseline" key/value seqlen list
    * actual_max_q_seq_len: actual "baseline" query max seq len (may be <=
      max_q_seq_len due to randomness)
    * actual_max_kv_seq_len: actual "baseline" key/value max seq len (may
      be <= max_kv_seq_len due to randomness)
    * prefill_q_seq_lens: "prefill" query seqlen list
    * prefill_kv_seq_lens: "prefill" key/value seqlen list
    * decode_q_seq_lens: "decode" query seqlen list (all ones)
    * decode_kv_seq_lens: "decode" key/value seqlen list
    '''

    if force_max_len:
        q_seq_lens = [max_q_seq_len for _ in range(batch_size)]
    else:
        q_seq_lens = [
            random.randint(2, max_q_seq_len) for _ in range(batch_size)
        ]
    kv_seq_lens = None
    if attn_type != AttentionType.ENCODER_DECODER:
        # K,V seq lens match Q for self-attention
        kv_seq_lens = q_seq_lens
    else:
        # K,V seq lens are distinct from Q seq lens & random
        if force_max_len:
            kv_seq_lens = [max_kv_seq_len for _ in range(batch_size)]
        else:
            kv_seq_lens = [
                random.randint(2, max_kv_seq_len) for _ in range(batch_size)
            ]

    actual_max_q_seq_len = max(q_seq_lens)
    actual_max_kv_seq_len = max(kv_seq_lens)

    query = torch.rand(
        (batch_size, max_q_seq_len, num_heads, head_size)).to(device)
    key = torch.rand(
        (batch_size, max_kv_seq_len, num_heads, head_size)).to(device)
    value = torch.rand(
        (batch_size, max_kv_seq_len, num_heads, head_size)).to(device)

    prefill_query = torch.zeros(
        (batch_size, max_q_seq_len, num_heads, head_size)).to(device)
    prefill_key = torch.zeros(
        (batch_size, max_kv_seq_len, num_heads, head_size)).to(device)
    prefill_value = torch.zeros(
        (batch_size, max_kv_seq_len, num_heads, head_size)).to(device)

    decode_query = torch.zeros(
        (batch_size, 1, num_heads, head_size)).to(device)
    decode_key = torch.zeros((batch_size, 1, num_heads, head_size)).to(device)
    decode_value = torch.zeros(
        (batch_size, 1, num_heads, head_size)).to(device)

    for bdx, (q_seq_len, kv_seq_len) in enumerate(zip(q_seq_lens,
                                                      kv_seq_lens)):
        query[bdx, q_seq_len:, :, :] = 0
        key[bdx, kv_seq_len:, :, :] = 0
        value[bdx, kv_seq_len:, :, :] = 0

        prefill_query[bdx,
                      0:(q_seq_len - 1), :, :] = query[bdx,
                                                       0:(q_seq_len - 1), :, :]
        prefill_key[bdx,
                    0:(kv_seq_len - 1), :, :] = key[bdx,
                                                    0:(kv_seq_len - 1), :, :]
        prefill_value[bdx, 0:(kv_seq_len -
                              1), :, :] = value[bdx, 0:(kv_seq_len - 1), :, :]

        decode_query[bdx, :, :, :] = query[bdx,
                                           (q_seq_len - 1):q_seq_len, :, :]
        decode_key[bdx, :, :, :] = key[bdx, (kv_seq_len - 1):kv_seq_len, :, :]
        decode_value[bdx, :, :, :] = value[bdx,
                                           (kv_seq_len - 1):kv_seq_len, :, :]

    prefill_q_seq_lens = [plen - 1 for plen in q_seq_lens]
    prefill_kv_seq_lens = [plen - 1 for plen in kv_seq_lens]

    decode_q_seq_lens = [1 for _ in q_seq_lens]
    decode_kv_seq_lens = [1 for _ in kv_seq_lens]

    return query, \
           key, \
           value, \
           prefill_query, \
           prefill_key, \
           prefill_value, \
           decode_query, \
           decode_key, \
           decode_value, \
           q_seq_lens, \
           kv_seq_lens, \
           actual_max_q_seq_len, \
           actual_max_kv_seq_len, \
           prefill_q_seq_lens, \
           prefill_kv_seq_lens, \
           decode_q_seq_lens, \
           decode_kv_seq_lens


def pack_tensor(unpacked_tensor: torch.Tensor, seq_lens: List[int],
                device: Union[torch.device, str]) -> tuple:
    '''
    Pack a batch_size x padded_seq_len x num_heads x head_size tensor into an
    unpadded number_of_tokens x num_heads x head_size tensor, where
    number_of_tokens = sum(seq_lens)

    Arguments:

    * unpacked_tensor: batch_size x padded_seq_len x num_heads x head_size
    * seq_lens: list of token counts for each seq
    * device: CPU or CUDA device

    Returns

    * packed_tensor: number_of_tokens x num_heads x head_size
    * start_loc_list: start idx of each batch elt in packed_tensor; [0] +
      list(itertools.accumulate(seq_lens))
    '''

    num_tok = sum(seq_lens)
    num_heads = unpacked_tensor.shape[-2]
    head_size = unpacked_tensor.shape[-1]
    start_loc_list = [0] + list(itertools.accumulate(seq_lens))
    packed_tensor = torch.zeros((num_tok, num_heads, head_size), device=device)

    for bdx, (seq_len, start_loc) in enumerate(zip(seq_lens, start_loc_list)):

        packed_tensor[start_loc:(
            start_loc + seq_len), :, :] = unpacked_tensor[bdx, :seq_len, :, :]

    return packed_tensor, start_loc_list


def pack_qkv(query: torch.Tensor, key: torch.Tensor, value: torch.Tensor,
             q_seq_lens: List[int], kv_seq_lens: List[int],
             device: Union[torch.device, str]) -> tuple:
    '''
    Individually pack each of Q, K and V, each with dimensions batch_size x
    padded_seq_len x num_heads x head_size, into respective number_of_tokens x
    num_heads x head_size tensors.
    
    For Q, number_of_tokens = sum(q_seq_lens).

    For K and V, number_of_tokens = sum(kv_seq_lens)

    Arguments:

    * query: batch_size x padded_seq_len x num_heads x head_size
    * key: batch_size x padded_seq_len x num_heads x head_size
    * value: batch_size x padded_seq_len x num_heads x head_size
    * q_seq_lens: list of token counts for each query
    * kv_seq_lens: list of token counts for each key/value

    Returns

    * packed_query: number_of_tokens x num_heads x head_size
    * packed_key: number_of_tokens x num_heads x head_size
    * packed_value: number_of_tokens x num_heads x head_size
    * q_start_loc_list: start idx of each query in packed_query
    * kv_start_loc_list: start idx of each {key,value} in packed_{key,value}
    '''

    if query is None:
        packed_query = None
        q_start_loc_list = None
    else:
        packed_query, q_start_loc_list = pack_tensor(query,
                                                     q_seq_lens,
                                                     device=device)
    packed_key, kv_start_loc_list = pack_tensor(key,
                                                kv_seq_lens,
                                                device=device)
    packed_value, _ = pack_tensor(value, kv_seq_lens, device=device)
    return packed_query, \
           packed_key, \
           packed_value, \
           q_start_loc_list, \
           kv_start_loc_list


def make_backend(backend_name: str) -> AttentionBackend:
    '''
    Construct the backend instance determined by the backend_name string
    argument.

    "XFORMERS" -> construct xformers backend

    TODO: other backends

    Note: at time of writing the Attention wrapper automatically selects
    its own backend for Attention.forward(); so the backend instance which
    you generate with this function is not meant to be used for *running*
    inference, but rather for generating compatible metadata structures
    using backend.make_metadata()


    Returns:

    * Backend instance
    '''
    if backend_name == "XFORMERS":
        return XFormersBackend()
    raise AssertionError(
        f"Unrecognized backend_name {backend_name} for unit test")


def make_metadata_tensors(seq_lens: List[int], context_lens: List[int],
                          encoder_seq_lens: List[int],
                          device: Union[torch.device, str]) -> tuple:
    '''
    Build scalar & tensor values required to build attention metadata structure.

    Arguments:

    * is_prompt: True -> Prefill, False -> Decode
    * seq_lens: list of token-counts for each seq
    * context_lens: list of context length values for each seq
    * device: CPU or CUDA device

    Returns:

    * seq_lens_tensor: seq_lens list, as tensor
    * context_lens_tensor: context_lens list, as tensor
    * max_query_len: max(seq_lens) if is_seq, o/w 1
    * max_context_len: max(context_lens)
    * max_seq_len: max(seq_lens)
    * seq_start_loc: start idx of each sequence
    * query_start_loc: start idx of each query
    '''
    seq_lens_tensor = maybe_make_int_tensor(seq_lens, device)
    context_lens_tensor = maybe_make_int_tensor(context_lens, device)
    max_context_len = maybe_max(context_lens)
    max_seq_len = maybe_max(seq_lens)

    encoder_seq_lens_tensor = maybe_make_int_tensor(encoder_seq_lens, device)
    max_encoder_seq_len = None if encoder_seq_lens is None else \
                            max(encoder_seq_lens)

    seq_start_loc = None

    return seq_lens_tensor, \
           context_lens_tensor, \
           max_context_len, \
           max_seq_len, \
           seq_start_loc, \
           encoder_seq_lens_tensor, \
           max_encoder_seq_len


def make_kv_cache(num_blocks: int,
                  num_heads: int,
                  head_size: int,
                  block_size: int,
                  device: Union[torch.device, str],
                  default_val: float = 0.0) -> torch.Tensor:
    '''
    Create a fake KV cache.

    Arguments:

    * num_blocks: number of blocks in the KV cache
    * num_heads: number of attention heads
    * head_size: head dimension
    * block_size: number of offsets within a block
    * device: CPU or CUDA device
    * default_val: initialization value for KV cache elements

    Returns:

    * kv_cache: 2 x num_blocks x (block_size * num_heads * head_size)
    '''

    kv_cache = torch.rand(
        (2, num_blocks, block_size * num_heads * head_size)).to(device)
    if default_val is not None:
        kv_cache[:, :, :] = default_val
    return kv_cache


def num_tokens_to_min_blocks(num_tokens: int, block_size: int) -> int:
    '''
    Compute the minimum number of blocks required to hold num_tokens tokens,
    given block_size
    '''
    return (num_tokens + block_size) // block_size


def make_block_tables_slot_mapping(block_size: int,
                                   seq_lens: List,
                                   device: Union[torch.device, str],
                                   block_base_addr: int = 0) -> tuple:
    '''
    Construct fake block tables & slot mappings.

    For a sequence with num_tokens tokens the minimum number
    of required KV cache blocks is

    num_blocks = (num_tokens + block_size) // block_size

    Then the minimum KV cache size in blocks is

    total_cache_blocks = sum(num_blocks for all seqs) 

    Then, the blocktable mapping counts downward from

    block_base_addr + total_cache_blocks

    to

    block_base_addr
    

    Arguments:

    * block_size: number of offsets per block
    * seq_lens: list of token-counts for each sequence
    * block_base_addr: the block table base address
    * device: CPU or CUDA device

    Return:

    * decode_block_tables_tensor: fake the state of the block tables during
      decode
    * decode_slot_mapping_tensor: fake the state of the slot mapping during
      decode
    * prefill_slot_mapping_tensor: fake the state of the slot mapping during
      prefill
    * prefill_block_tables_tensor: fake the state of the block tables during
      prefill
    * slot_mapping_tensor: union of prefill and decode slot mappings
    * empty_slot_mapping_tensor: empty slot mapping (useful for decode phase
      cross attention)
    * max_block_idx: the highest block address within this block table
    '''

    # Provision minimum number of KV cache blocks
    num_blocks_list = [
        num_tokens_to_min_blocks(num_tokens, block_size)
        for num_tokens in seq_lens
    ]
    max_block_table_len = max(num_blocks_list)
    block_table_pad_tokens = 10

    block_tables = []
    prefill_slot_mapping = []
    decode_slot_mapping = []
    slot_mapping = []
    # Compute uppermost address of block table
    total_cache_blocks = sum(num_blocks_list)
    block_base_idx = block_base_addr + total_cache_blocks
    max_block_idx = block_base_idx
    for sdx, num_tokens in enumerate(seq_lens):
        num_blocks = num_blocks_list[sdx]
        block_table = list(
            range(block_base_idx, block_base_idx - num_blocks, -1))
        for idx in range(num_tokens):
            mapping_value = (
                idx % block_size) + block_table[idx // block_size] * block_size
            slot_mapping.append(mapping_value)
            if idx < num_tokens - 1:
                prefill_slot_mapping.append(mapping_value)
            elif idx == num_tokens - 1:
                decode_slot_mapping.append(mapping_value)

        block_base_idx -= num_blocks
        block_tables.append(block_table)

    prefill_block_tables_tensor = torch.tensor([], device=device)
    decode_block_tables_tensor = make_tensor_with_pad(
        block_tables,
        max_len=max_block_table_len + block_table_pad_tokens,
        pad=0,
        dtype=torch.int,
        device=device,
    )
    prefill_slot_mapping_tensor = maybe_make_long_tensor(
        prefill_slot_mapping, device)
    decode_slot_mapping_tensor = maybe_make_long_tensor(
        decode_slot_mapping, device)
    slot_mapping_tensor = maybe_make_long_tensor(slot_mapping, device)
    empty_slot_mapping_tensor = maybe_make_long_tensor([], device)

    return decode_block_tables_tensor, \
           decode_slot_mapping_tensor, \
           prefill_slot_mapping_tensor, \
           prefill_block_tables_tensor, \
           slot_mapping_tensor, \
           empty_slot_mapping_tensor, \
           max_block_idx


def make_test_metadata(
    attn_backend: AttentionBackend,
    is_prompt: bool,
    seq_lens: List[int],
    context_lens: List[int],
    block_tables: torch.Tensor,
    slot_mapping: torch.Tensor,
    is_encoder_only_test: bool,
    num_prefills_or_decodes: int,
    num_prefill_or_decode_tokens: int,
    device: Union[torch.device, str],
    encoder_seq_lens: Optional[List[int]] = None,
    cross_block_tables: Optional[torch.Tensor] = None,
    cross_slot_mapping: Optional[List[int]] = None,
) -> AttentionMetadata:
    '''
    Construct fake attention metadata for a combined self-/cross-attention
    scenario i.e. an encoder/decoder model. 

    is_encoder_only_test=True causes the default attention metadata attention
    type to be AttentionType.ENCODER. False causes the default to 
    be AttentionType.DECODER.

    Assumptions:

    * No chunked prefill -> a batch is 100% prefill or 100% decode, never both

    Arguments:

    * attn_backend: Backend for sourcing attention kernels
    * is_prompt: prefill if True, o/w decode
    * seq_lens: list of token counts for each sequence
    * context_lens: list of context lengths for each sequence
    * block_tables: self-attention block tables
    * slot_mapping: self-attention slot_mapping
    * is_encoder_only_test: True if testing encoder; False if testing
      decoder self-attention or encoder/decoder cross-attention.
    * device: CPU or CUDA device
    * encoder_seq_lens: list of token counts for each encoder sequence, if any
      exist
    * cross_block_tables: cross-attention block tables, if required
    * cross_slot_mapping: cross-attention slot mapping, if required

    Return:

    * AttentionMetadata structure supporting self- and cross-attention
    '''

    default_attn_type = AttentionType.ENCODER if is_encoder_only_test \
                          else AttentionType.DECODER

    if is_prompt:
        num_prefills = num_prefills_or_decodes
        num_prefill_tokens = num_prefill_or_decode_tokens
        num_decode_tokens = 0

        seq_lens_tensor, \
        context_lens_tensor, \
        _, \
        _, \
        _, \
        encoder_seq_lens_tensor, \
        max_encoder_seq_len = make_metadata_tensors(seq_lens,
                              context_lens,
                              encoder_seq_lens,
                              device=device)

        return attn_backend.make_metadata(
            num_prefills=num_prefills,
            slot_mapping=slot_mapping,
            num_prefill_tokens=num_prefill_tokens,
            num_decode_tokens=num_decode_tokens,
            seq_lens=seq_lens,
            seq_lens_tensor=seq_lens_tensor,
            max_prefill_seq_len=None if seq_lens is None else max(seq_lens),
            max_decode_seq_len=0,
            context_lens_tensor=context_lens_tensor,
            block_tables=block_tables,
            use_cuda_graph=False,
            _attn_type=default_attn_type,
            encoder_seq_lens=encoder_seq_lens,
            encoder_seq_lens_tensor=encoder_seq_lens_tensor,
            max_encoder_seq_len=max_encoder_seq_len,
            cross_slot_mapping=cross_slot_mapping,
            cross_block_tables=cross_block_tables)

    else:  # not is_prompt

        num_prefills = 0
        num_prefill_tokens = 0
        num_decode_tokens = num_prefill_or_decode_tokens

        seq_lens_tensor, \
        context_lens_tensor, \
        _, \
        _, \
        _, \
        encoder_seq_lens_tensor, \
        max_encoder_seq_len = make_metadata_tensors(seq_lens,
                                  context_lens,
                                  encoder_seq_lens,
                                  device=device)

        return attn_backend.make_metadata(
            num_prefills=num_prefills,
            slot_mapping=slot_mapping,
            num_prefill_tokens=num_prefill_tokens,
            num_decode_tokens=num_decode_tokens,
            seq_lens=seq_lens,
            seq_lens_tensor=seq_lens_tensor,
            max_prefill_seq_len=0,
            max_decode_seq_len=max(seq_lens),
            context_lens_tensor=context_lens_tensor,
            block_tables=block_tables,
            use_cuda_graph=False,
            _attn_type=default_attn_type,
            encoder_seq_lens=encoder_seq_lens,
            encoder_seq_lens_tensor=encoder_seq_lens_tensor,
            max_encoder_seq_len=max_encoder_seq_len,
            cross_slot_mapping=cross_slot_mapping,
            cross_block_tables=cross_block_tables)
