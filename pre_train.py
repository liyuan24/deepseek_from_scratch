import os
import argparse
import torch
import numpy as np
import json
import tiktoken
from config import DeepSeekConfig
from deepseek import DeepSeekModelForCausalLM
import torch.nn as nn
import time
import math
from contextlib import nullcontext
import bitsandbytes as bnb
from enum import Enum
from dataloader import TinyShakespeareDataLoader, FineWebEduDataLoader, DataLoader


class DatasetName(Enum):
    TINY_SHAKESPEARE = "tinyshakespeare"
    FINE_WEB_EDU = "fineweb_edu"

ptdtype = {
    "float32": torch.float32,
    "bfloat16": torch.bfloat16,
    "float16": torch.float16,
}


def configure_optimizers(
    model, weight_decay, learning_rate, betas, fused, use_eight_bit_optimizer=False
):
    # start with all of the candidate parameters
    param_dict = {pn: p for pn, p in model.named_parameters()}
    # filter out those that do not require grad
    param_dict = {pn: p for pn, p in param_dict.items() if p.requires_grad}
    # create optim groups. Any parameters that is 2D will be weight decayed, otherwis    no.
    # i.e. all weight tensors in matmuls + embeddings decay, all biases and layernorms don't.
    decay_params = [p for n, p in param_dict.items() if p.dim() >= 2]
    nodecay_params = [p for n, p in param_dict.items() if p.dim() < 2]
    optim_groups = [
        {"params": decay_params, "weight_decay": weight_decay},
        {"params": nodecay_params, "weight_decay": 0.0},
    ]
    num_decay_params = sum(p.numel() for p in decay_params)
    num_nodecay_params = sum(p.numel() for p in nodecay_params)
    print(
        f"num decayed parameter tensors: {len(decay_params)}, with {num_decay_params:,} parameters"
    )
    print(
        f"num non-decayed parameter tensors: {len(nodecay_params)}, with {num_nodecay_params:,} parameters"
    )
    # Create AdamW optimizer and use the fused version if it is available
    if use_eight_bit_optimizer:
        # fuse is not supported
        optimizer = bnb.optim.AdamW8bit(
            optim_groups, lr=learning_rate, betas=betas
        )
    else:
        optimizer = torch.optim.AdamW(
            optim_groups, lr=learning_rate, betas=betas, fused=fused
        )
    return optimizer


# learning rate decay scheduler (cosine with warmup)
def get_lr(it, warmup_iters, lr_decay_iters, learning_rate, min_lr):
    # 1) linear warmup for warmup_iters steps
    if it < warmup_iters:
        return learning_rate * (it + 1) / (warmup_iters + 1)
    # 2) if it > lr_decay_iters, return min learning rate
    if it > lr_decay_iters:
        return min_lr
    # 3) in between, use cosine decay down to min learning rate
    decay_ratio = (it - warmup_iters) / (lr_decay_iters - warmup_iters)
    assert 0 <= decay_ratio <= 1
    coeff = 0.5 * (1.0 + math.cos(math.pi * decay_ratio))  # coeff ranges 0..1
    return min_lr + coeff * (learning_rate - min_lr)


def get_dataloader(dataset_name, split, batch_size, seq_len, device):
    if dataset_name == DatasetName.TINY_SHAKESPEARE.value:
        dataloader = TinyShakespeareDataLoader(batch_size, seq_len, split, device)
    elif dataset_name == DatasetName.FINE_WEB_EDU.value:
        dataloader = FineWebEduDataLoader(batch_size, seq_len, split, device)
    else:
        raise ValueError(f"Dataset {dataset_name} is not supported")
    return dataloader


@torch.no_grad()
def evalaute(
    model: nn.Module,
    eval_iters: int,
    eval_dataloader: DataLoader,
):
    model.eval()
    losses = torch.zeros(eval_iters)
    for i in range(eval_iters):
        x, y = eval_dataloader.next_batch()
        _, loss, _ = model(x, y)
        losses[i] = loss.item()
    model.train()
    return losses.mean()


