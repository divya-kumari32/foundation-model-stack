import argparse
import functools
import logging
import math
import os
import time
from typing import Callable, Union

import torch
import torch.distributed as dist
import torch.nn as nn
import torch.nn.functional as F
from fm_nlp.architecture import Llama, add_llama_args
from fm_nlp.pretraining.args import (
    add_ckp_args,
    add_config_args,
    add_profiler_args,
    add_training_args,
    add_vocab_args,
    validate_arg_tokens,
    validate_args,
)
from torch.distributed.fsdp import FullyShardedDataParallel as FSDP
from torch.distributed.fsdp import MixedPrecision, ShardingStrategy
from torch.distributed.fsdp.wrap import enable_wrap, transformer_auto_wrap_policy, wrap
from torch.profiler import ProfilerAction, ProfilerActivity
from torch.profiler import schedule as prof_schedule

from fm import data as fmdata
from fm import utils
from fm.modules import LayerNormParameterized
from fm.utils import (
    get_datasets_and_weights,
    get_local_rank,
    get_rank,
    get_world_size,
    human_readable_report_and_log,
    run_rank_n,
)
from fm.utils.profiling import maybe_profile, trace_handler
from fms.models import llama
from fms.models.llama import LLaMABlock

from transformers import LlamaForCausalLM

torch._inductor.config.joint_graph_constant_folding = False


class Speculator(nn.Module):
    def __init__(self, emb_dim=4096, vocab_size=32000, n_heads=4):
        super().__init__()
        self.nheads = n_heads
        self.emb_dim = emb_dim
        self.vsize = vocab_size
        self.emb = nn.ModuleList([nn.Embedding(vocab_size, emb_dim) for _ in range(n_heads)])
        self.proj = nn.ModuleList([nn.Linear(emb_dim * 2, emb_dim, bias=False) for _ in range(n_heads)])
        self.head = nn.ModuleList([nn.Linear(emb_dim, vocab_size, bias=False) for _ in range(n_heads)])
        self.ln = nn.ModuleList(
            [LayerNormParameterized(emb_dim, elementwise_shift=True, elementwise_scale=True) for _ in range(n_heads)]
        )
        self.a = nn.GELU()
        self.reset_params()

    def reset_params(self):
        for m in self.modules():
            if isinstance(m, nn.Embedding) or isinstance(m, nn.Linear):
                nn.init.trunc_normal_(m.weight, 0, 1 / self.emb_dim**0.5)
            elif isinstance(m, LayerNormParameterized):
                m.weight.data.fill_(1)
                m.bias.data.zero_()

    def forward(self, state, inds):
        # state: b n d
        # inds: b n+2 (..., pred token, n+2, n+3)
        out = []
        for i in range(self.nheads):
            h_inds = inds[:, i : i + state.size(1)]
            z = self.emb[i](h_inds)  # b n d
            z = torch.cat([state, z], dim=2)  # b n 2d
            state = self.a(self.ln[i](self.proj[i](z)))  # b n d
            out.append(self.head[i](state))  # b n v
        return torch.stack(out, dim=0)  # h b n v


loglevel = os.environ.get("LOGLEVEL", "WARNING").upper()
logging.basicConfig(level=loglevel)


parser = argparse.ArgumentParser(description="Llama training on IBM NLP data")

# Config args
config_arg_group = parser.add_argument_group("General Config")
add_config_args(config_arg_group)

# Profiler args
prof_arg_group = parser.add_argument_group("Profiler Config")
add_profiler_args(prof_arg_group)

# Checkpoint args
ckp_arg_group = parser.add_argument_group("Checkpoint Config")
add_ckp_args(ckp_arg_group)
parser.add_argument(
    "--flexible_load", default=False, action="store_true", help="Disable strict loading for single-file checkpoints?"
)
ckp_arg_group.add_argument(
    "--base_path",
    type=str,
    default="",
    help="Checkpoint file or directory for initial load. If directory, loads latest.",
)

# Vocab args
vocab_arg_group = parser.add_argument_group("Vocab Config")
add_vocab_args(vocab_arg_group)

