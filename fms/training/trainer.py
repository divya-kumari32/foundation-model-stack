import functools
from contextlib import nullcontext
from typing import List, Optional

import torch
from torch import amp, nn
from torch.optim import Optimizer
from torch.utils.data import DataLoader, DistributedSampler

from fms.training.plugins import TrainerPlugin
from fms.utils import print0

import csv
import numpy as np
import pickle

def __one_step(
    model: nn.Module,
    input: torch.Tensor,
    label: torch.Tensor,
    loss_fn: nn.Module,
    grad_scaler: Optional[amp.GradScaler],
):
    autocast = (
        torch.autocast(device_type="cuda") if grad_scaler is not None else nullcontext()
    )
    with autocast:
        output = model(input)

        print0("Model parameters and gradient data types:")
        for name, param in model.named_parameters():
            grad_dtype = param.grad.dtype if param.grad is not None else 'No gradient'
            print0(f"Parameter: {name}, Data type: {param.dtype}, Gradient data type: {grad_dtype}")

        loss = loss_fn(output, label)

    if grad_scaler is not None:
        grad_scaler.scale(loss).backward()
    else:
        loss.backward()
    return loss


def __optimize(model, optimizer, grad_scaler):
    if grad_scaler is not None:
        grad_scaler.unscale_(optimizer)
        torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
        grad_scaler.step(optimizer)
        grad_scaler.update()
    else:
        nn.utils.clip_grad_norm_(model.parameters(), 1.0)
        optimizer.step()
    optimizer.zero_grad()


def __one_epoch(
    model: nn.Module,
    optimizer: Optimizer,
    data: DataLoader,
    device,
    loss_fn,
    epoch: int,
    prev_step: int,
    plugins: List[TrainerPlugin],
    accum_iters: int = 1,
):
    print0("Epoch", epoch)
    model.train()

    grad_scaler = None
    # grad_scaler = torch.cuda.amp.GradScaler()

    if data.sampler is not None and isinstance(data.sampler, DistributedSampler):
        data.sampler.set_epoch(epoch)

    optimized = False
    optimizer.zero_grad()

    highest_step = prev_step

    for step, (input, label) in enumerate(data):
        step = prev_step + step + 1
        highest_step = step

        batch_size = input.shape[0]
        input_length = input.shape[1]

        input = input.to(device)
        label = label.to(device)

        loss = __one_step(model, input, label, loss_fn, grad_scaler)
        
        # with open('gradients.csv', 'a', newline='') as file:
        #     writer = csv.writer(file)
            
        #     # Log gradients to the CSV file
        #     for name, param in model.named_parameters():
        #         if param.grad is not None:
        #             for elem in param.grad.view(-1):
        #                 writer.writerow([step, name, elem.item()])

        # def bucket_gradients(gradients, bins):
        #     hist, _ = np.histogram(gradients, bins=bins)
        #     return hist
        
        # def count_in_range(gradients, lower_bound, upper_bound):
        #     return ((gradients >= lower_bound) & (gradients <= upper_bound)).sum()
        
        # gradient_stats = {}
        # bins = np.logspace(-35, 35, base=2, num=71)
        # for name, param in model.named_parameters():
        #     if param.grad is not None:
        #         gradients = param.grad.view(-1).float().numpy(force=True) 
                # gradient_stats[name] = {
                #     "in_range": count_in_range(gradients, 2**-31, 2**32),
                #     "buckets": bucket_gradients(gradients, bins)
                # }

        # gradient_stats_all.append((step, gradient_stats)) 

        if (step + 1) % accum_iters == 0:
            __optimize(model, optimizer, grad_scaler)
            optimized = True
        else:
            optimized = False

        metrics = {
            "loss": loss,
            "batch_size": batch_size,
            "input_length": input_length,
        }

        # After loop or at certain checkpoints
        # with open('loss_stats_fp16.csv', 'a', newline='') as file:
        #     writer = csv.writer(file)
        #     writer.writerow([step, metrics['loss']])

        for plugin in plugins:
            plugin.step(epoch, step, metrics)

    if not optimized:
        __optimize(model, optimizer, grad_scaler)
    metrics = {
        "batch_size": batch_size,
        "input_length": input_length,
    }
    for plugin in plugins:
        plugin.step(epoch, step=highest_step, metrics=metrics, end_of_epoch=True)


def train(
    model,
    optimizer,
    dataloader: DataLoader,
    device,
    loss_fn: nn.Module,
    start_epoch: int = 0,
    epochs: int = 1,
    prev_step: int = -1,
    trainer_plugins: List[TrainerPlugin] = [],
    grad_accum_iters: int = 1,
):
    for epoch in range(start_epoch, start_epoch + epochs):
        __one_epoch(
            model,
            optimizer,
            dataloader,
            device,
            loss_fn,
            epoch,
            prev_step,
            trainer_plugins,
            accum_iters=grad_accum_iters,
        )