def get_model_config(args):
    with open("config.json", "r") as f:
        config = json.load(f)
    return config


def get_model(args):
    config = get_model_config(args)
    model = DeepSeekModelForCausalLM(DeepSeekConfig(**config))
    model.to(args.device)
    total_params, activated_params = model.get_total_parameters()
    print(f"Total parameters: {total_params:,}")
    print(f"Activated parameters: {activated_params:,}")
    print(f"Activated parameters ratio: {activated_params / total_params:.2%}")
    return model


def forward_and_backward(model, x, y, optimizer, ctx: torch.autocast = nullcontext()):
    with ctx:
        _, loss, _ = model(x, y)
    loss.backward()
    optimizer.step()
    optimizer.zero_grad(set_to_none=True)


def train(args):
    global ptdtype
    ptdtype = ptdtype[args.dtype]
    ctx = (
        nullcontext()
        if args.device == "cpu"
        else torch.amp.autocast(device_type=args.device, dtype=ptdtype)
    )
    if args.wandb_log:
        import wandb

        wandb.init(
            project=args.wandb_project,
            name=args.wandb_run_name,
            config=get_wandb_config(args),
        )
    train_loader = get_dataloader(args.dataset, "train", args.batch_size, args.max_position_embeddings, args.device)
    val_loader = get_dataloader(args.dataset, "val", args.batch_size, args.max_position_embeddings, args.device)
    model = get_model(args)
    optimizer = configure_optimizers(
        model,
        args.adamw_weight_decay,
        args.learning_rate,
        (args.adamw_beta1, args.adamw_beta2),
        args.adamw_use_fused,
        args.use_eight_bit_optimizer,
    )
    optimizer.zero_grad(set_to_none=True)
    best_val_loss = 1e9
    iter_num = 0
    if args.resume:
        checkpoint = torch.load(os.path.join(args.out_dir, "ckpt.pt"))
        config = checkpoint["model_config"]
        model = DeepSeekModelForCausalLM(config)
        model.to(args.device)
        model.load_state_dict(checkpoint["model"])
        optimizer.load_state_dict(checkpoint["optimizer"])
        best_val_loss = checkpoint["best_val_loss"]
        iter_num = checkpoint["iter_num"]
    while iter_num < args.max_train_steps:
        # determine and set the learning rate for this iteration
        lr = (
            get_lr(
                iter_num,
                args.warmup_iters,
                args.lr_decay_iters,
                args.learning_rate,
                args.min_learning_rate,
            )
            if args.decay_lr
            else args.learning_rate
        )
        for param_group in optimizer.param_groups:
            param_group["lr"] = lr
        for _ in range(args.gradient_accumulation_steps):
            x, y = train_loader.next_batch()
            with ctx:
                _, train_loss, _ = model(x, y)
            train_loss = train_loss / args.gradient_accumulation_steps
            train_loss.backward()
        if args.grad_clip > 0:
            torch.nn.utils.clip_grad_norm_(model.parameters(), args.grad_clip)
        optimizer.step()
        optimizer.zero_grad(set_to_none=True)
        if (iter_num + 1) % args.eval_interval == 0:
            val_loss = evalaute(
                model,
                args.eval_iters,
                val_loader,
            )
            if args.wandb_log:
                wandb.log(
                    {
                        "Step": iter_num,
                        "Train Loss": train_loss.item(),
                        "Val Loss": val_loss,
                        "Learning Rate": lr,
                    }
                )
            if val_loss < best_val_loss:
                best_val_loss = val_loss
                if iter_num > 0:
                    checkpoint = {
                        "model": model.state_dict(),
                        "optimizer": optimizer.state_dict(),
                        "model_config": model.config,
                        "iter_num": iter_num,
                        "best_val_loss": best_val_loss,
                        "training_args": args,
                    }
                    torch.save(
                        checkpoint, os.path.join(args.out_dir, args.checkpoint_path)
                    )
            print(
                f"step {iter_num+1}: train loss: {train_loss.item():.4f}, val loss: {val_loss:.4f}"
            )
        iter_num += 1