# Model args
model_arg_group = parser.add_argument_group("Model Config")
add_llama_args(model_arg_group)
parser.add_argument(
    "--n_specu_heads",
    type=int,
    default=3,
    help="Number of words to ingest before making trainable predictions",
)

# Training args
training_arg_group = parser.add_argument_group("Training Config")
add_training_args(training_arg_group)
parser.add_argument(
    "--prompt_len",
    type=int,
    default=1,
    help="Number of words to ingest before making trainable predictions",
)
training_arg_group.add_argument("--decay_interval", type=int, default=0, help="Number of steps for cosine annealing")

parser.add_argument(
    "--override_init",
    default=False,
    action="store_true",
    help="Override FMS init scheme with original Llama 0.02 scheme?",
)

parser.add_argument("--targ_type", type=str, default="", help="Special targeting options (n+2, greedy, sampled)")

args = parser.parse_args()

# Define reporting fns

run_rank_n(os.makedirs)(args.log_path, exist_ok=True)
report = functools.partial(human_readable_report_and_log, args.log_path)


def sync_report(*args, **kwargs):
    dist.barrier()
    report(*args, **kwargs)


# Handle args
rank = get_rank()
local_rank = get_local_rank()
world_size = get_world_size()

torch.manual_seed(args.seed)
torch.cuda.manual_seed_all(args.seed)
torch.backends.cuda.matmul.allow_tf32 = True
torch.cuda.set_device(local_rank)

args = validate_args(args, world_size)
validate_arg_tokens(args, "pad,sep", allow_no_pad=True)

if args.decay_interval == 0:
    args.decay_interval = args.num_steps

datasets, weights = get_datasets_and_weights(args.datasets, args.dataset_weights)
is_testrun = args.testrun_data_index != -100

report(
    "Starting training run:",
    world_size=world_size,
    torch_version=torch.__version__,
    **vars(args),
)

# Model setup policies
mp_policy = (
    None
    if not args.bf16
    else MixedPrecision(
        param_dtype=torch.bfloat16,
        reduce_dtype=torch.bfloat16,
        buffer_dtype=torch.bfloat16,
        _module_classes_to_ignore=[],
    )
)
wrapping_policy = functools.partial(transformer_auto_wrap_policy, transformer_layer_cls={LLaMABlock})
model_sharding_strategies = {
    "fsdp": ShardingStrategy.FULL_SHARD,
    "hsdp": ShardingStrategy.HYBRID_SHARD,
    "ddp": ShardingStrategy.NO_SHARD,
}
model_sharding_strategy = model_sharding_strategies[args.parallel_mode]

# reset stats
torch.cuda.reset_peak_memory_stats(device=torch.cuda.current_device())