def get_wandb_config(args):
    config = {
        "batch_size": args.batch_size,
        "learning_rate": args.learning_rate,
        "use_fused_adamw": args.adamw_use_fused,
    }
    config.update(get_model_config(args))
    return config


def estimate_throughput(args):
    if args.wandb_log:
        import wandb

        wandb.init(
            project=args.wandb_project,
            name=args.wandb_run_name,
            config=get_wandb_config(args),
        )
    data_dir = os.path.join("data", args.dataset)
    model = get_model(args)
    optimizer = torch.optim.AdamW(
        model.parameters(), lr=args.learning_rate, fused=args.adamw_use_fused
    )
    total_tokens = 0
    total_time = 0
    torch.cuda.synchronize()
    start_time = time.time()
    train_loader = get_dataloader(args.dataset, "train", args.batch_size, args.max_position_embeddings, args.device)
    for i in range(args.max_train_steps):
        x, y = train_loader.next_batch()
        forward_and_backward(model, x, y, optimizer)
        total_tokens += x.shape[0] * x.shape[1]
    torch.cuda.synchronize()
    end_time = time.time()
    throughput = total_tokens / (end_time - start_time)
    if args.wandb_log:
        wandb.log({"Training Throughput": throughput})
    else:
        print(f"Training throughput: {throughput:.2f} tokens/s")


def main(args):
    if not args.estimate_throughput:
        train(args)
    else:
        estimate_throughput(args)


if __name__ == "__main__":
    args = argparse.ArgumentParser()
    args.add_argument("--device", type=str, default="cuda")
    args.add_argument("--dataset", type=str, default="fineweb_edu")

    args.add_argument("--warmup-steps", type=int, default=20)
    args.add_argument("--learning-rate", type=float, default=5e-4)
    args.add_argument("--min-learning-rate", type=float, default=5e-5)
    args.add_argument("--max-position-embeddings", type=int, default=512)

    args.add_argument("--eval-iters", type=int, default=5)
    args.add_argument("--eval-interval", type=int, default=10)
    args.add_argument("--dtype", type=str, default="bfloat16")
    args.add_argument("--measure-throughput-interval", type=int, default=100)
    args.add_argument("--estimate-throughput", type=bool, default=False)

    args.add_argument("--wandb-log", type=bool, default=True)
    args.add_argument("--wandb-project", type=str, default="deepseek training")
    args.add_argument(
        "--wandb-run-name", type=str, default="8_bit_optimizer"
    )

    args.add_argument("--adamw-use-fused", type=bool, default=True)

    args.add_argument("--max-train-steps", type=int, default=30000)
    args.add_argument("--batch-size", type=int, default=8)
    args.add_argument("--gradient-accumulation-steps", type=int, default=8)
    args.add_argument("--warmup-iters", type=int, default=500)
    args.add_argument("--lr-decay-iters", type=int, default=1000)
    args.add_argument("--decay-lr", type=bool, default=True)

    args.add_argument("--out-dir", type=str, default="output")
    args.add_argument("--resume", type=bool, default=False)
    args.add_argument(
        "--checkpoint-path",
        type=str,
        default="8_bit_optimizer_ckpt.pt",
    )

    # adamw arguments
    args.add_argument("--adamw-beta1", type=float, default=0.9)
    args.add_argument("--adamw-beta2", type=float, default=0.95)
    args.add_argument("--adamw-weight-decay", type=float, default=0.1)
    args.add_argument("--use-eight-bit-optimizer", type=bool, default=True)

    args.add_argument("--grad-clip", type=float, default=1.0)

    main(args.parse_args())