def generate(
    model: Union[Callable, torch.nn.Module],
    input_ids: torch.LongTensor,
    max_seq_len: int = 2048,
    max_new_tokens: int = 256,
    temperature: float = 1.0,
    top_k: int = 10,
    do_sample: bool = True,
    num_beams: int = 1,
):
    """
    A trivial generate function that can be used for validation/testing in
    cases where HF is not available.
    We could add implementations for other types of generation, but this is
    enough for making sure a model is working.
    Does not implement batching nor beam search, but those could be added.

    Args:
        model: A function or nn.Module that takes a batch of input_ids and
            returns logits
        prefix: A tensor of token IDs.
        max_seq_len: the sequence length of the model
        max_new_tokens: max tokens to generate
        temperature: temperature of softmax when sampling
        top_k: only search among top k tokens
        do_sample: multinomial sampling. False for greedy.
        num_beams: TODO: support beam search
        use_cache: requires that the model accept use_cache and
            past_key_value_states args in forward method.
    """
    batched = False
    if num_beams != 1:
        raise NotImplementedError("generate() does yet not support beam search")
    if type(input_ids) == torch.Tensor:
        if input_ids.dim() != 1:
            batched = True
    else:
        raise RuntimeError("generate() requires a tensor of token ids as the prefix")

    if not batched:
        input_ids = input_ids.unsqueeze(0)

    embeds = None
    result = input_ids
    next_input = input_ids
    kwargs = dict()
    kwargs["past_key_value_states"] = None

    for _ in range(max_new_tokens):
        input_ids = next_input[:, -max_seq_len:]
        output = model.forward(input_ids, include_embeds=True, use_cache=True, **kwargs)
        logits, past_key_value_states, z = output
        # kv updates are required for torch.compile with
        # mode='reduce-overhead'
        # n_kv_s = []
        # for layer_idx in range(len(past_key_value_states)):
        #     n_kv_s.append([])
        #     for tensor_idx in range(len(past_key_value_states[layer_idx])):
        #         n_kv_s[layer_idx].append(
        #             past_key_value_states[layer_idx][tensor_idx].clone(memory_format=torch.contiguous_format).detach()
        #         )
        #         # torch._dynamo.mark_dynamic(n_kv_s[layer_idx][tensor_idx], 2)
        n_kv_s = past_key_value_states
        kwargs["past_key_value_states"] = n_kv_s
        logits = logits[:, -1, :]

        if do_sample:
            # get logits from last value in sequence nad scale
            logits = logits / temperature
            if top_k:
                v, _ = torch.topk(logits, top_k)
                logits[logits < v[:, [-1]]] = -float("inf")

            probs = F.softmax(logits, dim=-1)
            next_val = torch.multinomial(probs, num_samples=1)
        else:
            next_val = torch.argmax(logits, dim=-1).unsqueeze(0).t()

        result = torch.cat((result, next_val), dim=-1)
        if embeds is None:
            embeds = z
        else:
            embeds = torch.cat((embeds, z), dim=-2)

        next_input = next_val

    if not batched:
        result = result[0]
    return result, embeds  # , n_kv_s


# Training loop
def train_func(args):
    emu_factor = args.simulated_gpus // world_size
    if emu_factor > 1:
        report(
            WARNING="Grad accumulation has not been fully tested with FSDP. Correctness for this run is NOT guaranteed!"
        )
    report("Setting up NCCL...")
    utils.setup_distributed()

    bsize = args.b_size
    effective_bsize = args.b_size * args.simulated_gpus
    start_step = 0
    tokens_seen = 0

    # Model
    report("Constructing model...")

    model = LlamaForCausalLM.from_pretrained("/lustre/llama_weights/hf/13B-F/")
    model = llama.convert_hf_llama(model)
    model = model.cpu()

    # Wrap model
    report(f"Applying wrapper for parallelization mode={args.parallel_mode}...")
    model = FSDP(
        model,
        auto_wrap_policy=wrapping_policy,
        mixed_precision=mp_policy,
        sharding_strategy=model_sharding_strategy,
        device_id=local_rank,
        limit_all_gathers=True,
        use_orig_params=True,
    )
    model.to(device=local_rank)
    model.rot_emb.compute_freqs_cis(model.shared.emb.weight.device, args.seq_len)

    speculator = Speculator(args.emb_dim, args.vocab, args.n_specu_heads)

    loss_fn = nn.CrossEntropyLoss(label_smoothing=args.label_smoothing)

    num_params = [utils.pcount(model), utils.pcount(speculator)]

    # Load pretrained model from checkpoint
    if len(args.base_path) > 0:
        report("Loading base model...")
        checkpoint_data = torch.load(args.base_path, map_location="cpu")
        model.load_state_dict(checkpoint_data["model_state"], strict=not args.flexible_load)
        report("Base model loaded!")
        del checkpoint_data

    speculator = FSDP(
        speculator,
        auto_wrap_policy=None,
        mixed_precision=mp_policy,
        sharding_strategy=ShardingStrategy.NO_SHARD,
        device_id=local_rank,
        limit_all_gathers=True,
        use_orig_params=True,
    )
    sync_report(
        "Model created!",
        num_params=num_params[0],
        num_spec_params=num_params[1],
    )

    # Optimizers
    report(lr=args.lr)
    optimizer = torch.optim.AdamW(
        speculator.parameters(), weight_decay=0.1, lr=args.lr, betas=(args.beta1, args.beta2)
    )

    schedule = lambda x: min(
        1 - (1 - min(x, args.warmup_interval) / args.warmup_interval) ** 2,  # parabolic anneal
        0.1 + 0.5 * (1 - 0.1) * (1 + math.cos(min(x, args.decay_interval) / args.decay_interval * math.pi)),
    )  # cos anneal to 10% @ 250k

    updated_schedule = lambda x: schedule(x + start_step)
    scheduler = torch.optim.lr_scheduler.LambdaLR(optimizer, updated_schedule)

    # Dataloaders
    def make_dataset(path):
        base_scalable = fmdata.Scalable_Shard_Dataset if args.oversample_weights else fmdata.Scalable_Sampling_Dataset
        data = base_scalable(
            path,
            fmdata.Shard_Doc_Dataset if args.disable_streaming else fmdata.Streaming_Doc_Dataset,
            rank,
            world_size,
            args.sep_token,
            trainsplit=1 if not is_testrun else 0.05,
            is_val=False,
            min_length=3,
            datasets=datasets,
            weights=weights if not is_testrun else None,
            testrun_data_index=args.testrun_data_index,
            seed=args.seed,
            verbose=(rank == 0),
            n_logical_shards=args.logical_shards,
        )
        data = fmdata.Buffer_Dataset(
            data,
            [256],
            bos_token=args.sep_token,
            pack_hard=True,
        )
        data = fmdata.Preload_Buffer_Dataset(data, 10000 if not is_testrun else 100)
        data = fmdata.Preprocess_Dataset(data, lambda x: torch.IntTensor(x))
        return data

    report("Constructing datasets...", effective_bsize=effective_bsize)
    train_data = make_dataset(args.data_path)
    train_loader = iter(torch.utils.data.DataLoader(train_data, num_workers=0, batch_size=bsize))
    sync_report("Datasets constructed!")

    # Open checkpointer
    checkpointer = utils.Llama_Checkpointer(
        args.log_path,
        args.num_ckps,
        "ddp",
        report_fn=report,
    )

    # Load from checkpoint
    report("Loading checkpoint...")
    speculator, optimizer, train_loader, start_step, tokens_seen = checkpointer.load(
        speculator,
        None if args.drop_optimizer else optimizer,
        None if args.drop_dataset else train_loader,
        path=args.ckp_path,
        reset_stepcount=args.reset_stepcount,
        strict=not args.flexible_load,
    )
    signature = None if not args.make_signatures else utils.get_signature(model, device=local_rank, params=1)
    report("Checkpoint loaded!", signature=signature)

    # Override loaded optim hyperparams with the current values
    for g in optimizer.param_groups:
        g["initial_lr"] = args.lr
        g["betas"] = (args.beta1, args.beta2)

    # Clear memory
    torch.cuda.empty_cache()

    # Train
    report(
        "Beginning training! Depending on dataset size and config, initial few steps may be slow.",
        num_steps=args.num_steps - start_step,
    )
    model.train()
    losstracker = torch.zeros(args.n_specu_heads).to(local_rank)
    cliptracker = 0
    trackertracker = 0
    start = time.time()
    loop_start = time.time()
    step = start_step - 1
    with maybe_profile(
        use_profiler=args.profile,
        activities=[ProfilerActivity.CPU, ProfilerActivity.CUDA],
        schedule=prof_schedule(
            skip_first=args.profile_skip,
            wait=args.profile_wait,
            warmup=args.profile_warmup,
            active=args.profile_active,
            repeat=1,
        ),
        on_trace_ready=functools.partial(trace_handler, output_path=args.log_path, extra_name=str(rank)),
        with_stack=args.profile_remove_stack,
        profile_memory=args.profile_remove_memory,
        record_shapes=args.profile_remove_shapes,
    ) as prof:
        for step in range(start_step, args.num_steps):
            optimizer.zero_grad()
            model.zero_grad()
            verbose = step == start_step or step % args.save_interval == 0
            if verbose:
                sync_report("Workers synchronized!")
            for ministep in range(emu_factor):
                inp = next(train_loader)
                if verbose and ministep == 0:
                    sync_report("Collected data")
                inp = inp.to(local_rank)
                dist.barrier()
                with torch.no_grad():
                    targs, embeds = generate(model, inp, 4096, args.seq_len, do_sample=True)
                targs = targs[:, -args.seq_len :]
                embeds = embeds[:, -args.seq_len : -3]
                preds = speculator(embeds.detach(), targs[:, :-1].detach())
                losses = []
                for i in range(args.n_specu_heads):
                    pred = preds[i]
                    targ = targs[:, i + 1 : pred.size(1) + i + 1]  # b n
                    loss = loss_fn(pred.reshape(-1, pred.size(2)), targ.long().reshape(-1))
                    loss = loss.div(emu_factor)
                    losses.append(loss)
                    losstracker[i] += loss.item()
                dist.barrier()

                if verbose and ministep == 0:
                    sync_report("Got through forward pass", ntok=inp.size(1))
                if (ministep + 1) == emu_factor:
                    sum(losses).backward()
                    track_norm = speculator.clip_grad_norm_(args.clip_th).item()
                    if track_norm > args.clip_th:
                        cliptracker += 1
                else:
                    sum(losses).backward()
                if verbose and ministep == 0:
                    sync_report("Got through backward pass")

                # deallocating GPU memory for the pred tensor
                del pred, preds, embeds, targ, targs

            optimizer.step()
            scheduler.step()
            if verbose:
                sync_report("Got through one step")
            tokens_seen += effective_bsize * args.seq_len
            trackertracker += 1

            # Report training loss and speed
            if (step + 1) % args.report_interval == 0:
                dist.all_reduce(losstracker, op=dist.ReduceOp.SUM)
                trainloss = losstracker / trackertracker / world_size
                elapsed_time = time.time() - loop_start
                elapsed_tokens = (step - start_step) * effective_bsize * args.seq_len
                lossdict = {"trainloss_" + str(i + 1): trainloss[i].item() for i in range(args.n_specu_heads)}
                sync_report(
                    step=step + 1,
                    lr=optimizer.param_groups[0]["lr"],
                    gnorm=track_norm,
                    tokens_seen=tokens_seen,
                    percentclip=cliptracker / trackertracker,
                    **lossdict,
                    speed=(time.time() - start) / trackertracker,
                    elapsed_time=elapsed_time,
                    tok_per_sec_per_gpu=int((elapsed_tokens) / world_size / elapsed_time),
                    tok_per_day=int((elapsed_tokens / elapsed_time) * 3600 * 24),
                )
                losstracker.zero_()
                cliptracker = 0
                trackertracker = 0
                start = time.time()

            # Checkpoint model
            if (step + 1) % args.save_interval == 0:
                torch.cuda.empty_cache()
                if args.profile and prof.current_action == ProfilerAction.RECORD_AND_SAVE:
                    report("You are profiling a checkpointing step, be careful about it!")
                signature = (
                    None if not args.make_signatures else utils.get_signature(model, device=local_rank, params=1)
                )
                cudastats = torch.cuda.memory_summary(device=torch.cuda.current_device(), abbreviated=True)
                report("Starting distributed checkpoint save...", signature=signature, mem_summary=cudastats)
                overwritten = checkpointer.save(
                    step + 1,
                    speculator,
                    optimizer,
                    train_loader,
                    tokens_seen=tokens_seen,
                    loss=trainloss,
                    signature=signature,
                )
                if overwritten:
                    report("Checkpoint", overwritten, "dumped")
                torch.cuda.empty_cache()

                model.train()
                start = time.time()

            # Profiler step
            if args.profile:
                prof.step()

    sync_report(msg="Writing final checkpoint", step=step + 1)
    checkpointer.save_single_file(
        step + 1,
        speculator,
        tokens_seen=tokens_seen,
        loss=trainloss,
        signature=signature,
    )
    report("Final checkpoint written!")
    # Cleanup
    dist.barrier()
    dist.destroy_process_group()


start = time.time()
train_func(args)
report("Job Complete!", total_time=utils.human_readable_time(time.time() - start))